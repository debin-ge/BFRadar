-- 006 记住"不在收录范围"的判定（《优化方案 v1.0》E18，D67）
-- LLM 判定帖子卖的只是四类以外的产品（D62）时，优惠被删、原文标 ignore，但规则层证据不足时宁可多收：原帖内容没变，
-- reprocess 或之后的轮次按规则又会把它建成优惠。out_of_scope_hash 记下判定时的内容哈希，内容没变就沿用这个判定。
-- 已有数据：三个论坛源（LET、LES、LEB）里标为 ignore 的原文，几乎都是 LLM 或迁移 005 判出的不收（本机库实测
-- 75 条里 67 条按规则会被判回优惠），一并补上；NodeSeek、Reddit 的 ignore 是规则判的，不补，规则改进后照常能收回来。

ALTER TABLE raw_posts ADD COLUMN out_of_scope_hash TEXT;

UPDATE raw_posts SET out_of_scope_hash = content_hash
WHERE record_kind = 'ignore' AND source IN ('lowendtalk', 'lowendspirit', 'lowendbox');
