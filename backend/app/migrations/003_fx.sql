-- 003 汇率与起价（《需求与方案说明》第 8、11 节，M1.11，D47）
-- 起价由一个显示字符串（只能是一种语言，如 "€18/年"）改为原币种金额、币种、周期三列，页面按语言格式化。
ALTER TABLE deals DROP COLUMN min_price_display;
ALTER TABLE deals ADD COLUMN min_price REAL;
ALTER TABLE deals ADD COLUMN min_price_currency TEXT CHECK (min_price_currency IN
    ('USD', 'EUR', 'GBP', 'CNY', 'HKD', 'CAD', 'AUD', 'SGD', 'JPY'));
ALTER TABLE deals ADD COLUMN min_price_period TEXT CHECK (min_price_period IN
    ('month', 'quarter', 'semi', 'year', 'biennial', 'triennial', 'onetime', 'hour'));

-- 帖子视图"起价最低"排序
CREATE INDEX deals_min_price_cny_year ON deals (min_price_cny_year);
