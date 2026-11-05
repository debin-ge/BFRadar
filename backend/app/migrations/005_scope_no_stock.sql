-- 005 收录范围收窄与去掉库存（《需求与方案说明》D62、D63）
-- D62：只收 VPS、独服、虚拟主机、存储四类产品。只卖其他产品（邮箱、域名、托管机柜、IP 租赁、授权等）的优惠整条删掉，
--   套餐与变更随外键级联删除，对应原文标为 ignore（之后重新抓到也不会再建优惠）；其余优惠的产品类型去掉这四类以外的取值。
--   product_types_json 为空的不动（旧数据里不会有，留给下次重算）。
-- D63：不再收集库存。deals 的 stock_* 四列、plans 的 stock_status 列删掉，变更记录里的库存变更一并删掉。
--   其他变更记录不动；tracked_json 也不动：比对基准的版本号已升到 2，旧基准整体不参与下一次比较。

UPDATE raw_posts SET record_kind = 'ignore' WHERE url IN (
    SELECT d.url FROM deals d
    WHERE json_array_length(d.product_types_json) > 0
      AND NOT EXISTS (SELECT 1 FROM json_each(d.product_types_json) j
                      WHERE j.value IN ('vps', 'dedicated', 'hosting', 'storage'))
);

DELETE FROM deals
WHERE json_array_length(product_types_json) > 0
  AND NOT EXISTS (SELECT 1 FROM json_each(deals.product_types_json) j
                  WHERE j.value IN ('vps', 'dedicated', 'hosting', 'storage'));

UPDATE deals SET product_types_json = (
    SELECT json_group_array(value) FROM (
        SELECT j.value AS value FROM json_each(deals.product_types_json) j
        WHERE j.value IN ('vps', 'dedicated', 'hosting', 'storage') ORDER BY j.key
    )
)
WHERE EXISTS (SELECT 1 FROM json_each(deals.product_types_json) j
              WHERE j.value NOT IN ('vps', 'dedicated', 'hosting', 'storage'));

DELETE FROM changes WHERE field = 'stock_status';

ALTER TABLE deals DROP COLUMN stock_status;
ALTER TABLE deals DROP COLUMN stock_evidence;
ALTER TABLE deals DROP COLUMN stock_source;
ALTER TABLE deals DROP COLUMN stock_seen_at;
ALTER TABLE plans DROP COLUMN stock_status;
