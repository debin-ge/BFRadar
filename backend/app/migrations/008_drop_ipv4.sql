-- 008 不再记录 IPv4（D69）
-- 商家默认都给 IPv4，“无独立 IPv4”的区分与显示一并去掉。NAT 仍按套餐名与备注识别，不依赖这一列。
-- 这一列是抽取出来的，不是原始数据；库里的旧值直接丢掉。

ALTER TABLE plans DROP COLUMN ipv4;
