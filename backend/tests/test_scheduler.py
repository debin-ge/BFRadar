"""调度器（M1.13）：任务与档位、黑五周切换、唤醒 LLM、手动刷新互斥、汇率过期判断、备份与保留。"""

import sqlite3
import threading
import time
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from app import scheduler as sched_mod
from app.config import load_settings
from app.db import Database
from app.http import HttpClient
from app.pipeline import LlmReport
from app.translate import TranslateReport
from app.scheduler import RefreshBusy, Scheduler, backup_db

from conftest import noop_sleep

LLM_ENV = {"LLM_BASE_URL": "https://llm.example.com/v1", "LLM_API_KEY": "k", "LLM_MODEL": "m"}
NORMAL_DAY = datetime(2026, 11, 20, 4, tzinfo=UTC)  # 北京时间 11-20 12:00
BF_EVE = datetime(2026, 11, 23, 15, 59, tzinfo=UTC)  # 北京时间 11-23 23:59


def make(tmp_path, server, *, env=None, clock=lambda: NORMAL_DAY, sources="lowendtalk,nodeseek,lowendbox"):
    settings = load_settings({"SOURCES": sources, **(env or {})}, env_file=None)
    db = Database(tmp_path / "radar.db")
    factory = lambda: HttpClient(settings, transport=httpx.MockTransport(server.handler), sleep=noop_sleep)  # noqa: E731
    return Scheduler(settings, db=db, http_factory=factory, clock=clock)


def intervals(s: Scheduler) -> dict[str, float]:
    return {job.id: job.trigger.interval.total_seconds() / 60 for job in s._sched.get_jobs() if job.id.startswith("source:")}


def test_jobs_and_tiers(tmp_path, server):
    s = make(tmp_path, server)
    s.start(paused=True)
    try:
        assert intervals(s) == {"source:lowendtalk": 60, "source:nodeseek": 5, "source:lowendbox": 60}
        jobs = {job.id: job for job in s._sched.get_jobs()}
        assert set(jobs) == {"source:lowendtalk", "source:nodeseek", "source:lowendbox", "fx", "backup", "tier"}
        assert jobs["source:lowendtalk"].next_run_time == NORMAL_DAY + timedelta(seconds=3)  # 启动后 3 秒先跑一轮
        assert jobs["fx"].next_run_time == NORMAL_DAY + timedelta(seconds=3)  # 库里没有汇率：启动时先拉
        assert jobs["backup"].next_run_time.astimezone(s._tz).hour == 4
        assert s.status()["tier"] == "normal" and s.status()["intervals"]["nodeseek"] == 5
    finally:
        s.shutdown(wait=False)


def test_bf_week_switch_and_llm_job(tmp_path, server):
    now = {"t": BF_EVE}
    s = make(tmp_path, server, env=LLM_ENV, clock=lambda: now["t"])
    s.start(paused=True)
    try:
        assert "llm" in {job.id for job in s._sched.get_jobs()}  # LLM 已配置才有队列任务
        assert s.tier() == "normal"
        now["t"] = BF_EVE + timedelta(minutes=2)  # 北京时间跨入 11-24
        s.retier()
        assert s.tier() == "bf"
        assert intervals(s) == {"source:lowendtalk": 30, "source:nodeseek": 3, "source:lowendbox": 60}
    finally:
        s.shutdown(wait=False)


def test_run_source_wakes_llm_when_deals_change(tmp_path, server):
    s = make(tmp_path, server, env=LLM_ENV)
    s.start(paused=True)
    try:
        before = s._sched.get_job("llm").next_run_time
        report = s.run_source("lowendbox")
        assert report.error is None and report.deals["created"] == 7  # 8 条优惠里有 1 条只卖托管机柜，不收（D62）
        assert s._sched.get_job("llm").next_run_time == NORMAL_DAY < before  # 唤醒 LLM 队列
        assert s.run_source("lowendbox").changed_urls == []  # 第二轮没有变化
    finally:
        s.shutdown(wait=False)


def test_run_llm_drains_the_queue_in_batches(tmp_path, server, monkeypatch):
    sizes = iter([20, 20, 3])
    calls = []

    def fake_queue(db, client, settings, *, limit):
        calls.append(limit)
        return LlmReport(candidates=next(sizes))

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    translated = []
    monkeypatch.setattr(sched_mod, "run_llm_queue", fake_queue)
    monkeypatch.setattr(sched_mod, "run_translate", lambda db, client, settings: translated.append(client) or
                        TranslateReport())
    s = make(tmp_path, server, env=LLM_ENV)
    s._llm_factory = FakeClient
    s.run_llm()
    assert calls == [20, 20, 20] and not s.status()["llm_running"]
    assert len(translated) == 1  # 队列处理完顺带翻译一轮社区标题（O-14）


def test_refresh_runs_all_sources_once_at_a_time(tmp_path, server):
    gate = threading.Event()
    handler = server.handler

    def slow(request):
        gate.wait(5)
        return handler(request)

    s = make(tmp_path, server, sources="nodeseek,lowendbox")
    s._http_factory = lambda: HttpClient(s.settings, transport=httpx.MockTransport(slow), sleep=noop_sleep)
    s.db.migrate()
    run_id = s.refresh()
    with pytest.raises(RefreshBusy):
        s.refresh()
    gate.set()
    deadline = time.monotonic() + 60
    while s.status()["refreshing"] and time.monotonic() < deadline:
        time.sleep(0.05)
    conn = s.db.conn()
    row = conn.execute("SELECT reason, ok, finished_at FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert (row["reason"], row["ok"]) == ("manual", 1) and row["finished_at"]
    sources = {r[0] for r in conn.execute("SELECT source FROM run_sources WHERE run_id = ?", (run_id,))}
    assert sources == {"nodeseek", "lowendbox"}
    assert s.refresh() > run_id  # 上一轮结束后可以再刷新
    while s.status()["refreshing"] and time.monotonic() < deadline:
        time.sleep(0.05)


def test_fx_staleness(tmp_path, server):
    s = make(tmp_path, server)
    s.db.migrate()
    conn = s.db.conn()
    assert s._fx_stale()
    conn.execute("INSERT INTO fx_rates VALUES ('2026-11-19', '{}', 'ecb', '2026-11-19T20:00:00Z')")
    assert not s._fx_stale()  # 8 小时前拉过
    conn.execute("UPDATE fx_rates SET fetched_at = '2026-11-18T20:00:00Z'")
    assert s._fx_stale()


def test_backup_writes_a_copy_and_prunes_old_ones(tmp_path):
    db = Database(tmp_path / "radar.db")
    db.migrate()
    conn = db.conn()
    conn.execute("INSERT INTO vendors (slug, name) VALUES ('vm6', 'VM6')")
    folder = tmp_path / "backups"
    folder.mkdir()
    for day in ("2026-10-01", "2026-11-06", "2026-11-07"):
        (folder / f"radar-{day}.db").write_bytes(b"old")
    (folder / "notes.txt").write_text("keep")
    path = backup_db(conn, folder, date(2026, 11, 20))
    assert path.name == "radar-2026-11-20.db"
    copy = sqlite3.connect(path)
    assert copy.execute("SELECT name FROM vendors").fetchall() == [("VM6",)]
    copy.close()
    assert sorted(p.name for p in folder.iterdir()) == ["notes.txt", "radar-2026-11-07.db", "radar-2026-11-20.db"]
    assert backup_db(conn, folder, date(2026, 11, 20)) == path  # 同一天重跑覆盖


def test_listeners_hear_about_every_data_job(tmp_path, server, monkeypatch, caplog):
    heard = []
    monkeypatch.setattr(sched_mod, "run_llm_queue", lambda db, client, settings, *, limit: LlmReport(calls=2))

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    s = make(tmp_path, server, env=LLM_ENV, sources="nodeseek,lowendbox")
    s._llm_factory = FakeClient
    s.add_listener(lambda: heard.append("cache"))
    s.add_listener(lambda: 1 / 0)  # 一个监听者出错不影响任务与其他监听者
    s.db.migrate()
    s.run_source("lowendbox")
    assert heard == ["cache"] and "通知数据更新失败" in caplog.text
    s.run_llm()  # 一批有调用就通知
    assert heard == ["cache"] * 2
    s.refresh()
    deadline = time.monotonic() + 60
    while s.status()["refreshing"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert heard == ["cache"] * 3  # 手动刷新整轮跑完才通知一次
