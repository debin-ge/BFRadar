import json
from datetime import UTC, datetime

import pytest

from app.db import connect, migrate
from app.models import RawPost
from app.repo import finish_run, last_source_state, record_source, save_raw_posts, start_run

URL = "https://lowendtalk.com/discussion/222765"


@pytest.fixture
def conn():
    c = connect(":memory:")
    migrate(c)
    return c


def let_post(body="<p>£27 a year with BFVPS55</p>", title="BLACK FRIDAY | UK VPS"):
    return RawPost("lowendtalk", URL, title, body, author="VM6", published_at=datetime(2026, 10, 30, 18, 17, tzinfo=UTC),
                   categories=["Offers"])


def row(conn):
    return conn.execute("SELECT * FROM raw_posts WHERE url = ?", (URL,)).fetchone()


def test_new_unchanged_updated_cycle(conn):
    s = save_raw_posts(conn, [let_post()], fetched_at="2026-11-02T10:00:00Z")
    assert s.new == [URL]
    r = row(conn)
    assert r["text_struct"] == "£27 a year with BFVPS55"
    assert r["published_at"] == "2026-10-30T18:17:00Z"
    assert json.loads(r["categories_json"]) == ["Offers"]
    conn.execute("UPDATE raw_posts SET record_kind = 'offer'")

    s = save_raw_posts(conn, [let_post("<p>£27  a year\nwith BFVPS55</p>")],
                       fetched_at="2026-11-02T11:00:00Z")
    assert s.unchanged == [URL]  # 只有空白差异
    assert row(conn)["fetched_at"] == "2026-11-02T11:00:00Z"
    assert row(conn)["record_kind"] == "offer"

    s = save_raw_posts(conn, [let_post(title="[SOLD OUT] BLACK FRIDAY | UK VPS")],
                       fetched_at="2026-11-02T12:00:00Z")
    assert s.updated == [URL]
    assert row(conn)["title"].startswith("[SOLD OUT]")
    assert row(conn)["record_kind"] is None  # 待重新分类


def test_links_are_stored(conn):
    save_raw_posts(conn, [let_post('<a href="https://www.vm6.co.uk/promotions">deals</a>')],
                   fetched_at="t")
    assert json.loads(row(conn)["links_json"]) == [[1, "deals", "https://www.vm6.co.uk/promotions"]]


def test_last_source_state_skips_failed_runs(conn):
    assert last_source_state(conn, "nodeseek") == {}
    r1 = start_run(conn, "cli")
    record_source(conn, r1, "nodeseek", started_at="2026-11-02T10:00:00Z", fetched=20, new_items=20,
                  extra={"max_id": 1027376})
    finish_run(conn, r1, ok=True)
    r2 = start_run(conn, "scheduled")
    record_source(conn, r2, "nodeseek", started_at="2026-11-02T10:05:00Z", error="FetchError: boom")
    finish_run(conn, r2, ok=False)
    assert last_source_state(conn, "nodeseek") == {"max_id": 1027376}
    runs = conn.execute("SELECT id, ok, reason, finished_at FROM runs ORDER BY id").fetchall()
    assert [(r["ok"], r["reason"]) for r in runs] == [(1, "cli"), (0, "scheduled")]
    assert all(r["finished_at"] for r in runs)
