"""命令行（M1.14）：stats、show、reprocess、llm-backfill --dry-run、export-gold --dry-run、report-gold（只跑调试集）。"""

import os

import pytest

import main
from app.config import load_settings
from app.db import Database
from app.pipeline import crawl
from app.sources import build_sources

from conftest import noop_sleep

LLM_ENV = {"LLM_BASE_URL": "https://llm.example.com/v1", "LLM_API_KEY": "k", "LLM_MODEL": "m"}


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    """一个采集过 LET 快照的库，模块内共用（只读命令）。"""
    import httpx

    from app.http import HttpClient
    from conftest import FixtureServer

    path = tmp_path_factory.mktemp("cli") / "radar.db"
    settings = load_settings({"DB_PATH": str(path), "SOURCES": "lowendtalk"}, env_file=None)
    db = Database(path)
    db.migrate()
    with HttpClient(settings, transport=httpx.MockTransport(FixtureServer().handler), sleep=noop_sleep) as http:
        crawl(db, build_sources(settings, http, sleep=noop_sleep), reason="test")
    return path


def run(monkeypatch, capsys, db_path, *argv, env=None):
    settings = load_settings({"DB_PATH": str(db_path), "SOURCES": "lowendtalk", **(env or {})}, env_file=None)
    monkeypatch.setattr(main, "load_settings", lambda: settings)  # 不读开发者本机的 .env
    code = main.main(list(argv))
    return code, capsys.readouterr().out


def test_stats(monkeypatch, capsys, db_path):
    code, out = run(monkeypatch, capsys, db_path, "stats")
    assert code == 0
    assert "优惠 100（" in out and "套餐 " in out and "skipped 100" in out
    assert "lowendtalk" in out and "汇率：库里没有" in out


def test_show_latest_and_url(monkeypatch, capsys, db_path):
    code, out = run(monkeypatch, capsys, db_path, "show", "--latest", "2")
    assert code == 0 and out.count("https://lowendtalk.com/discussion/") == 2 and " · " in out
    code, out = run(monkeypatch, capsys, db_path, "show", "--url",
                    "https://lowendtalk.com/discussion/222765/black-friday-uk-vps-epyc?utm_source=x")
    assert code == 0
    assert "商家 VM6" in out and "BFVPS55" in out and "≈ ¥" in out and "名称" in out  # 起价与套餐表
    code, _ = run(monkeypatch, capsys, db_path, "show", "--url", "https://lowendtalk.com/discussion/1")
    assert code == 1


def test_reprocess_is_idempotent(monkeypatch, capsys, tmp_path, db_path):
    copy = tmp_path / "copy.db"
    Database(db_path).conn().execute("VACUUM INTO ?", (str(copy),))
    code, out = run(monkeypatch, capsys, copy, "reprocess", "--source", "lowendtalk")
    assert code == 0 and "重算 100 条原文" in out and "新 0 / 改 0 / 变更 0" in out


def test_llm_backfill_dry_run(monkeypatch, capsys, db_path):
    code, out = run(monkeypatch, capsys, db_path, "llm-backfill", "--dry-run", env=LLM_ENV)
    assert code == 0 and "待补抽 100 条" in out
    code, _ = run(monkeypatch, capsys, db_path, "llm-backfill", "--dry-run")
    assert code == 2  # LLM 未配置


def test_export_gold_dry_run_lists_new_candidates(monkeypatch, capsys, db_path):
    code, out = run(monkeypatch, capsys, db_path, "export-gold", "--source", "lowendtalk", "--limit", "3",
                    "--dry-run")
    lines = [ln for ln in out.splitlines() if ln.startswith("let-")]
    assert code == 0 and len(lines) == 3
    assert not any(ln.split()[0] in {"let-222765", "let-222164"} for ln in lines)  # 已在金标集中的跳过


def test_report_gold_dev_split(monkeypatch, capsys, db_path):
    code, out = run(monkeypatch, capsys, db_path, "report-gold", "--split", "dev", "--mode", "rules")
    assert code == 0 and "规则模式" in out and "holdout" not in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root 什么目录都能写")
def test_unwritable_data_dir_fails_with_a_clear_message(monkeypatch, capsys, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o555)
    settings = load_settings({"DB_PATH": str(data / "radar.db")}, env_file=None)
    monkeypatch.setattr(main, "load_settings", lambda: settings)
    monkeypatch.delenv("RUN_AS_USER", raising=False)
    try:
        assert main.main(["stats"]) == 2
    finally:
        data.chmod(0o755)
    assert f"数据目录错误：数据目录 {data} 当前用户" in capsys.readouterr().err


def test_config_command_does_not_touch_the_data_dir(monkeypatch, capsys, tmp_path):
    settings = load_settings({"DB_PATH": str(tmp_path / "nope" / "radar.db")}, env_file=None)
    monkeypatch.setattr(main, "load_settings", lambda: settings)
    assert main.main(["config"]) == 0
    assert not (tmp_path / "nope").exists()
