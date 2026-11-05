"""社区讨论标题的机器翻译（《优化方案 v1.0》O-14，D67）。

英文站的社区栏原来是一排中文标题（NodeSeek），中文站里 Reddit 的标题是英文。只翻侧栏与商家页会用到的那部分：
本季、命中黑五词的情报，最新的优先；中文帖译成英文填 title_en，英文帖译成中文填 title_zh。

一次送 BATCH 个标题，要求按原顺序返回同样条数的 JSON 数组。标题里没有可以核对的事实（不像优惠有价格、优惠码），
不做反幻觉校验，只查条数、空值与长度（译文不超过原文的 MAX_RATIO 倍，防止模型借题发挥；一个汉字常译成四五个
英文字母，中译英放宽）；对不上就整批作废，
这批标题记一次失败，满 MAX_FAILED 次不再试。调度器在 LLM 队列之后顺带跑一轮（每轮至多 PER_RUN 条）。
"""

import json
import logging
import sqlite3
from dataclasses import dataclass, field

from .config import Settings
from .db import Database, transaction
from .extract.llm import LlmClient, LlmError
from .web import fmt

log = logging.getLogger(__name__)

BATCH = 20
PER_RUN = 60
MAX_RATIO = {"zh": 6, "en": 3}  # 原文语言 → 译文最多是原文字数的几倍（另加 20 字余量）
MAX_FAILED = 3
TARGET = {"zh": ("title_en", "英文"), "en": ("title_zh", "简体中文")}  # 原文语言 → (译名列, 目标语言)


@dataclass
class TranslateReport:
    candidates: int = 0
    calls: int = 0
    done: int = 0
    failed: int = 0  # 作废的批次里的标题数
    tokens: int = 0
    errors: list[str] = field(default_factory=list)


def candidates(conn: sqlite3.Connection, season_start: str, limit: int | None = None) -> list[sqlite3.Row]:
    """待翻译的情报：本季、黑五、还没有另一种语言的标题、失败未满 MAX_FAILED 次；最新的优先。"""
    return conn.execute(
        "SELECT id, title, lang FROM intel WHERE is_black_friday = 1 AND published_at >= ? AND translate_failed < ?"
        " AND ((lang = 'zh' AND title_en IS NULL) OR (lang = 'en' AND title_zh IS NULL))"
        " ORDER BY published_at DESC, id DESC LIMIT ?", (season_start, MAX_FAILED, -1 if limit is None else limit),
    ).fetchall()


def build_messages(titles: list[str], target: str) -> list[dict]:
    system = (f"把用户给出的论坛帖子标题逐条翻译成{target}。只翻译，不解释、不补充、不合并；商家名、产品型号、价格、"
              "优惠码与网址照原样保留。只输出一个 JSON 对象：{\"titles\": [译文, …]}，条数与顺序与输入完全一致。")
    return [{"role": "system", "content": system},
            {"role": "user", "content": json.dumps({"titles": titles}, ensure_ascii=False)}]


def check(titles: list[str], data: dict, lang: str) -> list[str] | None:
    """译文与原文逐条对齐、都不为空、都不过长时返回译文，否则 None（整批作废）。"""
    out = data.get("titles") if isinstance(data, dict) else None
    if not isinstance(out, list) or len(out) != len(titles):
        return None
    out = [x.strip() if isinstance(x, str) else "" for x in out]
    if any(not x or len(x) > MAX_RATIO[lang] * len(src) + 20 for x, src in zip(out, titles)):
        return None
    return out


def run_translate(db: Database, client: LlmClient, settings: Settings, *, limit: int | None = PER_RUN) -> TranslateReport:
    conn = db.conn()
    rows = candidates(conn, fmt.season_start_iso(settings.season_start), limit)
    report = TranslateReport(candidates=len(rows))
    for lang, (column, target) in TARGET.items():
        group = [r for r in rows if r["lang"] == lang]
        for i in range(0, len(group), BATCH):
            batch = group[i:i + BATCH]
            titles = [r["title"] for r in batch]
            report.calls += 1
            try:
                resp = client.complete(build_messages(titles, target))
                report.tokens += (resp.usage or {}).get("total_tokens") or 0
                out = check(titles, resp.data, lang)
            except LlmError as e:  # 服务不可用：这一轮到此为止，不记失败，下轮再试
                report.errors.append(str(e))
                log.warning("标题翻译调用失败：%s", e)
                return report
            with transaction(conn):
                if out is None:
                    conn.executemany("UPDATE intel SET translate_failed = translate_failed + 1 WHERE id = ?",
                                     [(r["id"],) for r in batch])
                    report.failed += len(batch)
                    log.warning("标题翻译结果对不上，%d 条作废", len(batch))
                else:
                    conn.executemany(f"UPDATE intel SET {column} = ? WHERE id = ?",
                                     [(x, r["id"]) for x, r in zip(out, batch)])
                    report.done += len(batch)
    return report
