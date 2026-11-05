"""社区讨论标题的机器翻译（优化方案 O-14，D67）：只翻本季黑五情报；整批对齐，对不上整批作废并记失败；
服务不可用时不记失败；标题改了译名作废；页面按语言取译名。"""

import json

import httpx
import pytest

from app.config import load_settings
from app.db import Database
from app.extract.llm import LlmClient
from app.models import Intel
from app.repo import save_intel
from app.translate import BATCH, MAX_FAILED, candidates, check, run_translate

from conftest import noop_sleep

LLM_ENV = {"LLM_BASE_URL": "https://llm.example.com/v1", "LLM_API_KEY": "k", "LLM_MODEL": "m"}
SEASON = "2026-10-01T00:00:00Z"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "radar.db")
    d.migrate()
    return d


def add(conn, n, title, lang, *, bf=True, when="2026-11-01T00:00:00Z"):
    save_intel(conn, Intel(url=f"https://x.test/{n}", source="nodeseek", title=title, lang=lang, published_at=when,
                           first_seen_at=when, is_black_friday=bf))


def server(calls, reply):
    def handler(request):
        titles = json.loads(json.loads(request.content)["messages"][1]["content"])["titles"]
        calls.append(titles)
        data = reply(titles)
        if isinstance(data, int):
            return httpx.Response(data, json={"error": "down"})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(data, ensure_ascii=False)}}],
                                         "usage": {"total_tokens": 10}})
    return handler


def test_candidates_are_season_black_friday_threads_without_a_translation(db):
    conn = db.conn()
    add(conn, 1, "黑五 买什么", "zh")
    add(conn, 2, "Best Black Friday VPS?", "en", when="2026-11-02T00:00:00Z")
    add(conn, 3, "随便聊聊", "zh", bf=False)
    add(conn, 4, "黑五 去年", "zh", when="2025-11-01T00:00:00Z")
    assert [r["title"] for r in candidates(conn, SEASON)] == ["Best Black Friday VPS?", "黑五 买什么"]


def test_batches_are_aligned_and_saved_per_language(db):
    settings = load_settings(LLM_ENV, env_file=None)
    conn = db.conn()
    for n in range(BATCH + 2):
        add(conn, n, f"黑五 帖子 {n}", "zh")
    add(conn, 99, "Black Friday thread", "en")
    calls = []
    reply = lambda titles: {"titles": [f"T:{t}" for t in titles]}  # noqa: E731
    with LlmClient(settings, transport=httpx.MockTransport(server(calls, reply)), sleep=noop_sleep) as client:
        r = run_translate(db, client, settings, limit=None)
    assert (r.candidates, r.calls, r.done, r.failed, r.tokens) == (BATCH + 3, 3, BATCH + 3, 0, 30)
    assert [len(c) for c in calls] == [BATCH, 2, 1]
    row = conn.execute("SELECT title_en, title_zh FROM intel WHERE url = 'https://x.test/5'").fetchone()
    assert (row["title_en"], row["title_zh"]) == ("T:黑五 帖子 5", None)
    assert conn.execute("SELECT title_zh FROM intel WHERE url = 'https://x.test/99'").fetchone()[0] == "T:Black Friday thread"
    assert candidates(conn, SEASON) == []


def test_a_misaligned_batch_is_dropped_and_counted(db):
    settings = load_settings(LLM_ENV, env_file=None)
    conn = db.conn()
    add(conn, 1, "黑五 一", "zh")
    add(conn, 2, "黑五 二", "zh")
    calls = []
    with LlmClient(settings, transport=httpx.MockTransport(server(calls, lambda t: {"titles": ["only one"]})),
                   sleep=noop_sleep) as client:
        for _ in range(MAX_FAILED):
            assert run_translate(db, client, settings).failed == 2
        assert run_translate(db, client, settings).candidates == 0  # 满次数不再试
    assert conn.execute("SELECT COUNT(*) FROM intel WHERE title_en IS NOT NULL").fetchone()[0] == 0


def test_service_errors_are_not_counted_against_the_titles(db):
    settings = load_settings(LLM_ENV, env_file=None)
    conn = db.conn()
    add(conn, 1, "黑五 一", "zh")
    with LlmClient(settings, transport=httpx.MockTransport(server([], lambda t: 503)), sleep=noop_sleep) as client:
        r = run_translate(db, client, settings)
    assert r.errors and r.done == 0
    assert conn.execute("SELECT translate_failed FROM intel").fetchone()[0] == 0


def test_check_rejects_empty_and_runaway_translations():
    assert check(["黑五"], {"titles": ["Black Friday"]}, "zh") == ["Black Friday"]
    assert check(["黑五"], {"titles": [" "]}, "zh") is None
    assert check(["Hi"], {"titles": ["你好" * 20]}, "en") is None  # 借题发挥
    assert check(["a", "b"], {"titles": ["一"]}, "en") is None and check(["a"], ["x"], "en") is None


def test_a_changed_title_clears_the_translation(db):
    conn = db.conn()
    add(conn, 1, "黑五 一", "zh")
    conn.execute("UPDATE intel SET title_en = 'BF one', translate_failed = 2")
    add(conn, 1, "黑五 一", "zh")  # 标题没变：译名保留
    assert conn.execute("SELECT title_en FROM intel").fetchone()[0] == "BF one"
    add(conn, 1, "黑五 一（更新）", "zh")
    row = conn.execute("SELECT title_en, translate_failed FROM intel").fetchone()
    assert (row["title_en"], row["translate_failed"]) == (None, 0)
