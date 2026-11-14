#!/usr/bin/env python3
"""生成本地单文件预览页 preview.html。

把仓库里全部文章 Markdown 内嵌进一个 HTML（含本地 vendored 的 marked.js），
双击 file:// 打开即可离线浏览，侧栏按时间倒序 + 标题搜索。
图片路径改写为仓库内相对路径（static/images/...），file:// 下可直接显示。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POSTS_DIR = ROOT / "content" / "posts"
MARKED_PATH = ROOT / "tools" / "vendor" / "marked.min.js"
OUTPUT_PATH = ROOT / "preview.html"


def strip_frontmatter(text: str) -> tuple[dict, str]:
    """拆出我们生成的 frontmatter（已知字段用正则取）。"""
    meta: dict = {}
    body = text
    m = re.match(r"^---\n(.*?)\n---\n", text, flags=re.S)
    if not m:
        return meta, body
    front = m.group(1)
    body = text[m.end():]

    def grab(key: str) -> str:
        km = re.search(rf"^{key}: (.+)$", front, flags=re.M)
        if not km:
            return ""
        val = km.group(1).strip()
        if val.startswith('"') and val.endswith('"'):
            val = json.loads(val)  # 我们用 JSON 转义写的字符串
        return val

    meta["title"] = grab("title")
    meta["date"] = grab("date")
    m_params = re.search(r"^params:\n((?:  .+\n?)+)", front, flags=re.M)
    if m_params:
        sm = re.search(r"^  source: (.+)$", m_params.group(1), flags=re.M)
        if sm:
            val = sm.group(1).strip()
            meta["source"] = json.loads(val) if val.startswith('"') else val
    return meta, body


def build_preview(manifest: dict | None = None) -> Path:
    """扫描 content/posts 生成 preview.html，返回输出路径。"""
    articles = []
    for md_path in sorted(POSTS_DIR.glob("*.md")):
        text = md_path.read_text(encoding="utf-8")
        meta, body = strip_frontmatter(text)
        title = meta.get("title") or md_path.stem
        date = meta.get("date") or ""
        # file:// 下站内绝对路径不可用 -> 改成仓库相对路径
        body = body.replace("](/images/", "](static/images/")
        source = meta.get("source", "")
        articles.append({
            "file": md_path.name,
            "title": title,
            "date": date,
            "source": source,
            "md": body,
        })

    # 新文章在前
    articles.sort(key=lambda a: a["date"], reverse=True)

    marked_js = MARKED_PATH.read_text(encoding="utf-8")
    # HTML 规范：script 数据里只需转义 </script 和 <!--，JSON 里 / 可安全转义
    marked_js = re.sub(r"</(\s*)script", r"<\\/\1script", marked_js, flags=re.I)
    marked_js = marked_js.replace("<!--", "<\\!--")
    # 防止内嵌 JSON 提前结束 <script>（JSON 中 \/ 是合法转义）
    payload = json.dumps(articles, ensure_ascii=False).replace("</", "<\\/")

    html = TEMPLATE.replace("/*__MARKED_JS__*/", marked_js) \
                   .replace("__ARTICLES_JSON__", payload) \
                   .replace("__COUNT__", str(len(articles)))
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    return OUTPUT_PATH


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>十方上学记 · 文章预览</title>
<style>
  :root {
    --bg: #ffffff; --bg-soft: #f6f7f9; --fg: #1f2328; --fg-dim: #6a737d;
    --line: #e4e7eb; --accent: #0969da; --active-bg: #eaf3fd;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d1117; --bg-soft: #161b22; --fg: #e6edf3; --fg-dim: #8b949e;
      --line: #30363d; --accent: #58a6ff; --active-bg: #1f2e3f;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; color: var(--fg); background: var(--bg); }
  header { position: fixed; top: 0; left: 0; right: 0; height: 52px; display: flex; align-items: center; gap: 12px; padding: 0 16px; border-bottom: 1px solid var(--line); background: var(--bg); z-index: 10; }
  header h1 { font-size: 15px; margin: 0; font-weight: 600; white-space: nowrap; }
  header .count { color: var(--fg-dim); font-size: 12px; white-space: nowrap; }
  #search { flex: 1; max-width: 360px; margin-left: auto; padding: 6px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--bg-soft); color: var(--fg); font-size: 13px; outline: none; }
  #search:focus { border-color: var(--accent); }
  nav { position: fixed; top: 52px; bottom: 0; left: 0; width: 300px; overflow-y: auto; border-right: 1px solid var(--line); background: var(--bg-soft); padding: 8px; }
  nav .item { padding: 8px 10px; border-radius: 8px; cursor: pointer; }
  nav .item:hover { background: var(--active-bg); }
  nav .item.active { background: var(--active-bg); }
  nav .item .d { font-size: 11px; color: var(--fg-dim); }
  nav .item .t { font-size: 13px; line-height: 1.45; margin-top: 2px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  main { position: fixed; top: 52px; bottom: 0; left: 300px; right: 0; overflow-y: auto; }
  .wrap { max-width: 760px; margin: 0 auto; padding: 36px 28px 80px; }
  .meta { color: var(--fg-dim); font-size: 13px; margin-bottom: 4px; }
  .links { margin: 6px 0 0; font-size: 13px; }
  .links a { color: var(--accent); text-decoration: none; margin-right: 14px; }
  article { line-height: 1.85; font-size: 16px; }
  article img { max-width: 100%; height: auto; border-radius: 6px; }
  article h1, article h2, article h3 { line-height: 1.4; }
  article blockquote { margin: 1em 0; padding: 2px 14px; border-left: 3px solid var(--accent); background: var(--bg-soft); border-radius: 0 6px 6px 0; color: var(--fg-dim); }
  article hr { border: none; border-top: 1px solid var(--line); margin: 2em 0; }
  article a { color: var(--accent); }
  article code { background: var(--bg-soft); padding: 1px 5px; border-radius: 4px; font-size: 90%; }
  .placeholder { color: var(--fg-dim); text-align: center; margin-top: 30vh; }
  @media (max-width: 900px) {
    nav { width: 240px; }
    main { left: 240px; }
  }
</style>
</head>
<body>
<header>
  <h1>十方上学记</h1>
  <span class="count">共 __COUNT__ 篇 · 内容与 content/posts/ 实时同步</span>
  <input id="search" type="search" placeholder="搜索文章标题…">
</header>
<nav id="list"></nav>
<main><div class="wrap" id="view"><div class="placeholder">← 从左侧选择一篇文章</div></div></main>
<script>
/*__MARKED_JS__*/
</script>
<script>
const ARTICLES = __ARTICLES_JSON__;
marked.setOptions({ gfm: true, breaks: true });
const listEl = document.getElementById('list');
const viewEl = document.getElementById('view');
const searchEl = document.getElementById('search');
let current = -1;

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function renderList(filter) {
  const f = (filter || '').trim().toLowerCase();
  listEl.innerHTML = '';
  ARTICLES.forEach((a, i) => {
    if (f && !a.title.toLowerCase().includes(f)) return;
    const item = document.createElement('div');
    item.className = 'item' + (i === current ? ' active' : '');
    item.innerHTML = '<div class="d">' + esc((a.date || '').replace('T', ' ').slice(0, 16)) + '</div>' +
                     '<div class="t">' + esc(a.title) + '</div>';
    item.onclick = () => show(i);
    listEl.appendChild(item);
  });
}

function show(i) {
  current = i;
  const a = ARTICLES[i];
  const srcLink = a.source ? '<a href="' + esc(a.source) + '" target="_blank" rel="noopener">微信原文</a>' : '';
  const mdLink = '<a href="content/posts/' + encodeURIComponent(a.file) + '" target="_blank" rel="noopener">打开 .md</a>';
  viewEl.innerHTML =
    '<h1>' + esc(a.title) + '</h1>' +
    '<div class="meta">' + esc((a.date || '').replace('T', ' ').slice(0, 16)) + '</div>' +
    '<div class="links">' + srcLink + mdLink + '</div><hr>' +
    '<article>' + marked.parse(a.md) + '</article>';
  document.querySelectorAll('nav .item').forEach(el => el.classList.remove('active'));
  renderList(searchEl.value);
  viewEl.parentElement.scrollTop = 0;
}

searchEl.addEventListener('input', () => renderList(searchEl.value));

// 默认打开第一篇（最新）
if (ARTICLES.length) { show(0); }
renderList('');
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    path = build_preview(None)
    print(f"preview.html 已生成：{path}")
