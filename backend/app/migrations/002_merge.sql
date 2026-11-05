-- 002 合并与变更检测（《需求与方案说明》第 12.6 节，M1.10，D46）
-- llm_output_json：LLM 原始输出（校验前的 JSON 对象），llm_status = done 时保存。原帖未变而需要重新合并时
--   （规则或校验改进）用它重算，不必再调用 LLM。
-- tracked_json：变更检测的比对基准，见 app/extract/merge.py 的 tracked()。
ALTER TABLE deals ADD COLUMN llm_output_json TEXT;
ALTER TABLE deals ADD COLUMN tracked_json TEXT NOT NULL DEFAULT '{}';

-- "最近 24 小时变更"标记按时间取全部优惠的变更
CREATE INDEX changes_time ON changes (changed_at);
