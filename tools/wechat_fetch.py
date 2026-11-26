#!/usr/bin/env python3
"""微信公众号文章抓取与转换。

抓取 mp.weixin.qq.com 文章页，把 #js_content 正文转成 Markdown，
图片下载到本地 static/images/，并生成 Hugo frontmatter。

风控检测：返回页面包含「环境异常 / 操作频繁」等标记时判定为被拦截，
调用方据此做指数退避或中止本轮。
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from markdownify import MarkdownConverter

log = logging.getLogger("wechat_fetch")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 页面被风控/删除的特征文本
BLOCK_MARKS = ("环境异常", "完成验证后即可继续访问", "操作频繁", "当前环境异常，完成验证")
DELETED_MARKS = ("该内容已被发布者删除", "此内容因违规无法查看", "该内容暂时无法查看")

# 需要替换成「移步原文」提示的嵌入组件
EMBED_TAGS = (
    "mpvideosnap", "mp-commonvideosnap", "mp-miniprogram", "mp-commonminiprogram",
    "mpvoice", "mp-commonvoice", "iframe", "video", "audio", "embed", "object",
    "mp-commonaisetting", "mp-commonpastepost", "mp-commonpoc", "wx-open-launch-weapp",
)

# 清理掉的干扰节点
REMOVE_SELECTORS = (
    "script", "style", "link",
    "#js_pc_qr_code", ".qr_code_pc_outer", "#js_tags_preview",
    ".rich_media_tool", "#js_bottom_area", ".rich_media_area_extra",
    "mp-style-type", "mp-common-profile", "#js_profile_qrcode",
)

FMT_EXT = {
    "jpeg": "jpg", "jpg": "jpg", "png": "png", "gif": "gif",
    "webp": "webp", "svg": "svg", "bmp": "bmp",
}


def normalize_url(url: str) -> str:
    """微信文章链接统一成 https 并去掉跟踪参数。"""
    url = url.strip().replace("http://", "https://")
    parsed = urlparse(url)
    keep = {"__biz", "mid", "idx", "sn", "chksm"}
    query = "&".join(
        f"{k}={v[0]}" for k, v in parse_qs(parsed.query).items() if k in keep
    )
    return f"https://{parsed.netloc}{parsed.path}?{query}" if query else url


def to_original_image_url(url: str) -> str:
    """微信图床 URL 尾段缩放参数改为 /0（原图）。

    公众号导出的封面 URL：头条是 /640（实际 1080px 版本）、次条是 /300
    （实际 150px 缩略图，博客里放大会发虚）。/0 返回 CDN 存储的原始尺寸。
    """
    return re.sub(
        r"(mmbiz\.qpic\.cn/(?:sz_)?mmbiz\w+/[A-Za-z0-9_-]+/)\d+",
        r"\g<1>0",
        url,
    )


def article_key(url: str) -> str:
    """文章唯一键：优先 sn，退回 mid+idx，最后整条 URL。"""
    q = parse_qs(urlparse(url).query)
    sn = (q.get("sn") or [""])[0]
    if sn:
        return f"sn:{sn}"
    mid = (q.get("mid") or [""])[0]
    idx = (q.get("idx") or [""])[0]
    if mid:
        return f"mid:{mid}:{idx}"
    return "url:" + url


class ImageStore:
    """把微信图床图片下载到 static/images/YYYY-MM/ 下。"""

    def __init__(self, images_root: Path, referer: str = "https://mp.weixin.qq.com/"):
        self.root = images_root
        self.session = requests.Session()
        self.session.trust_env = False  # 微信图床必须本机直连，绕过系统/环境代理
        self.session.headers.update({
            "User-Agent": UA,
            "Referer": referer,
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        })
        self.downloaded = 0  # 本轮累计下载张数

    def _ext_for(self, url: str, resp: requests.Response | None) -> str:
        q = parse_qs(urlparse(url).query)
        fmt = (q.get("wx_fmt") or [""])[0].lower()
        if fmt in FMT_EXT:
            return FMT_EXT[fmt]
        if resp is not None:
            ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
            guess = {
                "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
                "image/webp": "webp", "image/svg+xml": "svg",
            }.get(ctype)
            if guess:
                return guess
        return "jpg"

    def download(self, url: str, month_dir: str) -> str | None:
        """下载单张图片，返回站内绝对路径；失败返回 None。"""
        url = url.replace("http://", "https://")
        digest = hashlib.md5(url.encode()).hexdigest()[:16]
        target_dir = self.root / month_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        # 先按 URL 参数猜扩展名；若已有同名文件直接复用（断点续抓）
        probe = target_dir / f"{digest}.*"
        existing = sorted(target_dir.glob(f"{digest}.*"))
        if existing:
            return "/images/" + month_dir + "/" + existing[0].name
        for attempt in range(3):
            try:
                resp = self.session.get(url, timeout=30)
                if resp.status_code == 200 and resp.content and len(resp.content) > 512:
                    ext = self._ext_for(url, resp)
                    path = target_dir / f"{digest}.{ext}"
                    path.write_bytes(resp.content)
                    self.downloaded += 1
                    time.sleep(random.uniform(0.2, 0.6))
                    return "/images/" + month_dir + "/" + path.name
                log.warning("图片响应异常 %s -> HTTP %s", url[:80], resp.status_code)
            except requests.RequestException as exc:
                log.warning("图片下载失败(%d/3) %s: %s", attempt + 1, url[:80], exc)
            time.sleep(1.5 * (attempt + 1))
        return None


class WeChatFetcher:
    """抓取文章页并转换为 Markdown。"""

    def __init__(self, images: ImageStore):
        self.session = requests.Session()
        self.session.trust_env = False  # 微信正文页必须本机直连，绕过系统/环境代理
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://mp.weixin.qq.com/",
        })
        self.images = images
        self.consecutive_blocked = 0

    def fetch_html(self, url: str) -> tuple[str, str]:
        """返回 (status, html)。status: ok | blocked | deleted | error"""
        try:
            resp = self.session.get(normalize_url(url), timeout=30)
        except requests.RequestException as exc:
            log.error("请求失败 %s: %s", url[:80], exc)
            return "error", ""
        if resp.status_code != 200:
            return "error", ""
        html = resp.text
        if any(mark in html for mark in BLOCK_MARKS):
            return "blocked", html
        if any(mark in html for mark in DELETED_MARKS):
            return "deleted", html
        if 'id="js_content"' not in html:
            # 没有正文的其它异常页（如聚合提示页）
            return "error", html
        return "ok", html

    # ---------- HTML -> Markdown ----------

    def _download_imgs(self, soup: BeautifulSoup, month_dir: str) -> None:
        for img in list(soup.find_all("img")):
            src = img.get("data-src") or img.get("src") or ""
            if not src.startswith(("http://", "https://")):
                img.decompose()
                continue
            local = self.images.download(src, month_dir)
            if local:
                img.attrs = {"src": local, "alt": (img.get("alt") or "").strip()}
            else:
                img.replace_with(f"[图片下载失败，请移步原文查看]({normalize_url(src)})")

    def _replace_embeds(self, soup: BeautifulSoup, source_url: str) -> None:
        note = f"🎬 此处为视频/音频或小程序内容，请[移步原文]({source_url})观看。"
        for tag_name in EMBED_TAGS:
            for node in list(soup.find_all(tag_name)):
                node.replace_with(note)

    def _clean(self, soup: BeautifulSoup) -> None:
        for sel in REMOVE_SELECTORS:
            for node in soup.select(sel):
                node.decompose()
        # 去掉内联样式（保留结构），再清掉纯空白节点
        for node in soup.find_all(style=True):
            node.attrs = {}
        for node in soup.find_all(["section", "span", "p", "div", "blockquote"]):
            if not node.get_text(strip=True) and not node.find(["img", "br", "hr"]):
                node.decompose()

    def fetch_article(
        self, url: str, month_dir: str
    ) -> tuple[str, str, str | None]:
        """抓取并转换单篇。

        返回 (status, markdown_body, og_cover)。status 同 fetch_html。
        """
        status, html = self.fetch_html(url)
        if status != "ok":
            self.consecutive_blocked = (
                self.consecutive_blocked + 1 if status == "blocked" else 0
            )
            return status, "", None
        self.consecutive_blocked = 0

        soup = BeautifulSoup(html, "html.parser")
        og_cover = None
        meta_og = soup.find("meta", attrs={"property": "og:image"})
        if meta_og and meta_og.get("content"):
            og_cover = meta_og["content"]

        content = soup.find(id="js_content")
        if content is None:  # 理论上 fetch_html 已过滤
            return "error", "", None

        self._clean(content)
        self._download_imgs(content, month_dir)
        self._replace_embeds(content, normalize_url(url))

        body = MarkdownConverter(heading_style="ATX", bullets="-").convert_soup(content)
        body = self._postprocess(body)
        return "ok", body, og_cover

    @staticmethod
    def _postprocess(md: str) -> str:
        md = md.replace("\xa0", " ")
        md = re.sub(r"[ \t]+\n", "\n", md)
        md = re.sub(r"\n{3,}", "\n\n", md)
        md = re.sub(r"^\s+|\s+$", "", md)
        # markdownify 偶尔产出空链接 [text]() 保留文本
        md = re.sub(r"\[([^\]]+)\]\(\s*\)", r"\1", md)
        return md


def build_frontmatter(meta: dict, cover_local: str | None) -> str:
    """生成 Hugo frontmatter。字符串统一走 JSON 转义（合法 YAML）。"""
    lines = ["---"]
    lines.append(f"title: {json.dumps(meta['title'], ensure_ascii=False)}")
    lines.append(f"date: {meta['publishedAt']}")
    digest = (meta.get("digest") or "").strip()
    if digest:
        lines.append(f"description: {json.dumps(digest, ensure_ascii=False)}")
    if cover_local:
        lines.append("cover:")
        # 去掉前导 /：absURL 对 / 开头路径只拼主机名，会丢掉 Pages 子路径
        lines.append(f"  image: {json.dumps(cover_local.lstrip('/'), ensure_ascii=False)}")
        lines.append("  relative: false")
    params = {
        "source": normalize_url(meta["url"]),
        "original": bool(meta.get("original")),
        "textOnly": bool(meta.get("textOnly")),
    }
    if meta.get("reads") is not None:
        params["readsAtExport"] = meta["reads"]
    if meta.get("likes") is not None:
        params["likesAtExport"] = meta["likes"]
    lines.append("params:")
    for k, v in params.items():
        lines.append(f"  {k}: {json.dumps(v, ensure_ascii=False)}")
    lines.append("draft: false")
    lines.append("---")
    return "\n".join(lines)


def render_article_md(meta: dict, body: str, cover_local: str | None) -> str:
    fm = build_frontmatter(meta, cover_local)
    source = normalize_url(meta["url"])
    footer = (
        f"\n---\n\n> 本文首发于微信公众号「辰爸学育儿」，"
        f"[查看原文]({source})。\n"
    )
    return f"{fm}\n\n{body}\n{footer}"


def sanitize_filename(title: str) -> str:
    """标题 -> 文件名安全字符串。"""
    name = title.strip()
    name = re.sub(r"[\\/:*?\"<>|#\x00-\x1f]", "", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip(" .")
    if len(name) > 80:
        name = name[:80].rstrip(" .")
    return name or "untitled"
