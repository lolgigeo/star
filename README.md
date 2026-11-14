# 十方上学记 · 公众号备份博客

微信公众号「辰爸学育儿」的文章自动抓取、转存 Markdown，并通过 Hugo + PaperMod 发布到 GitHub Pages。

> 项目背景、架构设计与决策记录见 [docs/PLAN.md](docs/PLAN.md)。

- 博客地址：https://lolgigeo.github.io/star.github.io/
- 本地预览：双击打开仓库根目录的 `preview.html`（离线可用，支持搜索）

## 目录结构

| 路径 | 说明 |
|---|---|
| `content/posts/*.md` | 全部文章（按标题命名，既是备份也是博客源） |
| `static/images/YYYY-MM/` | 从微信抓取的封面图和正文图 |
| `preview.html` | 单文件本地预览页（所有 MD 内嵌，双击即用） |
| `exports/*.json` | 公众号文章清单导出文件（可选的全量数据源） |
| `data/manifest.json` | 同步状态清单（文章 → 文件/图片/状态，用于差量） |
| `tools/` | Python 同步脚本 |
| `themes/PaperMod/` | 博客主题（直接 vendored） |
| `logs/sync.log` | 同步日志 |

## 日常使用

```bash
# 手动同步一次（自动读 exports/ 和 RSS、抓新文章、重建预览、提交推送）
./sync.sh

# 只抓 5 篇测试 / 不推送到 GitHub
./sync.sh --limit 5 --no-push
```

每日 20:30、23:30 由 ZCode 定时自动化自动执行。

## 新文章如何被发现

- **RSS 订阅源**（主要）：`tools/sync.py` 里的 `RSS_URL`（feed.zoro.build 绑定公众号「辰爸学育儿」生成），每天自动轮询，按文章 `sn` 参数与 `data/manifest.json` 差量。
- **手动导出 JSON**（兜底）：新的公众号历史文章导出 JSON 放进 `exports/` 即可，下次同步自动识别。RSS 只保留最近几条，若自动化中断超过一天漏抓，靠导出补。

## 抓取注意事项

- 微信对数据中心 IP 有风控（返回「环境异常」验证页），**抓取必须在本地执行**；GitHub Actions 只负责构建部署页面。
- 脚本自带 3–6 秒随机限速、浏览器 UA、失败重试；连续 5 篇被风控会自动中止本轮，下次运行从断点续抓。
- 下载失败的文章（status 非 ok/deleted）会在下次同步时自动重试。

## 博客构建

GitHub Actions（`.github/workflows/hugo.yml`）在每次 push 到 main 后自动构建并部署 Pages。本地预览：

```bash
hugo server   # http://localhost:1313/
```
