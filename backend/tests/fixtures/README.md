# 抓取样本（fixtures）

本目录下的 `lowendtalk_*`、`lowendspirit_*`、`lowendbox_*`、`reddit_*` 和 `nodeseek/` 是各来源 RSS 的原样快照，
内容是第三方用户和站点的帖子原文，版权归原作者与原站所有，**不随仓库发布**（已写进 `.gitignore`）。

只有 `fx_*.json`（欧洲央行与 open.er-api 的汇率接口响应）随仓库提供。

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
