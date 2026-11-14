# 项目规划与设计文档

> 本文是 2026-11-14 项目启动时的规划存档。
> 日常使用见 [README.md](README.md)。

## 一、背景与目标

把微信公众号「辰爸学育儿」（biz `MzIwMjM0NDM1Nw==`，文章标题统一带「十方上学记｜」前缀）的文章：

1. 抓取为 **Markdown 文件存入本仓库**（按文章标题命名）——作为长期备份；
2. 用 **Hugo + PaperMod** 主题发布为 GitHub Pages 博客（https://lolgigeo.github.io/star/ ）；
3. 生成一个**本地单文件 HTML 预览页**，双击即可离线浏览全部 MD 文章；
4. 之后**每日自动发现并同步新文章**（RSS 增量）。

范围：**只处理 RSS 当前能抓到的文章 + 以后新发的文章**（不做历史全量回补；RSS 目前只有 3 篇）。管线与姊妹项目 `../lolgigeo`（「文森和他的孩子们」）完全一致，仅换数据源与站点信息。

## 二、关键决策及理由

| 决策 | 选择 | 理由 |
|---|---|---|
| 抓取执行位置 | **必须本地**（家庭宽带） | 微信对数据中心 IP 有风控（「环境异常」验证页）；GitHub Actions 只负责 Hugo 构建部署 |
| 新文章发现 | **RSS 源轮询**（feed.zoro.build 绑定公众号） | RSS 为 RSS 2.0、链接直达微信原文，只保留最近几条 → 做增量发现；`exports/` 兜底逻辑保留（放入导出 JSON 即可补历史） |
| 图片存储 | **下载到仓库** | 微信图床有防盗链和失效风险，本地化才是真备份；封面一律 `/0` 原图规格（1280px） |
| 博客主题 | **Hugo + PaperMod**（vendored） | 与 lolgigeo 保持一致，构建快、CJK 友好 |
| 本地预览 | **单文件 preview.html** | marked.js vendored 内嵌，双击 `file://` 即开、完全离线 |
| 每日自动化 | **ZCode 定时自动化** | 每天 20:30、23:30 各跑一次 `./sync.sh`；无新文章时空跑零副作用 |
| 仓库 | `lolgigeo/star` | 项目页，URL 为 `https://lolgigeo.github.io/star/`（非用户主页；2026-11-14 由 `star.github.io` 改名而来） |

## 三、架构

```
┌─ 数据源 ──────────────────────────────────┐
│  feed.zoro.build RSS（增量，最近 ~3-6 条） │
│  exports/*.json（可选兜底）                │
└──────────────┬───────────────────────────┘
               ▼
   tools/sync.py  ＝ 差量引擎（按微信 URL 的 sn 参数与 data/manifest.json 对比）
               ▼
   tools/wechat_fetch.py  ＝ 单篇抓取（本地网络 + 浏览器 UA + 限速 3~6s + 风控退避）
               │    · #js_content → Markdown（markdownify）
               │    · 图片下载到 static/images/YYYY-MM/（封面一律 /0 原图规格）
               │    · 视频号/音频组件 → 「移步原文」提示
               ▼
   content/posts/*.md（按标题命名） + data/manifest.json（状态清单，断点续抓）
               │
               ├──▶ tools/build_preview.py ─▶ preview.html（单文件离线预览）
               │
               └──▶ git push ─▶ GitHub Actions（.github/workflows/hugo.yml）
                                    ▼
                          hugo --minify ─▶ GitHub Pages
                          （https://lolgigeo.github.io/star/）
```

## 四、关键实现细节

- **子路径部署**：本站是项目页，部署在 `/star/` 子路径下。Hugo 的 `absURL`/`relURL` 对 `/` 开头的路径只拼主机名，因此：
  - 封面 frontmatter **不带前导斜杠**（`images/...`）→ PaperMod `absURL` 拼出含子路径的完整 URL（含 og:image）；
  - 正文 MD 图片保留 `/images/...` 写法（与 preview.html 的改写约定一致），由 `layouts/_default/_markup/render-image.html` 渲染钩子改写为含子路径的 URL。
  - 不用 `relativeURLs = true`：实测会把主题静态资源改写成双重路径（`./star/...`），且改不了模板里 absURL 的输出。
- **风控检测**：页面含「环境异常 / 操作频繁」等标记即判定被拦；连续 5 篇被拦自动中止本轮，下次运行续抓。
- **差量键**：微信文章 URL 的 `sn` 参数（唯一），退回 `mid+idx`。
- **MD frontmatter**：`title / date / cover / params(source, original)`，正文尾部附「查看原文」链接；RSS 无摘要与阅读数，`description`/`readsAtExport` 留空。
- **文件命名**：标题清洗文件系统特殊字符；标题自带「十方上学记｜」前缀，原样保留。

## 五、实施结果（2026-11-14 首次上线）

| 计划项 | 结果 |
|---|---|
| RSS 首次同步 | ✅ 3 篇全部成功 |
| 图片本地化 | ✅ 3 张封面（1280px 原图规格） |
| 本地预览页 | ✅ preview.html 生成（模板沿用 lolgigeo 已验证版本） |
| Hugo 构建 | ✅ 14 页，零 ERROR / 零 WARN，子路径链接验证通过 |
| 每日自动化 | ✅ 已创建（20:30 / 23:30） |

## 六、遗留与注意事项

- GitHub Pages 需在仓库设置里开启（Settings → Pages → Source: **GitHub Actions**），本机无 `gh` CLI，走网页操作。
- RSS 只保留最近几条：若自动化中断超过一天漏抓，把新导出的 JSON 放入 `exports/` 补同步。
- 抓取必须在本地执行（微信风控），不要把同步搬到 Actions 里跑。
