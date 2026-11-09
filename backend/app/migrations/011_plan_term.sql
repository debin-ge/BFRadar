-- 011 合约期与续费价（D85）
-- plans.term_months：一次付清的合约月数（主流品牌常见 12–48 个月预付，价格写成折合月价）；原文没写为空。
-- plans.renewal_price：续费价，与 price 同币种、同周期（"Renews at $11.99/mo"）；原文没写为空。
-- 已有的套餐由 reprocess 补上（LET 系帖子很少写，绝大多数仍为空）。

ALTER TABLE plans ADD COLUMN term_months INTEGER CHECK (term_months IS NULL OR term_months BETWEEN 1 AND 120);
ALTER TABLE plans ADD COLUMN renewal_price REAL CHECK (renewal_price IS NULL OR renewal_price >= 0);
