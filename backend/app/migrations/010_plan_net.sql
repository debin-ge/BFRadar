-- 010 线路与网络标签（《优化方案 v1.0》O-16，D71）
-- plans.net_json：标签键的 JSON 数组（cn2gia、cn2gt、9929、4837、cmin2、cmi、bgp、native_ip、3net）；已有的套餐由 reprocess 补上。

ALTER TABLE plans ADD COLUMN net_json TEXT NOT NULL DEFAULT '[]';
