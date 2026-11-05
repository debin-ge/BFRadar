"""页面上的数字、容量、时间与倒计时格式（纯函数，便于测试）。"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

BLACK_FRIDAY = datetime(2026, 11, 27, tzinfo=ZoneInfo("America/New_York"))  # 美东零点开始
CYBER_MONDAY_END = datetime(2026, 12, 1, tzinfo=ZoneInfo("America/New_York"))


def num(value: float | None) -> str:
    """整数不带小数，其余最多两位并去掉末尾的 0。"""
    if value is None:
        return ""
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def size_gb(value: float | None) -> str:
    """内存与存储：不足 1 GB 写 MB，1000 GB 起写 TB（存储按 1 TB = 1000 GB，金标集规范 6.4）。"""
    if value is None:
        return ""
    if value < 1:
        return f"{num(value * 1024)} MB"
    if value >= 1000:
        return f"{num(round(value / 1000, 2))} TB"
    return f"{num(value)} GB"


def speed(gbps: float | None) -> str:
    if gbps is None:
        return ""
    return f"{num(gbps * 1000)} Mbps" if gbps < 1 else f"{num(gbps)} Gbps"


def span(lo: float, hi: float, unit=num) -> str:
    return unit(lo) if lo == hi else f"{unit(lo)}–{unit(hi)}"


def parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def local(value: str | datetime | None, tz: str) -> datetime | None:
    ts = parse_ts(value) if isinstance(value, str) or value is None else value
    return ts.astimezone(ZoneInfo(tz)) if ts else None


def clock(value: str | datetime | None, tz: str, now: datetime) -> str:
    """当天只写时分，其余写月-日 时分。"""
    ts = local(value, tz)
    if ts is None:
        return ""
    return f"{ts:%H:%M}" if ts.date() == now.astimezone(ts.tzinfo).date() else f"{ts:%m-%d %H:%M}"


def day(value: str | None, tz: str) -> str:
    ts = local(value, tz)
    return f"{ts:%m-%d}" if ts else ""


def ago(value: str | None, now: datetime) -> tuple[str, int]:
    """相对时间的（单位, 数量）：now / minutes / hours / days。"""
    ts = parse_ts(value)
    if ts is None:
        return "days", 0
    seconds = max(0, (now - ts).total_seconds())
    if seconds < 60:
        return "now", 0
    if seconds < 3600:
        return "minutes", int(seconds // 60)
    if seconds < 86400:
        return "hours", int(seconds // 3600)
    return "days", int(seconds // 86400)


def countdown(now: datetime) -> tuple[str, int, int]:
    """（阶段, 天, 小时）：before 距黑五美东零点；during 黑五到网一结束；after 之后。"""
    if now < BLACK_FRIDAY:
        left = BLACK_FRIDAY - now
        return "before", left.days, left.seconds // 3600
    return ("during" if now < CYBER_MONDAY_END else "after"), 0, 0


def remaining(now: datetime) -> tuple[str, int, int, int, int]:
    """（阶段, 天, 时, 分, 秒）：before 距黑五美东零点；during 距网一结束；after 全为 0。首页的倒计时区块用。"""
    if now < BLACK_FRIDAY:
        phase, left = "before", BLACK_FRIDAY - now
    elif now < CYBER_MONDAY_END:
        phase, left = "during", CYBER_MONDAY_END - now
    else:
        return "after", 0, 0, 0, 0
    seconds = int(left.total_seconds())
    return phase, seconds // 86400, seconds % 86400 // 3600, seconds % 3600 // 60, seconds % 60


ENDED_GRACE_DAYS = 1  # 原帖里的日期多半是美国时间：截止日期过了一天以后才算结束


def days_left(ends_at: str, today: date) -> int:
    """距截止日期还有几天：当天为 0，已过为负数。"""
    return (date.fromisoformat(ends_at) - today).days


def ended_cutoff(today: date) -> str:
    """截止日期早于这一天的算已结束（ISO 日期按字符串比较，SQL 里直接用）。"""
    return (today - timedelta(days=ENDED_GRACE_DAYS)).isoformat()


def day_label(iso_day: str, lang: str, today: date) -> str:
    """截止日期的写法：中文 12-02，英文 Dec 2；不是今年的带上年份。"""
    d = date.fromisoformat(iso_day)
    if lang == "zh":
        return f"{d:%m-%d}" if d.year == today.year else f"{d:%Y-%m-%d}"
    label = f"{d:%b} {d.day}"
    return label if d.year == today.year else f"{label}, {d.year}"


def season_start_iso(season: date) -> str:
    return datetime(season.year, season.month, season.day, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def since(now: datetime, hours: int) -> str:
    return (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
