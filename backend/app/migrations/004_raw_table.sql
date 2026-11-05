-- 004 原文记下 HTML 里是否有表格（M1.12，D48）
-- 结构化文本里 HTML 表格与竖线写法同形，规则层判定套餐形态（table / pipe）要看原始 HTML；raw_posts 不存 HTML，
-- 入库时记下这一位。旧记录为 NULL，下次在 feed 中看到时补上。
ALTER TABLE raw_posts ADD COLUMN has_table INTEGER CHECK (has_table IN (0, 1));
