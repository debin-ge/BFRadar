"""流水线编排（《需求与方案说明》第 10.1、12、13 节，D22、D23、D25、D46–D48）。

一轮采集（crawl → crawl_source，每个源独立，任一源失败不影响其他源，FR-01）：
  1. 抓取并入库原文（一个事务）。
  2. 处理新帖、原文有变化的帖子与库里还缺对应记录的帖子：分类 → 优惠走规则抽取，与库里已有的 LLM 输出校验合并，
     人民币估算、变更检测后写库；情报写 intel；记录类型变了的删掉旧记录。每条一个事务，单条出错只记入
     统计。原文未变又在 feed 中看到的优惠只刷新"最后看到"时间（D23）。
  3. 写运行记录。
LLM 不在采集轮里同步调用（每条要几十秒，会拖住采集）：run_llm_queue 处理待调用的优惠（最新发布的优先），并发
LLM_CONCURRENCY；调用在事务之外进行，结果回来后在事务里核对原文未变再合并写库，运行记录记在 source = "llm" 名下。
命令行 crawl 在采集后跑一次队列，调度器（M1.13）把它作为独立任务；backfill=True 处理全部未完成的记录。
规则或分类改进后，未变化的帖子不会自动重算，用 reprocess 重算。
"""

import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .config import Settings
from .db import Database, transaction
from .extract.classify import Classification, classify
from .extract.llm import (MAX_POST_ATTEMPTS, LlmClient, LlmError, LlmState, build_messages, llm_after, llm_due,
                          rules_hint)
from .extract.merge import build_deal
from .extract.rules import extract
from .extract.validate import Merged, validate_and_merge
from .fx import apply_fx, latest_rates
from .models import Intel, IntelCategory, LlmStatus, RecordKind, iso, utcnow
from .repo import (DealSaved, drop_records, finish_run, last_source_state, record_source, save_deal, save_intel,
                   save_raw_posts, start_run, touch_deals)
from .sources import Source
from .textutil import Link
from .vendors import AliasIndex, build_index, link_intel, match_vendors

log = logging.getLogger(__name__)

LLM_SOURCE = "llm"  # LLM 队列的运行记录记在这个名下，不占用采集源的 state（last_source_state）
TODO_SQL = """
SELECT r.url FROM raw_posts r
LEFT JOIN deals d ON d.url = r.url
LEFT JOIN intel i ON i.url = r.url
WHERE r.source = ? AND (r.record_kind IS NULL OR (r.record_kind = 'offer' AND d.id IS NULL)
                         OR (r.record_kind = 'intel' AND i.id IS NULL))
ORDER BY r.published_at DESC
"""


@dataclass
class SourceReport:
    source: str
    fetched: int = 0
    new: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    kinds: Counter = field(default_factory=Counter)  # 新帖与原文有变化的帖子的记录类型
    deals: Counter = field(default_factory=Counter)  # created / updated / changes
    intel: int = 0  # 新增情报
    changed_urls: list[str] = field(default_factory=list)  # 新增或有变化的优惠，按 URL 清缓存用（D28）
    post_errors: list[str] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------- 单条处理


def _links(row) -> list[Link]:
    return [Link(*lk) for lk in json.loads(row["links_json"])]


def classify_row(row) -> Classification:
    return classify(row["source"], row["title"], row["text_struct"], json.loads(row["categories_json"]), row["author"])


def merge_offer(conn: sqlite3.Connection, row, c: Classification, settings: Settings, now: str, *,
                llm: tuple[LlmState, dict | None] | None = None) -> tuple[DealSaved | None, Merged | None]:
    """一条优惠：规则抽取 → 与 LLM 输出校验合并 → 组装 → 人民币估算 → 写库。调用方负责事务。

    LLM 判定帖子卖的只是 VPS、独服、虚拟主机、存储以外的产品时不收（D62）：删掉已有的优惠，原文标为 ignore，
    返回的写库结果为 None。

    llm 为 None 时按库里的 LLM 状态决定：同一内容已抽取过就用存下的输出重新合并（D46），否则记为待调用或跳过；
    LLM 队列传入本次调用后的（状态, 输出）。
    """
    links = _links(row)
    ex = extract(row["source"], row["title"], row["text_struct"], links, row["author"], c.lang, bool(row["has_table"]),
                 row["published_at"])
    if llm is None:
        prev = conn.execute("SELECT llm_status, llm_attempts, llm_hash, llm_output_json FROM deals WHERE url = ?",
                            (row["url"],)).fetchone()
        state = LlmState()
        if prev and not (prev["llm_status"] == LlmStatus.DONE and prev["llm_output_json"] is None):
            state = LlmState(LlmStatus(prev["llm_status"]), prev["llm_attempts"], prev["llm_hash"])
        state = llm_due(state, row["content_hash"], settings.llm_enabled)
        data = json.loads(prev["llm_output_json"]) if state.status is LlmStatus.DONE else None
    else:
        state, data = llm
    merged = None
    if data is not None:
        merged = validate_and_merge(data, title=row["title"], text=row["text_struct"], links=links,
                                    author=row["author"], lang=c.lang, rules=ex, is_black_friday=c.is_black_friday,
                                    event_type=c.event_type, product_types=c.product_types,
                                    published=row["published_at"])
    if merged is not None and not merged.product_types:  # 记下判定时的内容哈希：内容没变就不再按规则收回来（E18）
        conn.execute("UPDATE raw_posts SET record_kind = ?, out_of_scope_hash = ? WHERE url = ?",
                     (str(RecordKind.IGNORE), row["content_hash"], row["url"]))
        drop_records(conn, row["url"], keep=str(RecordKind.IGNORE))
        return None, merged
    deal = build_deal(row, c, ex, merged, llm=state, llm_output=data)
    apply_fx(deal, latest_rates(conn, static="static" in settings.fx_providers))
    return save_deal(conn, deal, now=now), merged


def process_post(conn: sqlite3.Connection, row, settings: Settings, now: str,
                 index: AliasIndex | None = None) -> tuple[RecordKind, DealSaved | None, bool]:
    """按记录类型处理一条一手原文，返回（类型, 优惠写库结果, 是否新增情报）。情报顺带匹配提到的商家（第 12.7 节）。
    调用方负责事务。"""
    c = classify_row(row)
    kind = c.record_kind
    if kind is RecordKind.OFFER and row["out_of_scope_hash"] and row["out_of_scope_hash"] == row["content_hash"]:
        kind = RecordKind.IGNORE  # LLM 判过不在收录范围、原帖内容没变：沿用判定，不按规则收回来（E18）
    if row["record_kind"] != kind:
        conn.execute("UPDATE raw_posts SET record_kind = ? WHERE url = ?", (str(kind), row["url"]))
    drop_records(conn, row["url"], keep=str(kind))
    saved, new_intel = None, False
    if kind is RecordKind.OFFER:
        saved, _ = merge_offer(conn, row, c, settings, now)
        if saved is None:  # LLM 输出表明产品不在收录范围内
            kind = RecordKind.IGNORE
    elif kind is RecordKind.INTEL:
        intel_id, new_intel = save_intel(conn, Intel(
            url=row["url"], source=row["source"], title=row["title"], category=c.intel_category or IntelCategory.OTHER,
            lang=c.lang, published_at=row["published_at"], first_seen_at=row["fetched_at"],
            is_black_friday=c.is_black_friday))
        link_intel(conn, intel_id, match_vendors(index or build_index(conn), row["title"], row["text_struct"]))
    return kind, saved, new_intel


def _process_urls(conn: sqlite3.Connection, urls: Iterable[str], settings: Settings, now: str, report: SourceReport,
                  count_kinds: set[str]) -> None:
    index = None  # 别名索引每轮建一次（第一次遇到情报时）
    for url in urls:
        try:
            with transaction(conn):
                row = conn.execute("SELECT * FROM raw_posts WHERE url = ?", (url,)).fetchone()
                if index is None and row["source"] not in ("telegram", "official"):  # 这两个源只出优惠
                    index = build_index(conn)
                kind, saved, new_intel = process_post(conn, row, settings, now, index)
        except Exception as e:  # 单条出错不影响同一轮的其他帖子
            log.exception("处理失败：%s", url)
            report.post_errors.append(f"{url}: {type(e).__name__}: {e}")
            continue
        if url in count_kinds:
            report.kinds[str(kind)] += 1
        report.intel += new_intel
        if saved:
            report.deals.update({"created": int(saved.created), "updated": int(saved.updated and not saved.created),
                                 "changes": len(saved.changes)})
            report.deals = +report.deals  # 去掉为 0 的项
            if saved.updated:
                report.changed_urls.append(url)


# ---------------------------------------------------------------- 采集轮


def crawl_source(db: Database, run_id: int, source: Source) -> SourceReport:
    """抓取、入库并处理一个源。抓取或入库出错只记入该源的运行记录，不影响其他源（FR-01）。"""
    conn = db.conn()
    started_at = iso(utcnow())
    report = SourceReport(source.id)
    try:
        result = source.fetch(last_source_state(conn, source.id))
        with transaction(conn):
            saved = save_raw_posts(conn, result.posts, fetched_at=started_at)
        report.fetched, report.new, report.updated, report.stats = len(result.posts), saved.new, saved.updated, result.stats
        with transaction(conn):
            touch_deals(conn, saved.unchanged, started_at)
        urls = [r["url"] for r in conn.execute(TODO_SQL, (source.id,))]
        _process_urls(conn, urls, source.settings, started_at, report, set(saved.new) | set(saved.updated))
    except Exception as e:
        log.exception("%s 采集失败", source.id)
        report.error = f"{type(e).__name__}: {e}"
    pipeline_stats = {"deals": dict(report.deals), "intel": report.intel, "post_errors": report.post_errors[:5]}
    with transaction(conn):
        record_source(conn, run_id, source.id, started_at=started_at, fetched=report.fetched,
                      new_items=len(report.new), updated=len(report.updated), error=report.error,
                      extra={**report.stats, "pipeline": pipeline_stats})
    log.info("%s 抓取 %d 新增 %d 更新 %d 分类 %s 优惠 %s 情报 +%d%s%s", source.id, report.fetched, len(report.new),
             len(report.updated), dict(report.kinds), dict(report.deals), report.intel,
             f" 单条出错 {len(report.post_errors)}" if report.post_errors else "",
             f" 错误 {report.error}" if report.error else "")
    return report


def crawl(db: Database, sources: Iterable[Source], reason: str) -> tuple[int, list[SourceReport]]:
    """一轮采集（启动、手动刷新、CLI 或调度器单源任务），每个源依次执行。"""
    conn = db.conn()
    with transaction(conn):
        run_id = start_run(conn, reason)
    reports = [crawl_source(db, run_id, source) for source in sources]
    with transaction(conn):
        finish_run(conn, run_id, ok=all(r.error is None for r in reports))
    return run_id, reports


def reprocess(db: Database, settings: Settings, sources: Iterable[str] | None = None) -> SourceReport:
    """规则、分类或校验改进后重算库里的原文：不抓取、不调用 LLM，已有的 LLM 输出照常合并。"""
    conn = db.conn()
    wanted = list(sources or settings.sources)
    report = SourceReport("reprocess")
    if not wanted:
        return report
    urls = [r["url"] for r in conn.execute(
        f"SELECT url FROM raw_posts WHERE source IN ({', '.join('?' * len(wanted))}) ORDER BY published_at DESC",
        wanted)]
    report.fetched = len(urls)
    _process_urls(conn, urls, settings, iso(utcnow()), report, set(urls))
    return report


# ---------------------------------------------------------------- LLM 队列


@dataclass
class LlmReport:
    candidates: int = 0  # 本次要调用的优惠数
    calls: int = 0
    done: int = 0
    failed: int = 0
    stale: int = 0  # 调用期间原帖又变了（或不再是优惠），结果作废，下一轮重来
    units: int = 0  # 校验单元
    rejected: int = 0  # 被拒单元
    tokens: Counter = field(default_factory=Counter)
    changed_urls: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def llm_candidates(conn: sqlite3.Connection, *, backfill: bool = False, limit: int | None = None) -> list[str]:
    """待调用 LLM 的优惠，最新发布的优先：pending 与未满重试次数的 failed；backfill 时为全部未完成的（D33、D44）。"""
    if backfill:
        sql, params = "SELECT url FROM deals WHERE llm_status != 'done'", []
    else:
        sql = "SELECT url FROM deals WHERE llm_status = 'pending' OR (llm_status = 'failed' AND llm_attempts < ?)"
        params = [MAX_POST_ATTEMPTS]
    sql += " ORDER BY published_at DESC, id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [r["url"] for r in conn.execute(sql, params)]


def _call_llm(client: LlmClient, row: dict) -> tuple[dict | None, dict, LlmError | None]:
    """工作线程：重做规则抽取作为提示并调用 LLM，不碰数据库。"""
    try:
        links = _links(row)
        c = classify_row(row)
        ex = extract(row["source"], row["title"], row["text_struct"], links, row["author"], c.lang,
                     bool(row["has_table"]), row["published_at"])
        resp = client.complete(build_messages(row["source"], row["published_at"], row["title"], row["text_struct"],
                                              links, rules_hint(ex)))
        return resp.data, resp.usage, None
    except LlmError as e:
        return None, {}, e
    except Exception as e:  # 意外错误按服务故障处理（不计入帖子的失败次数），队列继续
        log.exception("LLM 调用意外失败：%s", row["url"])
        return None, {}, LlmError(f"{type(e).__name__}: {e}")


def _save_llm_result(conn: sqlite3.Connection, row: dict, state: LlmState, data: dict | None, settings: Settings,
                     report: LlmReport) -> None:
    url = row["url"]
    try:
        with transaction(conn):
            current = conn.execute("SELECT * FROM raw_posts WHERE url = ?", (url,)).fetchone()
            c = classify_row(current) if current and current["content_hash"] == row["content_hash"] else None
            if c is None or c.record_kind is not RecordKind.OFFER:
                report.stale += 1
                return
            saved, merged = merge_offer(conn, current, c, settings, iso(utcnow()), llm=(state, data))
    except Exception as e:
        log.exception("LLM 结果写库失败：%s", url)
        report.errors.append(f"{url}: {type(e).__name__}: {e}")
        return
    if state.status is LlmStatus.DONE:
        report.done += 1
    else:
        report.failed += 1
    if merged:
        report.units += merged.llm_fields
        report.rejected += merged.llm_rejected
    if saved is None or saved.updated:  # 不收的优惠被删掉，页面缓存也要换
        report.changed_urls.append(url)


def run_llm_queue(db: Database, client: LlmClient, settings: Settings, *, limit: int | None = None,
                  backfill: bool = False) -> LlmReport:
    """调用 LLM 处理待处理的优惠（D22），每条结果回来就写库；本次运行记在 source = "llm" 名下。
    队列为空时不记运行（调度器每 2 分钟查一次，空轮次会淹没运行记录，D54）。"""
    conn = db.conn()
    started_at = iso(utcnow())
    report = LlmReport()
    jobs: list[tuple[dict, LlmState]] = []
    for url in llm_candidates(conn, backfill=backfill, limit=limit):
        row = conn.execute("SELECT * FROM raw_posts WHERE url = ?", (url,)).fetchone()
        deal = conn.execute("SELECT llm_status, llm_attempts, llm_hash FROM deals WHERE url = ?", (url,)).fetchone()
        if row is None or deal is None:
            continue
        state = LlmState(LlmStatus(deal["llm_status"]), deal["llm_attempts"], deal["llm_hash"])
        due = llm_due(state, row["content_hash"], True, backfill=backfill)
        if due.status is LlmStatus.PENDING:
            jobs.append((dict(row), due))
    report.candidates = len(jobs)
    if not jobs:
        return report
    with ThreadPoolExecutor(max_workers=settings.llm_concurrency) as pool:
        futures = {pool.submit(_call_llm, client, row): (row, due) for row, due in jobs}
        for i, future in enumerate(as_completed(futures), 1):
            row, due = futures[future]
            data, usage, error = future.result()
            report.calls += 1
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                report.tokens[key] += usage.get(key) or 0
            if error:
                report.errors.append(f"{row['url']}: {error}")
            log.info("LLM %d/%d %s：%s", i, len(jobs), row["url"], f"失败 {error}" if error else "完成")
            _save_llm_result(conn, row, llm_after(due, row["content_hash"], error), data, settings, report)
    with transaction(conn):
        run_id = start_run(conn, "llm-backfill" if backfill else "llm")
        record_source(conn, run_id, LLM_SOURCE, started_at=started_at, fetched=report.candidates, updated=report.done,
                      llm_calls=report.calls, llm_rejected=report.rejected,
                      extra={"failed": report.failed, "stale": report.stale, "units": report.units,
                             "tokens": dict(report.tokens), "errors": report.errors[:5]})
        finish_run(conn, run_id, ok=not (report.calls and report.done == 0))
    return report
