#!/usr/bin/env python3
"""公众号文章同步主入口（十方上学记）。

数据源：
  - exports/*.json（含仓库根目录的历史导出文件，若有）：全量文章清单
  - feed.zoro.build RSS 订阅：增量发现新文章

流程：合并候选 -> 与 data/manifest.json 差量 -> 逐篇抓取转 MD（限速）->
更新清单 -> 重建 preview.html -> git 提交推送。

用法：
  .venv/bin/python tools/sync.py                  # 增量同步（默认 json+rss）
  .venv/bin/python tools/sync.py --limit 5        # 只抓 5 篇（测试）
  .venv/bin/python tools/sync.py --no-push        # 不同步到 GitHub
  .venv/bin/python tools/sync.py --source rss     # 只查 RSS
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser

from wechat_fetch import (
    ImageStore,
    WeChatFetcher,
    article_key,
    normalize_url,
    render_article_md,
    sanitize_filename,
    to_original_image_url,
)

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "data" / "manifest.json"
POSTS_DIR = ROOT / "content" / "posts"
IMAGES_DIR = ROOT / "static" / "images"
LOG_DIR = ROOT / "logs"
EXPORT_GLOBS = [ROOT / "exports" / "*.json", ROOT / "*历史文章*.json"]
RSS_URL = "https://feed.zoro.build/feed/yuSlDu_T4JTBt-shZlfl9-sOWmXdWzmr/a/2727.xml"

CONSECUTIVE_BLOCK_LIMIT = 5   # 连续被风控 N 篇则中止本轮
ARTICLE_DELAY = (3.0, 6.0)    # 每篇文章之间随机间隔秒数

log = logging.getLogger("sync")

TZ_CN = timezone(timedelta(hours=8))


# ---------------- manifest ----------------

def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {"version": 1, "articles": {}}


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    tmp.replace(MANIFEST_PATH)


# ---------------- 候选收集 ----------------

def candidates_from_json(manifest: dict) -> dict[str, dict]:
    """从导出的 JSON 文件收集候选（按 exportedAt 新者覆盖）。"""
    files: list[Path] = []
    for pattern in EXPORT_GLOBS:
        files.extend(p for p in ROOT.glob(pattern.relative_to(ROOT).as_posix())
                     if p.is_file() and ".venv" not in p.parts)
    files = sorted(set(files), key=lambda p: p.stat().st_mtime)
    found: dict[str, dict] = {}
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("跳过无法解析的 JSON：%s", path.name)
            continue
        articles = data.get("articles") if isinstance(data, dict) else None
        if not articles:
            continue
        log.info("读取导出文件 %s：%d 篇", path.name, len(articles))
        for a in articles:
            if not a.get("url") or not a.get("title"):
                continue
            found[article_key(a["url"])] = a
    return found


def candidates_from_rss() -> dict[str, dict]:
    """从 RSS 订阅源收集候选。"""
    feed = feedparser.parse(RSS_URL)
    if feed.bozo and not feed.entries:
        log.warning("RSS 解析失败：%s", feed.bozo_exception)
        return {}
    found: dict[str, dict] = {}
    for entry in feed.entries:
        link = getattr(entry, "link", "")
        title = getattr(entry, "title", "")
        if not link or not title:
            continue
        published = ""
        pub = getattr(entry, "published_parsed", None)
        if pub:
            dt = datetime(*pub[:6], tzinfo=timezone.utc).astimezone(TZ_CN)
            published = dt.isoformat(timespec="seconds")
        summary = ""
        raw = getattr(entry, "summary", "") or ""
        if raw.strip():
            from bs4 import BeautifulSoup
            summary = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)[:300]
        found[article_key(link)] = {
            "title": html.unescape(title),
            "url": link,
            "publishedAt": published,
            "publishedAtUnix": None,
            "coverUrl": None,
            "digest": summary,
            "original": True,
            "reads": None,
            "likes": None,
            "textOnly": False,
            "_from": "rss",
        }
    log.info("RSS 返回 %d 条候选", len(found))
    return found


def merge_candidates(*sources: dict[str, dict]) -> dict[str, dict]:
    """合并候选；RSS 补充 JSON 没有的新文章，但不覆盖 JSON 的富字段。"""
    merged: dict[str, dict] = {}
    for src in sources:
        for key, cand in src.items():
            if key in merged:
                existing = merged[key]
                for field in ("publishedAt", "coverUrl", "digest"):
                    if not existing.get(field) and cand.get(field):
                        existing[field] = cand[field]
            else:
                merged[key] = dict(cand)
    return merged


# ---------------- 抓取 ----------------

def pick_filename(title: str, manifest: dict, used: set[str]) -> str:
    base = sanitize_filename(title)
    candidate = base
    n = 1
    while True:
        conflict = any(
            v.get("file") == f"content/posts/{candidate}.md"
            for v in manifest["articles"].values()
        ) or (f"content/posts/{candidate}.md" in used)
        if not conflict:
            used.add(f"content/posts/{candidate}.md")
            return candidate
        n += 1
        candidate = f"{base}-{n}"


def month_of(meta: dict) -> str:
    published = meta.get("publishedAt") or ""
    m = re.match(r"(\d{4}-\d{2})", published)
    if m:
        return m.group(1)
    return datetime.now(TZ_CN).strftime("%Y-%m")


def sync_article(meta: dict, fetcher: WeChatFetcher, images: ImageStore,
                 manifest: dict, used: set[str]) -> dict:
    """抓取单篇并写 MD。返回 manifest 条目。"""
    key = article_key(meta["url"])
    month = month_of(meta)
    status, body, og_cover = fetcher.fetch_article(meta["url"], month)

    entry = {
        "title": meta["title"],
        "url": normalize_url(meta["url"]),
        "publishedAt": meta.get("publishedAt") or "",
        "status": status,
        "error": None,
        "file": None,
        "images": [],
        "cover": None,
        "syncedAt": datetime.now(TZ_CN).isoformat(timespec="seconds"),
    }

    if status == "deleted":
        log.warning("文章已被删除，跳过：%s", meta["title"])
        return entry
    if status != "ok":
        entry["error"] = f"fetch status={status}"
        return entry

    # 封面：JSON 里的 coverUrl 优先，否则用页面 og:image；统一升级为原图规格
    cover_src = meta.get("coverUrl") or og_cover
    cover_local = None
    if cover_src:
        cover_local = images.download(to_original_image_url(cover_src), month)
        entry["cover"] = cover_local

    if not body.strip():
        body = "（正文为空或仅含无法转换的内容，请移步原文查看。）"

    fname = pick_filename(meta["title"], manifest, used)
    md_path = POSTS_DIR / f"{fname}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_article_md(meta, body, cover_local), encoding="utf-8")

    entry["status"] = "ok"
    entry["file"] = str(md_path.relative_to(ROOT))
    # 精确记录本篇用到的图片（正文引用 + 封面）
    used_imgs = set(re.findall(r"/images/[\w\-.]+", body))
    if cover_local:
        used_imgs.add(cover_local)
    entry["images"] = sorted(used_imgs)
    return entry


# ---------------- git ----------------

def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=check
    )


def git_commit_push(new_count: int, first_title: str | None, do_push: bool) -> str:
    """有变更则提交并推送。返回状态：nochange | committed | pushed | push_failed"""
    dirty = git("status", "--porcelain").stdout.strip()
    if not dirty:
        log.info("仓库无变更，跳过提交")
        return "nochange"
    git("add", "-A")
    if new_count > 0:
        msg = f"sync: 新增 {new_count} 篇文章"
        if first_title:
            msg += f"（{first_title[:40]} 等）"
    else:
        msg = "sync: 刷新预览页与清单"
    git("commit", "-m", msg)
    if not do_push:
        log.info("已提交（--no-push，未推送）：%s", msg)
        return "committed"
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    result = git("push", "origin", branch, check=False)
    if result.returncode != 0:
        log.error("git push 失败：%s", (result.stderr or "").strip()[:500])
        return "push_failed"
    log.info("已推送到 origin/%s", branch)
    return "pushed"


# ---------------- main ----------------

def main() -> int:
    parser = argparse.ArgumentParser(description="公众号文章同步")
    parser.add_argument("--source", choices=["both", "json", "rss"], default="both")
    parser.add_argument("--limit", type=int, default=0, help="最多抓取篇数（0 不限）")
    parser.add_argument("--no-push", action="store_true", help="只提交不推送")
    parser.add_argument("--no-git", action="store_true", help="不提交不推送")
    parser.add_argument("--no-preview", action="store_true", help="跳过重建 preview.html")
    args = parser.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "sync.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    manifest = load_manifest()
    images = ImageStore(IMAGES_DIR)
    fetcher = WeChatFetcher(images)

    # 1. 收集候选
    sources = []
    if args.source in ("both", "json"):
        sources.append(candidates_from_json(manifest))
    if args.source in ("both", "rss"):
        sources.append(candidates_from_rss())
    candidates = merge_candidates(*sources)
    log.info("候选总数 %d，manifest 已有 %d 篇", len(candidates), len(manifest["articles"]))

    # 2. 差量：不在 manifest 或状态非 ok/deleted 的都重试
    todo_keys = []
    for key, cand in candidates.items():
        entry = manifest["articles"].get(key)
        if entry is None or entry.get("status") not in ("ok", "deleted"):
            todo_keys.append(key)
    # 旧文章在前
    def sort_key(k):
        return candidates[k].get("publishedAtUnix") or candidates[k].get("publishedAt") or ""
    todo_keys.sort(key=sort_key)
    if args.limit:
        todo_keys = todo_keys[: args.limit]
    log.info("待抓取 %d 篇", len(todo_keys))

    # 3. 逐篇抓取
    used = set()
    ok_count = deleted_count = fail_count = 0
    aborted = False
    for i, key in enumerate(todo_keys, 1):
        meta = candidates[key]
        log.info("[%d/%d] 抓取：%s", i, len(todo_keys), meta["title"])
        try:
            entry = sync_article(meta, fetcher, images, manifest, used)
        except Exception as exc:  # 单篇异常不中断整体
            log.exception("抓取异常：%s", meta["title"])
            entry = {"title": meta["title"], "url": meta["url"], "status": "failed",
                     "error": str(exc)[:300], "file": None, "images": []}
        manifest["articles"][key] = entry
        save_manifest(manifest)

        if entry["status"] == "ok":
            ok_count += 1
        elif entry["status"] == "deleted":
            deleted_count += 1
        else:
            fail_count += 1

        if fetcher.consecutive_blocked >= CONSECUTIVE_BLOCK_LIMIT:
            log.error("连续 %d 篇被风控拦截，中止本轮（下次运行会重试剩余文章）",
                      fetcher.consecutive_blocked)
            aborted = True
            break

        if i < len(todo_keys):
            time.sleep(random.uniform(*ARTICLE_DELAY))

    log.info("本轮完成：成功 %d，删除 %d，失败 %d，图片下载 %d 张",
             ok_count, deleted_count, fail_count, images.downloaded)

    # 4. 重建本地预览页
    if not args.no_preview:
        from build_preview import build_preview
        preview_path = build_preview(manifest)
        log.info("预览页已生成：%s", preview_path)

    # 5. 提交推送
    push_result = "nochange"
    if not args.no_git:
        first_title = candidates[todo_keys[0]]["title"] if todo_keys else None
        push_result = git_commit_push(ok_count, first_title, do_push=not args.no_push)

    push_msg = {
        "nochange": "无需推送（无变更）",
        "committed": "已提交未推送（--no-push）",
        "pushed": "是",
        "push_failed": "否（push 失败，稍后手动 git push）",
    }[push_result]
    print()
    print("=== 同步摘要 ===")
    print(f"成功 {ok_count} 篇 / 删除 {deleted_count} 篇 / 失败 {fail_count} 篇")
    print(f"图片下载 {images.downloaded} 张")
    print(f"推送到 GitHub：{push_msg}")
    if aborted:
        print("注意：本轮因风控中止，剩余文章下次运行自动重试。")
        return 1
    if fail_count or push_result == "push_failed":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
