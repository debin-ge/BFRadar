"""合并与变更检测（第 12.6 节，D32、D46）：一个 URL 一条优惠；原帖改动产生变更与标记，抽取方式变化不产生。"""

import json
from dataclasses import fields
from datetime import UTC, datetime

import pytest

from app.db import connect, migrate
from app.extract.classify import classify
from app.extract.llm import LlmState, llm_after, llm_due
from app.extract.merge import TRACK_VERSION, Change, ChangeFlag, diff, flags, price_list, specs_range
from app.extract.merge import build_deal
from app.extract.rules import extract
from app.extract.validate import validate_and_merge
from app.models import Deal, ExtractedBy, LlmStatus, Period, Plan, RawPost
from app.repo import load_deal, recent_flags, save_deal, save_raw_posts
from app.sources.base import feed_posts
from app.textutil import Link
from app.vendors import is_generic, resolve_vendor, slugify, vendor_key
from conftest import snapshot

URL = "https://lowendtalk.com/discussion/990001"
T1, T2, T3, T4, T5 = (f"2026-11-20T1{h}:00:00Z" for h in range(1, 6))
TITLE = "VM6 | Black Friday UK VPS | 50% off recurring"
ROWS = ["VPS-2G | 2 vCPU | 2 GB RAM | 40 GB NVMe | London | £20/year | "
        "<a href='https://www.vm6.co.uk/order?pid=1'>Order</a>",
        "VPS-4G | 4 vCPU | 4 GB RAM | 80 GB NVMe | London | £35/year | "
        "<a href='https://www.vm6.co.uk/order?pid=2'>Order</a>"]
INTRO = "Black Friday at VM6: 50% off recurring with code BF50."
OUTRO = "Thanks for reading. Monthly plans get 30% off."


def body(intro=INTRO, rows=ROWS, outro=OUTRO):
    return f"<p>{intro}</p><p>{'<br>'.join(rows)}</p><p>{outro}</p>"


def llm_plan(name, cores, ram, disk, price, link):
    return {"name": name, "cpu_cores": cores, "ram_gb": ram, "disk_gb": disk, "disk_type": "NVMe",
            "locations": ["London"], "price": price, "currency": "GBP", "period": "year", "order_link_index": link}


# 与规则层只有折扣不同（规则取 50%，LLM 取 30%），两者都有原文依据
LLM = {
    "vendor": "VM6", "is_black_friday": True, "event_type": "black_friday", "product_types": ["vps"],
    "discount": {"pct": 30, "recurring": "recurring", "text": "30% off"},
    "coupons": [{"code": "BF50", "applies_to": None}],
    "plans": [llm_plan("VPS-2G", 2, 2, 40, 20, 1), llm_plan("VPS-4G", 4, 4, 80, 35, 2)],
    "purchase_link_index": 1,
    "summary_zh": "VM6 黑五英国 VPS 年付 £20 起，续费同价", "summary_en": "VM6 Black Friday: UK VPS from £20/year",
    "title_zh": "VM6 | 黑五英国 VPS | 续费同价", "title_en": None, "confidence": 0.9,
}


@pytest.fixture
def conn():
    c = connect(":memory:")
    migrate(c)
    return c


def merge_row(conn, row, *, at, llm=None):
    """流水线对一条优惠原文的处理：分类 → 规则 → LLM（None 未配置；"pending" 已配置未处理；dict 本轮调用的输出）
    → 校验合并 → 组装 → 保存。同一内容已抽取过时用库里的 LLM 输出重新合并。"""
    links = [Link(*lk) for lk in json.loads(row["links_json"])]
    c = classify(row["source"], row["title"], row["text_struct"], json.loads(row["categories_json"]))
    ex = extract(row["source"], row["title"], row["text_struct"], links, row["author"], c.lang, False)
    prev = conn.execute("SELECT llm_status, llm_attempts, llm_hash, llm_output_json FROM deals WHERE url = ?",
                        (row["url"],)).fetchone()
    state = LlmState(LlmStatus(prev["llm_status"]), prev["llm_attempts"], prev["llm_hash"]) if prev else LlmState()
    state = llm_due(state, row["content_hash"], enabled=llm is not None)
    data = None
    if state.status is LlmStatus.DONE:
        data = json.loads(prev["llm_output_json"])
    elif state.status is LlmStatus.PENDING and isinstance(llm, dict):
        data, state = llm, llm_after(state, row["content_hash"])
    merged = None
    if data is not None:
        merged = validate_and_merge(data, title=row["title"], text=row["text_struct"], links=links,
                                    author=row["author"], lang=c.lang, rules=ex, is_black_friday=c.is_black_friday,
                                    event_type=c.event_type, product_types=c.product_types)
    deal = build_deal(row, c, ex, merged, llm=state, llm_output=data)
    return deal, save_deal(conn, deal, now=at)


def process(conn, *, title=TITLE, html=None, at=T1, llm=None):
    post = RawPost("lowendtalk", URL, title, html or body(), author="VM6", categories=["Offers"],
                   published_at=datetime(2026, 11, 20, 8, tzinfo=UTC))
    save_raw_posts(conn, [post], fetched_at=at)
    row = conn.execute("SELECT * FROM raw_posts WHERE url = ?", (URL,)).fetchone()
    return merge_row(conn, row, at=at, llm=llm)


def stored_changes(conn):
    return [Change(r["field"], r["old_value"], r["new_value"])
            for r in conn.execute("SELECT * FROM changes ORDER BY id")]


def assert_same(built: Deal, loaded: Deal) -> None:
    for f in fields(Deal):
        if f.name in ("id", "vendor_id", "updated_at", "plans"):
            continue
        a, b = getattr(built, f.name), getattr(loaded, f.name)
        if f.name == "vendor":  # 泛词商家不入库（D52）
            a = None if not a or is_generic(a) else vendor_key(a)
            b = b and vendor_key(b)
        assert a == b, f"{built.url} {f.name}: {a!r} != {b!r}"
    for p, q in zip(built.plans, loaded.plans, strict=True):
        for f in fields(Plan):
            if f.name not in ("id", "deal_id"):
                assert getattr(p, f.name) == getattr(q, f.name), f"{built.url} 套餐 {p.position} {f.name}"


# ---------------------------------------------------------------- 组装与读写


def test_build_save_load(conn):
    deal, saved = process(conn)
    assert saved.created and saved.updated and saved.changes == []
    d = load_deal(conn, URL)
    assert_same(deal, d)
    assert (d.vendor, d.coupon_codes, d.discount_pct, str(d.recurring)) == ("VM6", ["BF50"], 50, "recurring")
    assert d.is_black_friday is True and str(d.event_type) == "black_friday" and d.sources == ["lowendtalk"]
    assert [(p.name, p.price, p.currency, str(p.period)) for p in d.plans] == [
        ("VPS-2G", 20.0, "GBP", "year"), ("VPS-4G", 35.0, "GBP", "year")]
    assert d.specs_range == {"cpu_cores": [2.0, 4.0], "ram_gb": [2.0, 4.0], "disk_gb": [40.0, 80.0],
                             "locations": ["London"]}
    assert d.price_list == [{"amount": 20.0, "currency": "GBP", "period": "year"},
                            {"amount": 35.0, "currency": "GBP", "period": "year"}]
    assert d.excerpt.startswith("Black Friday at VM6") and len(d.excerpt) <= 300
    assert (d.llm_status, d.extracted_by, d.llm_output, d.summary_zh) == (LlmStatus.SKIPPED, ExtractedBy.RULES,
                                                                          None, None)
    assert (d.first_seen_at, d.last_seen_at, d.updated_at) == (T1, T1, T1)


def test_unchanged_post_only_refreshes_seen_time(conn):
    process(conn, at=T1)
    _, saved = process(conn, at=T2)
    assert not saved.created and not saved.updated and saved.changes == []
    d = load_deal(conn, URL)
    assert (d.last_seen_at, d.updated_at, d.first_seen_at) == (T2, T1, T1)
    assert conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0] == 1


def test_real_feed_roundtrip_and_idempotence(conn):
    posts = feed_posts("lowendtalk", snapshot("lowendtalk_offers_2026-11-02.xml").read_bytes())
    save_raw_posts(conn, posts, fetched_at=T1)
    rows = conn.execute("SELECT * FROM raw_posts").fetchall()
    assert len(rows) == 100
    for row in rows:
        deal, saved = merge_row(conn, row, at=T1)
        assert saved.created
        assert_same(deal, load_deal(conn, row["url"]))
    for row in rows:  # 原文没变：第二轮全部不更新、不产生变更
        _, saved = merge_row(conn, row, at=T2)
        assert not saved.updated and saved.changes == [], row["url"]
    assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 0
    vendors = conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]
    assert 0 < vendors <= 100


# ---------------------------------------------------------------- 变更与标记


def test_title_edit_records_a_change_but_no_flag(conn):
    process(conn, at=T1)
    _, saved = process(conn, title=f"[EXTENDED] {TITLE}", at=T2)
    assert saved.updated and saved.changes == [Change("title", TITLE, f"[EXTENDED] {TITLE}")]
    assert stored_changes(conn) == saved.changes
    assert recent_flags(conn, since=T1) == {}  # 标题变更不生成标记


def test_price_drop_and_new_code(conn):
    process(conn, at=T1)
    html = body(intro=f"{INTRO} Cyber Monday code CM40 also works.",
                rows=[ROWS[0].replace("£20", "£18"), ROWS[1].replace("£35", "£39")])
    _, saved = process(conn, html=html, at=T2)
    old2g = {"name": "VPS-2G", "cpu_cores": 2.0, "ram_gb": 2.0, "disk_gb": 40.0, "currency": "GBP",
             "period": "year", "locations": ["London"]}
    old4g = {**old2g, "name": "VPS-4G", "cpu_cores": 4.0, "ram_gb": 4.0, "disk_gb": 80.0}
    assert [(c.field, c.old and json.loads(c.old), c.new and json.loads(c.new)) for c in saved.changes] == [
        ("coupon_codes", ["BF50"], ["BF50", "CM40"]),
        ("plan_price", {**old2g, "price": 20.0}, {**old2g, "price": 18.0}),
        ("plan_price", {**old4g, "price": 35.0}, {**old4g, "price": 39.0}),
    ]
    assert recent_flags(conn, since=T1) == {saved.deal_id: {ChangeFlag.PRICE_DROP, ChangeFlag.NEW_CODE}}


def test_extraction_changes_do_not_create_changes(conn):
    """LLM 补抽完成、原帖小改而 LLM 未处理（临时用规则结果）都只改展示值，不是商家改了优惠（D46）。"""
    _, saved = process(conn, at=T1, llm="pending")
    assert load_deal(conn, URL).llm_status is LlmStatus.PENDING

    _, saved = process(conn, at=T2, llm=LLM)  # 同一内容，LLM 抽取完成：折扣展示值 50 → 30
    d = load_deal(conn, URL)
    assert saved.updated and saved.changes == []
    assert (d.discount_pct, d.llm_status, d.extracted_by, d.llm_output) == (30, LlmStatus.DONE, ExtractedBy.LLM, LLM)
    assert d.summary_zh and d.title_zh and d.confidence == 0.9

    _, saved = process(conn, at=T3, llm=LLM)  # 原文未变：用库里的 LLM 输出重新合并，结果不变
    assert not saved.updated and saved.changes == []

    typo = body(outro=OUTRO.replace("reading.", "reading!"))
    _, saved = process(conn, html=typo, at=T4, llm="pending")  # 原帖小改：LLM 待处理，展示值暂回规则结果
    d = load_deal(conn, URL)
    assert saved.updated and saved.changes == []
    assert (d.discount_pct, d.llm_status, d.llm_output) == (50, LlmStatus.PENDING, None)

    _, saved = process(conn, html=typo, at=T5, llm=LLM)
    assert saved.changes == [] and load_deal(conn, URL).discount_pct == 30
    assert stored_changes(conn) == []


def test_real_discount_change_is_recorded_with_shown_values(conn):
    process(conn, at=T1, llm=LLM)
    html = body(intro=INTRO.replace("50%", "60%"), outro=OUTRO)
    _, saved = process(conn, title=TITLE.replace("50%", "60%"), html=html, at=T2, llm="pending")
    # 规则层 50 → 60 确认原文变了；记录写展示值（之前页面上是 LLM 的 30）
    assert Change("discount_pct", "30", "60") in saved.changes
    assert recent_flags(conn, since=T1)[saved.deal_id] == {ChangeFlag.PRICE_DROP}


def test_no_stock_column_and_single_source(conn):
    """库存不再收集（D63）；同一原帖只有一条优惠，来源只有它自己。"""
    _, saved = process(conn, at=T1)
    d = load_deal(conn, URL)
    assert d.sources == ["lowendtalk"] and saved.changes == []
    assert not hasattr(d, "stock_status")
    assert conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0] == 1


# ---------------------------------------------------------------- 纯函数


def snap(content="h1", title="t", shown=(50, ["BF50"]), rules=None, plans=()):
    d, c = shown
    rd, rc = rules or shown
    return {"v": TRACK_VERSION, "content_hash": content, "title": title,
            "shown": {"discount_pct": d, "coupon_codes": c},
            "rules": {"discount_pct": rd, "coupon_codes": rc, "plans": list(plans)}}


def test_diff_needs_a_comparable_baseline():
    assert diff(None, snap()) == [] and diff({}, snap()) == []
    assert diff({**snap(), "v": TRACK_VERSION - 1}, snap(content="h2", title="new")) == []
    # 原帖内容没变：展示值变了也不记
    assert diff(snap(), snap(shown=(60, ["X"]), rules=(60, ["X"]))) == []


def test_diff_plan_prices_align_by_key_and_skip_ambiguous():
    a = {"name": "KVM 1", "ram_gb": 1.0, "currency": "USD", "period": "year", "locations": ["LA"]}
    b = {"name": "KVM 2", "ram_gb": 2.0, "currency": "USD", "period": "year"}
    old = snap(plans=[[a, 10.0], [b, 20.0], [b, 25.0]])
    new = snap(content="h2", plans=[[{**a, "name": "kvm 1 ", "locations": ["la"]}, 9.0], [b, 19.0], [b, 26.0]])
    assert [(c.field, json.loads(c.new)["price"]) for c in diff(old, new)] == [("plan_price", 9.0)]


def test_flags_follow_current_state():
    def f(*changes, codes=()):
        return flags([Change(*c) for c in changes], coupon_codes=codes)

    assert f(("discount_pct", None, "30")) == {ChangeFlag.PRICE_DROP}
    assert f(("discount_pct", "50", "40")) == set()
    assert f(("plan_price", '{"price": 5}', '{"price": 6}')) == set()
    assert f(("coupon_codes", '["A"]', '["A", "B"]'), codes=["A", "B"]) == {ChangeFlag.NEW_CODE}
    assert f(("coupon_codes", '["A"]', '["A", "B"]'), codes=["A"]) == set()  # 新码后来又删了
    assert f(("title", "a", "b")) == set()
    assert {x.value for x in ChangeFlag} == {"price_drop", "new_code"}  # 补货、售罄标记随库存一起去掉（D63）


def test_price_list_adds_start_prices_not_in_plans():
    plans = [Plan(price=5.0, currency="USD", period=Period.MONTH), Plan(price=5.0, currency="USD", period=Period.MONTH),
             Plan(price=None)]
    starts = [{"amount": 5.0, "currency": "USD", "period": "month"}, {"amount": 3.0, "currency": "USD", "period": None}]
    assert price_list(plans, starts) == [{"amount": 5.0, "currency": "USD", "period": "month"},
                                         {"amount": 3.0, "currency": "USD", "period": None, "from": True}]


def test_specs_range_ignores_missing_values():
    plans = [Plan(ram_gb=1, disk_gb=None, locations=["LA"]), Plan(ram_gb=4, disk_gb=50, locations=["LA", "NY"])]
    assert specs_range(plans) == {"ram_gb": [1, 4], "disk_gb": [50, 50], "locations": ["LA", "NY"]}
    assert specs_range([]) == {}


# ---------------------------------------------------------------- 商家归并


def test_vendor_resolution(conn):
    a = resolve_vendor(conn, "Godlike.host")
    assert resolve_vendor(conn, "godlike host") == resolve_vendor(conn, "GODLIKE-HOST") == a
    assert conn.execute("SELECT slug, name FROM vendors WHERE id = ?", (a,)).fetchone()[:] == ("godlike-host",
                                                                                              "Godlike.host")
    assert resolve_vendor(conn, "VM6") != resolve_vendor(conn, "VM6 Networks")  # 长短写法由别名表归并（M2.6）
    assert resolve_vendor(conn, None) is None and resolve_vendor(conn, " - ") is None
    assert slugify("白丝云 Cloud") == "白丝云-cloud"

    conn.execute("INSERT INTO vendors (slug, name, aliases_json) VALUES ('racknerd', 'RackNerd', '[\"瑞克\"]')")
    rn = conn.execute("SELECT id FROM vendors WHERE slug = 'racknerd'").fetchone()[0]
    assert resolve_vendor(conn, "瑞克") == resolve_vendor(conn, "Rack Nerd") == rn
    conn.execute("INSERT INTO vendors (slug, name) VALUES ('cc', 'CloudCone')")  # 人工维护的 slug 与名称不同
    cc = resolve_vendor(conn, "CC")
    assert conn.execute("SELECT slug FROM vendors WHERE id = ?", (cc,)).fetchone()[0] == "cc-2"
