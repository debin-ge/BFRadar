# 金标集 v1.0（标注中）

标注口径见 [《金标集标注规范 v1.0》](../../docs/黑五雷达-金标集标注规范-v1.0.md)。本文件记录规范未写细的标注约定、配额执行情况（D37）、v1.1 待补清单与调试集 / 留出集名单。

| 项 | 状态 |
|---|---|
| 版本 | v1.0，未冻结（M1 验收前打 `gold-v1.0` 标签） |
| 已标注 | LET 15、LES 5、LEB 5，共 25 条（11-02，标注人 claude，待复核） |
| 待标注 | NodeSeek 10 条：快照池积累至 11-03 12:00（本机时间）后选样标注，并按第 2.4 节划分 |
| 校验 | `uv run python tests/gold_eval.py --validate` 0 错误；`--self-check` 金标自评 100% |

## 标注约定

以下是标注时遇到、规范未写细的情形的统一处理。复核确认后回写规范。

| # | 情形 | 约定 | 样本 |
|---|---|---|---|
| 1 | 套餐级库存 | 只在原文单独点名该套餐时填写；对整个区块或全帖的说明只记在记录级 | let-222765 'Limited slots per plan'；let-222165 Mac mini '2 available' |
| 2 | `price_note` | 列出的价格本身就是活动价（含"用码后价"）→ promo；列的是原价、折扣需另用码或百分比 → regular；原文看不出 → null | — |
| 3 | 首期价与续费价并列 | 按 6.11 取促销价（首期 / 首月价），续费价写 notes | let-222871、let-222539 |
| 4 | 套餐名 | 原文有套餐名或标题行则用之；表格行首为型号（独服 CPU）时用型号；只有规格数字的行为 null | let-222865、let-222774 |
| 5 | 机房 | 区块级机房说明适用于区块内全部套餐；APAC、EU 这类大区不当机房；原文只给出区域名的机房（如 'USA East'）照写 | let-222164 Mumbai；let-222534；let-222871 |
| 6 | IPv4（已取消） | D69 起不标：商家默认都给 IPv4，不区分，金标集不再有 `ipv4` 字段 | — |
| 7 | 混合介质与 SAS | HDD 主盘 + 少量 NVMe 时 `disk_gb` 取主盘容量、NVMe 写 notes；SAS 未注明 SSD 时 `disk_type` 留空 | let-221797、let-222157、let-222165 |
| 8 | 形态（5.12 补充） | "字段: 值"逐行书写算 list；以 `·`、`│` 等分隔符分隔字段的单行套餐与竖线同归 pipe | let-222809（list）；let-222165（pipe） |
| 9 | `purchase_url` | 无落地页且各套餐各有下单链接时取第一个下单链接；没有任何下单链接时取官网 | let-222164；les-11479 |
| 10 | 抽奖帖 | 无可购买套餐但命中黑五词时按 5.3 优先级取 black_friday，product_types 按赠品类型 | let-222735、les-11436 |
| 11 | 码的归属 | 只适用于部分套餐的码写入这些套餐的 `coupon_code`；全帖通用的码不写入套餐；指向本帖以外产品线的码只记录级 | let-222774、let-222157、leb-54089 |
| 12 | 不限流量 | unmetered / unlimited 时 `bandwidth_tb` 为 null，notes 写 unmetered | — |
| 13 | 产品范围（D62） | 只卖邮箱、域名、托管机柜等四类以外产品的帖子标 ignore；与四类产品一起卖时只标四类 | leb-54089、leb-54311、les-11521（改标 ignore）；let-222302、let-222735（去掉 other、mail） |
| 14 | 库存（D63） | 不再标 `stock_status` 与 `stock_evidence`（记录级与套餐级都去掉），评分里也没有库存准确率 | — |
| 15 | 截止日期 `ends_at`（v1.1，5.13） | 活动最后一天；开奖日、返现申请截止、优惠码只在首月有效不算；区间取后一天；抽奖取报名截止日；没写为 null | let-222534（Oct 26–31，不是 11-05 的申请截止）；les-11436（报名截止）；let-222765、les-11544 |

另：规范示例 10.1 是 let-222164 的简化版，示例中的优惠码 BFHD10 不在原帖中，以本样本标注为准。

## 配额执行情况（D37）

只用本季帖（`pubDate ≥ 2026-10-01`）。11-02 快照池：LET 本季 52 条（黑五标题 4 条），LES 本季 43 条（7 个分类 feed 去重，黑五 2 条），LEB 本季 20 条。

| 来源 | 项 | 原配额 | 实际 | 缺口 |
|---|---|---|---|---|
| LET | 黑五帖 | 8 | 4（池内全部） | 4 |
| LET | 非黑五帖 | 7 | 11（补足总数 15） | — |
| LET | 竖线 pipe | 4 | 4（其中 1 条为 `·` 分隔，约定 8） | 0 |
| LET | 表格 table | 3 | 3 | 0 |
| LET | 列表 list | 5 | 6（补足） | — |
| LET | 自由文本 freetext | 3 | 1（池内仅 let-222302 为真正的段落散价） | 2 |
| LET | 无套餐 none | — | 1（黑五抽奖帖，必须收录） | — |
| LES | 黑五帖 | ≥ 2 | 2 | 0 |
| LEB | 优惠 / 资讯 | 3 / 2 | 3 / 2 | 0 |

### v1.1 待补

11-17 起增补 v1.1 时先补以下缺口（新增文件，先作盲测再并入调试集）：

| 来源 | 类别 | 缺口 |
|---|---|---|
| LET | 黑五帖（真实黑五活动帖） | 4 |
| LET | 自由文本形态 | 2 |

## 调试集与留出集（第 2.4 节）

划分方法：按来源分层随机抽取，种子 `bfradar-gold-v1.0`；约束为 LET 留出集恰含 1 条黑五帖且覆盖至少 3 种套餐形态、LES 留出集恰含 1 条黑五帖、let-222765 与其 LES 转发帖 les-11544 落在同一组（内容几乎相同，分开会让留出集失去独立性）。NodeSeek 3 条留出样本在 11-03 标注后补入。

留出集（7 / 10）：let-222302、let-222735、let-222774、let-222809、les-11436、les-11479、leb-54089。注意 LET 唯一的自由文本样本 let-222302 在留出集中，调试集的自由文本只有 leb-54311。

## 样本清单

| id | split | 黑五 | 类型 | 活动 | 形态 | 套餐数 | 商家 |
|---|---|---|---|---|---|---|---|
| leb-54089 | holdout | 否 | offer | launch | list | 3 | Cloudnium |
| leb-54294 | dev | 否 | intel | other | — | 0 | — |
| leb-54301 | dev | 否 | intel | other | — | 0 | MassiveGrid |
| leb-54311 | dev | 否 | offer | holiday | freetext | 2 | Mango Mail |
| leb-54352 | dev | 否 | offer | sale | list | 3 | EnderHost |
| les-11436 | holdout | 是 | offer | black_friday | none | 0 | DartNode |
| les-11479 | holdout | 否 | offer | sale | table | 2 | Swify |
| les-11519 | dev | 否 | offer | sale | list | 6 | SmokyHosts |
| les-11521 | dev | 否 | offer | holiday | list | 3 | DragonWebHost |
| les-11544 | dev | 是 | offer | black_friday | list | 9 | VM6 |
| let-221797 | dev | 否 | offer | restock | table | 19 | DeluxHost |
| let-221991 | dev | 否 | offer | sale | pipe | 13 | KhanWebHost |
| let-222157 | dev | 否 | offer | sale | list | 7 | NolimitHost |
| let-222164 | dev | 是 | offer | black_friday | pipe | 20（截断） | HostDZire |
| let-222165 | dev | 否 | offer | sale | pipe | 13 | QuickServers |
| let-222302 | holdout | 否 | offer | sale | freetext | 15 | IPYE |
| let-222462 | dev | 否 | offer | sale | list | 4 | SystemFreaks |
| let-222534 | dev | 否 | offer | anniversary | list | 1 | GreenCloud |
| let-222539 | dev | 否 | offer | sale | list | 6 | HostNoc |
| let-222735 | holdout | 是 | offer | black_friday | none | 0 | MailBig |
| let-222765 | dev | 是 | offer | black_friday | list | 9 | VM6 |
| let-222774 | holdout | 否 | offer | holiday | table | 9 | TNAHosting |
| let-222809 | holdout | 否 | offer | holiday | list | 3 | HostSailor |
| let-222865 | dev | 是 | offer | black_friday | table | 3 | PureVoltage |
| let-222871 | dev | 否 | offer | sale | pipe | 4 | Godlike.host |

金标套餐合计 154 条。复核时建议优先抽查 5 条黑五帖：let-222164、let-222765、let-222865、les-11544、les-11436。
