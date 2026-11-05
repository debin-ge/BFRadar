-- 009 活动有效期（《优化方案 v1.0》O-15，D70）
-- starts_at、ends_at：原帖写明的活动起止日期（ISO 日期，没写为空）；ends_source 记是规则层还是 LLM 给的。
-- 页面据此显示"截止 12-02（还剩 27 天）"、把已结束的排到最后；已有的优惠由 reprocess 补上。

ALTER TABLE deals ADD COLUMN starts_at TEXT;
ALTER TABLE deals ADD COLUMN ends_at TEXT;
ALTER TABLE deals ADD COLUMN ends_source TEXT CHECK (ends_source IN ('rule', 'llm'));
