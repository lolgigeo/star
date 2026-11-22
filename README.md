# 十方上学记 · 公众号备份博客

微信公众号「辰爸学育儿」的文章自动抓取、转存 Markdown，并通过 Hugo + PaperMod 发布到 GitHub Pages。

> 项目背景、架构设计与决策记录见 [docs/PLAN.md](docs/PLAN.md)。

- 博客地址：https://lolgigeo.github.io/star/
- 本地预览：双击打开仓库根目录的 `preview.html`（离线可用，支持搜索）

## 目录结构

| 路径 | 说明 |
|---|---|
| `content/posts/*.md` | 全部文章（按标题命名，既是备份也是博客源） |
| `static/images/YYYY-MM/` | 从微信抓取的封面图和正文图 |
| `preview.html` | 单文件本地预览页（所有 MD 内嵌，双击即用） |
| `exports/*.json` | 公众号文章清单导出文件（可选的全量数据源） |
| `data/manifest.json` | 同步状态清单（文章 → 文件/图片/状态，用于差量） |
| `credentials/` | 公众号后台登录态（符号链接 → `../lolgigeo/credentials`，已 gitignore，绝不入库） |
| `tools/` | Python 同步脚本（`sync.py` 主入口、`mp_list.py` 后台数据源） |
| `themes/PaperMod/` | 博客主题（直接 vendored） |
| `logs/sync.log` | 同步日志 |

## 日常使用

```bash
# 手动同步一次（json + mp + rss 三源合并、抓新文章、重建预览、提交推送）
./sync.sh

# 只抓 5 篇测试 / 不推送到 GitHub
./sync.sh --limit 5 --no-push
```

每日 21:30 由 ZCode 定时自动化自动执行。

## 新文章如何被发现

- **公众号后台接口**（主要）：`tools/mp_list.py` 用登录态（`credentials/mp_credentials.json`，与姊妹项目 lolgigeo 共享同一份）调 `appmsg/list_ex` 按 fakeid 拉「辰爸学育儿」最新文章，按 `sn` 参数与 `data/manifest.json` 差量。凭据缺失或登录态过期时自动跳过，不中断同步。移植步骤见 lolgigeo 仓库的《MP数据源移植指南.md》。
- **RSS 订阅源**（备用）：feed.zoro.build 绑定本公众号生成的订阅。取阅套餐 2026-11-21 到期后已停更（返回「套餐已到期」提示条目，同步时自动过滤），保留以防续费恢复。
- **手动导出 JSON**（兜底）：新的公众号历史文章导出 JSON 放进 `exports/` 即可，下次同步自动识别，用于补历史。

## 抓取注意事项

- 微信对数据中心 IP 有风控（返回「环境异常」验证页），**抓取必须在本地执行**；GitHub Actions 只负责构建部署页面。
- 脚本自带 3–6 秒随机限速、浏览器 UA、失败重试；连续 5 篇被风控会自动中止本轮，下次运行从断点续抓。
- 下载失败的文章（status 非 ok/deleted）会在下次同步时自动重试。
- **MP 登录态续期**：若日志出现「MP 数据源跳过：…登录态已失效」或 `mp_list.py check` 失败，重新登录 mp.weixin.qq.com，重取 cookie（+token）覆盖 `../lolgigeo/credentials/mp_credentials.json` 的这两个字段（`accounts` 不用动）。周期几天到几周不等，每次约 1 分钟。

## 博客构建

GitHub Actions（`.github/workflows/hugo.yml`）在每次 push 到 main 后自动构建并部署 Pages。本地预览：

```bash
hugo server   # http://localhost:1313/
```
