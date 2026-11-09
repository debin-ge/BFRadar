"""页面、接口与内部页面数据共用的站点信息：采集源顺序、下一轮采集时间，页头页脚上随数据变化的文字（D60）。"""

import sqlite3

from ..config import Settings
from ..fx import latest_rates
from . import fmt
from .queries import last_update
from .view import View

STATUS_SOURCES = ("lowendtalk", "lowendspirit", "lowendbox", "telegram", "official", "nodeseek", "reddit", "v2ex",
                  "nodeloc", "hostingdiscussion")
OFFER_JOBS = tuple(f"source:{s}" for s in ("lowendtalk", "lowendspirit", "lowendbox", "telegram", "official"))


def next_crawl(scheduler) -> str | None:
    """下一轮优惠源采集的时间（调度器在本进程时才有）。"""
    if scheduler is None:
        return None
    jobs = scheduler.status()["jobs"]
    times = [jobs[j] for j in OFFER_JOBS if jobs.get(j)]
    return min(times) if times else None


def chrome(settings: Settings, conn: sqlite3.Connection, view: View, upcoming: str | None) -> dict:
    """首页倒计时区块与页头的更新时间、页脚的汇率说明，按语言与显示时区格式化好。"""
    t = view.t
    phase, days, hours = fmt.countdown(view.now)
    _, _, _, minutes, seconds = fmt.remaining(view.now)
    start, end = (fmt.local(x, view.tz) for x in (fmt.BLACK_FRIDAY, fmt.CYBER_MONDAY_END))
    updated = last_update(conn)
    freshness = t("header.updated", time=view.clock(updated)) if updated else t("header.never")
    if upcoming:
        freshness += view.sep("comma") + t("header.next", time=view.clock(upcoming))
    fx = latest_rates(conn, static="static" in settings.fx_providers)
    fx_note = (t("footer.fx", provider="ECB" if fx.provider == "ecb" else fx.provider, date=fx.date)
               if fx and fx.provider != "static" else t("footer.fx_static"))
    return {
        "countdown": {"target": fmt.BLACK_FRIDAY.isoformat(), "end": fmt.CYBER_MONDAY_END.isoformat(),
                      "phase": phase, "days": days, "hours": hours, "minutes": minutes, "seconds": seconds,
                      "text": t(f"countdown.{phase}", days=days, hours=hours),
                      "when": {"before": t("countdown.when.before", time=f"{start:%m-%d %H:%M}",
                                           et=f"{fmt.BLACK_FRIDAY:%m-%d}"),
                               "during": t("countdown.when.during", time=f"{end:%m-%d %H:%M}",
                                           et=f"{fmt.CYBER_MONDAY_END:%m-%d}")}},
        "freshness": freshness, "fx_note": fx_note,
    }
