"""数据访问：原文入库（raw_posts）、优惠与套餐（deals / plans / changes）、运行记录（runs / run_sources）。

新帖判定只看规范化 URL 是否已在库中（D23）；一个原帖 URL 只有一条优惠；变更检测的口径见 app/extract/merge.py（D46）。
"""

import json
import sqlite3
from dataclasses import dataclass, field, fields

from .extract.merge import Change, ChangeFlag, diff, flags
from .models import (Deal, DiskType, EventType, ExtractedBy, Intel, LlmStatus, Period, Plan, PlanFormat, PriceNote,
                     ProductType, RawPost, Recurring, iso, utcnow)
from .textutil import content_hash, structure_html
from .vendors import resolve_vendor


@dataclass
class SaveStats:
    new: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def save_raw_posts(conn: sqlite3.Connection, posts: list[RawPost], *, fetched_at: str) -> SaveStats:
    """写入一批原文，调用方负责事务。

    - 未见过的 URL：插入，记为新帖。
    - 再次出现：内容哈希变化则更新原文并清空 record_kind（待重新分类），否则只刷新 fetched_at。
      fetched_at 即"最后一次在 feed 中看到"的时间（D23）。
    """
    stats = SaveStats()
    for post in posts:
        st = structure_html(post.content_html)
        digest = content_hash(post.title, st.text)
        row = conn.execute("SELECT content_hash, extra_json FROM raw_posts WHERE url = ?", (post.url,)).fetchone()
        has_table = int("<td" in post.content_html.lower())  # 与金标评估的判定一致；结构化文本里表格与竖线同形
        content = {
            "source": post.source, "title": post.title, "author": post.author, "published_at": iso(post.published_at),
            "categories_json": _dumps(post.categories), "text_struct": st.text, "links_json": _dumps(st.links),
            "content_hash": digest, "has_table": has_table,
        }
        if row is None:
            _insert(conn, post.url, fetched_at, content, post.extra)
            stats.new.append(post.url)
            continue
        extra = json.loads(row["extra_json"]) | post.extra
        extra_changed = _dumps(extra) != row["extra_json"]
        if row["content_hash"] != digest or extra_changed:
            _update_content(conn, post.url, fetched_at, content, extra)
            stats.updated.append(post.url)
        else:  # has_table 顺带补上 004 迁移前入库的记录
            conn.execute("UPDATE raw_posts SET fetched_at = ?, has_table = ? WHERE url = ?", (fetched_at, has_table, post.url))
            stats.unchanged.append(post.url)
    return stats


def _insert(conn: sqlite3.Connection, url: str, fetched_at: str, content: dict, extra: dict) -> None:
    cols = {"url": url, "fetched_at": fetched_at, **content, "extra_json": _dumps(extra)}
    conn.execute(f"INSERT INTO raw_posts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(cols.values()))


def _update_content(conn: sqlite3.Connection, url: str, fetched_at: str, content: dict, extra: dict) -> None:
    cols = {"fetched_at": fetched_at, **content, "extra_json": _dumps(extra), "record_kind": None}
    conn.execute(f"UPDATE raw_posts SET {', '.join(f'{c} = ?' for c in cols)} WHERE url = ?", (*cols.values(), url))


# ---------------------------------------------------------------- 优惠与套餐

DEAL_JSON = {"sources", "product_types", "coupon_codes", "specs_range", "price_list", "llm_output", "tracked"}
DEAL_ENUMS = {"event_type": EventType, "recurring": Recurring, "plan_format": PlanFormat, "extracted_by": ExtractedBy,
              "llm_status": LlmStatus, "min_price_period": Period}
PLAN_ENUMS = {"disk_type": DiskType, "period": Period, "price_note": PriceNote, "extracted_by": ExtractedBy}
SEEN = ("last_seen_at",)  # 每次在 feed 中看到都会刷新，不算记录更新


@dataclass
class DealSaved:
    deal_id: int
    created: bool
    updated: bool  # 新建，或库中字段与套餐（不含"最后看到"时间与比对基准）有变化
    changes: list[Change]


def _column(value):
    if isinstance(value, bool):
        return int(value)
    return str(value) if isinstance(value, str) else value  # 枚举存取值


def _deal_columns(deal: Deal, vendor_id: int | None) -> dict:
    cols: dict = {}
    for f in fields(Deal):
        if f.name in ("id", "plans", "vendor"):
            continue
        value = getattr(deal, f.name)
        if f.name in DEAL_JSON:
            cols[f"{f.name}_json"] = None if value is None else _dumps(value)
        else:
            cols[f.name] = _column(value)
    cols["vendor_id"] = vendor_id
    return cols


def _plan_columns(plan: Plan) -> dict:
    cols = {f.name: _column(getattr(plan, f.name)) for f in fields(Plan)
            if f.name not in ("id", "deal_id", "locations", "net")}
    cols["locations_json"] = _dumps(plan.locations)
    cols["net_json"] = _dumps(plan.net)
    return cols


def _insert_row(conn: sqlite3.Connection, table: str, cols: dict) -> int:
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    return conn.execute(sql, tuple(cols.values())).lastrowid


def _write_plans(conn: sqlite3.Connection, deal_id: int, plans: list[dict]) -> None:
    conn.execute("DELETE FROM plans WHERE deal_id = ?", (deal_id,))
    for plan in plans:
        _insert_row(conn, "plans", {"deal_id": deal_id, **plan})


def _stored_plans(conn: sqlite3.Connection, deal_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM plans WHERE deal_id = ? ORDER BY position", (deal_id,)).fetchall()


def save_deal(conn: sqlite3.Connection, deal: Deal, *, now: str) -> DealSaved:
    """写入一条优惠及其套餐，调用方负责事务（第 12.6 节）。

    新 URL 插入。已有记录先与上次保存的比对基准求变更（merge.diff）写入 changes；字段或套餐有变化才整体改写并
    刷新 updated_at，否则只刷新"最后看到"时间与比对基准。
    """
    cols = _deal_columns(deal, resolve_vendor(conn, deal.vendor))
    plans = [_plan_columns(p) for p in deal.plans]
    old = conn.execute("SELECT * FROM deals WHERE url = ?", (deal.url,)).fetchone()
    if old is None:
        cols.update(first_seen_at=deal.first_seen_at or now, updated_at=now)
        deal_id = _insert_row(conn, "deals", cols)
        _write_plans(conn, deal_id, plans)
        return DealSaved(deal_id, True, True, [])
    deal_id = old["id"]
    del cols["first_seen_at"], cols["updated_at"]
    old_plans = [{k: r[k] for k in r.keys() if k not in ("id", "deal_id")} for r in _stored_plans(conn, deal_id)]
    updated = plans != old_plans or any(cols[k] != old[k] for k in cols if k not in (*SEEN, "tracked_json"))
    if updated:
        cols["updated_at"] = now
        conn.execute(f"UPDATE deals SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?", (*cols.values(), deal_id))
        _write_plans(conn, deal_id, plans)
    else:
        keep = {k: cols[k] for k in (*SEEN, "tracked_json")}
        conn.execute(f"UPDATE deals SET {', '.join(f'{k} = ?' for k in keep)} WHERE id = ?", (*keep.values(), deal_id))
    changes = diff(json.loads(old["tracked_json"]), deal.tracked)
    for ch in changes:
        conn.execute("INSERT INTO changes (deal_id, changed_at, field, old_value, new_value) VALUES (?, ?, ?, ?, ?)",
                     (deal_id, now, ch.field, ch.old, ch.new))
    return DealSaved(deal_id, False, updated, changes)


def _from_row(cls, row: sqlite3.Row, json_fields: set[str], enums: dict, skip: set[str]):
    values = {}
    for f in fields(cls):
        if f.name in skip:
            continue
        if f.name in json_fields:
            raw = row[f"{f.name}_json"]
            values[f.name] = None if raw is None else json.loads(raw)
        else:
            value = row[f.name]
            values[f.name] = enums[f.name](value) if f.name in enums and value is not None else value
    return cls(**values)


def load_deal(conn: sqlite3.Connection, url: str) -> Deal | None:
    """按原帖 URL 读取一条优惠及其套餐；vendor 为商家标准名。"""
    row = conn.execute("SELECT d.*, v.name AS vendor FROM deals d LEFT JOIN vendors v ON v.id = d.vendor_id"
                       " WHERE d.url = ?", (url,)).fetchone()
    if row is None:
        return None
    deal = _from_row(Deal, row, DEAL_JSON, DEAL_ENUMS, {"plans"})
    deal.is_black_friday = bool(deal.is_black_friday)
    deal.product_types = [ProductType(p) for p in deal.product_types]
    deal.plans = [plan_from_row(r) for r in _stored_plans(conn, deal.id)]
    return deal


def plan_from_row(row: sqlite3.Row) -> Plan:
    return _from_row(Plan, row, {"locations", "net"}, PLAN_ENUMS, set())


def touch_deals(conn: sqlite3.Connection, urls: list[str], seen_at: str) -> None:
    """原文未变、又在 feed 中看到：只刷新"最后看到"（D23）。调用方负责事务。"""
    conn.executemany("UPDATE deals SET last_seen_at = ? WHERE url = ?", [(seen_at, url) for url in urls])


def drop_records(conn: sqlite3.Connection, url: str, *, keep: str | None) -> None:
    """原帖的记录类型变了：删掉不再对应的优惠（连同套餐与变更）或情报。调用方负责事务。"""
    if keep != "offer":
        conn.execute("DELETE FROM deals WHERE url = ?", (url,))
    if keep != "intel":
        conn.execute("DELETE FROM intel WHERE url = ?", (url,))


def save_intel(conn: sqlite3.Connection, intel: Intel) -> tuple[int, bool]:
    """按 URL 写入一条情报，返回（id, 是否新建）；商家关联由调用方用 vendors.link_intel 写。调用方负责事务。"""
    cols = {"source": intel.source, "category": str(intel.category), "title": intel.title, "lang": intel.lang,
            "published_at": intel.published_at, "is_black_friday": int(intel.is_black_friday)}
    if row := conn.execute("SELECT id, title FROM intel WHERE url = ?", (intel.url,)).fetchone():
        if row["title"] != intel.title:  # 标题改了：旧译名作废，重新翻译（O-14）
            cols.update(title_zh=None, title_en=None, translate_failed=0)
        conn.execute(f"UPDATE intel SET {', '.join(f'{k} = ?' for k in cols)} WHERE url = ?", (*cols.values(), intel.url))
        return row["id"], False
    return _insert_row(conn, "intel", {"url": intel.url, **cols, "first_seen_at": intel.first_seen_at}), True


def latest_runs(conn: sqlite3.Connection, per_source: int = 20) -> dict[str, list[sqlite3.Row]]:
    """各源最近 per_source 轮（新的在前）；状态页与统计只看最近几轮，不扫全表。"""
    out: dict[str, list[sqlite3.Row]] = {}
    for r in conn.execute(
        "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY source ORDER BY started_at DESC, run_id DESC) AS rn"
        " FROM run_sources) WHERE rn <= ? ORDER BY source, rn", (per_source,),
    ):
        out.setdefault(r["source"], []).append(r)
    return out


def site_stats(conn: sqlite3.Connection, *, season_start: str, since: str) -> dict:
    """总数与分组统计（命令行 stats、/api/stats、/healthz）。since 之后首次看到的优惠计为新增。"""
    def one(sql: str, *params) -> int:
        return conn.execute(sql, params).fetchone()[0]

    last_ok = dict(conn.execute("SELECT source, MAX(started_at) FROM run_sources WHERE error IS NULL GROUP BY source"))
    runs: dict[str, dict] = {}
    for source, rows in latest_runs(conn).items():
        failing = next((i for i, r in enumerate(rows) if r["error"] is None), len(rows))
        runs[source] = {"last_at": rows[0]["started_at"], "last_ok_at": last_ok.get(source), "failing": failing,
                        "fetched": rows[0]["fetched"], "new": rows[0]["new_items"], "error": rows[0]["error"]}
    fx = conn.execute("SELECT date, provider, fetched_at FROM fx_rates ORDER BY date DESC LIMIT 1").fetchone()
    return {
        "deals": {"total": one("SELECT COUNT(*) FROM deals"),
                  "season": one("SELECT COUNT(*) FROM deals WHERE published_at >= ?", season_start),
                  "bf": one("SELECT COUNT(*) FROM deals WHERE is_black_friday = 1 AND published_at >= ?",
                            season_start),
                  "new": one("SELECT COUNT(*) FROM deals WHERE first_seen_at >= ?", since)},
        "plans": one("SELECT COUNT(*) FROM plans"),
        "intel": {"total": one("SELECT COUNT(*) FROM intel"),
                  "bf": one("SELECT COUNT(*) FROM intel WHERE is_black_friday = 1")},
        "vendors": one("SELECT COUNT(*) FROM vendors"),
        "raw": {f"{r[0]}/{r[1] or '-'}": r[2] for r in conn.execute(
            "SELECT source, record_kind, COUNT(*) FROM raw_posts GROUP BY 1, 2 ORDER BY 1, 2")},
        "llm": {r[0]: r[1] for r in conn.execute("SELECT llm_status, COUNT(*) FROM deals GROUP BY 1")},
        "fx": dict(fx) if fx else None,
        "runs": runs,
    }


def deal_changes(conn: sqlite3.Connection, deal_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT changed_at, field, old_value, new_value FROM changes WHERE deal_id = ?"
                        " ORDER BY changed_at, id", (deal_id,)).fetchall()


def recent_flags(conn: sqlite3.Connection, since: str) -> dict[int, set[ChangeFlag]]:
    """since 之后有变更的优惠 → "降价 / 新码"标记（第 12.6 节，页面取最近 24 小时）。"""
    rows = conn.execute(
        "SELECT c.deal_id, c.field, c.old_value, c.new_value, d.coupon_codes_json"
        " FROM changes c JOIN deals d ON d.id = c.deal_id WHERE c.changed_at >= ? ORDER BY c.id", (since,),
    ).fetchall()
    by_deal: dict[int, tuple[sqlite3.Row, list[Change]]] = {}
    for r in rows:
        by_deal.setdefault(r["deal_id"], (r, []))[1].append(Change(r["field"], r["old_value"], r["new_value"]))
    out = {}
    for deal_id, (r, changes) in by_deal.items():
        if found := flags(changes, coupon_codes=json.loads(r["coupon_codes_json"])):
            out[deal_id] = found
    return out


# ---------------------------------------------------------------- 运行记录


def start_run(conn: sqlite3.Connection, reason: str) -> int:
    return conn.execute("INSERT INTO runs (started_at, reason) VALUES (?, ?)", (iso(utcnow()), reason)).lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, ok: bool) -> None:
    conn.execute("UPDATE runs SET finished_at = ?, ok = ? WHERE id = ?", (iso(utcnow()), int(ok), run_id))


def record_source(conn: sqlite3.Connection, run_id: int, source: str, *, started_at: str, fetched: int = 0,
                  new_items: int = 0, updated: int = 0, llm_calls: int = 0, llm_rejected: int = 0,
                  error: str | None = None, extra: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO run_sources (run_id, source, started_at, finished_at, fetched, new_items, updated, llm_calls,"
        " llm_rejected, error, extra_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, source, started_at, iso(utcnow()), fetched, new_items, updated, llm_calls, llm_rejected, error,
         _dumps(extra or {})),
    )


def last_source_state(conn: sqlite3.Connection, source: str) -> dict:
    """该源最近一次成功运行的 stats，作为下一轮 fetch() 的 state（如 NodeSeek 的 max_id）。"""
    row = conn.execute(
        "SELECT extra_json FROM run_sources WHERE source = ? AND error IS NULL ORDER BY started_at DESC, run_id DESC"
        " LIMIT 1", (source,),
    ).fetchone()
    return json.loads(row["extra_json"]) if row else {}
