"""合并与变更检测（《需求与方案说明》第 12.6 节，D46、D63）。

build_deal 把一条优惠原文的规则层结果与 LLM 校验合并结果（validate.py；LLM 未处理时为 None）组装成 Deal。
LLM 与规则的取舍已在 validate.py 完成，这里只负责组装。

变更检测（D46）：每次保存都存一份比对基准 tracked()，与上次保存的基准逐字段比较（diff）：
  - 原帖内容（内容哈希）没变时不记变更。LLM 补抽完成、提示词升级、规则改进只改变抽取结果，不是商家改了帖子。
  - 折扣、优惠码要展示值与规则层值都变了才记：LLM 对同一帖子两次抽取可能给出不同说法，规则层是确定的，
    用它确认原文确实变了；记录里写展示值，与页面一致。
  - 套餐价格只比规则层套餐，按名称、配置、币种、周期与机房对齐；同一键出现多次的不比。
最近 24 小时的变更生成"降价 / 新码"标记（flags），并与当前状态核对：已经删掉的码不算新码。
"""

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from ..locations import normalize_all as normalize_locations
from ..models import Deal, ExtractedBy, LlmStatus, Plan
from ..textutil import make_excerpt
from . import keywords as kw
from .classify import Classification
from .llm import LlmState
from .rules import Extraction, completeness
from .validate import Merged

TRACK_VERSION = 2  # tracked() 的结构改动时递增；版本不同的旧基准不参与比较
FLAG_WINDOW_HOURS = 24

RANGE_FIELDS = ("cpu_cores", "ram_gb", "disk_gb", "bandwidth_tb", "port_gbps")


class ChangeFlag(StrEnum):
    PRICE_DROP = "price_drop"  # 降价
    NEW_CODE = "new_code"  # 新码


@dataclass(frozen=True)
class Change:
    """changes 表的一行。discount_pct 存数字，coupon_codes 存 JSON 数组，plan_price 存 JSON 对象（套餐身份字段加 price）。"""

    field: str
    old: str | None
    new: str | None


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------- 组装


def specs_range(plans: list[Plan]) -> dict:
    """范围表达：各配置字段的最小与最大值，另附机房（按出现顺序去重）。"""
    out: dict = {}
    for name in RANGE_FIELDS:
        values = [v for p in plans if (v := getattr(p, name)) is not None]
        if values:
            out[name] = [min(values), max(values)]
    if locations := list(dict.fromkeys(loc for p in plans for loc in p.locations)):
        out["locations"] = locations
    if net := list(dict.fromkeys(tag for p in plans for tag in p.net)):  # 线路标签（O-16）：各套餐的并集
        out["net"] = net
    return out


def price_list(plans: list[Plan], start_prices: list[dict] = ()) -> list[dict]:
    """帖子级价格列表：套餐价格去重，再加套餐里没有的起价（标 "from": true，第 12.4 节）。"""
    out: list[dict] = []
    for p in plans:
        item = {"amount": p.price, "currency": p.currency, "period": str(p.period) if p.period else None}
        if p.price is not None and item not in out:
            out.append(item)
    out += [{**s, "from": True} for s in start_prices if s not in out]
    return out


def _day(value) -> str | None:
    return value.isoformat() if value else None


def build_deal(row: Mapping | sqlite3.Row, c: Classification, rules: Extraction, merged: Merged | None, *,
               llm: LlmState, llm_output: dict | None) -> Deal:
    """row 为 raw_posts 的一行；llm 为本轮处理后的 LLM 状态，llm_output 为 merged 所依据的 LLM 原始输出。

    人民币估算与起价（plans.price_cny_year、min_price*）在保存前由 fx.apply_fx 填写。
    """
    text = row["text_struct"]
    src = merged or rules  # 帖子级字段：LLM 合并结果（其中校验失败的单元已回退规则），否则规则
    plans = list(src.plans)
    for p in plans:  # 机房统一成规范名（"Frankfurt, Germany" → Frankfurt，O-12）
        p.locations = normalize_locations(p.locations)
    deal = Deal(
        url=row["url"], source=row["source"], title=row["title"], sources=[row["source"]],
        vendor=src.vendor, author=row["author"], published_at=row["published_at"], first_seen_at=row["fetched_at"],
        last_seen_at=row["fetched_at"], content_hash=row["content_hash"],
        is_black_friday=merged.is_black_friday if merged else c.is_black_friday,
        event_type=merged.event_type if merged else c.event_type,
        product_types=list(merged.product_types if merged else c.product_types),
        discount_pct=src.discount_pct, discount_text=src.discount_text, recurring=src.recurring,
        coupon_codes=list(src.coupon_codes), specs_range=specs_range(plans),
        price_list=price_list(plans, rules.start_prices),
        purchase_url=src.purchase_url, plan_format=rules.plan_format, excerpt=make_excerpt(text), lang=c.lang,
        starts_at=_day(rules.starts_at),
        ends_at=_day(merged.ends_at if merged else rules.ends_at),
        ends_source=(merged.ends_source if merged else "rule" if rules.ends_at else None),
        completeness=completeness(src, bool(kw.PRICE.search(text))),
        llm_status=llm.status, llm_attempts=llm.attempts, llm_hash=llm.hash,
        llm_output=llm_output if llm.status is LlmStatus.DONE else None, plans=plans,
    )
    if merged:
        deal.title_zh, deal.title_en = merged.title_zh, merged.title_en
        deal.summary_zh, deal.summary_en, deal.confidence = merged.summary_zh, merged.summary_en, merged.confidence
        deal.extracted_by = ExtractedBy(merged.extracted_by)
    deal.tracked = tracked(deal, rules)
    return deal


# ---------------------------------------------------------------- 变更检测（D46）


def _plan_key(p: Plan) -> dict:
    key = {"name": p.name, "cpu_cores": p.cpu_cores, "ram_gb": p.ram_gb, "disk_gb": p.disk_gb,
           "currency": p.currency, "period": str(p.period) if p.period else None, "locations": list(p.locations)}
    return {k: v for k, v in key.items() if v not in (None, [])}


def tracked(deal: Deal, rules: Extraction) -> dict:
    """比对基准：原帖内容，折扣、码的展示值与规则层值，规则层套餐的价格。"""
    return {
        "v": TRACK_VERSION, "content_hash": deal.content_hash, "title": deal.title,
        "shown": {"discount_pct": deal.discount_pct, "coupon_codes": list(deal.coupon_codes)},
        "rules": {"discount_pct": rules.discount_pct, "coupon_codes": list(rules.coupon_codes),
                  "plans": [[_plan_key(p), p.price] for p in rules.plans if p.price is not None]},
    }


def _str(value) -> str | None:
    return None if value is None else str(value)


def _unique_plans(plans: list) -> dict[str, tuple[dict, float]]:
    """对齐键（名称与机房忽略大小写）→（身份字段, 价格）；同一键出现多次的去掉。"""
    groups: dict[str, list] = {}
    for key, price in plans:
        norm = {**key, "name": (key.get("name") or "").casefold().strip(),
                "locations": sorted(x.casefold() for x in key.get("locations", []))}
        groups.setdefault(_dumps(norm), []).append((key, price))
    return {k: v[0] for k, v in groups.items() if len(v) == 1}


def diff(old: Mapping | None, new: Mapping) -> list[Change]:
    """上次保存以来原帖带来的变化；没有可比的旧基准（新记录、基准版本变了）时不记。"""
    if not old or old.get("v") != TRACK_VERSION or new.get("v") != TRACK_VERSION:
        return []
    if old["content_hash"] == new["content_hash"]:
        return []
    was, cur, was_r, cur_r = old["shown"], new["shown"], old["rules"], new["rules"]
    out: list[Change] = []
    if old["title"] != new["title"]:
        out.append(Change("title", old["title"], new["title"]))
    if was["discount_pct"] != cur["discount_pct"] and was_r["discount_pct"] != cur_r["discount_pct"]:
        out.append(Change("discount_pct", _str(was["discount_pct"]), _str(cur["discount_pct"])))
    if set(was["coupon_codes"]) != set(cur["coupon_codes"]) and set(was_r["coupon_codes"]) != set(cur_r["coupon_codes"]):
        out.append(Change("coupon_codes", _dumps(was["coupon_codes"]), _dumps(cur["coupon_codes"])))
    before = _unique_plans(was_r["plans"])
    for k, (key, price) in _unique_plans(cur_r["plans"]).items():
        if k in before and before[k][1] != price:
            out.append(Change("plan_price", _dumps({**before[k][0], "price": before[k][1]}),
                              _dumps({**key, "price": price})))
    return out


def flags(changes: Iterable[Change], *, coupon_codes: Iterable[str]) -> set[ChangeFlag]:
    """一条优惠最近的变更 → 标记；与当前状态矛盾的不标（已删掉的码不算新码）。"""
    codes = set(coupon_codes)
    out: set[ChangeFlag] = set()
    for ch in changes:
        if ch.field == "plan_price":
            if json.loads(ch.new)["price"] < json.loads(ch.old)["price"]:
                out.add(ChangeFlag.PRICE_DROP)
        elif ch.field == "discount_pct":
            if int(ch.new or 0) > int(ch.old or 0):
                out.add(ChangeFlag.PRICE_DROP)
        elif ch.field == "coupon_codes":
            if (set(json.loads(ch.new)) - set(json.loads(ch.old))) & codes:
                out.add(ChangeFlag.NEW_CODE)
    return out
