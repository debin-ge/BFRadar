"""流水线（M1.12，D48）：采集 → 分类 → 规则 → 合并 → 写库；单条出错隔离；LLM 队列。"""

import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import load_settings
from app.db import Database
from app.extract.llm import MAX_POST_ATTEMPTS, LlmClient
from app.models import ExtractedBy, LlmStatus, PlanFormat, RawPost
from app import pipeline
from app.pipeline import LLM_SOURCE, crawl, llm_candidates, reprocess, run_llm_queue
from app.repo import last_source_state, load_deal
from app.sources import build_sources
from app.sources.base import FetchResult

from conftest import noop_sleep

LLM_ENV = {"LLM_BASE_URL": "https://llm.example.com/v1", "LLM_API_KEY": "k", "LLM_MODEL": "m"}
T0 = datetime(2026, 11, 20, 8, tzinfo=UTC)
TITLE = "VM6 | Black Friday UK VPS | 50% off recurring"
ROWS = ["VPS-2G | 2 vCPU | 2 GB RAM | 40 GB NVMe | London | £20/year | "
        "<a href='https://www.vm6.co.uk/order?pid=1'>Order</a>",
        "VPS-4G | 4 vCPU | 4 GB RAM | 80 GB NVMe | London | £35/year | "
        "<a href='https://www.vm6.co.uk/order?pid=2'>Order</a>"]
OUTRO = "Monthly plans get 30% off."


def body(rows=ROWS, outro=OUTRO, table=False):
    if table:
        cells = "".join(f"<tr>{''.join(f'<td>{c}</td>' for c in r.split(' | '))}</tr>" for r in rows)
        listing = f"<table>{cells}</table>"
    else:
        listing = f"<p>{'<br>'.join(rows)}</p>"
    return f"<p>Black Friday at VM6: 50% off recurring with code BF50.</p>{listing}<p>{outro}</p>"


def url(n):
    return f"https://lowendtalk.com/discussion/{990000 + n}"


def let(n, title=TITLE, html=None, hours=0):
    return RawPost("lowendtalk", url(n), title, html or body(), author="VM6", categories=["Offers"],
                   published_at=T0 + timedelta(hours=hours))


class FakeSource:
    """按轮次返回给定的原文；id 与 settings 的用法同真实插件。"""

    def __init__(self, settings, source_id, *rounds):
        self.settings, self.id, self.rounds, self.states = settings, source_id, list(rounds), []

    def fetch(self, state=None):
        self.states.append(state)
        posts = self.rounds.pop(0)
        return FetchResult(posts, {"count": len(posts)})


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "radar.db")
    d.migrate()
    return d


def deal_count(conn):
    return conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]


# ---------------------------------------------------------------- 真实快照全链路


def test_fixture_crawl_builds_every_record(db, settings, http):
    _, reports = crawl(db, build_sources(settings, http, sleep=noop_sleep), reason="test")
    conn = db.conn()
    by = {r.source: r for r in reports}
    assert all(r.error is None and not r.post_errors for r in reports)
    missing = conn.execute("SELECT COUNT(*) FROM raw_posts r LEFT JOIN deals d ON d.url = r.url"
                           " WHERE r.record_kind = 'offer' AND d.id IS NULL").fetchone()[0]
    missing_intel = conn.execute("SELECT COUNT(*) FROM raw_posts r LEFT JOIN intel i ON i.url = r.url"
                                 " WHERE r.record_kind = 'intel' AND i.id IS NULL").fetchone()[0]
    assert (missing, missing_intel) == (0, 0)
    offers = conn.execute("SELECT COUNT(*) FROM raw_posts WHERE record_kind = 'offer'").fetchone()[0]
    assert deal_count(conn) == offers == sum(r.deals["created"] for r in reports)
    assert by["lowendtalk"].deals["created"] == 100 and by["nodeseek"].intel + by["reddit"].intel > 0
    assert conn.execute("SELECT COUNT(*) FROM deals WHERE llm_status != 'skipped'").fetchone()[0] == 0  # LLM 未配置
    assert conn.execute("SELECT COUNT(*) FROM deals WHERE plan_format = 'table'").fetchone()[0] > 0  # 原文记下了 <td>
    assert conn.execute("SELECT COUNT(*) FROM deals WHERE min_price_cny_year IS NOT NULL").fetchone()[0] > 0  # 内置汇率
    assert len(by["lowendtalk"].changed_urls) == 100

    _, again = crawl(db, build_sources(settings, http, sleep=noop_sleep), reason="test")
    assert all(not r.deals and not r.intel and not r.changed_urls and not r.kinds for r in again)
    second = conn.execute("SELECT started_at FROM run_sources WHERE source = 'lowendtalk' ORDER BY started_at DESC"
                          " LIMIT 1").fetchone()[0]
    assert {r[0] for r in conn.execute("SELECT DISTINCT last_seen_at FROM deals WHERE source = 'lowendtalk'")} == {second}


# ---------------------------------------------------------------- 采集轮的处理


def test_edit_records_changes_and_seen_posts_are_touched(db, settings, monkeypatch):
    rounds = iter([datetime(2026, 11, 20, 10, tzinfo=UTC), datetime(2026, 11, 20, 11, tzinfo=UTC)])
    monkeypatch.setattr(pipeline, "utcnow", lambda: next(rounds))  # 每轮开始时间
    src = FakeSource(settings, "lowendtalk", [let(1), let(2)], [let(1, title=f"[EXTENDED] {TITLE}"), let(2)])
    crawl(db, [src], reason="test")
    _, [r] = crawl(db, [src], reason="test")
    assert (dict(r.deals), r.changed_urls, dict(r.kinds)) == ({"updated": 1, "changes": 1}, [url(1)], {"offer": 1})
    assert all(type(v) is int for v in r.deals.values())  # 写进运行记录的 JSON 是数字不是 true
    conn = db.conn()
    fields = [row["field"] for row in conn.execute("SELECT field FROM changes ORDER BY id")]
    assert fields == ["title"]
    d2 = load_deal(conn, url(2))  # 原文未变：只刷新"最后看到"
    assert (d2.first_seen_at, d2.updated_at, d2.last_seen_at) == (
        "2026-11-20T10:00:00Z", "2026-11-20T10:00:00Z", "2026-11-20T11:00:00Z")
    assert src.states[1]["count"] == 2 and "pipeline" in src.states[1]  # 下一轮拿到上一轮的 stats


def test_kind_change_drops_the_old_record(db, settings):
    offer = RawPost("lowendbox", "https://lowendbox.com/blog/x/", "HostX KVM deal", "<p>2 GB KVM $20/year, 30% off</p>",
                    published_at=T0)
    news = RawPost("lowendbox", offer.url, "HostX KVM deal", "<p>We review the HostX network.</p>", published_at=T0)
    src = FakeSource(settings, "lowendbox", [offer], [news])
    crawl(db, [src], reason="test")
    conn = db.conn()
    assert deal_count(conn) == 1
    _, [r] = crawl(db, [src], reason="test")
    assert (deal_count(conn), r.intel, dict(r.kinds)) == (0, 1, {"intel": 1})
    assert conn.execute("SELECT category FROM intel").fetchone()[0] == "news"  # LEB 的情报都是资讯


def test_one_bad_post_does_not_stop_the_round(db, settings, monkeypatch):
    original = pipeline.process_post

    def flaky(conn, row, settings, now, index=None):
        if row["url"] == url(1):
            raise ValueError("boom")
        return original(conn, row, settings, now, index)

    monkeypatch.setattr(pipeline, "process_post", flaky)
    src = FakeSource(settings, "lowendtalk", [let(1), let(2)], [let(1), let(2)])
    run_id, [r] = crawl(db, [src], reason="test")
    conn = db.conn()
    assert r.error is None and len(r.post_errors) == 1 and "boom" in r.post_errors[0]
    assert deal_count(conn) == 1 and conn.execute("SELECT ok FROM runs WHERE id = ?", (run_id,)).fetchone()[0] == 1
    assert conn.execute("SELECT record_kind FROM raw_posts WHERE url = ?", (url(1),)).fetchone()[0] is None  # 已回滚

    monkeypatch.setattr(pipeline, "process_post", original)
    _, [r] = crawl(db, [src], reason="test")  # 原文没变，但缺记录：下一轮补上
    assert r.deals["created"] == 1 and deal_count(conn) == 2


def test_table_flag_drives_plan_format(db, settings):
    src = FakeSource(settings, "lowendtalk", [let(1, html=body(table=True)), let(2)])
    crawl(db, [src], reason="test")
    conn = db.conn()
    assert conn.execute("SELECT has_table FROM raw_posts WHERE url = ?", (url(1),)).fetchone()[0] == 1
    assert load_deal(conn, url(1)).plan_format is PlanFormat.TABLE
    assert load_deal(conn, url(2)).plan_format is PlanFormat.PIPE


def test_reprocess_rebuilds_from_raw_posts(db, settings):
    crawl(db, [FakeSource(settings, "lowendtalk", [let(1), let(2)])], reason="test")
    conn = db.conn()
    conn.execute("DELETE FROM deals WHERE url = ?", (url(2),))
    r = reprocess(db, settings, ["lowendtalk"])
    assert (r.fetched, r.deals["created"], r.deals["updated"], r.deals["changes"]) == (2, 1, 0, 0)


# ---------------------------------------------------------------- LLM 队列


def llm_reply(hour: int) -> dict:
    """按发布时间区分帖子：10 点的给出干净结果，9 点的回 400，8 点的编造一个码。"""
    data = {
        "vendor": "VM6", "is_black_friday": True, "event_type": "black_friday", "product_types": ["vps"],
        "discount": {"pct": 30, "recurring": "recurring", "text": "30% off"},
        "coupons": [{"code": "BF50", "applies_to": None}], "plans": [],
        "purchase_link_index": 1,
        "summary_zh": "VM6 黑五英国 VPS 年付 £20 起", "summary_en": "VM6 Black Friday: UK VPS from £20/year",
        "title_zh": "VM6 | 黑五英国 VPS", "title_en": None, "confidence": 0.9,
    }
    if hour == 8:
        data["coupons"].append({"code": "FAKE99", "applies_to": None})
    return data


def llm_server(calls: list):
    def handler(request):
        content = json.loads(request.content)["messages"][1]["content"]
        hour = int(re.search(r"发布时间：2026-11-20T(\d\d)", content).group(1))
        calls.append(hour)
        if hour == 9:
            return httpx.Response(400, json={"error": "content rejected"})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(llm_reply(hour))}}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})
    return handler


def test_llm_queue_merges_results_and_retries_failures(db):
    settings = load_settings(LLM_ENV, env_file=None)
    src = FakeSource(settings, "lowendtalk", [let(1, hours=2), let(2, hours=1), let(3, hours=0)])
    crawl(db, [src], reason="test")
    conn = db.conn()
    assert llm_candidates(conn) == [url(1), url(2), url(3)]  # 最新发布的优先
    assert llm_candidates(conn, limit=1) == [url(1)]

    calls: list = []
    with LlmClient(settings, transport=httpx.MockTransport(llm_server(calls)), sleep=noop_sleep) as client:
        r = run_llm_queue(db, client, settings)
        assert (r.candidates, r.calls, r.done, r.failed, r.stale) == (3, 3, 2, 1, 0)
        assert r.rejected == 1 and r.units > 0 and r.tokens["total_tokens"] == 300
        d1, d2, d3 = (load_deal(conn, url(n)) for n in (1, 2, 3))
        assert (d1.llm_status, d1.extracted_by, d1.discount_pct, d1.summary_zh) == (
            LlmStatus.DONE, ExtractedBy.LLM, 30, "VM6 黑五英国 VPS 年付 £20 起")
        assert d1.llm_output["discount"]["pct"] == 30
        assert (d2.llm_status, d2.llm_attempts, d2.discount_pct) == (LlmStatus.FAILED, 1, 50)  # 规则结果照常
        assert d3.coupon_codes == ["BF50"]  # 编造的码被拒

        for attempt in range(2, MAX_POST_ATTEMPTS + 1):  # 400 算帖子本身的失败：每轮重试直到满次数
            assert llm_candidates(conn) == [url(2)]
            run_llm_queue(db, client, settings)
            assert load_deal(conn, url(2)).llm_attempts == attempt
        assert llm_candidates(conn) == [] and llm_candidates(conn, backfill=True) == [url(2)]
        assert run_llm_queue(db, client, settings, backfill=True).calls == 1

    crawl(db, [FakeSource(settings, "lowendtalk", [let(1, hours=2)])], reason="test")
    assert load_deal(conn, url(1)).summary_zh  # 原文未变的再次采集沿用存下的 LLM 输出
    runs = conn.execute("SELECT r.reason, s.llm_calls FROM runs r JOIN run_sources s ON s.run_id = r.id"
                        " WHERE s.source = ? ORDER BY r.id", (LLM_SOURCE,)).fetchall()
    assert [tuple(x) for x in runs][:2] == [("llm", 3), ("llm", 1)] and runs[-1]["reason"] == "llm-backfill"
    assert last_source_state(conn, "lowendtalk")["count"] == 1  # LLM 运行不占用采集源的 state


def test_llm_result_is_dropped_when_the_post_changed(db):
    settings = load_settings(LLM_ENV, env_file=None)
    crawl(db, [FakeSource(settings, "lowendtalk", [let(1, hours=2)])], reason="test")
    conn = db.conn()
    row = dict(conn.execute("SELECT * FROM raw_posts").fetchone())
    stale = {**row, "content_hash": "edited-meanwhile"}
    report = pipeline.LlmReport()
    pipeline._save_llm_result(conn, stale, pipeline.llm_after(pipeline.LlmState(), "edited-meanwhile"),
                              llm_reply(10), settings, report)
    assert report.stale == 1 and load_deal(conn, url(1)).llm_status is LlmStatus.PENDING


def test_out_of_scope_verdict_survives_reprocess_until_the_post_changes(db):
    """LLM 判定帖子卖的不是四类产品（D62）后，优惠删掉；reprocess 与再次采集都不按规则把它收回来，
    除非原帖内容改了（E18，D67）。"""
    settings = load_settings(LLM_ENV, env_file=None)
    crawl(db, [FakeSource(settings, "lowendtalk", [let(1, hours=2)])], reason="test")
    conn = db.conn()
    row = dict(conn.execute("SELECT * FROM raw_posts").fetchone())
    report = pipeline.LlmReport()
    pipeline._save_llm_result(conn, row, pipeline.llm_after(pipeline.LlmState(), row["content_hash"]),
                              {**llm_reply(10), "product_types": ["mail"]}, settings, report)
    assert deal_count(conn) == 0
    raw = conn.execute("SELECT record_kind, out_of_scope_hash, content_hash FROM raw_posts").fetchone()
    assert raw["record_kind"] == "ignore" and raw["out_of_scope_hash"] == raw["content_hash"]

    r = reprocess(db, settings, ["lowendtalk"])
    assert deal_count(conn) == 0 and r.deals["created"] == 0
    crawl(db, [FakeSource(settings, "lowendtalk", [let(1, hours=2)])], reason="test")  # 原文没变
    assert deal_count(conn) == 0
    edited = let(1, hours=2, html=body(outro="Now also with 64 GB plans."))
    crawl(db, [FakeSource(settings, "lowendtalk", [edited])], reason="test")  # 原文改了：重新按规则收，等 LLM 再判
    assert deal_count(conn) == 1 and load_deal(conn, url(1)).llm_status is LlmStatus.PENDING
