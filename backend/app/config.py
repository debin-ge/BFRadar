"""配置：环境变量（可选项目根目录 .env 文件）→ Settings。

变量与默认值见《需求与方案说明》第 18 节；真实环境变量优先于 .env。
"""

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
SOURCE_IDS = ("lowendtalk", "lowendspirit", "lowendbox", "nodeseek", "reddit", "telegram", "official", "v2ex", "nodeloc",
              "hostingdiscussion")
# 第 13 节与 D24：（常态，黑五周）采集间隔，单位分钟
DEFAULT_INTERVALS = {
    "lowendtalk": (60, 30),
    "lowendspirit": (60, 60),
    "lowendbox": (60, 60),
    "nodeseek": (5, 3),
    "reddit": (20, 10),  # D83：三个入口轮流抓，每轮一个请求
    "telegram": (30, 10),  # D81：黑五高峰约 180 条/天
    "official": (240, 120),  # D84：主流品牌官网活动页，每页几百 KB，改价不频繁
    "v2ex": (60, 30),  # D88：小社区情报，窗口 50 条约 3 天到 3 周
    "nodeloc": (60, 30),
    "hostingdiscussion": (60, 30),
}
LES_CATEGORIES = (
    "offers", "vps", "dedicated-server", "shared-hosting", "services", "free-offers-and-stuff", "must-see-deals-",
)  # D36
# D83：Reddit 社区版块（合并成一个请求）与主流品牌官方版块（官方账号在这里发的促销帖算优惠）
REDDIT_SUBREDDITS = ("VPS", "webhosting", "hetzner", "Hosting", "HostingReport", "SelfHosting")
REDDIT_VENDOR_SUBREDDITS = (
    "Hostinger", "CloudwaysbyDO", "SiteGroundOfficial", "OVHcloud", "digital_ocean", "Contabo", "RackNerd", "netcup",
    "vultr", "linode", "Ionos", "Namecheap", "LightNodeVPS", "hostney",
)
FX_PROVIDERS = ("ecb", "erapi", "static")
SECRET_FIELDS = {"refresh_token", "llm_api_key", "cf_api_token"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    site_url: str = "http://127.0.0.1:8000"
    site_name_zh: str = "黑五雷达"
    site_name_en: str = "BFRadar"
    site_fullname_en: str = "BlackFridayRadar"
    contact_email: str = ""  # 写进默认 User-Agent（第 17 节；D69 起关于页不再展示）
    host: str = "0.0.0.0"
    port: int = 8000
    db_path: Path = ROOT / "data" / "radar.db"
    sources: tuple[str, ...] = SOURCE_IDS
    intervals: dict[str, int] = field(default_factory=lambda: {s: v[0] for s, v in DEFAULT_INTERVALS.items()})
    intervals_bf: dict[str, int] = field(default_factory=lambda: {s: v[1] for s, v in DEFAULT_INTERVALS.items()})
    bf_week_start: date = date(2026, 11, 24)
    bf_week_end: date = date(2026, 12, 1)
    season_start: date = date(2026, 10, 1)
    today: date | None = None  # 固定"今天"（测试与演示用）：判断活动是否结束、还剩几天都以它为准；空为真实日期（TODAY）
    reddit_subreddits: tuple[str, ...] = REDDIT_SUBREDDITS
    les_categories: tuple[str, ...] = LES_CATEGORIES
    refresh_token: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_concurrency: int = 3
    llm_timeout: float = 120.0
    fx_providers: tuple[str, ...] = FX_PROVIDERS
    display_tz_zh: str = "Asia/Shanghai"
    display_tz_en: str = "UTC"
    https_proxy: str = ""
    user_agent: str = ""
    cf_zone_id: str = ""
    cf_api_token: str = ""
    log_level: str = "INFO"
    http_timeout: float = 20.0
    fx_http_timeout: float = 30.0

    @property
    def llm_enabled(self) -> bool:
        """三项齐全才启用 LLM（FR-05），否则纯规则模式。"""
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    def is_bf_week(self, day: date) -> bool:
        return self.bf_week_start <= day <= self.bf_week_end

    def interval(self, source: str, day: date) -> int:
        """该源在某天的采集间隔（分钟），黑五周自动切换档位（FR-19）。"""
        return (self.intervals_bf if self.is_bf_week(day) else self.intervals)[source]

    def launch_warnings(self) -> list[str]:
        """上线前必填却还空着（或仍是本机地址）的配置，返回字典键 config.*（优化方案 O-05）。只提醒、不拦截：
        本机开发本来就空着这些值。`main.py config`、serve 启动日志与状态页都显示。"""
        host = (urlsplit(self.site_url).hostname or "").lower()
        missing = []
        if not host or host in ("localhost", "0.0.0.0", "::1") or host.startswith("127."):
            missing.append("config.site_url")
        if not self.contact_email:
            missing.append("config.contact_email")
        if not self.refresh_token:
            missing.append("config.refresh_token")
        return missing

    def redacted(self) -> dict[str, object]:
        """用于日志与 `main.py config` 输出，密钥只显示是否已设置。"""
        out = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = ("已设置" if value else "") if f.name in SECRET_FIELDS else value
        return out


def read_env_file(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE 行；忽略空行与 # 注释，去掉成对引号。"""
    env = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ConfigError(f"{path.name} 第 {lineno} 行不是 KEY=VALUE 格式")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:  # 未加引号时，行首或空白后的 # 起为注释
            value = re.split(r"(?:^|\s)#", value, maxsplit=1)[0].strip()
        env[key.strip()] = value
    return env


class _Reader:
    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = env

    def text(self, key: str, default: str) -> str:
        value = self.env.get(key, "").strip()
        return value or default

    def integer(self, key: str, default: int, minimum: int = 0) -> int:
        raw = self.env.get(key, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise ConfigError(f"{key} 应为整数，实际 {raw!r}") from None
        if value < minimum:
            raise ConfigError(f"{key} 不能小于 {minimum}")
        return value

    def number(self, key: str, default: float) -> float:
        raw = self.env.get(key, "").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            raise ConfigError(f"{key} 应为数字，实际 {raw!r}") from None
        if value <= 0:
            raise ConfigError(f"{key} 必须大于 0")
        return value

    def day(self, key: str, default: date) -> date:
        raw = self.env.get(key, "").strip()
        if not raw:
            return default
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise ConfigError(f"{key} 应为 YYYY-MM-DD，实际 {raw!r}") from None

    def items(self, key: str, default: tuple[str, ...], allowed: tuple[str, ...] | None = None) -> tuple[str, ...]:
        raw = self.env.get(key, "").strip()
        if not raw:
            return default
        items = tuple(dict.fromkeys(x.strip() for x in raw.split(",") if x.strip()))
        if allowed is not None and (unknown := [x for x in items if x not in allowed]):
            raise ConfigError(f"{key} 含未知取值 {unknown}，可选：{','.join(allowed)}")
        return items

    def timezone(self, key: str, default: str) -> str:
        value = self.text(key, default)
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(f"{key} 不是有效的时区名：{value!r}") from None
        return value


def load_settings(environ: Mapping[str, str] | None = None, env_file: Path | None = ROOT / ".env") -> Settings:
    env: dict[str, str] = read_env_file(env_file) if env_file and env_file.exists() else {}
    env.update(os.environ if environ is None else environ)
    r = _Reader(env)

    defaults = Settings()
    sources = r.items("SOURCES", SOURCE_IDS, allowed=SOURCE_IDS)
    intervals = {s: r.integer(f"INTERVAL_{s.upper()}", defaults.intervals[s], minimum=1) for s in SOURCE_IDS}
    intervals_bf = {s: r.integer(f"INTERVAL_{s.upper()}_BF", defaults.intervals_bf[s], minimum=1) for s in SOURCE_IDS}
    bf_start, bf_end = r.day("BF_WEEK_START", defaults.bf_week_start), r.day("BF_WEEK_END", defaults.bf_week_end)
    if bf_end < bf_start:
        raise ConfigError("BF_WEEK_END 早于 BF_WEEK_START")
    db_path = Path(r.text("DB_PATH", "data/radar.db"))
    log_level = r.text("LOG_LEVEL", "INFO").upper()
    if log_level not in logging.getLevelNamesMapping():
        raise ConfigError(f"LOG_LEVEL 不是有效的日志级别：{log_level!r}")
    site_url = r.text("SITE_URL", defaults.site_url).rstrip("/")
    contact = r.text("CONTACT_EMAIL", "")
    if contact and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", contact):
        raise ConfigError(f"CONTACT_EMAIL 不是邮箱地址：{contact!r}")

    return Settings(
        site_url=site_url,
        site_name_zh=r.text("SITE_NAME_ZH", defaults.site_name_zh),
        site_name_en=r.text("SITE_NAME_EN", defaults.site_name_en),
        site_fullname_en=r.text("SITE_FULLNAME_EN", defaults.site_fullname_en),
        contact_email=contact,
        host=r.text("HOST", defaults.host),
        port=r.integer("PORT", defaults.port, minimum=1),
        db_path=db_path if db_path.is_absolute() else ROOT / db_path,
        sources=sources,
        intervals=intervals,
        intervals_bf=intervals_bf,
        bf_week_start=bf_start,
        bf_week_end=bf_end,
        season_start=r.day("SEASON_START", defaults.season_start),
        today=r.day("TODAY", None),
        reddit_subreddits=r.items("REDDIT_SUBREDDITS", defaults.reddit_subreddits),
        les_categories=r.items("LES_CATEGORIES", defaults.les_categories),
        refresh_token=r.text("REFRESH_TOKEN", ""),
        llm_base_url=r.text("LLM_BASE_URL", "").rstrip("/"),
        llm_api_key=r.text("LLM_API_KEY", ""),
        llm_model=r.text("LLM_MODEL", ""),
        llm_concurrency=r.integer("LLM_CONCURRENCY", defaults.llm_concurrency, minimum=1),
        llm_timeout=r.number("LLM_TIMEOUT", defaults.llm_timeout),
        fx_providers=r.items("FX_PROVIDER", defaults.fx_providers, allowed=FX_PROVIDERS),
        display_tz_zh=r.timezone("DISPLAY_TZ_ZH", defaults.display_tz_zh),
        display_tz_en=r.timezone("DISPLAY_TZ_EN", defaults.display_tz_en),
        https_proxy=r.text("HTTPS_PROXY", ""),
        user_agent=r.text("USER_AGENT", f"Mozilla/5.0 (compatible; BFRadar/0.1; +{site_url}"
                                        f"{'; ' + contact if contact else ''})"),
        cf_zone_id=r.text("CF_ZONE_ID", ""),
        cf_api_token=r.text("CF_API_TOKEN", ""),
        log_level=log_level,
        http_timeout=r.number("HTTP_TIMEOUT", defaults.http_timeout),
        fx_http_timeout=r.number("FX_HTTP_TIMEOUT", defaults.fx_http_timeout),
    )
