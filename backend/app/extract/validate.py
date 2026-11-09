"""LLM 输出的反幻觉校验与合并（《需求与方案说明》第 12.5、12.6 节，D22）。

逐个"字段单元"校验（商家、是否黑五、活动类型、产品类型、折扣、每个优惠码、购买链接、每个套餐、摘要与标题翻译）：
  - 优惠码必须在原文逐字出现（大小写敏感）
  - 套餐价格必须是原文出现的数字（链接序号 [n] 不算），币种、周期与介质在白名单内，数值在合理区间内
  - 链接序号必须在链接表范围内
  - 折扣百分比与折扣文字须在原文中
  - 商家须在标题、正文、发帖人或链接域名中出现；黑五判定为真时原文须有黑五词
  - 摘要与标题翻译里出现的价格、百分比与"N 折"须能在原文找到（百分比可为 N% off 或 100−N，折可换算）
失败的单元回退到规则结果并计入 rejected。套餐：竖线 / 表格且规则完整度 ≥ 0.8 时以规则为准；
否则用通过校验的 LLM 套餐（通过的不足一半时整体回退规则）。

通过校验但比规则结果"更弱且不矛盾"时保留规则结果（D45）：同一品牌的长短写法（VM6 / VM6 Networks）取规则的
域名锚定写法；LLM 的 recurring=unknown 不覆盖规则的明确值；购买链接与规则所选同属一个商家站点时保留规则的
（按落地页 → 第一个下单链接 → 主站的固定顺序选）。换了品牌等真正矛盾的说法仍以 LLM 为准。
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from ..models import (CURRENCIES, DiskType, EventType, ExtractedBy, Period, Plan, PlanFormat, PriceNote, ProductType,
                      Recurring)
from ..textutil import Link
from . import keywords as kw
from .dates import as_datetime, find_dates, plausible
from .rules import PRICE_AMOUNT, Extraction, domain_label, in_range, net_in, parse_amount

RULES_WIN_FORMATS = {PlanFormat.PIPE, PlanFormat.TABLE}
RULES_WIN_COMPLETENESS = 0.8
SUMMARY_LIMITS = {"summary_zh": 60, "summary_en": 140, "title_zh": 200, "title_en": 200}
_WIDE = re.compile(r"[\u2026\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]")  # 汉字、全角标点与省略号
_TOKEN_CHAR = re.compile(r"[A-Za-z0-9.,$€£¥/%+\-]")  # 截断不能落在这类字符中间（价格、型号、单位）
_CLAUSE_END = "，。；、：,;:"
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")
_ZHE = re.compile(r"(\d+(?:\.\d+)?)\s*折")


def zh_len(text: str) -> float:
    """按"字"计长度：汉字与全角符号 1 个，英文字母、数字与半角符号半个。"""
    return sum(1 if _WIDE.match(ch) else 0.5 for ch in text)


def clip(text: str, limit: float, measure=len) -> str | None:
    """超长时在分句处（其次是词或汉字之间）截断并加 "…"，不切断数字、价格与英文词；无处可切返回 None。"""
    if measure(text) <= limit:
        return text
    budget, used, safe, clause = limit - measure("…"), 0.0, 0, 0
    for i, ch in enumerate(text):
        used += measure(ch)
        if used > budget:
            break
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if ch in _CLAUSE_END and not (ch in ",." and nxt.isdigit()):
            clause = i + 1
        if not (_TOKEN_CHAR.match(ch) and nxt and _TOKEN_CHAR.match(nxt)):
            safe = i + 1
    cut = clause if clause and clause >= safe / 2 else safe
    head = text[:cut].rstrip(" " + _CLAUSE_END)
    return head + "…" if head else None


def numbers_in(text: str) -> set[float]:
    """原文中的数字（含千分位与欧式小数两种读法）；链接序号 [n] 不算。"""
    nums: set[float] = set()
    for raw in re.findall(r"\d+(?:[.,]\d+)*", re.sub(r"\[\d+\]", " ", text)):
        for candidate in (raw.replace(",", ""), raw.replace(".", "").replace(",", ".")):
            try:
                nums.add(round(float(candidate), 4))
            except ValueError:
                pass
    return nums


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def _name_words(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", name.lower())


def same_brand(rules_name: str | None, llm_name: str) -> bool:
    """一方是另一方开头的若干个词（VM6 / VM6 Networks、DeluxHost / DeluxHost.net）：同一品牌的长短写法。"""
    if not rules_name:
        return False
    short, long_ = sorted((_name_words(rules_name), _name_words(llm_name)), key=len)
    return bool(short) and long_[:len(short)] == short


def _flat(text: str) -> str:
    return " ".join(text.split()).lower()


@dataclass
class Merged:
    """合并后的帖子级结果；rejections 记录每个被回退的单元，供状态页与排查。"""

    vendor: str | None
    is_black_friday: bool
    event_type: EventType
    product_types: list[ProductType]  # 空 = LLM 判定帖子卖的只是四类以外的产品，整条不收（D62）
    discount_pct: int | None
    discount_text: str | None
    recurring: Recurring
    coupon_codes: list[str]
    purchase_url: str | None
    plans: list[Plan]
    plans_from: str  # rules / llm
    starts_at: date | None = None  # 活动起止日期（O-15）；截止日期取通过校验的 LLM 值，否则规则层的
    ends_at: date | None = None
    ends_source: str | None = None  # rule / llm
    summary_zh: str | None = None
    summary_en: str | None = None
    title_zh: str | None = None
    title_en: str | None = None
    confidence: float | None = None
    llm_fields: int = 0
    llm_rejected: int = 0
    rejections: list[str] = field(default_factory=list)

    @property
    def extracted_by(self) -> str:
        return "llm" if self.llm_fields > self.llm_rejected else "rules"


class _Checker:
    def __init__(self, title: str, text: str, links: list[Link], author: str | None) -> None:
        self.source = f"{title}\n{text}"
        self.free = bool(kw.FREE_PLAN.search(title))  # 标题写明送免费主机 / VPS，才认 0 价套餐（D55）
        self.flat = _flat(self.source)
        self.numbers = numbers_in(self.source)
        self.net_tags = set(net_in(self.source))  # 原文里写了的线路标签：LLM 给的标签要落在这里面（O-16）
        self.links = links
        hosts = " ".join(re.sub(r"^https?://", "", lk.href).split("/")[0] for lk in links)
        self.vendor_haystack = _norm(f"{self.source} {author or ''} {hosts}")
        self.fields = 0
        self.rejected: list[str] = []

    def unit(self, ok: bool, what: str) -> bool:
        self.fields += 1
        if not ok:
            self.rejected.append(what)
        return ok

    def in_text(self, value: str | None, case_sensitive: bool = False) -> bool:
        if not value:
            return False
        return value in self.source if case_sensitive else _flat(value) in self.flat

    def link(self, index) -> str | None:
        if isinstance(index, int) and not isinstance(index, bool) and 1 <= index <= len(self.links):
            return self.links[index - 1].href
        return None

    def figures_grounded(self, text: str) -> bool:
        """摘要里的价格须是原文数字；百分比须是原文的 N 或 100−N；"N 折"须能换算回原文的折扣（6 折 = 40% off）。"""
        def known(*values: float) -> bool:
            return any(round(v, 4) in self.numbers for v in values)

        if not all(known(parse_amount(m.group("a") or m.group("b"))) for m in PRICE_AMOUNT.finditer(text)):
            return False
        if not all(known(float(m.group(1)), 100 - float(m.group(1))) for m in _PERCENT.finditer(text)):
            return False
        for m in _ZHE.finditer(text):
            v = float(m.group(1))
            off = 100 - (v * 10 if v < 10 else v)  # 6 折、8.5 折、85 折
            if not known(v, off, 100 - off):
                return False
        return True


def _enum(enum_cls, value):
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return None


def _number(value, name: str):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return in_range(name, float(value))


def validate_and_merge(data: dict, *, title: str, text: str, links: list[Link], author: str | None, lang: str,
                       rules: Extraction, is_black_friday: bool, event_type: EventType,
                       product_types: list[ProductType], published: datetime | str | None = None) -> Merged:
    c = _Checker(title, text, links, author)
    has_bf_words = bool(kw.BLACK_FRIDAY.search(c.source) or kw.BF_SHORT.search(c.source)
                        or kw.CYBER_MONDAY.search(c.source))

    # ---- 商家
    vendor = rules.vendor
    v = data.get("vendor")
    if v is not None and c.unit(isinstance(v, str) and len(_norm(v)) >= 2 and _norm(v) in c.vendor_haystack,
                                f"vendor {v!r} 不在原文、发帖人或链接域名中"):
        vendor = rules.vendor if same_brand(rules.vendor, v) else v.strip()

    # ---- 分类（LLM 覆盖规则，校验失败回退）
    bf, event, products = is_black_friday, event_type, list(product_types)
    if isinstance(data.get("is_black_friday"), bool):
        if c.unit(not data["is_black_friday"] or has_bf_words, "is_black_friday=true 但原文没有黑五词"):
            bf = data["is_black_friday"]
    if (e := _enum(EventType, data.get("event_type"))) is not None or data.get("event_type") is not None:
        ok = e is not None and (e not in (EventType.BLACK_FRIDAY, EventType.CYBER_MONDAY) or bf)
        if c.unit(ok, f"event_type {data.get('event_type')!r} 无效或与黑五判定矛盾"):
            event = e
    if isinstance(data.get("product_types"), list) and data["product_types"]:
        # 四类以外的取值（邮箱、域名、托管机柜、其他）是合法说法但不收：去掉；只剩它们时 products 为空，帖子整条不收（D62）
        raw = data["product_types"]
        if c.unit(all(isinstance(p, str) and (_enum(ProductType, p) or p in kw.OUT_OF_SCOPE_PRODUCTS) for p in raw),
                  f"product_types {raw!r} 含无效取值"):
            products = list(dict.fromkeys(p for p in map(lambda x: _enum(ProductType, x), raw) if p))

    # ---- 折扣
    pct, dtext, recurring = rules.discount_pct, rules.discount_text, rules.recurring
    d = data.get("discount")
    if isinstance(d, dict):
        p, t, r = d.get("pct"), d.get("text"), _enum(Recurring, d.get("recurring"))
        pct_ok = p is None or (isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 100
                               and ({float(p), float(100 - p), (100 - p) / 10} & c.numbers))
        if c.unit(pct_ok and r is not None and (t is None or c.in_text(t)),
                  f"discount {d!r}：百分比或文字不在原文中，或 recurring 无效"):
            pct, dtext = p, t
            recurring = rules.recurring if r is Recurring.UNKNOWN else r

    # ---- 优惠码：逐个校验
    codes = list(rules.coupon_codes)
    raw_codes = data.get("coupons")
    if isinstance(raw_codes, list):
        valid = []
        for item in raw_codes:
            code = item.get("code") if isinstance(item, dict) else item
            if c.unit(isinstance(code, str) and c.in_text(code.strip(), case_sensitive=True),
                      f"优惠码 {code!r} 未在原文逐字出现"):
                if code.strip() not in valid:
                    valid.append(code.strip())
        if valid or not raw_codes:  # 全部被拒才回退规则
            codes = valid

    # ---- 购买链接
    purchase = rules.purchase_url
    if data.get("purchase_link_index") is not None:
        href = c.link(data["purchase_link_index"])
        if c.unit(href is not None, f"purchase_link_index {data['purchase_link_index']!r} 超出链接表"):
            site = domain_label(href)
            if not (purchase and site and site == domain_label(purchase)):
                purchase = href

    # ---- 套餐
    llm_plans = [p for raw in data.get("plans") or [] if (p := _plan(raw, c, codes)) is not None]
    raw_count = len(data.get("plans") or [])
    rules_win = rules.plan_format in RULES_WIN_FORMATS and rules.completeness >= RULES_WIN_COMPLETENESS and rules.plans
    if rules_win or not llm_plans or len(llm_plans) * 2 < raw_count:
        plans, plans_from = rules.plans, "rules"
    else:
        plans, plans_from = llm_plans, "llm"
    title_net = net_in(title)  # 标题里写的线路是整帖共用的：LLM 套餐没标线路时沿用
    for i, p in enumerate(plans):
        p.position = i
        if plans_from == "llm" and not p.net:
            p.net = list(title_net)
        if plans_from == "llm":  # 合约期与续费价只有规则层抽（D85）：同价同币种同周期的规则套餐有就沿用
            same = next((r for r in rules.plans if (r.price, r.currency, r.period) == (p.price, p.currency, p.period)
                         and (r.term_months or r.renewal_price is not None)), None)
            if same:
                p.term_months, p.renewal_price = same.term_months, same.renewal_price

    # ---- 截止日期：LLM 给的必须是原文出现过的日期（相对写法按发布时间推算后也算），否则用规则层的
    ends, ends_source = rules.ends_at, "rule" if rules.ends_at else None
    raw_end = data.get("ends_at")
    if raw_end is not None:
        found = find_dates(c.source, published)
        try:
            when = date.fromisoformat(raw_end) if isinstance(raw_end, str) else None
        except ValueError:
            when = None
        pub_dt = as_datetime(published)
        if c.unit(when is not None and when in found.mentioned and plausible(when, pub_dt.date() if pub_dt else None),
                  f"ends_at {raw_end!r} 不是原文出现过的日期，或离发布时间太远"):
            ends, ends_source = when, "llm"

    merged = Merged(vendor, bf, event, products, pct, dtext, recurring, codes, purchase, plans, plans_from,
                    starts_at=rules.starts_at, ends_at=ends, ends_source=ends_source)

    # ---- 摘要与标题翻译（规则层没有，对应校验失败时留空）
    for key, limit in SUMMARY_LIMITS.items():
        value = data.get(key)
        if value is None or (key == "title_zh" and lang == "zh") or (key == "title_en" and lang == "en"):
            continue
        ok = isinstance(value, str) and value.strip() and c.figures_grounded(value)
        if c.unit(bool(ok), f"{key} 含原文没有的价格或折扣数字，或为空"):
            setattr(merged, key, clip(value.strip(), limit, zh_len if key == "summary_zh" else len))
    conf = data.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        merged.confidence = max(0.0, min(1.0, float(conf)))
    merged.llm_fields, merged.llm_rejected, merged.rejections = c.fields, len(c.rejected), c.rejected
    return merged


def _plan(raw, c: _Checker, codes: list[str]) -> Plan | None:
    """校验一个 LLM 套餐：价格必须在原文中、币种与周期在白名单内；其余字段不合格时置空并计入拒绝。
    0 价只在标题写明送免费主机 / VPS 时认，币种与周期留空，与规则层一致（D55）。"""
    if not isinstance(raw, dict):
        c.unit(False, f"套餐不是对象：{raw!r}")
        return None
    free = c.free and raw.get("price") == 0 and not isinstance(raw.get("price"), bool)
    price = 0.0 if free else _number(raw.get("price"), "price")
    currency = None if free else raw.get("currency")
    period = None if free or raw.get("period") is None else _enum(Period, raw.get("period"))
    ok = free or (price is not None and round(price, 4) in c.numbers and currency in CURRENCIES
                  and (raw.get("period") is None or period is not None))
    if not c.unit(ok, f"套餐 {raw.get('name')!r} {raw.get('price')!r} {currency!r}/{raw.get('period')!r}：价格不在原文或币种 / 周期无效"):
        return None
    order_url = None
    if raw.get("order_link_index") is not None:
        order_url = c.link(raw["order_link_index"])
        c.unit(order_url is not None, f"套餐 order_link_index {raw['order_link_index']!r} 超出链接表")
    code = raw.get("coupon_code")
    if code is not None and code not in codes:
        c.unit(False, f"套餐优惠码 {code!r} 未通过校验")
        code = None
    locations = [x.strip() for x in raw.get("locations") or [] if isinstance(x, str) and x.strip()][:8]
    asked = [x for x in raw.get("net") or [] if isinstance(x, str)]
    net = [x for x in dict.fromkeys(asked) if x in c.net_tags]
    if asked:
        c.unit(len(net) == len(set(asked)), f"套餐 {raw.get('name')!r} 的线路标签 {asked!r} 含原文没写的")
    return Plan(
        name=raw.get("name") if isinstance(raw.get("name"), str) else None,
        cpu_cores=_number(raw.get("cpu_cores"), "cpu_cores"),
        cpu_desc=raw.get("cpu_desc") if isinstance(raw.get("cpu_desc"), str) else None,
        ram_gb=_number(raw.get("ram_gb"), "ram_gb"), disk_gb=_number(raw.get("disk_gb"), "disk_gb"),
        disk_type=_enum(DiskType, raw.get("disk_type")),
        bandwidth_tb=_number(raw.get("bandwidth_tb"), "bandwidth_tb"),
        port_gbps=_number(raw.get("port_gbps"), "port_gbps"),
        locations=locations, net=net, price=price, currency=currency, period=period,
        price_note=_enum(PriceNote, raw.get("price_note")), order_url=order_url, coupon_code=code,
        notes=raw.get("notes") if isinstance(raw.get("notes"), str) else None,
        extracted_by=ExtractedBy.LLM,
    )
