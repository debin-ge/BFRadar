"""调度器（《需求与方案说明》第 13 节，D24、D25、D48）。

进程内 APScheduler BackgroundScheduler，任务都在工作线程里执行，每个线程用自己的数据库连接（Database 按线程缓存）：
  - 每个采集源一个任务（max_instances=1、coalesce=True），间隔按当天是否黑五周取档位（日期按 DISPLAY_TZ_ZH）；
    每天 00:00 重新核对档位。一轮采集有优惠新增或变化时唤醒 LLM 任务。
  - LLM 队列（LLM 已配置时）：每 2 分钟一次，每批 20 条（最新发布的优先），队列空了或超过 10 分钟就停，留给下一次。
  - 汇率：每日 09:00；启动时库里的汇率超过 20 小时没更新就先拉一次。
  - 数据库备份：每日 04:00 VACUUM INTO data/backups/radar-{日期}.db，保留 14 天（第 15.6 节）。
启动后 3 秒先跑一轮全部采集源。手动刷新（POST /api/refresh，FR-16）用 refresh()：同一时间只允许一轮，与定时任务
按源互斥，不会同时采集同一个源。每跑完一轮采集、一批 LLM 或一次汇率更新就通知监听者（网站用来清内存缓存）。
"""

import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import Settings
from .db import Database, transaction
from .extract.llm import LlmClient
from .fx import update_rates
from .http import HttpClient
from .models import iso, utcnow
from .pipeline import SourceReport, crawl, crawl_source, run_llm_queue
from .translate import run_translate
from .repo import finish_run, start_run
from .sources import build_sources
from .vendors import prepare

log = logging.getLogger(__name__)

STARTUP_DELAY = timedelta(seconds=3)
LLM_EVERY_MINUTES = 2
LLM_BATCH = 20
LLM_BUDGET_SECONDS = 600
FX_STALE = timedelta(hours=20)
BACKUP_KEEP_DAYS = 14
BACKUP_NAME = re.compile(r"radar-(\d{4}-\d{2}-\d{2})\.db")


class RefreshBusy(RuntimeError):
    """已有一轮手动刷新在进行（API 返回 409）。"""


def backup_db(conn: sqlite3.Connection, folder: Path, day: date, keep_days: int = BACKUP_KEEP_DAYS) -> Path:
    """VACUUM INTO 写当日备份（先写临时文件再改名，同一天重跑会覆盖），并删掉超过保留天数的旧备份。"""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"radar-{day.isoformat()}.db"
    tmp = target.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    conn.execute("VACUUM INTO ?", (str(tmp),))
    os.replace(tmp, target)
    oldest = day - timedelta(days=keep_days - 1)
    for path in folder.glob("radar-*.db"):
        if (m := BACKUP_NAME.fullmatch(path.name)) and date.fromisoformat(m.group(1)) < oldest:
            path.unlink()
    return target


class Scheduler:
    def __init__(self, settings: Settings, *, db: Database | None = None,
                 http_factory: Callable[[], HttpClient] | None = None,
                 llm_factory: Callable[[], LlmClient] | None = None,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.settings = settings
        self.db = db or Database(settings.db_path)
        self._http_factory = http_factory or (lambda: HttpClient(settings))
        self._llm_factory = llm_factory or (lambda: LlmClient(settings))
        self._clock = clock
        self._tz = ZoneInfo(settings.display_tz_zh)
        self._locks = {s: threading.Lock() for s in settings.sources}
        self._refresh_lock = threading.Lock()
        self._llm_running = threading.Event()
        self._stopping = threading.Event()
        self._intervals: dict[str, int] = {}
        self._listeners: list[Callable[[], None]] = []
        self._sched = BackgroundScheduler(timezone="UTC", job_defaults={"max_instances": 1, "coalesce": True,
                                                                         "misfire_grace_time": 300})

    # ------------------------------------------------------------ 生命周期

    def today(self) -> date:
        return self._clock().astimezone(self._tz).date()

    def tier(self) -> str:
        return "bf" if self.settings.is_bf_week(self.today()) else "normal"

    def start(self, *, paused: bool = False) -> None:
        prepare(self.db)  # 迁移并同步商家别名表
        first = self._clock() + STARTUP_DELAY
        for sid in self.settings.sources:
            self._intervals[sid] = self.settings.interval(sid, self.today())
            self._sched.add_job(self.run_source, IntervalTrigger(minutes=self._intervals[sid]), args=[sid],
                                id=f"source:{sid}", name=f"采集 {sid}", next_run_time=first)
        if self.settings.llm_enabled:
            self._sched.add_job(self.run_llm, IntervalTrigger(minutes=LLM_EVERY_MINUTES), id="llm", name="LLM 队列",
                                next_run_time=first + timedelta(seconds=30))
        fx_first = {"next_run_time": first} if self._fx_stale() else {}
        self._sched.add_job(self.run_fx, CronTrigger(hour=9, minute=0, timezone=self._tz), id="fx", name="汇率",
                            **fx_first)
        self._sched.add_job(self.run_backup, CronTrigger(hour=4, minute=0, timezone=self._tz), id="backup",
                            name="备份")
        self._sched.add_job(self.retier, CronTrigger(hour=0, minute=0, second=30, timezone=self._tz), id="tier",
                            name="档位")
        self._sched.start(paused=paused)
        log.info("调度器启动：%s档位，%s", "黑五周" if self.tier() == "bf" else "常态",
                 "、".join(f"{s} {m} 分钟" for s, m in self._intervals.items()))

    def add_listener(self, fn: Callable[[], None]) -> None:
        """数据可能变了时调用 fn（一轮采集跑完，不论成败；一批 LLM；汇率更新），网站用来清内存缓存（第 15.5 节）。"""
        self._listeners.append(fn)

    def _changed(self) -> None:
        for fn in self._listeners:
            try:
                fn()
            except Exception:
                log.exception("通知数据更新失败")

    def shutdown(self, wait: bool = True) -> None:
        self._stopping.set()
        self._sched.shutdown(wait=wait)

    def status(self) -> dict:
        """状态页与 /healthz 用：当前档位、各源间隔、各任务下次运行时间、是否有手动刷新或 LLM 在跑。"""
        return {"tier": self.tier(), "intervals": dict(self._intervals),
                "jobs": {job.id: iso(job.next_run_time) for job in self._sched.get_jobs()},
                "refreshing": self._refresh_lock.locked(), "llm_running": self._llm_running.is_set()}

    def retier(self) -> None:
        """按当天档位核对各源间隔，跨入或离开黑五周时改用新间隔（FR-19）。"""
        for sid, minutes in list(self._intervals.items()):
            new = self.settings.interval(sid, self.today())
            if new != minutes:
                self._intervals[sid] = new
                self._sched.reschedule_job(f"source:{sid}", trigger=IntervalTrigger(minutes=new))
                log.info("%s 改用%s档位：每 %d 分钟", sid, "黑五周" if self.tier() == "bf" else "常态", new)

    # ------------------------------------------------------------ 任务

    def run_source(self, source_id: str, reason: str = "scheduled") -> SourceReport:
        try:
            with self._locks[source_id], self._http_factory() as http:
                _, [report] = crawl(self.db, build_sources(self.settings, http, [source_id]), reason=reason)
        finally:
            self._changed()
        if report.changed_urls:
            self._wake_llm()
        return report

    def refresh(self, reason: str = "manual", timeout: float = 10.0) -> int:
        """手动刷新一轮全部采集源（FR-16），在后台线程执行；返回运行编号。已有一轮在跑时抛 RefreshBusy。"""
        if not self._refresh_lock.acquire(blocking=False):
            raise RefreshBusy("已有一轮手动刷新在进行")
        started, box = threading.Event(), {}

        def work() -> None:
            try:
                conn = self.db.conn()
                with transaction(conn):
                    box["run_id"] = start_run(conn, reason)
                started.set()
                reports = []
                with self._http_factory() as http:
                    for source in build_sources(self.settings, http):
                        with self._locks[source.id]:
                            reports.append(crawl_source(self.db, box["run_id"], source))
                with transaction(conn):
                    finish_run(conn, box["run_id"], ok=all(r.error is None for r in reports))
                if any(r.changed_urls for r in reports):
                    self._wake_llm()
            except Exception:
                log.exception("手动刷新失败")
            finally:
                self._changed()
                started.set()
                self.db.close()  # 这个线程的连接
                self._refresh_lock.release()

        threading.Thread(target=work, name="refresh", daemon=True).start()
        started.wait(timeout)
        if "run_id" not in box:
            raise RuntimeError("手动刷新未能开始，见日志")
        return box["run_id"]

    def run_llm(self) -> None:
        """一批一批处理 LLM 队列，队列空了、超过时间预算或调度器停止时退出。"""
        if not self.settings.llm_enabled:
            return
        self._llm_running.set()
        deadline = time.monotonic() + LLM_BUDGET_SECONDS
        try:
            with self._llm_factory() as client:
                while not self._stopping.is_set():
                    report = run_llm_queue(self.db, client, self.settings, limit=LLM_BATCH)
                    if report.calls:
                        self._changed()
                    if report.candidates < LLM_BATCH or time.monotonic() > deadline:
                        break
                if not self._stopping.is_set():  # 顺带翻译社区讨论标题（O-14），每轮至多 translate.PER_RUN 条
                    try:
                        if run_translate(self.db, client, self.settings).done:
                            self._changed()
                    except Exception:  # 翻译出错不影响 LLM 队列与下一轮
                        log.exception("社区标题翻译失败")
        finally:
            self._llm_running.clear()

    def _wake_llm(self) -> None:
        if self.settings.llm_enabled and not self._llm_running.is_set() and self._sched.get_job("llm"):
            self._sched.modify_job("llm", next_run_time=self._clock())

    def run_fx(self) -> None:
        with self._http_factory() as http:
            update_rates(self.db.conn(), http, self.settings, now=iso(self._clock()))
        self._changed()

    def _fx_stale(self) -> bool:
        row = self.db.conn().execute("SELECT MAX(fetched_at) FROM fx_rates").fetchone()
        return row[0] is None or datetime.fromisoformat(row[0]) < self._clock() - FX_STALE

    def run_backup(self) -> Path | None:
        if str(self.db.path) == ":memory:":
            return None
        path = backup_db(self.db.conn(), Path(self.db.path).parent / "backups", self.today())
        log.info("数据库已备份：%s", path)
        return path
