"""SQLite 连接与版本化迁移。

WAL 模式，读写互不阻塞；连接不跨线程共享，采集线程与 Web 请求各用自己的连接（D25）。同一进程里的写事务按数据库
文件排队（进程内写锁）：各源采集、LLM 队列、汇率任务在不同线程同时写库时，SQLite 的忙等是"睡一会再抢"，
不排队，持续争用下会有线程等满超时报 "database is locked"（D54）；busy_timeout 只用来等别的进程（如命令行 crawl）。
迁移文件为 app/migrations/NNN_说明.sql，按版本号顺序执行，当前版本记在 PRAGMA user_version。
"""

import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .extract.keywords import NAT
from .locations import normalize as normalize_location
from .locations import region as location_region

MIGRATIONS = Path(__file__).with_name("migrations")
BUSY_TIMEOUT_MS = 30_000  # 等别的进程的写事务
WRITE_WAIT = 300  # 秒：等本进程其他线程的写事务；超过说明有事务卡住了
_write_locks: dict[str, threading.RLock] = {}
_write_locks_guard = threading.Lock()


class Connection(sqlite3.Connection):
    write_lock: threading.RLock  # 同一数据库文件的连接共用；可重入：误嵌套的事务照旧由 SQLite 报错，而不是卡住


def _write_lock(path: Path | str) -> threading.RLock:
    if str(path) == ":memory:":
        return threading.RLock()  # 每个内存库各是一个库
    key = str(Path(path).resolve())
    with _write_locks_guard:
        return _write_locks.setdefault(key, threading.RLock())


def connect(path: Path | str) -> Connection:
    """打开连接并设置 PRAGMA。autocommit 模式，事务用 transaction() 显式控制。"""
    in_memory = str(path) == ":memory:"
    if not in_memory:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, factory=Connection)
    conn.write_lock = _write_lock(path)
    conn.row_factory = sqlite3.Row
    if not in_memory:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.create_function("is_nat", 2, is_nat, deterministic=True)
    conn.create_function("loc_region", 1, location_region, deterministic=True)  # 机房所在地区（套餐视图按地区筛选）
    conn.create_function("loc_norm", 1, normalize_location, deterministic=True)  # 机房规范名（旧数据未重算也能按城市筛）
    return conn


def is_nat(name: str | None, notes: str | None) -> bool:
    """NAT 套餐：套餐名或备注里单独出现 NAT（keywords.NAT）。SQL 里同名函数供套餐视图筛选用，与页面上的标签同一口径。"""
    return bool(NAT.search(name or "") or NAT.search(notes or ""))


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE 写事务：先拿本进程的写锁（同一库的写事务排队），异常时回滚，正常结束时提交。"""
    lock = getattr(conn, "write_lock", None) or threading.RLock()
    if not lock.acquire(timeout=WRITE_WAIT):
        raise sqlite3.OperationalError(f"database is locked：本进程的写事务等了 {WRITE_WAIT} 秒")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        lock.release()


def migration_files(directory: Path = MIGRATIONS) -> list[tuple[int, Path]]:
    found = []
    for path in directory.glob("*.sql"):
        m = re.fullmatch(r"(\d{3})_[\w-]+\.sql", path.name)
        if not m:
            raise ValueError(f"迁移文件名应为 NNN_说明.sql：{path.name}")
        found.append((int(m.group(1)), path))
    found.sort()
    if [v for v, _ in found] != list(range(1, len(found) + 1)):
        raise ValueError(f"迁移版本号应从 001 起连续：{[p.name for _, p in found]}")
    return found


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS) -> tuple[int, int]:
    """执行尚未执行的迁移，每个文件一个事务。返回（迁移前版本, 迁移后版本）。"""
    before = current = schema_version(conn)
    files = migration_files(directory)
    if current > len(files):
        raise RuntimeError(f"数据库版本 {current} 高于代码中的迁移数 {len(files)}，请升级代码")
    for version, path in files[current:]:
        script = path.read_text(encoding="utf-8")
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error as e:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise RuntimeError(f"迁移 {path.name} 失败：{e}") from e
        current = version
    return before, current


class Database:
    """按线程缓存连接的数据库句柄。"""

    def __init__(self, path: Path | str) -> None:
        self.path = path
        self._local = threading.local()

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = connect(self.path)
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def migrate(self) -> tuple[int, int]:
        return migrate(self.conn())
