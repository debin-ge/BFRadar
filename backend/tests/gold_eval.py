"""金标集评估脚本（M0.4）：校验标注，并按《金标集标注规范》第 8 节计分。

  uv run python tests/gold_eval.py --validate                  # 格式、枚举、数值范围、原文出现性
  uv run python tests/gold_eval.py --self-check                # 指标定义、样本统计、计分逻辑自检
  uv run python tests/gold_eval.py --pred preds.json [--split dev|holdout|all] [--mode rules|llm]
  uv run python tests/gold_eval.py --run [--diff]              # 用当前规则层现场抽取并计分（默认只跑调试集）
  uv run python tests/gold_eval.py --run --mode llm [--diff]   # 调用 .env 配置的真实 LLM，经校验合并后计分

预测文件为 {样本 id: labels}，labels 结构同金标；缺失的样本按全空预测计分。
"""

import argparse
import copy
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
from pool import TESTS, load_item, render  # noqa: E402  （pool 已把项目根加入 sys.path）

from app import models as m  # noqa: E402

GOLD = TESTS / "gold"

SOURCES = {"lowendtalk", "lowendspirit", "lowendbox", "nodeseek"}  # Reddit 不进金标集（第 2.1 节）
RECORD_KINDS = set(m.RecordKind)
EVENT_TYPES = set(m.EventType)
PRODUCT_TYPES = set(m.ProductType)
LANGS = set(m.LANGS)
RECURRING = set(m.Recurring)
PLAN_FORMATS = set(m.PlanFormat)
DISK_TYPES = set(m.DiskType)
CURRENCIES = set(m.CURRENCIES)
PERIODS = set(m.Period)
PRICE_NOTES = set(m.PriceNote)
INTEL_CATEGORIES = set(m.IntelCategory)
SPLITS = {"dev", "holdout"}
LABEL_KEYS = {
    "record_kind", "is_black_friday", "event_type", "product_types", "lang", "vendor", "discount", "coupon_codes",
    "purchase_url", "ends_at", "plan_format", "plans_truncated", "plans", "intel", "notes",
}
PLAN_KEYS = {
    "name", "cpu_cores", "cpu_desc", "ram_gb", "disk_gb", "disk_type", "bandwidth_tb", "port_gbps",
    "locations", "price", "currency", "period", "price_note", "order_url", "coupon_code", "notes",
}
RANGES = {  # 第 12.4 节合理性区间；端口按 10 Mbps–400 Gbps
    "cpu_cores": (1, 128), "ram_gb": (0.125, 1024), "disk_gb": (1, 200000), "bandwidth_tb": (0.1, 1000),
    "port_gbps": (0.01, 400), "price": (0.1, 100000),
}

METRICS = {  # 第 8 节口径
    "classification": "分类准确率 = record_kind、is_black_friday、event_type（仅 offer）三项准确率的平均",
    "vendor": "商家识别 = offer 记录中标准化后（小写、去空格与标点、别名映射）与金标相等的比例",
    "plan_recall": "套餐召回 = 一对一对齐成功的金标套餐数 / 金标套餐总数",
    "price_period": "价格+周期 = 对齐套餐中 price（相对误差 ≤ 1%）、currency、period 全对的数 / 金标套餐总数",
    "ram_disk": "内存+存储 = 对齐套餐中 ram_gb 相等且 disk_gb 相差 ≤ 5% 的数 / 金标套餐总数",
    "plan_precision": "套餐精确率 = 对齐成功的预测套餐数 / 预测套餐总数",
    "coupon_precision": "优惠码精确率 = 全集微平均：Σ 命中金标的预测码 / Σ 预测码（大小写敏感）",
    "coupon_recall": "优惠码召回 = 全集微平均：Σ 命中金标的预测码 / Σ 金标码",
    "discount_pct": "折扣 pct 准确率（offer）",
    "recurring": "recurring 准确率（offer）",
    "purchase_domain": "purchase_url 域名匹配率（offer，双方为空也算匹配）",
    "ends_at": "截止日期准确率（offer，规范 5.13）：与金标相等，金标写 null 时预测也须为空（误报算错）",
}
TARGETS = {"classification": 0.95, "vendor": 0.90, "price_period": 0.90, "ram_disk": 0.85, "coupon_precision": 0.95}
PLAN_METRICS = {"plan_recall", "price_period", "ram_disk", "plan_precision"}
RULE_MODE_FORMATS = {"pipe", "table"}  # D21：规则模式下套餐级目标只对这两种形态判定


# ---------------------------------------------------------------- 加载与校验


def load_gold(split: str = "all") -> list[dict]:
    samples = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(GOLD.glob("*.json"))]
    return [s for s in samples if split == "all" or s.get("split") == split]


def _norm_ws(text: str) -> str:
    return " ".join(text.split())


def _numbers(text: str) -> set[float]:
    nums = set()
    for raw in re.findall(r"\d+(?:[.,]\d+)*", text):
        for candidate in (raw.replace(",", ""), raw.replace(".", "").replace(",", ".")):
            try:
                nums.add(round(float(candidate), 4))
            except ValueError:
                pass
    return nums


@dataclass
class Check:
    sample_id: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def _enum(chk: Check, where: str, value, allowed: set, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if value not in allowed:
        chk.error(f"{where} = {value!r} 不在 {sorted(allowed)} 中")


def _number(chk: Check, where: str, value, key: str) -> None:
    if value is None:
        return
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        chk.error(f"{where} 应为数字，实际 {value!r}")
        return
    lo, hi = RANGES[key]
    if not lo <= value <= hi:
        chk.error(f"{where} = {value} 超出合理区间 [{lo}, {hi}]")


def _validate_ends_at(chk: Check, value, raw: str, published: str | None) -> None:
    """截止日期（规范 5.13）：YYYY-MM-DD，且是原文出现过的日期（相对写法按发布时间推算的也算）。"""
    if value is None:
        return
    from datetime import date

    from app.extract.dates import find_dates
    try:
        when = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        when = None
    if when is None:
        chk.error(f"ends_at {value!r} 应为 YYYY-MM-DD")
    elif when not in find_dates(raw, published).mentioned:
        chk.error(f"ends_at {value} 不是原文出现过的日期")


def validate_sample(sample: dict, path: Path) -> Check:
    chk = Check(sample.get("id", path.stem))
    if sample.get("id") != path.stem:
        chk.error(f"id {sample.get('id')!r} 与文件名不一致")
    _enum(chk, "source", sample.get("source"), SOURCES)
    _enum(chk, "split", sample.get("split"), SPLITS, nullable=True)
    if sample.get("split") is None:
        chk.warn("split 尚未划分")
    labels = sample.get("labels") or {}
    if missing := LABEL_KEYS - labels.keys():
        chk.error(f"labels 缺少字段 {sorted(missing)}")
        return chk
    if extra := labels.keys() - LABEL_KEYS:
        chk.error(f"labels 有多余字段 {sorted(extra)}")

    try:
        item = load_item(sample["fixture"], sample["fixture_item_guid"], sample["source"])
    except (KeyError, FileNotFoundError) as e:
        chk.error(f"找不到 fixture 原文：{e}")
        return chk
    body, links = render(item.html)
    raw = f"{item.title}\n{body}"
    flat = _norm_ws(raw).lower()
    hrefs = {href for _, _, href in links}
    numbers = _numbers(raw)

    def in_text(where: str, value: str, case_sensitive: bool) -> None:
        if value is None:
            return
        found = value in raw if case_sensitive else _norm_ws(value).lower() in flat
        if not found:
            chk.error(f"{where} {value!r} 未在原文中逐字出现")

    def is_link(where: str, url: str | None) -> None:
        if url is not None and url not in hrefs and url not in raw:
            chk.error(f"{where} {url!r} 不在原文链接表中")

    kind = labels["record_kind"]
    _enum(chk, "record_kind", kind, RECORD_KINDS)
    if not isinstance(labels["is_black_friday"], bool):
        chk.error("is_black_friday 应为 true/false")
    _enum(chk, "event_type", labels["event_type"], EVENT_TYPES)
    _enum(chk, "lang", labels["lang"], LANGS)
    for pt in labels["product_types"]:
        _enum(chk, "product_types[]", pt, PRODUCT_TYPES)

    if kind == "offer":
        if labels["intel"] is not None:
            chk.error("offer 记录的 intel 应为 null")
        _enum(chk, "plan_format", labels["plan_format"], PLAN_FORMATS)
        discount = labels["discount"] or {}
        _enum(chk, "discount.recurring", discount.get("recurring"), RECURRING)
        pct = discount.get("pct")
        if pct is not None and (not isinstance(pct, int) or not 1 <= pct <= 100):
            chk.error(f"discount.pct = {pct!r} 应为 1–100 的整数")
        if discount.get("text") and len(discount["text"]) > 60:
            chk.error("discount.text 超过 60 字符")
        in_text("discount.text", discount.get("text"), case_sensitive=False)
        codes = labels["coupon_codes"]
        if len(codes) != len(set(codes)):
            chk.error("coupon_codes 有重复")
        for code in codes:
            in_text("coupon_codes[]", code, case_sensitive=True)
        is_link("purchase_url", labels["purchase_url"])
        _validate_ends_at(chk, labels["ends_at"], raw, sample.get("published_at"))
        vendor = labels["vendor"]
        if vendor and re.sub(r"\W", "", vendor.lower()) not in re.sub(r"\W", "", f"{flat} {item.author}".lower()):
            chk.warn(f"vendor {vendor!r} 未在标题、正文或发帖人中出现")
        _validate_plans(chk, labels, in_text, is_link, numbers)
    else:
        if labels["ends_at"] is not None:
            chk.error("ends_at：只有 offer 记录标截止日期")
        if kind == "intel":
            intel = labels["intel"] or {}
            _enum(chk, "intel.category", intel.get("category"), INTEL_CATEGORIES)
            if not isinstance(intel.get("vendor_mention"), list):
                chk.error("intel.vendor_mention 应为数组")
        elif labels["intel"] is not None:
            chk.error("ignore 记录的 intel 应为 null")
        if labels["event_type"] != "other":
            chk.error(f"{kind} 记录的 event_type 应为 other")
        if labels["plans"] or labels["coupon_codes"] or labels["purchase_url"] or any((labels["discount"] or {}).values()):
            chk.error(f"{kind} 记录不应标套餐、码、折扣或购买链接")
    return chk


def _validate_plans(chk: Check, labels: dict, in_text, is_link, numbers: set[float]) -> None:
    plans = labels["plans"]
    if len(plans) > 20:
        chk.error(f"plans 共 {len(plans)} 条，超过 20 条应截断并置 plans_truncated")
    if labels["plans_truncated"] and len(plans) != 20:
        chk.error("plans_truncated 为 true 时应恰好标注 20 条")
    if labels["plan_format"] == "none" and plans:
        chk.warn("plan_format 为 none 但有套餐")
    for i, plan in enumerate(plans):
        where = f"plans[{i}]"
        if missing := PLAN_KEYS - plan.keys():
            chk.error(f"{where} 缺少字段 {sorted(missing)}")
            continue
        if extra := plan.keys() - PLAN_KEYS:
            chk.error(f"{where} 有多余字段 {sorted(extra)}")
        for key in RANGES:
            _number(chk, f"{where}.{key}", plan[key], key)
        if plan["price"] is None:
            chk.error(f"{where} 没有价格，不构成套餐（第 6.1 节）")
        elif round(float(plan["price"]), 4) not in numbers:
            chk.warn(f"{where}.price {plan['price']} 未在原文数字中出现")
        _enum(chk, f"{where}.currency", plan["currency"], CURRENCIES, nullable=plan["price"] is None)
        _enum(chk, f"{where}.period", plan["period"], PERIODS, nullable=True)
        if plan["period"] is None and not plan["notes"]:
            chk.warn(f"{where}.period 为 null 但 notes 未说明")
        _enum(chk, f"{where}.price_note", plan["price_note"], PRICE_NOTES, nullable=True)
        _enum(chk, f"{where}.disk_type", plan["disk_type"], DISK_TYPES, nullable=True)
        if not isinstance(plan["locations"], list):
            chk.error(f"{where}.locations 应为数组")
        is_link(f"{where}.order_url", plan["order_url"])
        in_text(f"{where}.coupon_code", plan["coupon_code"], case_sensitive=True)


def validate_all() -> int:
    paths = sorted(GOLD.glob("*.json"))
    n_err = 0
    for path in paths:
        chk = validate_sample(json.loads(path.read_text(encoding="utf-8")), path)
        n_err += len(chk.errors)
        for msg in chk.errors:
            print(f"ERROR {chk.sample_id}: {msg}")
        for msg in chk.warnings:
            print(f"WARN  {chk.sample_id}: {msg}")
    print(f"\n校验 {len(paths)} 个样本：{n_err} 个错误")
    return 1 if n_err else 0


# ---------------------------------------------------------------- 计分


def norm_name(value: str | None, aliases: dict[str, str] | None = None) -> str | None:
    if value is None:
        return None
    key = re.sub(r"[\W_]+", "", value.lower())
    return (aliases or {}).get(key, key)


def _num_eq(a, b) -> bool:
    return (a is None and b is None) or (a is not None and b is not None and abs(a - b) < 1e-6)


def _within(a, b, tol: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol * abs(b)


def _domain(url: str | None) -> str | None:
    return (urlsplit(url).hostname or "").removeprefix("www.") if url else None


def _price_err(pred: dict, gold: dict) -> float:
    if pred.get("price") is None or gold.get("price") is None:
        return float("inf")
    return abs(pred["price"] - gold["price"]) / max(abs(gold["price"]), 1e-9)


def align_plans(gold: list[dict], pred: list[dict]) -> list[tuple[int, int]]:
    """一对一对齐（第 8.3 节、D34、D42）：按金标顺序逐个匹配；先按名称+周期，再按内存+存储+周期；
    多个候选时取价格相对误差最小者，再取位置最接近者。返回 [(金标下标, 预测下标)]。"""
    used: set[int] = set()
    pairs = []
    for gi, g in enumerate(gold):
        def by_name(p):
            return g.get("name") and norm_name(p.get("name")) == norm_name(g["name"]) and p.get("period") == g.get("period")

        def by_spec(p):
            # 没有内存的产品（存储、邮箱、虚拟主机）双方内存都为空时，以存储与周期对齐（D42）
            ram_ok = (_num_eq(p.get("ram_gb"), g["ram_gb"]) if g.get("ram_gb") is not None
                      else p.get("ram_gb") is None and g.get("disk_gb") is not None)
            return ram_ok and _within(p.get("disk_gb"), g.get("disk_gb"), 0.05) and p.get("period") == g.get("period")

        for rule in (by_name, by_spec):
            candidates = [pi for pi, p in enumerate(pred) if pi not in used and rule(p)]
            if candidates:
                best = min(candidates, key=lambda pi: (_price_err(pred[pi], g), abs(pi - gi)))
                used.add(best)
                pairs.append((gi, best))
                break
    return pairs


@dataclass
class Ratio:
    hit: int = 0
    total: int = 0
    mean: float | None = None  # 由若干子项平均得到时（分类准确率）直接给值

    def add(self, hit: bool | int, n: int = 1) -> None:
        self.hit += int(hit)
        self.total += n

    @property
    def value(self) -> float | None:
        if self.mean is not None:
            return self.mean
        return self.hit / self.total if self.total else None

    def __str__(self) -> str:
        if self.value is None:
            return "   —        "
        counts = "(平均) " if self.mean is not None else f"{self.hit:>3}/{self.total:<3}"
        return f"{self.value:6.1%} {counts}"


def score(samples: list[dict], preds: dict[str, dict], aliases: dict[str, str] | None = None,
          plan_formats: set[str] | None = None) -> dict[str, Ratio]:
    """按第 8 节计分。plan_formats 不为 None 时，套餐级指标只统计这些形态的样本。"""
    r = {k: Ratio() for k in ("record_kind", "is_black_friday", "event_type", *METRICS)}
    r.pop("classification")
    for s in samples:
        g = s["labels"]
        p = preds.get(s["id"]) or {}
        r["record_kind"].add(p.get("record_kind") == g["record_kind"])
        r["is_black_friday"].add(p.get("is_black_friday") == g["is_black_friday"])
        if g["record_kind"] != "offer":
            continue
        r["event_type"].add(p.get("event_type") == g["event_type"])
        r["vendor"].add(norm_name(p.get("vendor"), aliases) == norm_name(g["vendor"], aliases))
        gd, pd = g["discount"] or {}, p.get("discount") or {}
        r["discount_pct"].add(pd.get("pct") == gd.get("pct"))
        r["recurring"].add(pd.get("recurring") == gd.get("recurring"))
        r["purchase_domain"].add(_domain(p.get("purchase_url")) == _domain(g["purchase_url"]))
        r["ends_at"].add((p.get("ends_at") or None) == (g.get("ends_at") or None))
        gold_codes, pred_codes = set(g["coupon_codes"]), set(p.get("coupon_codes") or [])
        r["coupon_precision"].add(len(pred_codes & gold_codes), len(pred_codes))
        r["coupon_recall"].add(len(pred_codes & gold_codes), len(gold_codes))
        if plan_formats is not None and g["plan_format"] not in plan_formats:
            continue
        gp, pp = g["plans"], p.get("plans") or []
        if g.get("plans_truncated"):  # 只计已标注的前 N 个套餐（第 5.11、8.3 节）
            pp = pp[:len(gp)]
        pairs = align_plans(gp, pp)
        r["plan_recall"].add(len(pairs), len(gp))
        r["plan_precision"].add(len(pairs), len(pp))
        r["price_period"].add(sum(
            _price_err(pp[pi], gp[gi]) <= 0.01 and pp[pi].get("currency") == gp[gi]["currency"]
            and pp[pi].get("period") == gp[gi]["period"] for gi, pi in pairs), len(gp))
        r["ram_disk"].add(sum(
            _num_eq(pp[pi].get("ram_gb"), gp[gi]["ram_gb"]) and _within(pp[pi].get("disk_gb"), gp[gi]["disk_gb"], 0.05)
            for gi, pi in pairs), len(gp))
    parts = [r[k].value for k in ("record_kind", "is_black_friday", "event_type") if r[k].value is not None]
    r["classification"] = Ratio(mean=sum(parts) / len(parts)) if parts else Ratio()
    return r


def report(samples: list[dict], preds: dict[str, dict], split: str, mode: str) -> int:
    groups = {"dev": [s for s in samples if s.get("split") == "dev"],
              "holdout": [s for s in samples if s.get("split") == "holdout"], "all": samples}
    columns = ["dev", "holdout", "all"] if split == "all" else [split]
    scored = {c: score(groups[c], preds) for c in columns}
    gated = {c: score(groups[c], preds, plan_formats=RULE_MODE_FORMATS) for c in columns} if mode == "rules" else scored
    print(f"模式：{'规则模式' if mode == 'rules' else 'LLM 模式'}；样本 " + "，".join(f"{c} {len(groups[c])}" for c in columns))
    print(f"{'指标':<18}" + "".join(f"{c:>20}" for c in columns) + "   目标")
    failed = 0
    for key in METRICS:
        src = gated if key in PLAN_METRICS else scored
        row = f"{key:<18}" + "".join(f"{str(src[c][key]):>20}" for c in columns)
        target = TARGETS.get(key)
        if target is not None:
            value = src[columns[-1]][key].value
            ok = value is not None and value >= target
            failed += not ok
            scope = "（竖线/表格）" if mode == "rules" and key in PLAN_METRICS else ""
            row += f"   ≥ {target:.0%}{scope} {'✓' if ok else '✗'}"
        print(row)
    if mode == "rules":
        print("\n规则模式下套餐级目标只对竖线与表格形态判定（D21）；全部形态分层：")
        for fmt in sorted({s["labels"]["plan_format"] for s in samples if s["labels"]["record_kind"] == "offer"}):
            sub = score([s for s in samples if s["labels"]["plan_format"] == fmt], preds)
            print(f"  {fmt:<9} 召回 {sub['plan_recall']}  价格+周期 {sub['price_period']}  内存+存储 {sub['ram_disk']}")
    if split == "all":
        for key in TARGETS:
            dev, hold = scored["dev"][key].value, scored["holdout"][key].value
            if dev is not None and hold is not None and dev - hold > 0.10:
                print(f"⚠ 疑似过拟合：{key} 留出集 {hold:.1%} 比调试集 {dev:.1%} 低 10 个百分点以上（第 8.7 节）")
    return 1 if failed else 0


# ---------------------------------------------------------------- 现场抽取


def predict(sample: dict, llm=None) -> dict:
    """用当前规则层（给了 llm 时再经 LLM 与反幻觉校验合并）对一条金标样本抽取，返回与金标同结构的 labels。"""
    from app.extract.classify import classify
    from app.extract.rules import extract

    item = load_item(sample["fixture"], sample["fixture_item_guid"], sample["source"])
    st = render(item.html)
    c = classify(sample["source"], item.title, st.text, item.categories)
    labels = {
        "record_kind": str(c.record_kind), "is_black_friday": c.is_black_friday, "event_type": str(c.event_type),
        "product_types": [str(p) for p in c.product_types], "lang": c.lang,
        "intel": {"category": str(c.intel_category), "vendor_mention": []} if c.intel_category else None,
        "plan_format": None, "plans": [], "coupon_codes": [], "vendor": None, "purchase_url": None,
        "discount": {"pct": None, "recurring": None, "text": None}, "_evidence": c.evidence,
    }
    if c.record_kind == "offer":
        ex = extract(sample["source"], item.title, st.text, st.links, item.author, c.lang, "<td" in item.html.lower(),
                     sample.get("published_at"))
        labels.update(
            plan_format=str(ex.plan_format), vendor=ex.vendor, purchase_url=ex.purchase_url,
            ends_at=ex.ends_at.isoformat() if ex.ends_at else None,
            coupon_codes=ex.coupon_codes,
            discount={"pct": ex.discount_pct, "recurring": str(ex.recurring), "text": ex.discount_text},
            plans=[plan_labels(p) for p in ex.plans],
        )
        if llm is not None:
            labels["_llm"] = llm_labels(llm, sample, item, st, c, ex, labels)
    return labels


def plan_labels(plan) -> dict:
    return {k: (str(v) if hasattr(v, "value") else v) for k, v in vars_of(plan).items() if k in PLAN_KEYS}


def llm_labels(llm, sample, item, st, c, ex, labels: dict) -> dict:
    """LLM 模式：调用 LLM，校验合并后覆盖 labels；调用失败时保留规则结果。返回本条的调用记录。"""
    from app.extract.llm import LlmError, build_messages, rules_hint
    from app.extract.validate import validate_and_merge

    msgs = build_messages(sample["source"], sample.get("published_at"), item.title, st.text, st.links, rules_hint(ex))
    try:
        resp = llm.complete(msgs)
    except LlmError as e:
        return {"error": str(e)}
    mg = validate_and_merge(resp.data, title=item.title, text=st.text, links=st.links, author=item.author,
                            lang=c.lang, rules=ex, is_black_friday=c.is_black_friday, event_type=c.event_type,
                            product_types=c.product_types, published=sample.get("published_at"))
    labels.update(
        ends_at=mg.ends_at.isoformat() if mg.ends_at else None,
        is_black_friday=mg.is_black_friday, event_type=str(mg.event_type),
        product_types=[str(p) for p in mg.product_types], vendor=mg.vendor, purchase_url=mg.purchase_url,
        coupon_codes=mg.coupon_codes,
        discount={"pct": mg.discount_pct, "recurring": str(mg.recurring), "text": mg.discount_text},
        plans=[plan_labels(p) for p in mg.plans],
    )
    return {"plans_from": mg.plans_from, "fields": mg.llm_fields, "rejections": mg.rejections,
            "usage": resp.usage, "summary_zh": mg.summary_zh, "summary_en": mg.summary_en}


def llm_report(samples: list[dict], preds: dict[str, dict], verbose: bool) -> None:
    """LLM 调用概况：失败、校验拒绝的单元、套餐来源、token 用量；verbose 时列出摘要供人工查看。"""
    runs = {s["id"]: preds[s["id"]]["_llm"] for s in samples if "_llm" in preds[s["id"]]}
    ok = {k: v for k, v in runs.items() if "error" not in v}
    fields, rejected = sum(v["fields"] for v in ok.values()), sum(len(v["rejections"]) for v in ok.values())
    tokens = Counter()
    for v in ok.values():
        for k, n in (v["usage"] or {}).items():
            items = n.items() if isinstance(n, dict) else [("", n)]
            tokens.update({f"{k}.{sub}" if sub else k: x for sub, x in items if isinstance(x, int) and x})
    rate = f"（{rejected / fields:.1%}）" if fields else ""
    print(f"\nLLM：调用 {len(runs)} 条，失败 {len(runs) - len(ok)} 条；校验单元 {fields} 个，拒绝 {rejected} 个{rate}")
    print("套餐来源：" + "，".join(f"{k} {n}" for k, n in Counter(v["plans_from"] for v in ok.values()).items())
          + ("；token：" + "，".join(f"{k} {n}" for k, n in tokens.items()) if tokens else ""))
    for sid, v in runs.items():
        if "error" in v:
            print(f"  [失败] {sid}：{v['error']}")
        for r in v.get("rejections", []):
            print(f"  [拒绝] {sid}：{r}")
        if verbose and "error" not in v:
            print(f"  [摘要] {sid}：{v['summary_zh']} / {v['summary_en']}")


def vars_of(obj) -> dict:
    from dataclasses import asdict

    return asdict(obj)


DIFF_FIELDS = ("record_kind", "is_black_friday", "event_type", "product_types", "lang", "plan_format", "ends_at")


def segment_coverage(samples: list[dict]) -> tuple[dict[str, Counter], list[str]]:
    """分段召回：金标套餐的价格出现在某个块里即算覆盖；配置覆盖另看内存数值能否在块或其上下文中找到。

    链接序号 [n] 不算数字。多余块 = 不含任何金标价格的块（截断样本不计）。按金标 plan_format 分组。
    """
    from app.extract.segment import segment

    rows: dict[str, Counter] = {}
    misses = []
    for s in samples:
        g = s["labels"]
        if g["record_kind"] != "offer" or not g["plans"]:
            continue
        item = load_item(s["fixture"], s["fixture_item_guid"], s["source"])
        st = render(item.html)
        segs = segment(st.text, st.links)
        no_refs = lambda lines: re.sub(r"\[\d+\]", " ", "\n".join(lines))  # noqa: E731
        seg_nums = [_numbers(no_refs(x.lines)) for x in segs]
        ctx_nums = [n | _numbers(no_refs(x.context)) for n, x in zip(seg_nums, segs)]
        c = rows.setdefault(g["plan_format"], Counter())
        golden_prices = set()
        for i, plan in enumerate(g["plans"]):
            price = round(float(plan["price"]), 4)
            golden_prices.add(price)
            hit = [j for j, nums in enumerate(seg_nums) if price in nums]
            ram = plan["ram_gb"]
            ram_ok = ram is None or any({round(ram, 4), round(ram * 1024, 4)} & ctx_nums[j] for j in hit)
            c["plans"] += 1
            c["price"] += bool(hit)
            c["config"] += bool(hit) and ram_ok
            if not hit or not ram_ok:
                misses.append(f"  {s['id']:<12} plans[{i}] {plan['name'] or ''} {plan['price']} {plan['currency']}"
                              f"/{plan['period']}：{'价格未进任何块' if not hit else '块内找不到内存 ' + str(ram)}")
        c["segments"] += len(segs)
        if not g["plans_truncated"]:
            c["extra"] += sum(not (nums & golden_prices) for nums in seg_nums)
    rows["合计"] = sum(rows.values(), Counter())
    return rows, misses


def segment_report(samples: list[dict], show_misses: bool) -> None:
    rows, misses = segment_coverage(samples)
    print(f"{'形态':<10}{'金标套餐':>8}{'价格覆盖':>12}{'配置覆盖':>12}{'块数':>6}{'多余块':>8}")
    for fmt, c in rows.items():
        if c["plans"]:
            print(f"{fmt:<10}{c['plans']:>8}{c['price'] / c['plans']:>12.1%}{c['config'] / c['plans']:>12.1%}"
                  f"{c['segments']:>6}{c['extra']:>8}")
    if show_misses and misses:
        print("\n未覆盖：")
        print("\n".join(misses))


def _post_diffs(g: dict, p: dict) -> list[str]:
    out = []
    if norm_name(g["vendor"]) != norm_name(p.get("vendor")):
        out.append(f"vendor: {g['vendor']!r} → {p.get('vendor')!r}")
    gd, pd = g["discount"] or {}, p.get("discount") or {}
    for key in ("pct", "recurring"):
        if gd.get(key) != pd.get(key):
            out.append(f"discount.{key}: {gd.get(key)!r} → {pd.get(key)!r}")
    if set(g["coupon_codes"]) != set(p.get("coupon_codes") or []):
        out.append(f"codes: {g['coupon_codes']} → {p.get('coupon_codes')}")
    if _domain(g["purchase_url"]) != _domain(p.get("purchase_url")):
        out.append(f"purchase: {g['purchase_url']} → {p.get('purchase_url')}")
    return out


def _plan_diffs(g: dict, p: dict) -> list[str]:
    gp, pp = g["plans"], p.get("plans") or []
    if g["plans_truncated"]:
        pp = pp[:len(gp)]
    pairs = align_plans(gp, pp)
    out = []
    aligned_g = {gi for gi, _ in pairs}
    for gi, plan in enumerate(gp):
        if gi not in aligned_g:
            out.append(f"漏套餐 {plan['name']} {plan['price']}{plan['currency']}/{plan['period']} ram={plan['ram_gb']} disk={plan['disk_gb']}")
    for gi, pi in pairs:
        a, b = gp[gi], pp[pi]
        bad = [f"{k} {a[k]}→{b.get(k)}" for k in ("price", "currency", "period") if a[k] != b.get(k)
               and not (k == "price" and _price_err(b, a) <= 0.01)]
        if bad:
            out.append(f"套餐[{gi}] " + ", ".join(bad))
    aligned_p = {pi for _, pi in pairs}
    for pi, plan in enumerate(pp):
        if pi not in aligned_p:
            out.append(f"多套餐 {plan.get('name')} {plan.get('price')}{plan.get('currency')}/{plan.get('period')} "
                       f"ram={plan.get('ram_gb')} disk={plan.get('disk_gb')}")
    return out


def show_diff(samples: list[dict], preds: dict[str, dict]) -> None:
    """逐条列出分类与形态字段与金标不一致之处（intel 类别只在 intel 记录上比较）。"""
    rows = 0
    for s in samples:
        g, p = s["labels"], preds[s["id"]]
        diffs = [f"{f}: {g[f]!r} → {p.get(f)!r}" for f in DIFF_FIELDS
                 if (sorted(g[f]) if f == "product_types" else g[f]) != (sorted(p.get(f) or []) if f == "product_types" else p.get(f))]
        if g["record_kind"] == "intel" and (g["intel"] or {}).get("category") != (p.get("intel") or {}).get("category"):
            diffs.append(f"intel.category: {(g['intel'] or {}).get('category')!r} → {(p.get('intel') or {}).get('category')!r}")
        if g["record_kind"] == "offer":
            diffs += _post_diffs(g, p) + _plan_diffs(g, p)
        if diffs:
            rows += 1
            print(f"  {s['id']:<12} " + "\n               ".join(diffs))
    print(f"  不一致样本 {rows} / {len(samples)}")


# ---------------------------------------------------------------- 自检


def _synthetic_check() -> None:
    """用构造样本验证计分：同配置不同价格的两行只能各对齐一次；一条价格错误；一个多余的码。"""
    plan = {k: None for k in PLAN_KEYS} | {"locations": []}
    g_plans = [plan | {"ram_gb": 4, "disk_gb": 40, "price": 21, "currency": "EUR", "period": "year"},
               plan | {"ram_gb": 4, "disk_gb": 40, "price": 38, "currency": "EUR", "period": "biennial"},
               plan | {"ram_gb": 4, "disk_gb": 40, "price": 50, "currency": "EUR", "period": "year"}]
    p_plans = [copy.deepcopy(g_plans[0]), copy.deepcopy(g_plans[1]), copy.deepcopy(g_plans[0]) | {"price": 55}]
    labels = {"record_kind": "offer", "is_black_friday": True, "event_type": "black_friday", "vendor": "HostDZire",
              "discount": {"pct": 10, "recurring": "recurring", "text": None}, "coupon_codes": ["BFHD10"],
              "purchase_url": None, "plan_format": "pipe", "plans": g_plans}
    sample = {"id": "syn-1", "split": "dev", "labels": labels}
    pred = copy.deepcopy(labels) | {"plans": p_plans, "coupon_codes": ["BFHD10", "OFF"], "vendor": "hostdzire "}
    r = score([sample], {"syn-1": pred})
    expect = {"plan_recall": (3, 3), "price_period": (2, 3), "ram_disk": (3, 3), "plan_precision": (3, 3),
              "coupon_precision": (1, 2), "coupon_recall": (1, 1), "vendor": (1, 1), "event_type": (1, 1)}
    for key, (hit, total) in expect.items():
        assert (r[key].hit, r[key].total) == (hit, total), f"{key}: {r[key].hit}/{r[key].total} ≠ {hit}/{total}"
    empty = score([sample], {})
    assert empty["coupon_precision"].total == 0 and empty["plan_recall"].hit == 0


def self_check() -> int:
    print("指标定义（《金标集标注规范》第 8 节）：")
    for key, desc in METRICS.items():
        target = f"（目标 ≥ {TARGETS[key]:.0%}）" if key in TARGETS else ""
        print(f"  {key:<17} {desc}{target}")
    samples = load_gold()
    print(f"\n样本统计：共 {len(samples)} 条")
    by = lambda f: dict(sorted(Counter(f(s) for s in samples).items(), key=lambda kv: str(kv[0])))  # noqa: E731
    print(f"  来源      {by(lambda s: s['source'])}")
    print(f"  记录类型  {by(lambda s: s['labels']['record_kind'])}")
    print(f"  黑五      {by(lambda s: (s['source'], s['labels']['is_black_friday']))}")
    print(f"  套餐形态  {by(lambda s: s['labels']['plan_format'])}")
    print(f"  划分      {by(lambda s: s.get('split'))}")
    plans = Counter()
    for s in samples:
        plans[s["labels"]["plan_format"]] += len(s["labels"]["plans"])
    print(f"  金标套餐  {sum(plans.values())} 条，按形态 {dict(plans)}")

    _synthetic_check()
    print("\n计分自检：构造样本通过", end="")
    if samples:
        r = score(samples, {s["id"]: s["labels"] for s in samples})
        bad = [k for k, v in r.items() if v.value is not None and v.value < 1]
        assert not bad, f"金标自评未达 100%：{bad}"
        print("；金标自评全部 100%", end="")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--pred", type=Path, help="预测文件 {样本 id: labels}")
    ap.add_argument("--split", choices=["dev", "holdout", "all"], default="dev",
                    help="默认只评调试集；留出集只在 M1.15 运行（第 2.4 节）")
    ap.add_argument("--mode", choices=["rules", "llm"], default="rules")
    ap.add_argument("--run", action="store_true", help="用当前规则层现场抽取并计分")
    ap.add_argument("--diff", action="store_true", help="配合 --run / --segments，逐条列出不一致")
    ap.add_argument("--segments", action="store_true", help="套餐分段召回（按金标形态分组）")
    args = ap.parse_args(argv)
    if args.validate:
        return validate_all()
    if args.self_check:
        return self_check()
    if args.segments:
        segment_report(load_gold(args.split), args.diff)
        return 0
    if args.run:
        samples = load_gold(args.split)
        llm, workers = None, 1
        if args.mode == "llm":
            from app.config import load_settings
            from app.extract.llm import LlmClient

            settings = load_settings()
            if not settings.llm_enabled:
                print("LLM 未配置：在 .env 中填写 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL", file=sys.stderr)
                return 2
            llm, workers = LlmClient(settings), settings.llm_concurrency
        with ThreadPoolExecutor(workers) as ex:
            preds = dict(zip([s["id"] for s in samples], ex.map(lambda s: predict(s, llm), samples)))
        status = report(samples, preds, args.split, args.mode)
        if llm:
            llm_report(samples, preds, args.diff)
            llm.close()
        if args.diff:
            print("\n分类字段不一致：")
            show_diff(samples, preds)
        return status
    if args.pred:
        preds = json.loads(args.pred.read_text(encoding="utf-8"))
        return report(load_gold(args.split), preds, args.split, args.mode)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
