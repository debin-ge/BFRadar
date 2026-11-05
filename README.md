# 黑五雷达（BFRadar）

[English](README.en.md) · 中文

把黑五、网一期间散落在海外 VPS 论坛和中文社区里的优惠帖，整理成一份可以筛选、可以横向比较的清单。每小时更新，中英双语，开源（MIT）。

采集多个 VPS 论坛和社区的公开 RSS，把每条优惠抽取到**套餐级**字段：配置（CPU、内存、硬盘、带宽、端口、机房、线路标签）、原币种价格与人民币估算、优惠码、折扣性质（首期、永久、终身）、截止时间。只收 VPS、独立服务器、虚拟主机、存储四类产品；不收库存。每条优惠都链回原帖，页面上只摘录 300 字以内。

英文名 BFRadar，全称 BlackFridayRadar。

## 工作方式

```
RSS 入口 ──► 采集源插件 ──► 原文入库 ──► 分类 ──► 套餐分段 ──► 字段规则抽取 ──┐
                            (SQLite)                                           │
                                                                               ▼
              JSON 接口 · RSS · sitemap · 分享图 ◄── 合并 · 变更检测 ◄── LLM 补抽与反幻觉校验（可选）
                     ▲
                     └── React 前端（服务端渲染，中文在 /，英文在 /en/）
```

- **规则优先，LLM 可选**：价格、周期、配置、优惠码先由规则层确定性地抽取；配置了 OpenAI 兼容接口后，LLM 负责翻译标题、写摘要、补抽规则漏掉的字段。LLM 给出的价格和优惠码必须能在原帖里找到，否则丢弃并回退到规则结果。不配 LLM 也能完整运行。
- **变更检测**：同一原帖再次出现时逐字段比对，生成"降价""新码"标记。
- **前后端分离**：后端（Python / FastAPI）只出数据，不出页面；前端（React Router + Express）是站点唯一入口。

## 快速开始

需要 [uv](https://docs.astral.sh/uv/)（Python 3.12，版本由 `backend/.python-version` 固定）、Node 24 和 pnpm。

```bash
# 后端
cd backend
uv sync                              # 按 uv.lock 安装依赖
cp .env.example .env                 # 按需修改，留空即默认值
uv run python main.py init-db        # 创建数据库 data/radar.db
uv run python main.py crawl          # 采集一轮（--llm-limit 0 不调用 LLM）
uv run python main.py serve          # 启动后端和调度器，接口在 http://127.0.0.1:8000/api/…

# 前端（另开一个终端）
cd frontend
pnpm install
API_ORIGIN=http://127.0.0.1:8000 pnpm dev     # 打开 http://127.0.0.1:3000
```

`uv run python main.py --help` 列出其他命令（`stats`、`show`、`reprocess`、`llm-backfill`、`fx-update` 等）。

### 配置

全部走环境变量，也可写进 `backend/.env`（真实环境变量优先）。每一项的含义和默认值见 [`backend/.env.example`](backend/.env.example)，要点：

- `SITE_URL`、`CONTACT_EMAIL`：上线必填。它们会写进采集用的 User-Agent，让被采集的站点知道你是谁、怎么联系你。
- `SOURCES`、`INTERVAL_<源>`：启用哪些源、各自的间隔。
- `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`：三项齐全才启用 LLM。
- `HTTPS_PROXY`：采集走代理时设置，默认不需要。

## 本机部署

用 Docker Compose 在本机运行，起两个容器：`app`（后端与调度器）、`web`（前端，站点唯一入口）。只在本机 `127.0.0.1` 上开一个端口。

```bash
cp backend/.env.example backend/.env   # 应用配置，按需修改（建议填 CONTACT_EMAIL）
docker compose up -d --build           # 打开 http://127.0.0.1:3000/
```

- 数据库和备份在 `backend/data/`，停止、重建都不会丢。本机开发时的后端也读这个目录，两边不要同时运行。
- 换端口：`WEB_PORT=8080 docker compose up -d`。
- 采集要走本机代理：在 `backend/.env` 里写 `HTTPS_PROXY=http://host.docker.internal:<代理端口>`。
- 常用命令：`docker compose logs -f app` 看日志，`docker compose down` 停止。

## 采集原则与合规

BFRadar 的采集程序按下面的原则写成，部署自己的实例时请保持，并对自己的实例负责：

- **只读公开入口**：只访问公开的 RSS / Atom，不登录、不模拟浏览器。遇到 Cloudflare 质询立即失败并记入运行记录，**不做任何绕过**。
- **表明身份**：User-Agent 为 `Mozilla/5.0 (compatible; BFRadar/0.1; +SITE_URL; CONTACT_EMAIL)`，请填上你自己的站点地址和联系邮箱。
- **低频**：默认间隔见 `backend/.env.example`；遇到 429 / 5xx 按 `Retry-After` 退避。
- **只摘录，不转载**：页面和 API 只展示结构化字段、LLM 生成的摘要和 300 字以内的摘录，每条都链回原帖；社区讨论只收标题和链接。帖子全文只留在本地数据库里供抽取使用，不对外输出，也不展示发帖人。
- **帖子内容的版权属于原作者和原站**：MIT 许可证只覆盖本仓库的代码和文档，不覆盖被采集的内容。

各来源站点的使用条款和 `robots.txt` 各不相同，而且会变。部署前请逐一核对，并自行判断你的使用场景是否被允许；帖子里的用户名、联系方式等可能构成个人信息，不要公开或另作他用；收到移除内容的要求请及时响应。

## 许可证

[MIT](LICENSE) © 2026 BFRadar contributors