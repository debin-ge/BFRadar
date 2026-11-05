-- 001 初始结构（《需求与方案说明》第 11 节，含 D30、D32、D33 补充）
-- 时间一律为 UTC ISO 文本（2026-11-02T15:22:27Z）；*_json 列存 JSON 文本。
-- 枚举 CHECK 与 app/models.py 一致，修改时两处同步。

CREATE TABLE vendors (
    id                 INTEGER PRIMARY KEY,
    slug               TEXT NOT NULL UNIQUE,
    name               TEXT NOT NULL,
    aliases_json       TEXT NOT NULL DEFAULT '[]',
    short_aliases_json TEXT NOT NULL DEFAULT '[]',
    website            TEXT
);

-- 原文持久化（D15）：保存结构化文本与链接表，不存原始 HTML
CREATE TABLE raw_posts (
    url             TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    published_at    TEXT,
    title           TEXT NOT NULL,
    author          TEXT,
    categories_json TEXT NOT NULL DEFAULT '[]',
    text_struct     TEXT NOT NULL,
    links_json      TEXT NOT NULL DEFAULT '[]',
    extra_json      TEXT NOT NULL DEFAULT '{}',
    content_hash    TEXT NOT NULL,
    record_kind     TEXT CHECK (record_kind IN ('offer', 'intel', 'ignore'))
);
CREATE INDEX raw_posts_source_fetched ON raw_posts (source, fetched_at);

CREATE TABLE deals (
    id                 INTEGER PRIMARY KEY,
    url                TEXT NOT NULL UNIQUE,  -- 与 raw_posts.url 对应（不设外键）
    source             TEXT NOT NULL,
    sources_json       TEXT NOT NULL DEFAULT '[]',
    vendor_id          INTEGER REFERENCES vendors (id),
    title              TEXT NOT NULL,
    title_zh           TEXT,
    title_en           TEXT,
    author             TEXT,
    published_at       TEXT,
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    is_black_friday    INTEGER NOT NULL DEFAULT 0 CHECK (is_black_friday IN (0, 1)),
    event_type         TEXT NOT NULL DEFAULT 'other' CHECK (event_type IN
                           ('black_friday', 'cyber_monday', 'holiday', 'anniversary', 'restock', 'launch',
                            'giveaway', 'sale', 'other')),
    product_types_json TEXT NOT NULL DEFAULT '[]',
    discount_pct       INTEGER CHECK (discount_pct BETWEEN 1 AND 100),
    discount_text      TEXT,
    recurring          TEXT NOT NULL DEFAULT 'unknown' CHECK (recurring IN ('recurring', 'first_term', 'mixed', 'unknown')),
    coupon_codes_json  TEXT NOT NULL DEFAULT '[]',
    specs_range_json   TEXT NOT NULL DEFAULT '{}',
    price_list_json    TEXT NOT NULL DEFAULT '[]',
    min_price_cny_year REAL,
    min_price_display  TEXT,
    stock_status       TEXT NOT NULL DEFAULT 'UNVERIFIED' CHECK (stock_status IN
                           ('ON_SALE', 'LIMITED', 'SOLD_OUT', 'RESTOCKING', 'ENDED', 'UNVERIFIED')),
    stock_evidence     TEXT,
    stock_source       TEXT NOT NULL DEFAULT 'post' CHECK (stock_source = 'post'),
    stock_seen_at      TEXT,
    purchase_url       TEXT,
    plan_format        TEXT CHECK (plan_format IN ('pipe', 'table', 'list', 'freetext', 'none')),
    summary_zh         TEXT,
    summary_en         TEXT,
    excerpt            TEXT,
    lang               TEXT CHECK (lang IN ('zh', 'en')),
    extracted_by       TEXT NOT NULL DEFAULT 'rules' CHECK (extracted_by IN ('rules', 'llm')),
    completeness       REAL NOT NULL DEFAULT 0,
    confidence         REAL,
    llm_status         TEXT NOT NULL DEFAULT 'pending' CHECK (llm_status IN ('pending', 'done', 'failed', 'skipped')),
    llm_attempts       INTEGER NOT NULL DEFAULT 0,
    llm_hash           TEXT,
    updated_at         TEXT NOT NULL
);
CREATE INDEX deals_published ON deals (published_at);
CREATE INDEX deals_bf_published ON deals (is_black_friday, published_at);
CREATE INDEX deals_vendor ON deals (vendor_id);
CREATE INDEX deals_llm_status ON deals (llm_status);

-- 一行 = 一个配置 × 一个付费周期
CREATE TABLE plans (
    id             INTEGER PRIMARY KEY,
    deal_id        INTEGER NOT NULL REFERENCES deals (id) ON DELETE CASCADE,
    position       INTEGER NOT NULL,
    name           TEXT,
    cpu_cores      REAL,
    cpu_desc       TEXT,
    ram_gb         REAL,
    disk_gb        REAL,
    disk_type      TEXT CHECK (disk_type IN ('NVMe', 'SSD', 'HDD')),
    bandwidth_tb   REAL,
    port_gbps      REAL,
    ipv4           INTEGER,
    locations_json TEXT NOT NULL DEFAULT '[]',
    price          REAL,
    currency       TEXT CHECK (currency IN ('USD', 'EUR', 'GBP', 'CNY', 'HKD', 'CAD', 'AUD', 'SGD', 'JPY')),
    period         TEXT CHECK (period IN ('month', 'quarter', 'semi', 'year', 'biennial', 'triennial', 'onetime', 'hour')),
    price_note     TEXT CHECK (price_note IN ('promo', 'regular')),
    price_cny_year REAL,
    order_url      TEXT,
    coupon_code    TEXT,
    stock_status   TEXT NOT NULL DEFAULT 'UNVERIFIED' CHECK (stock_status IN
                       ('ON_SALE', 'LIMITED', 'SOLD_OUT', 'RESTOCKING', 'ENDED', 'UNVERIFIED')),
    notes          TEXT,
    confidence     REAL,
    extracted_by   TEXT NOT NULL DEFAULT 'rules' CHECK (extracted_by IN ('rules', 'llm')),
    UNIQUE (deal_id, position)
);
CREATE INDEX plans_price_cny_year ON plans (price_cny_year);

CREATE TABLE changes (
    id         INTEGER PRIMARY KEY,
    deal_id    INTEGER NOT NULL REFERENCES deals (id) ON DELETE CASCADE,
    changed_at TEXT NOT NULL,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT
);
CREATE INDEX changes_deal_time ON changes (deal_id, changed_at);

CREATE TABLE intel (
    id              INTEGER PRIMARY KEY,
    url             TEXT NOT NULL UNIQUE,
    source          TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'other' CHECK (category IN
                        ('resale', 'carpool', 'discussion', 'question', 'review', 'news', 'other')),
    title           TEXT NOT NULL,
    lang            TEXT CHECK (lang IN ('zh', 'en')),
    published_at    TEXT,
    first_seen_at   TEXT NOT NULL,
    is_black_friday INTEGER NOT NULL DEFAULT 0 CHECK (is_black_friday IN (0, 1))
);
CREATE INDEX intel_published ON intel (published_at);

-- 一条情报可提及多个商家（D33）
CREATE TABLE intel_vendors (
    intel_id      INTEGER NOT NULL REFERENCES intel (id) ON DELETE CASCADE,
    vendor_id     INTEGER NOT NULL REFERENCES vendors (id) ON DELETE CASCADE,
    matched_alias TEXT,
    PRIMARY KEY (intel_id, vendor_id)
);
CREATE INDEX intel_vendors_vendor ON intel_vendors (vendor_id);

CREATE TABLE fx_rates (
    date       TEXT PRIMARY KEY,
    rates_json TEXT NOT NULL,  -- {币种: 1 单位折合人民币}
    provider   TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

-- 一次触发（启动、定时、手动刷新）为一个 run；定时任务按源独立，一个 run 通常只含一个源
CREATE TABLE runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER CHECK (ok IN (0, 1)),
    reason      TEXT NOT NULL  -- startup / scheduled / manual / cli
);

CREATE TABLE run_sources (
    run_id       INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    source       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    fetched      INTEGER NOT NULL DEFAULT 0,
    new_items    INTEGER NOT NULL DEFAULT 0,
    updated      INTEGER NOT NULL DEFAULT 0,
    llm_calls    INTEGER NOT NULL DEFAULT 0,
    llm_rejected INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    extra_json   TEXT NOT NULL DEFAULT '{}',  -- 如 NodeSeek 本轮 ID 区间与缺口数
    PRIMARY KEY (run_id, source)
);
CREATE INDEX run_sources_source_time ON run_sources (source, started_at);
