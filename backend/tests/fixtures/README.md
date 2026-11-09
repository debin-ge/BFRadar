# 抓取样本（fixtures）

本目录下的 `lowendtalk_*`、`lowendspirit_*`、`lowendbox_*`、`reddit_*`、`nodeseek/`、`telegram/`、`official/` 和 `forums/` 是各来源 RSS、
频道页与官网活动页的原样快照，
内容是第三方用户和站点的帖子原文，版权归原作者与原站所有，**不随仓库发布**（已写进 `.gitignore`）。

只有 `fx_*.json`（欧洲央行与 open.er-api 的汇率接口响应）和项目自己写的 `empty_feed.xml`、`telegram_empty.html`
（新入口在 11-02 快照里没有对应文件时返回的空 feed 与空频道页）随仓库提供。

`telegram/` 是 Telegram 频道 @VPS_spiders 的网页预览：`vps_spiders_before_{id}.html` 为 2025 年黑五窗口
（消息 26150–26622）按 `?before=` 翻页的各页，供回放测试（D81）。`official/` 是 Hostinger 两个黑五页（D84），
`forums/` 是 V2EX、NodeLoc、HostingDiscussion 11-08 的 feed（D88）。

## 没有样本时

依赖样本的测试（采集、流水线、端到端、金标集评估等）会自动跳过，其余单元测试照常运行：

```bash
cd backend
uv run pytest        # 缺样本的测试显示为 skipped
```

## 自己抓一份

```bash
cd backend
uv run python tests/tools/snapshot.py          # 各源抓一次，保存到本目录
```

样本只用于本机测试，请勿提交或再分发。注意：

- 抓取遵守各站的使用条款与 robots.txt，见仓库根目录 README 的"采集原则与合规"一节。
- 测试里的断言（条数、帖子 ID、金标集 `tests/gold/`）对应维护者 2026-11-02 当天的快照；重新抓取的内容不同，
  这些断言会不成立。想完整复跑金标集评估，需要向维护者索取同一份快照（仅限个人研究）。
