import re
from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest

from app import models as m
from app.db import connect, migrate


@pytest.fixture(scope="module")
def schema():
    conn = connect(":memory:")
    migrate(conn)
    return conn


def columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def check_values(conn, table, column):
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
    match = re.search(rf"CHECK \({column} IN\s*\(([^)]*)\)\)", sql)
    assert match, f"{table}.{column} 没有 CHECK 约束"
    return set(re.findall(r"'([^']*)'", match.group(1)))


@pytest.mark.parametrize("table, column, allowed", [
    ("raw_posts", "record_kind", m.RecordKind),
    ("deals", "event_type", m.EventType),
    ("deals", "recurring", m.Recurring),
    ("deals", "plan_format", m.PlanFormat),
    ("deals", "lang", m.LANGS),
    ("deals", "extracted_by", m.ExtractedBy),
    ("deals", "llm_status", m.LlmStatus),
    ("deals", "min_price_currency", m.CURRENCIES),
    ("deals", "min_price_period", m.Period),
    ("plans", "disk_type", m.DiskType),
    ("plans", "currency", m.CURRENCIES),
    ("plans", "period", m.Period),
    ("plans", "price_note", m.PriceNote),
    ("intel", "category", m.IntelCategory),
])
def test_enums_match_check_constraints(schema, table, column, allowed):
    assert check_values(schema, table, column) == {str(v) for v in allowed}


@pytest.mark.parametrize("model, table, not_stored", [
    (m.Deal, "deals", {"plans", "vendor"}),
    (m.Plan, "plans", set()),
    (m.Intel, "intel", {"vendor_ids"}),
    (m.Vendor, "vendors", set()),
])
def test_model_fields_match_columns(schema, model, table, not_stored):
    """列表 / 字典字段对应 <字段>_json 列；两边不能有多余项。"""
    cols = columns(schema, table)
    mapped = set()
    for f in fields(model):
        if f.name in not_stored:
            continue
        col = f.name if f.name in cols else f"{f.name}_json"
        assert col in cols, f"{model.__name__}.{f.name} 没有对应的列"
        mapped.add(col)
    assert cols - mapped == set(), f"{table} 有模型中没有的列"


def test_event_type_order_is_priority():
    assert list(m.EventType)[:3] == [m.EventType.BLACK_FRIDAY, m.EventType.CYBER_MONDAY, m.EventType.HOLIDAY]
    assert list(m.EventType)[-1] == m.EventType.OTHER


def test_iso_normalizes_to_utc_seconds():
    cst = timezone(timedelta(hours=8))
    assert m.iso(datetime(2026, 11, 3, 8, 0, 5, 999, tzinfo=cst)) == "2026-11-03T00:00:05Z"
    assert m.iso(None) is None
    with pytest.raises(ValueError):
        m.iso(datetime(2026, 11, 3))


def test_mutable_defaults_are_independent():
    a, b = m.Plan(), m.Plan()
    a.locations.append("Norwich")
    assert b.locations == []


def test_only_four_product_types_are_kept():
    """D62：只收 VPS、独服、虚拟主机、存储；库存字段已去掉（D63）。"""
    assert [str(p) for p in m.ProductType] == ["vps", "dedicated", "hosting", "storage"]
    assert not any("stock" in f.name for f in fields(m.Deal)) and not any("stock" in f.name for f in fields(m.Plan))
