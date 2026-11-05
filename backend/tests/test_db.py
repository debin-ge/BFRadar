import json
import shutil
import sqlite3
import threading

import pytest

from app.db import MIGRATIONS, Database, connect, migrate, migration_files, schema_version, transaction


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
