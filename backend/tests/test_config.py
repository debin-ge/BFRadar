from datetime import date
from pathlib import Path

import pytest

from app.config import ROOT, SOURCE_IDS, ConfigError, load_settings, read_env_file


def settings(**env):
    return load_settings(env, env_file=None)


def test_defaults():
    s = settings()
    assert s.sources == SOURCE_IDS
    assert s.db_path == ROOT / "data" / "radar.db"
    assert s.season_start == date(2026, 10, 1)
    assert not s.llm_enabled
    assert s.user_agent == "Mozilla/5.0 (compatible; BFRadar/0.1; +http://127.0.0.1:8000)"


@pytest.mark.parametrize("day, source, minutes", [
    (date(2026, 11, 23), "nodeseek", 5),
    (date(2026, 11, 24), "nodeseek", 3),   # 黑五周首日（含）
    (date(2026, 12, 1), "lowendtalk", 30),  # 黑五周末日（含）
    (date(2026, 12, 2), "lowendtalk", 60),
    (date(2026, 11, 27), "reddit", 10),  # D83：三个入口轮转
    (date(2026, 11, 20), "reddit", 20),
    (date(2026, 11, 27), "telegram", 10),
    (date(2026, 11, 20), "v2ex", 60),  # D88
    (date(2026, 11, 27), "hostingdiscussion", 30),
])
def test_bf_week_intervals(day, source, minutes):
    assert settings().interval(source, day) == minutes


def test_overrides():
    s = settings(SOURCES="lowendtalk, nodeseek,lowendtalk", INTERVAL_NODESEEK="10", INTERVAL_LOWENDTALK_BF="15",
                 SEASON_START="2026-09-01", DB_PATH="/tmp/bfr.db", SITE_URL="https://bfradar.com/", LOG_LEVEL="debug")
    assert s.sources == ("lowendtalk", "nodeseek")
    assert s.intervals["nodeseek"] == 10
    assert s.intervals_bf["lowendtalk"] == 15
    assert s.season_start == date(2026, 9, 1)
    assert s.db_path == Path("/tmp/bfr.db")
    assert s.site_url == "https://bfradar.com"
    assert s.user_agent.endswith("+https://bfradar.com)")
    assert s.log_level == "DEBUG"


def test_contact_email_goes_into_the_default_user_agent():
    s = settings(SITE_URL="https://bfradar.com", CONTACT_EMAIL="ops@bfradar.com")
    assert s.contact_email == "ops@bfradar.com"
    assert s.user_agent == "Mozilla/5.0 (compatible; BFRadar/0.1; +https://bfradar.com; ops@bfradar.com)"
    assert settings(CONTACT_EMAIL="ops@bfradar.com", USER_AGENT="custom").user_agent == "custom"


def test_relative_db_path_is_under_project_root():
    assert settings(DB_PATH="var/x.db").db_path == ROOT / "var" / "x.db"


def test_llm_enabled_requires_all_three():
    assert not settings(LLM_BASE_URL="https://api.example.com/v1", LLM_MODEL="m").llm_enabled
    s = settings(LLM_BASE_URL="https://api.example.com/v1/", LLM_MODEL="m", LLM_API_KEY="k")
    assert s.llm_enabled
    assert s.llm_base_url == "https://api.example.com/v1"


@pytest.mark.parametrize("env", [
    {"SOURCES": "lowendtalk,hostloc"},
    {"INTERVAL_NODESEEK": "0"},
    {"INTERVAL_NODESEEK": "five"},
    {"BF_WEEK_START": "2026-13-01"},
    {"BF_WEEK_START": "2026-12-05"},  # 晚于默认结束日
    {"DISPLAY_TZ_ZH": "Mars/Base"},
    {"FX_PROVIDER": "ecb,oanda"},
    {"LOG_LEVEL": "LOUD"},
    {"HTTP_TIMEOUT": "0"},
    {"PORT": "0"},
    {"CONTACT_EMAIL": "not-an-email"},
])
def test_invalid_values_raise(env):
    with pytest.raises(ConfigError):
        settings(**env)


def test_env_file_parsing(tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        "# 注释\n"
        "\n"
        "SITE_URL=https://bfradar.com   # 行尾注释\n"
        "USER_AGENT=\"BFRadar/1.0 (contact # inside quotes)\"\n"
        "SITE_NAME_EN=https://example.com/page/#anchor\n"
        "REFRESH_TOKEN=   # 留空\n"
        "PORT=9000\n",
        encoding="utf-8",
    )
    env = read_env_file(f)
    assert env["SITE_URL"] == "https://bfradar.com"
    assert env["USER_AGENT"] == "BFRadar/1.0 (contact # inside quotes)"
    assert env["SITE_NAME_EN"] == "https://example.com/page/#anchor"  # 没有空格的 # 不算注释
    assert env["REFRESH_TOKEN"] == ""
    s = load_settings({"PORT": "9100"}, env_file=f)  # 真实环境变量优先
    assert s.port == 9100
    assert s.site_url == "https://bfradar.com"


def test_env_file_rejects_malformed_line(tmp_path):
    f = tmp_path / ".env"
    f.write_text("JUST_A_WORD\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        read_env_file(f)


def test_example_env_file_loads_as_defaults():
    assert load_settings({}, env_file=ROOT / ".env.example") == settings()


def test_redacted_hides_secrets():
    out = settings(REFRESH_TOKEN="s3cret", LLM_API_KEY="sk-xyz").redacted()
    assert out["refresh_token"] == "已设置"
    assert out["llm_api_key"] == "已设置"
    assert out["cf_api_token"] == ""
    assert "s3cret" not in str(out) and "sk-xyz" not in str(out)


def test_launch_warnings():
    """上线前必填却空着（或仍是本机地址）的配置：只提醒、不拦截（优化方案 O-05）。"""
    local = load_settings({}, env_file=None)
    assert local.launch_warnings() == ["config.site_url", "config.contact_email", "config.refresh_token"]
    for url in ("http://localhost:3000", "http://127.0.0.1:8000", "http://0.0.0.0", ""):
        assert "config.site_url" in load_settings({"SITE_URL": url}, env_file=None).launch_warnings(), url
    ready = load_settings({"SITE_URL": "https://bfradar.example", "CONTACT_EMAIL": "hi@bfradar.example",
                           "REFRESH_TOKEN": "x" * 32}, env_file=None)
    assert ready.launch_warnings() == []
