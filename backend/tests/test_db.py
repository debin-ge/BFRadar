import json
import os
import shutil
import sqlite3
import threading

import pytest

from app.db import (MIGRATIONS, Database, DataDirError, connect, migrate, migration_files, prepare_data_dir,
                    schema_version, transaction)


def test_migrate_creates_schema_and_is_idempotent(tmp_path):
    db = Database(tmp_path / "radar.db")
    latest = len(migration_files())
    assert db.migrate() == (0, latest)
    assert db.migrate() == (latest, latest)
    conn = db.conn()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"vendors", "raw_posts", "deals", "plans", "changes", "intel", "intel_vendors", "fx_rates", "runs",
            "run_sources"} <= tables


def test_failed_migration_rolls_back(tmp_path):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (x INTEGER);", encoding="utf-8")
    (tmp_path / "002_b.sql").write_text("CREATE TABLE b (x INTEGER);\nTHIS IS NOT SQL;", encoding="utf-8")
    conn = connect(tmp_path / "t.db")
    with pytest.raises(RuntimeError, match="002_b.sql"):
        migrate(conn, tmp_path)
    assert schema_version(conn) == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"a"}
    assert not conn.in_transaction


def test_migration_files_must_be_contiguous(tmp_path):
    (tmp_path / "001_a.sql").write_text("", encoding="utf-8")
    (tmp_path / "003_c.sql").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="连续"):
        migration_files(tmp_path)


def test_database_newer_than_code_is_refused(tmp_path):
    conn = connect(tmp_path / "t.db")
    conn.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="高于"):
        migrate(conn)


def _insert_raw(conn, url="https://lowendtalk.com/discussion/1"):
    conn.execute(
        "INSERT INTO raw_posts (url, source, fetched_at, title, text_struct, content_hash) VALUES (?, ?, ?, ?, ?, ?)",
        (url, "lowendtalk", "2026-11-02T00:00:00Z", "t", "body", "h"),
    )


def test_transaction_commits_and_rolls_back():
    conn = connect(":memory:")
    migrate(conn)
    with transaction(conn):
        _insert_raw(conn, "https://lowendtalk.com/discussion/1")
    with pytest.raises(ZeroDivisionError):
        with transaction(conn):
            _insert_raw(conn, "https://lowendtalk.com/discussion/2")
            1 / 0
    assert [r[0] for r in conn.execute("SELECT url FROM raw_posts")] == ["https://lowendtalk.com/discussion/1"]


def test_check_constraints_reject_unknown_enum_values():
    conn = connect(":memory:")
    migrate(conn)
    deal = ("INSERT INTO deals (url, source, title, first_seen_at, last_seen_at, content_hash, updated_at, event_type)"
            " VALUES ('u', 'lowendtalk', 't', 'x', 'x', 'h', 'x', ?)")
    conn.execute(deal, ("black_friday",))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(deal.replace("'u'", "'u2'"), ("blackfriday",))


def test_plans_cascade_with_deal():
    conn = connect(":memory:")
    migrate(conn)
    conn.execute("INSERT INTO deals (id, url, source, title, first_seen_at, last_seen_at, content_hash, updated_at)"
                 " VALUES (1, 'u', 'lowendtalk', 't', 'x', 'x', 'h', 'x')")
    conn.execute("INSERT INTO plans (deal_id, position, price, currency, period) VALUES (1, 0, 21, 'EUR', 'year')")
    conn.execute("DELETE FROM deals WHERE id = 1")
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


def test_connections_are_per_thread(tmp_path):
    db = Database(tmp_path / "radar.db")
    db.migrate()
    main_conn = db.conn()
    assert db.conn() is main_conn
    seen = []
    t = threading.Thread(target=lambda: seen.append(db.conn()))
    t.start()
    t.join()
    assert seen[0] is not main_conn


def hammer(path, threads: int = 8, rounds: int = 20) -> list[str]:
    """多个线程同时写同一个库，SQLite 自己的忙等调到 20 毫秒；返回各线程遇到的错误。"""
    import time

    errors: list[str] = []

    def writer(n: int) -> None:
        conn = connect(path)
        conn.execute("PRAGMA busy_timeout = 20")
        try:
            for i in range(rounds):
                with transaction(conn):
                    conn.execute("INSERT INTO runs (started_at, reason) VALUES (?, 'test')", (f"{n}-{i}",))
                    time.sleep(0.002)
        except sqlite3.OperationalError as e:
            errors.append(str(e))
        finally:
            conn.close()

    workers = [threading.Thread(target=writer, args=(n,)) for n in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    return errors


def test_writers_in_one_process_queue_instead_of_timing_out(tmp_path):
    """各源采集、LLM 队列、汇率任务在不同线程同时写库：本进程的写事务排队，不会等满忙等时间报错（D54）。"""
    db = Database(tmp_path / "radar.db")
    db.migrate()
    assert hammer(tmp_path / "radar.db") == []
    assert db.conn().execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 8 * 20
    a, b = connect(tmp_path / "radar.db"), connect(tmp_path / "./radar.db")
    assert a.write_lock is b.write_lock  # 同一个文件共用一把锁
    assert connect(tmp_path / "other.db").write_lock is not a.write_lock
    assert connect(":memory:").write_lock is not connect(":memory:").write_lock


def test_005_drops_stock_and_out_of_scope_products(tmp_path):
    """D62、D63：旧库升级——只卖其他产品的优惠连同套餐删掉，原文标 ignore；其余优惠去掉四类以外的产品类型；库存列与库存变更删掉。"""
    old = tmp_path / "old"
    old.mkdir()
    for version, path in migration_files():
        if version <= 4:
            shutil.copy(path, old / path.name)
    conn = connect(tmp_path / "t.db")
    assert migrate(conn, old) == (0, 4)
    products = {1: ["vps", "mail"], 2: ["mail", "domain"], 3: ["other"], 4: ["dedicated", "colo", "hosting"], 5: [],
                6: ["storage"]}
    for n, types in products.items():
        url = f"https://lowendtalk.com/discussion/{n}"
        _insert_raw(conn, url)
        conn.execute("INSERT INTO deals (id, url, source, title, first_seen_at, last_seen_at, content_hash, updated_at,"
                     " product_types_json, stock_status, stock_source) VALUES (?, ?, 'lowendtalk', 't', 'a', 'a', 'h', 'a',"
                     " ?, 'SOLD_OUT', 'post')", (n, url, json.dumps(types)))
        conn.execute("INSERT INTO plans (deal_id, position, price, stock_status) VALUES (?, 0, 5, 'LIMITED')", (n,))
        conn.execute("INSERT INTO changes (deal_id, changed_at, field, old_value, new_value) VALUES (?, 'a', ?, 'x', 'y')",
                     (n, "stock_status" if n == 1 else "title"))
    assert migrate(conn) == (4, len(migration_files()))

    kept = {r["id"]: json.loads(r["product_types_json"]) for r in conn.execute("SELECT * FROM deals")}
    assert kept == {1: ["vps"], 4: ["dedicated", "hosting"], 5: [], 6: ["storage"]}  # 空的不动
    assert {r[0] for r in conn.execute("SELECT deal_id FROM plans")} == {1, 4, 5, 6}  # 套餐随优惠删除
    assert [(r[0], r[1]) for r in conn.execute("SELECT deal_id, field FROM changes ORDER BY deal_id")] == [
        (4, "title"), (5, "title"), (6, "title")]  # 库存变更删掉，其他保留（优惠 1 只有库存变更）
    kinds = dict(conn.execute("SELECT url, record_kind FROM raw_posts"))
    assert kinds["https://lowendtalk.com/discussion/2"] == kinds["https://lowendtalk.com/discussion/3"] == "ignore"
    assert kinds["https://lowendtalk.com/discussion/1"] is None  # 还在的优惠不改
    deal_cols = {r[1] for r in conn.execute("PRAGMA table_info(deals)")}
    plan_cols = {r[1] for r in conn.execute("PRAGMA table_info(plans)")}
    assert not any("stock" in c for c in deal_cols | plan_cols)


# ---------------------------------------------------------------- 数据目录（prepare_data_dir）


def test_prepare_data_dir_creates_missing_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("RUN_AS_USER", raising=False)
    prepare_data_dir(tmp_path / "a" / "b" / "radar.db")
    assert (tmp_path / "a" / "b").is_dir()
    prepare_data_dir(":memory:")  # 内存库没有目录


@pytest.mark.skipif(os.geteuid() == 0, reason="root 什么目录都能写")
def test_prepare_data_dir_explains_unwritable_directory(tmp_path, monkeypatch):
    """目录不可写时说清楚是哪个目录、谁的权限，而不是等 SQLite 报 unable to open database file。"""
    monkeypatch.delenv("RUN_AS_USER", raising=False)
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o555)
    try:
        with pytest.raises(DataDirError, match=f"{data}.*不可写"):
            prepare_data_dir(data / "radar.db")
    finally:
        data.chmod(0o755)


class FakeRoot:
    """把当前进程装成 root：记录 chown 与降权调用，不真的执行。"""

    def __init__(self, monkeypatch, *, can_regain_root=False):
        self.calls: list[tuple] = []
        self.regain = can_regain_root
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(os, "lchown", lambda path, uid, gid: self.calls.append(("chown", os.path.basename(path), uid, gid)))
        monkeypatch.setattr(os, "setgroups", lambda groups: self.calls.append(("setgroups", tuple(groups))))
        monkeypatch.setattr(os, "setgid", lambda gid: self.calls.append(("setgid", gid)))
        monkeypatch.setattr(os, "setuid", self.setuid)

    def setuid(self, uid):
        if uid == 0 and not self.regain:
            raise PermissionError
        self.calls.append(("setuid", uid))


def test_root_hands_data_directory_to_run_as_user_then_drops_privileges(tmp_path, monkeypatch):
    import pwd
    from collections import namedtuple

    Entry = namedtuple("Entry", "pw_uid pw_gid pw_dir")
    monkeypatch.setattr(pwd, "getpwnam", lambda name: Entry(10001, 10002, "/app") if name == "bfradar" else (_ for _ in ()).throw(KeyError(name)))
    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    (data / "radar.db").write_text("x")
    (data / "backups" / "radar-1.db").write_text("x")
    monkeypatch.setenv("RUN_AS_USER", "bfradar")
    fake = FakeRoot(monkeypatch)
    prepare_data_dir(data / "radar.db")
    chowned = {c[1] for c in fake.calls if c[0] == "chown"}
    assert chowned == {"data", "backups", "radar.db", "radar-1.db"}  # 目录本身、子目录、已有文件都交出去
    assert all(c[2:] == (10001, 10002) for c in fake.calls if c[0] == "chown")
    steps = [c[0] for c in fake.calls]
    assert steps.index("setgid") < steps.index("setuid") and steps.index("setgroups") < steps.index("setgid")
    assert [c for c in fake.calls if c[0] in ("setgid", "setuid")] == [("setgid", 10002), ("setuid", 10001)]
    assert steps.index("setuid") > max(i for i, s in enumerate(steps) if s == "chown")  # 先交出去，再降权


def test_privilege_drop_that_can_regain_root_is_an_error(tmp_path, monkeypatch):
    import pwd
    from collections import namedtuple

    monkeypatch.setattr(pwd, "getpwnam", lambda name: namedtuple("E", "pw_uid pw_gid pw_dir")(10001, 10001, "/app"))
    monkeypatch.setenv("RUN_AS_USER", "bfradar")
    FakeRoot(monkeypatch, can_regain_root=True)
    with pytest.raises(DataDirError, match="降权失败"):
        prepare_data_dir(tmp_path / "radar.db")


def test_unknown_run_as_user_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("RUN_AS_USER", "no-such-user-bfradar")
    FakeRoot(monkeypatch)
    with pytest.raises(DataDirError, match="no-such-user-bfradar"):
        prepare_data_dir(tmp_path / "radar.db")


def test_run_as_user_is_ignored_for_non_root(tmp_path, monkeypatch):
    """非 root 进程（本机开发）即使环境里有 RUN_AS_USER 也不动属主、不降权。"""
    monkeypatch.setenv("RUN_AS_USER", "bfradar")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    called = []
    monkeypatch.setattr(os, "lchown", lambda *a: called.append(a))
    monkeypatch.setattr(os, "setuid", lambda *a: called.append(a))
    prepare_data_dir(tmp_path / "radar.db")
    assert called == []


def test_chown_failure_still_drops_privileges_and_the_writability_check_decides(tmp_path, monkeypatch, caplog):
    import pwd
    from collections import namedtuple

    monkeypatch.setattr(pwd, "getpwnam", lambda name: namedtuple("E", "pw_uid pw_gid pw_dir")(10001, 10001, "/app"))
    monkeypatch.setenv("RUN_AS_USER", "bfradar")
    fake = FakeRoot(monkeypatch)

    def refuse(path, uid, gid):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(os, "lchown", refuse)
    with caplog.at_level("WARNING", logger="bfradar"):
        prepare_data_dir(tmp_path / "radar.db")  # 目录对（这里是测试进程自己的）仍可写：不报错
    assert ("setuid", 10001) in fake.calls
    assert "无法把数据目录" in caplog.text and "Read-only file system" in caplog.text
