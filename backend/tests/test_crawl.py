from app.db import Database
from app.pipeline import crawl
from app.sources import build_sources

from conftest import ROUTES, noop_sleep


def run(db, settings, http, only=None):
    return crawl(db, build_sources(settings, http, only, sleep=noop_sleep), reason="cli")


def test_crawl_all_sources_from_fixtures(tmp_path, settings, http):
    db = Database(tmp_path / "radar.db")
    db.migrate()
    run_id, reports = run(db, settings, http)
    by_source = {r.source: r for r in reports}
    assert all(r.error is None for r in reports)
    assert by_source["lowendtalk"].fetched == 100 and len(by_source["lowendtalk"].new) == 100
    assert by_source["nodeseek"].stats["count"] == 20
    conn = db.conn()
    assert conn.execute("SELECT COUNT(*) FROM raw_posts").fetchone()[0] == sum(len(r.new) for r in by_source.values())
    rows = conn.execute("SELECT source, error, new_items FROM run_sources WHERE run_id = ?", (run_id,)).fetchall()
    assert len(rows) == 5 and all(r["error"] is None for r in rows)
    assert conn.execute("SELECT ok FROM runs WHERE id = ?", (run_id,)).fetchone()[0] == 1


def test_second_round_finds_nothing_new_and_carries_nodeseek_state(tmp_path, settings, http):
    db = Database(tmp_path / "radar.db")
    db.migrate()
    run(db, settings, http)
    _, reports = run(db, settings, http)
    assert all(not r.new and not r.updated for r in reports)
    ns = next(r for r in reports if r.source == "nodeseek")
    assert ns.stats["expected"] == 0 and ns.stats["overlap"] is True  # 上一轮 max_id 已作为 state 传入


def test_one_failing_source_does_not_stop_others(tmp_path, settings, http, server):
    server.overrides["https://lowendtalk.com/categories/offers/feed.rss"] = 503
    db = Database(tmp_path / "radar.db")
    db.migrate()
    run_id, reports = run(db, settings, http, ["lowendtalk", "nodeseek"])
    let, ns = reports
    assert let.error and "503" in let.error and let.fetched == 0
    assert ns.error is None and len(ns.new) == 20
    conn = db.conn()
    assert conn.execute("SELECT ok FROM runs WHERE id = ?", (run_id,)).fetchone()[0] == 0
    err = conn.execute("SELECT error FROM run_sources WHERE source = 'lowendtalk'").fetchone()[0]
    assert "503" in err
    assert server.requests.count("https://lowendtalk.com/categories/offers/feed.rss") == 3  # 退避重试 2 次
    assert set(ROUTES) >= set(server.requests)


def test_crawl_classifies_new_posts(tmp_path, settings, http):
    db = Database(tmp_path / "radar.db")
    db.migrate()
    _, reports = run(db, settings, http)
    by_source = {r.source: r for r in reports}
    assert by_source["lowendtalk"].kinds == {"offer": 100}
    assert sum(by_source["nodeseek"].kinds.values()) == 20
    conn = db.conn()
    assert conn.execute("SELECT COUNT(*) FROM raw_posts WHERE record_kind IS NULL").fetchone()[0] == 0
    news = conn.execute("SELECT record_kind FROM raw_posts WHERE url LIKE '%is-this-the-ai-nuclear-winter%'").fetchone()
    assert news["record_kind"] == "intel"
    _, again = run(db, settings, http)
    assert all(not r.kinds for r in again)  # 未变化的原文不重复分类
