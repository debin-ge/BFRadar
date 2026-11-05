"""字段规则抽取与合理性校验（《需求与方案说明》第 12.4 节；字段口径见《金标集标注规范》第 5、6 节）。

输入为结构化正文、链接表与分段结果；输出帖子级字段与套餐列表。规则只抽原文写明的信息，拿不准就留空。
"""

import functools
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import parse_qs, urlsplit

from ..models import DiskType, Period, Plan, PlanFormat, PriceNote, Recurring
from ..textutil import Link
from . import keywords as kw
from .dates import find_dates
from .segment import LINK_REF, ROW_SEP, Segment, cells, is_free_plan, is_title, order_refs, plan_format, segment

# ---------------------------------------------------------------- 合理性区间（第 12.4 节），超出即丢弃该字段

RANGES = {"cpu_cores": (1, 128), "ram_gb": (0.125, 1024), "disk_gb": (1, 200000), "bandwidth_tb": (0.1, 1000),
          "port_gbps": (0.01, 400), "price": (0.1, 100000)}


def in_range(name: str, value: float | None) -> float | None:
    if value is None:
        return None
    lo, hi = RANGES[name]
    return value if lo <= value <= hi else None


# ---------------------------------------------------------------- 价格、周期、币种

PRICE_AMOUNT = re.compile(
    r"(?P<pre>US\$|C\$|CA\$|A\$|AU\$|HK\$|S\$|SG\$|[$€£¥￥])\s?(?P<a>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)"
    r"|(?P<b>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)\s?(?P<post>[$€£]|USD|EUR|GBP|CNY|RMB|HKD|CAD|AUD|SGD|JPY|"
    r"美刀|刀|元|块|欧|镑)(?![A-Za-z])")
SYMBOL_CURRENCY = {"$": "USD", "US$": "USD", "C$": "CAD", "CA$": "CAD", "A$": "AUD", "AU$": "AUD", "HK$": "HKD",
                   "S$": "SGD", "SG$": "SGD", "€": "EUR", "£": "GBP", "RMB": "CNY", "美刀": "USD", "刀": "USD",
                   "元": "CNY", "块": "CNY", "欧": "EUR", "镑": "GBP"}
PERIOD_PATTERNS = [  # 长的写法在前；首个命中的位置决定周期
    (Period.HOUR, r"/\s?(?:hr|hour)\b|\bhourly\b|per hour|/小时"),
    (Period.TRIENNIAL, r"trienn?ial(?:ly)?|(?:for|/)\s?3\s?(?:years|yrs)|3-year|three years|三年"),
    (Period.BIENNIAL, r"bienn?ial(?:ly)?|(?:for|/)\s?2\s?(?:years|yrs)|2-year|two years|两年"),
    (Period.SEMI, r"semi-?annual(?:ly)?|(?:/|every|per)\s?6\s?months|半年"),
    (Period.QUARTER, r"quarter(?:ly)?|/\s?qtr|(?:/|every|per)\s?3\s?months|季付|/季"),
    (Period.YEAR, r"/\s?(?:yr|year|annum|y)\b|annual(?:ly)?|\byearly\b|per year|a year|/\s?annually|(?:1st|first)\s+year|"
                  r"年付|/年|每年|首年"),
    (Period.MONTH, r"/\s?(?:mo|month|m)\b|\bmonthly\b|per month|a month|/\s?30 days|first month|月付|/月|每月"),
    (Period.ONETIME, r"one-?time|lifetime (?:plan|license|licence)|买断"),
]
PERIOD_RX = [(p, re.compile(rx, re.I)) for p, rx in PERIOD_PATTERNS]
PERIOD_DEFAULT_RX = re.compile(r"(?:billed|billing|prices?(?: are)?(?: shown)?)\s+(monthly|annually|yearly|quarterly)"
                               r"|(monthly|annual|yearly) (?:prices|pricing|billing)|billing cycle\s*[:：]\s*(\w+)", re.I)
DERIVED_BEFORE = re.compile(r"(?:\(\s*[≈~]?|[≈~]|approx\.?|works out (?:at|to)|equivalent(?: to)?|i\.e\.|that'?s|="
                            r"|[$€£]\s?\d[\d.,]*\s*(?:[A-Z]{3})?\s*/)\s*$", re.I)  # 末项："£7.50 GBP / $10" 的另一种货币
NAME_AFTER_PRICE = re.compile(r"^\s+(?:server|vps|vds|plan|box|deal|special|vm)\b", re.I)  # "EPYC $1 Server" 是名称
REGULAR_BEFORE = re.compile(r"(?:normally|regular(?:ly)?(?: price)?|reg\.?|was|originally|instead of|list price|"
                            r"retail|then|renews? at|renewal(?: price)?|recurring price|after that|standard price|原价)"
                            r"\W{0,3}$", re.I)
PROMO_BEFORE = re.compile(r"(?:promo(?:tional)? price|special(?: price)?|early bird price|halloween|black friday|"
                          r"cyber monday|sale price|first (?:month|payment|year)(?: price)?|discounted(?: price)?|"
                          r"→|->|=>|优惠价|活动价)\W{0,3}$", re.I)
ARROW_AFTER = re.compile(r"^\s*(?:/\s?\w+\s*)?(?:→|->|=>)\s*(?:[$€£¥]|\d)")  # "$20 → $10"；"$5/mo → [3]" 是链接箭头
WITH_CODE_AFTER = re.compile(r"^[^$€£]{0,40}?\b(?:with|w/|using|use)\s+(?:the\s+)?(?:code|coupon|promo)?\s*"
                             r"(?:code\s+)?(?P<code>[A-Z0-9][A-Za-z0-9_-]{3,24})\b|^[^$€£]{0,30}?\bwith (?:the )?coupon\b")
PERIOD_LABEL_BEFORE = re.compile(r"(?:monthly|yearly|annually|quarterly|semi-?annually|biennially|triennially|"
                                 r"月付|年付|季付|半年付|两年付|三年付|首月)\s*[:：]?\s*$", re.I)  # "Monthly: $3.89"、"年付 99 元"


def parse_amount(raw: str) -> float:
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw):
        return float(raw.replace(",", ""))
    if re.fullmatch(r"\d+,\d{1,2}", raw):  # 欧式小数 3,25
        return float(raw.replace(",", "."))
    return float(raw.replace(",", ""))


def find_period(text: str, last: bool = False) -> Period | None:
    found = [(m.start(), p) for p, rx in PERIOD_RX for m in rx.finditer(text)]
    if not found:
        return None
    return (max if last else min)(found)[1]


def default_period(text: str) -> Period | None:
    """帖子级默认周期："All prices USD · billed monthly"、"Monthly prices below"、"Billing Cycle: Annual Billing"。"""
    m = PERIOD_DEFAULT_RX.search(text)
    if not m:
        return None
    word = next(g for g in m.groups() if g).lower()
    return {"monthly": Period.MONTH, "quarterly": Period.QUARTER}.get(word, Period.YEAR if word.startswith(("ann", "year"))
                                                                     else None)


def _currency(m: re.Match, after: str, lang: str) -> str:
    symbol = m.group("pre") or m.group("post")
    currency = SYMBOL_CURRENCY.get(symbol, symbol)
    if symbol in ("¥", "￥"):
        currency = "CNY" if lang == "zh" else "JPY"
    if currency == "USD" and (code := re.match(r"^\s?(CAD|AUD|SGD|HKD)\b", after)):
        currency = code.group(1)
    return currency


START_BEFORE = re.compile(r"(?:from|starting (?:at|from)|starts? (?:at|from)|as low as|低至|起价)\s*"
                          r"(?:a|an|only|just)?\s*[:：]?\s*$", re.I)
START_AFTER = re.compile(r"^\s*(?:/\s?\w+\s*)?起")  # "99 元起"、"$5/月起"


def start_prices(title: str, text: str, lang: str) -> list[dict]:
    """帖子级起价（"from $5/mo"、"Starting from $175"、"99 元起"）：不是套餐价格，只进帖子级价格列表
    （金标集规范第 11 节）。每 GB 之类的单价与越出合理区间的不算。"""
    out: list[dict] = []
    for line in [title, *text.splitlines()]:
        matches = list(PRICE_AMOUNT.finditer(line))
        for i, m in enumerate(matches):
            before = line[max(0, m.start() - 30):m.start()]
            after = line[m.end():matches[i + 1].start() if i + 1 < len(matches) else m.end() + 40]
            if not (START_BEFORE.search(before) or START_AFTER.match(after)) or kw.PRICE_PER_UNIT_AFTER.search(after):
                continue
            amount = in_range("price", parse_amount(m.group("a") or m.group("b")))
            if amount is None:
                continue
            period = find_period(after) or find_period(before, last=True)
            item = {"amount": amount, "currency": _currency(m, after, lang), "period": str(period) if period else None}
            if item not in out:
                out.append(item)
    return out


@dataclass
class PriceHit:
    amount: float
    currency: str
    period: Period | None
    kind: str  # promo / plain / regular / derived
    line: str
    start: int
    end: int
    code: str | None = None


def price_hits(line: str, lang: str, period_hint: Period | None = None) -> list[PriceHit]:
    """一行（或一个单元格）里的价格及其角色。限定价格（from / over / "99 元起" / 每 GB 单价）不返回。"""
    hits: list[PriceHit] = []
    matches = list(PRICE_AMOUNT.finditer(line))
    for i, m in enumerate(matches):
        raw = m.group("a") or m.group("b")
        amount = in_range("price", parse_amount(raw))
        if amount is None:
            continue
        before = line[max(0, m.start() - 30):m.start()]
        after_end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
        after = re.split(r"[，；。]", line[m.end():min(after_end, m.end() + 40)])[0]
        if (kw.PRICE_QUALIFIER_BEFORE.search(before) or kw.PRICE_PER_UNIT_AFTER.search(after)
                or NAME_AFTER_PRICE.match(after) or START_AFTER.match(after)):
            continue
        currency = _currency(m, after, lang)
        kind = "plain"
        code = None
        if DERIVED_BEFORE.search(before):
            kind = "derived"
        elif REGULAR_BEFORE.search(before) or ARROW_AFTER.search(after):
            kind = "regular"
        elif PROMO_BEFORE.search(before):
            kind = "promo"
        # 找"with CODE"时跳过括号里的换算价（"£27 a year (36.13 USD) with BFVPS55"）
        code_window = re.sub(r"\([^)]*\)", " ", line[m.end():m.end() + 70])
        if kind != "derived" and (w := WITH_CODE_AFTER.search(code_window)):
            kind, code = "promo", w.group("code")
        label = PERIOD_LABEL_BEFORE.search(before)
        period = (find_period(label.group(0)) if label else None) or find_period(after) or find_period(before, last=True)
        if period is None and i + 1 < len(matches) and re.fullmatch(r"\s*(?:[A-Z]{3})?\s*/\s*", line[m.end():after_end]):
            period = find_period(line[matches[i + 1].end():])  # "£7.50 GBP / $10 per month"：周期写在另一种货币之后
        period = period or period_hint
        hits.append(PriceHit(amount, currency, period, kind, line, m.start(), m.end(), code))
    return hits


# 表头指明价格列的角色（D57）："Regular | With SNOW50"、"Regular | Sale Price | ~Monthly"。"Standard"、"Special"
# 常是档位名（"Plan | Standard | Premium"），不算
COLUMN_DERIVED = re.compile(r"^\s*[~≈]|approx|equiv|effective|works out", re.I)
COLUMN_REGULAR = re.compile(r"regular|normal price|original|list price|retail|\bwas\b|before|full price|"
                            r"^\s*renew(?:al|s)?(?:\s+(?:price|cost|rate))?\s*$|原价|^\s*续费价?\s*$", re.I)  # "Renewal" 列；
# "Monthly price & renewal" 是"月付价（续费同价）"，不算
COLUMN_PROMO = re.compile(r"\bsale\b|promo|discount|special price|\bdeal\b|offer|\bnow\b|black ?friday|\bbf\b|cyber|"
                          r"\bwith\b|after|优惠|活动|折后|促销", re.I)


def column_role(label: str) -> str | None:
    """价格列表头 → 该列价格的角色（单元格自身没写明时用）：derived / regular / promo。"""
    if not label:
        return None
    if COLUMN_DERIVED.search(label):
        return "derived"
    if COLUMN_REGULAR.search(label):
        return "regular"
    if COLUMN_PROMO.search(label):
        return "promo"
    return None


def labelled_cells(line: str, header: list[str] | None) -> list[tuple[str, str]]:
    """一行按单元格拆开并配上表头标签；有表头时保留空单元格以免错列，多出的单元格标签为空。"""
    if not header:
        return [(c, "") for c in cells(line)]
    parts = [c.strip() for c in ROW_SEP.split(line)]
    while parts and not parts[0]:
        parts.pop(0)
    labels = header + [""] * max(0, len(parts) - len(header))
    return [(p, lab) for p, lab in zip(parts, labels, strict=False) if p]  # 表头比单元格多时截掉


def select_prices(hits: list[PriceHit]) -> list[PriceHit]:
    """去掉换算价与原价；有标明为活动价的就只留活动价；同价同周期去重。"""
    hits = [h for h in hits if h.kind != "derived"]
    if any(h.kind != "regular" for h in hits):
        hits = [h for h in hits if h.kind != "regular"]
    if any(h.kind == "promo" for h in hits):
        hits = [h for h in hits if h.kind == "promo"]
    seen, out = set(), []
    for h in hits:
        key = (h.amount, h.currency, h.period)
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out


# ---------------------------------------------------------------- 套餐配置

SIZE = r"(\d+(?:\.\d+)?)\s?(TB|TiB|T|GB|GiB|G|Gb|MB|MiB|M)\b"
SIZE_RX = re.compile(SIZE, re.I)
MULTI_SIZE = re.compile(r"(?:(\d+)\s*[x×]\s*)?" + SIZE, re.I)
STORAGE_WORD = re.compile(r"nvme|ssd|hdd|sata|sas\b|storage|disk|raid|硬盘|存储", re.I)
BW_WORD = re.compile(r"\bbw\b|bandwidth|traffic|transfer|outbound|流量", re.I)
RAM_WORD = re.compile(r"\bram\b|memory|内存|\bddr\d", re.I)
PORT_SPEED = re.compile(r"(\d+(?:\.\d+)?)\s?(Gbps|Gbit/s|Gbit|Gb/s|Mbps|Mbit/s|Mbit|Mbs)", re.I)
PORT_WORD = re.compile(r"\bport\b|uplink|connection|network|shared|@|网口|带宽", re.I)
UNMETERED = re.compile(r"unmetered|unlimited|不限", re.I)
CPU_PATTERNS = [
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:x\s*)?(?:v?CPUs?|vCores?|v?Cores?\b|核)", re.I),
    re.compile(r"(\d+)c/\d+t", re.I),
    re.compile(r"(?:cores?|vcpus?|cpu)\s*[:：]\s*(\d+)", re.I),
    re.compile(r"(\d+)\s+(?:[A-Za-z-]+\s+){1,3}cores?\b", re.I),
]
CPU_X_FREQ = re.compile(r"(\d+)\s*x\s*(?:AMD|Intel|\d+(?:\.\d+)?\s?GHz)", re.I)  # VPS 写法 "2 x AMD EPYC (…GHz)"
SHORTHAND = re.compile(r"(?<![A-Za-z0-9])(\d+)\s?[hcHC核]\s?(\d+)\s?[gG]")  # 2h4g、2c4g60g
CPU_MODEL = re.compile(r"(?:Dual\s+|\d\s?[x×]\s?)?(?:AMD\s+|Intel\s+|Apple\s+)?(?:EPYC|Ryzen|Xeon|Atom|Core i\d|Threadripper|"
                       r"M[1-4]\b|E[357]-\d{4})[\w\s.-]{0,24}?(?=\s*(?:[|·(,;/]|$|\d+\s?(?:vCPU|cores?|GB)|@|–|—))", re.I)
LIST_MARK = re.compile(r"^\s*(?:[-•●*]+|\d+[.)])\s*")
DECOR = re.compile(r"[^\w\s/#&+.,:()'’×-]+")  # emoji 与装饰符号


def _gb(value: str, unit: str, ram: bool = False) -> float:
    v, u = float(value), unit.upper()
    if u.startswith("T"):
        return v * 1000
    if u.startswith("M"):
        return round(v / 1024, 4) if ram else round(v / 1000, 4)
    return v


@dataclass
class Config:
    name: str | None = None
    cpu_cores: float | None = None
    cpu_desc: str | None = None
    ram_gb: float | None = None
    disk_gb: float | None = None
    disk_type: DiskType | None = None
    bandwidth_tb: float | None = None
    unmetered: bool = False
    port_gbps: float | None = None
    locations: list[str] = field(default_factory=list)
    net: list[str] = field(default_factory=list)


@functools.lru_cache(maxsize=8192)
def _net(text: str) -> tuple[str, ...]:
    found = []
    for tag, rx in kw.NET_TAGS.items():
        for m in rx.finditer(text):
            # 前后几个字里写了"不、无、no、not"的不记："不支持 CN2""no CN2 routing"
            if not kw.NET_NEGATION_BEFORE.search(text[max(0, m.start() - 6):m.start()]) \
                    and not kw.NET_NEGATION_AFTER.search(text[m.end():m.end() + 14]):
                found.append(tag)
                break
    return tuple(found)


def net_in(text: str) -> list[str]:
    """文本里写明的线路与网络标签（keywords.NET_TAGS），按词表顺序，否定句不算。"""
    return list(_net(text))


def locations_in(text: str) -> list[str]:
    return list(_locations(text))  # 调用方会往结果里追加，不能直接交出缓存


@functools.lru_cache(maxsize=8192)
def _locations(text: str) -> tuple[str, ...]:
    """按文本缓存：机房回看（块上方 40 行）会反复扫描同样的行，这个 200 多个分支的正则占了抽取一半时间。"""
    found: list[str] = []
    for m in kw.LOCATION_RX.finditer(text):
        name = kw.LOCATION_NAMES[int(next(k for k, v in m.groupdict().items() if v)[1:])]
        if name not in found:
            found.append(name)
    cities = [n for n in found if n not in kw.COUNTRIES]
    return tuple(cities or found)


def _disk_type(text: str) -> DiskType | None:
    t = text.lower()
    if "nvme" in t:
        return DiskType.NVME
    if "ssd" in t:
        return DiskType.SSD
    if "hdd" in t or "spinning" in t:
        return DiskType.HDD
    return None


def _apply_unit(cfg: Config, text: str, label: str, bare: list[float]) -> None:
    """一个单元（表格单元格或一行）可能含多个字段；已有的字段不覆盖，存储取最大值。"""
    lab = label.lower()
    if not text.strip():
        return
    # 表头指明字段的单元格
    if lab:
        if re.search(r"plan|name|package|套餐", lab) and cfg.name is None and re.search(r"[A-Za-z]|#\d", text):
            cfg.name = _clean_name(text)
            return
        if re.search(r"v?cpu|cores?|vcore|核", lab) and not re.search(r"thread", lab):
            if m := re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", text):
                cfg.cpu_cores = cfg.cpu_cores or in_range("cpu_cores", float(m.group(1)))
                return
        if re.search(r"ram|memory|内存", lab) and (m := SIZE_RX.search(text)):
            cfg.ram_gb = cfg.ram_gb or in_range("ram_gb", _gb(m.group(1), m.group(2), ram=True))
            return
        if re.search(r"storage|disk|ssd|nvme|hdd|硬盘", lab):
            _apply_disk(cfg, text, label)
            return
        if re.search(r"bandwidth|traffic|transfer|\bbw\b|流量", lab):
            _apply_bandwidth(cfg, text, force=True)
            return
        if re.search(r"port|网口", lab):
            if m := PORT_SPEED.search(text):
                cfg.port_gbps = cfg.port_gbps or _gbps(m)
            return
        if re.search(r"ipv4", lab):
            return  # 不记 IPv4（D69）：商家默认都给，只留下这一列不被当成别的字段
        if re.search(r"location|datacenter|机房", lab):
            cfg.locations = cfg.locations or locations_in(text)
            return
    # 无表头：按单元自身的关键词判断
    if m := SHORTHAND.search(text):
        cfg.cpu_cores = cfg.cpu_cores or in_range("cpu_cores", float(m.group(1)))
        cfg.ram_gb = cfg.ram_gb or in_range("ram_gb", float(m.group(2)))
    if cfg.cpu_cores is None:
        for rx in CPU_PATTERNS:
            if m := rx.search(text):
                cfg.cpu_cores = in_range("cpu_cores", float(m.group(1)))
                break
        if cfg.cpu_cores is None and "ghz" in text.lower() and (m := CPU_X_FREQ.search(text)):
            cfg.cpu_cores = in_range("cpu_cores", float(m.group(1)))
    if cfg.cpu_desc is None and (m := CPU_MODEL.search(text)):
        cfg.cpu_desc = m.group(0).strip(" -–—")
    if cfg.ram_gb is None and RAM_WORD.search(text):
        for m in SIZE_RX.finditer(text):
            tail = text[m.end():m.end() + 25]
            if not STORAGE_WORD.match(tail.strip()) and not BW_WORD.search(tail[:12]):
                cfg.ram_gb = in_range("ram_gb", _gb(m.group(1), m.group(2), ram=True))
                break
    if STORAGE_WORD.search(text):
        _apply_disk(cfg, text, "")
    if BW_WORD.search(text) or re.search(r"\bout\s*[:：]", text, re.I):
        _apply_bandwidth(cfg, text)
    if cfg.port_gbps is None and (m := PORT_SPEED.search(text)) and (PORT_WORD.search(text) or not BW_WORD.search(text)):
        cfg.port_gbps = _gbps(m)
    for loc in locations_in(text):
        if loc not in cfg.locations:
            cfg.locations.append(loc)
    # 只有一个容量、没有任何字段词：留作按位置推断（先内存后存储）
    if (re.fullmatch(r"\s*" + SIZE + r"(?:\s+(?:ECC\s+)?DDR\d\w*)?\s*", text, re.I)
            and not (RAM_WORD.search(text) and cfg.ram_gb)):
        m = SIZE_RX.search(text)
        bare.append(_gb(m.group(1), m.group(2), ram=True) if m.group(2).upper().startswith("M") else _gb(m.group(1), m.group(2)))


def _apply_disk(cfg: Config, text: str, label: str) -> None:
    for m in MULTI_SIZE.finditer(text):
        tail = text[m.end():m.end() + 30]
        if BW_WORD.search(tail[:15]) or RAM_WORD.match(tail.strip()) or re.match(r"\s*(?:ECC|DDR)", tail, re.I):
            continue
        if not label and not STORAGE_WORD.search(tail[:30]) and not STORAGE_WORD.search(text[max(0, m.start() - 12):m.start()]):
            continue
        size = _gb(m.group(2), m.group(3)) * (int(m.group(1)) if m.group(1) else 1)
        size = in_range("disk_gb", size)
        if size and (cfg.disk_gb is None or size > cfg.disk_gb):
            cfg.disk_gb = size
            cfg.disk_type = _disk_type(f"{label} {tail[:20]} {text[max(0, m.start() - 12):m.start()]}")


def _apply_bandwidth(cfg: Config, text: str, force: bool = False) -> None:
    if cfg.bandwidth_tb is not None:
        return
    if UNMETERED.search(text) and not SIZE_RX.search(text):
        cfg.unmetered = True
        return
    for m in SIZE_RX.finditer(text):
        context = text[max(0, m.start() - 15):m.end() + 20]
        if force or BW_WORD.search(context) or re.search(r"\bout\s*[:：]", context, re.I):
            if (value := in_range("bandwidth_tb", _gb(m.group(1), m.group(2)) / 1000)) is not None:
                cfg.bandwidth_tb = value  # 越出合理区间的（多半是邻近的硬盘容量）跳过，接着找
                return


def _gbps(m: re.Match) -> float | None:
    v = float(m.group(1))
    return in_range("port_gbps", v / 1000 if m.group(2).lower().startswith("m") else v)


def _clean_name(text: str) -> str | None:
    name = LINK_REF.sub("", LIST_MARK.sub("", text))
    name = re.split(r"\s+[–—-]\s+(?=[$€£\d])|:\s|\s+[|·]\s|\s+[–—]\s+(?:\d|from)", name)[0]
    name = DECOR.sub(" ", name)
    name = re.sub(r"\b(?:Best value|Most popular|Popular|Hot)\b.*$", "", name, flags=re.I)
    name = re.sub(r"[\s,:;–—-]+$", "", " ".join(name.split()))
    return name or None


def _row_name(first_cell: str) -> str | None:
    """行首单元格作名称：去掉其后的规格（"Canada-KVM2GB- 50 GB NVME" → "Canada-KVM2GB"）；纯规格则不作名称。"""
    head = re.split(r"[\s\-–—:]+(?=\d+(?:\.\d+)?\s?[GMT]B\b)", first_cell, maxsplit=1)[0]
    if not re.search(r"[A-Za-z]{2}", head) or SIZE_RX.fullmatch(head.strip()):
        return None
    return _clean_name(head)


SPEC_SPLIT = re.compile(r"\s+/\s+|,\s+|;\s+|\s+\+\s+(?=\D)")


def _split_specs(text: str) -> list[str]:
    """含两个以上容量的一段（"1 vCPU / 2GB RAM / 40GB NVMe / 3TB BW"）按 / , ; + 拆开，免得字段词串到相邻一项。"""
    if len(SIZE_RX.findall(text)) < 2:
        return [text]
    return [p for p in SPEC_SPLIT.split(text) if p.strip()]


def units_of(seg: Segment) -> list[tuple[str, str]]:
    """把块拆成（单元文本, 表头标签）：行按单元格（有表头则配标签），其他按行；含多个容量的单元格或行再按 / , ; + 拆开，
    单元格拆出的各段沿用该列的表头标签（D56）。"""
    units: list[tuple[str, str]] = []
    header_line = " | ".join(seg.header) if seg.header else None
    for line in seg.context + seg.lines:
        if line == header_line:
            continue
        if len(ROW_SEP.findall(line)) >= 2:
            pairs = labelled_cells(line, seg.header if line in seg.lines else None)
            units += [(piece, lab.lower()) for p, lab in pairs for piece in _split_specs(p)]
        elif len(SIZE_RX.findall(line)) >= 2:
            units += [(p, "") for p in _split_specs(line)]
        else:
            units.append((line, ""))
    return units


STORAGE_LIKE = re.compile(r"e-?mail|mailbox|storage|\bs3\b|backup|hosting|cpanel|directadmin|邮箱|存储|虚拟主机", re.I)
COMPUTE_LIKE = re.compile(r"\bvps\b|\bvds\b|\bkvm\b|server|dedicated|instance|独服|服务器", re.I)


def is_storage_like(title: str) -> bool:
    """邮箱、存储、虚拟主机类帖子：没有标注的容量指存储空间而不是内存。"""
    return bool(STORAGE_LIKE.search(title)) and not COMPUTE_LIKE.search(title)


def line_size(line: str) -> float | None:
    """行内恰好一个容量（不算流量与端口）时返回其 GB 数。"""
    sizes = [m for m in SIZE_RX.finditer(line) if not BW_WORD.search(line[m.end():m.end() + 15])]
    return in_range("disk_gb", _gb(sizes[0].group(1), sizes[0].group(2))) if len(sizes) == 1 else None


def parse_config(seg: Segment, lines_before: list[str], storage_like: bool = False) -> Config:
    cfg = Config()
    bare: list[float] = []
    for text, label in units_of(seg):
        _apply_unit(cfg, LINK_REF.sub("", text), label, bare)
    for value in bare:  # 无标注的容量：第一个是内存，第二个是存储（存储类帖子直接是存储）
        if cfg.ram_gb is None and value <= 1024 and not storage_like:
            cfg.ram_gb = in_range("ram_gb", value)
        elif cfg.disk_gb is None:
            cfg.disk_gb = in_range("disk_gb", value)
    if cfg.ram_gb is None and cfg.disk_gb is None:  # 整块只有一个没有字段词的容量
        sizes = {v for x in seg.lines if (v := line_size(LINK_REF.sub("", x))) is not None}
        if len(sizes) == 1:
            value = sizes.pop()
            if storage_like:
                cfg.disk_gb = value
            else:
                cfg.ram_gb = in_range("ram_gb", value)
    # 名称
    if seg.anchor == "row" and seg.lines:
        first = cells(seg.lines[0])[0] if cells(seg.lines[0]) else ""
        if cfg.name is None and not (seg.header and not re.search(r"plan|name|package", seg.header[0], re.I)):
            cfg.name = _row_name(first)
        elif cfg.name is None and seg.header and re.search(r"cpu|processor", seg.header[0], re.I):
            cfg.name = _clean_name(first)  # 独服表格行首是 CPU 型号
    elif seg.anchor in ("order_link", "title") and is_title(seg.lines, 0):
        cfg.name = _clean_name(seg.lines[0])
    elif seg.anchor == "spec_line" and (m := re.match(r"^\s*-\s*([A-Za-z][\w .+-]{1,30}?)\s*[,:]", seg.lines[0])):
        cfg.name = m.group(1).strip()
    # 机房：块内没有就往上找最近的机房词（区块标题，金标集规范 6.10）
    if not cfg.locations:
        for line in reversed(lines_before[-40:]):
            if found := locations_in(line):
                cfg.locations = found
                break
    return cfg


# ---------------------------------------------------------------- 帖子级字段

DISCOUNT_PCT = re.compile(r"(?:up\s*to\s*)?(\d{1,2}|100)\s?%\s?(?:\w+\s+)?(?:off|discount|recurring|lifetime|for life)"
                          r"|(?:save|extra|additional)\s+(\d{1,2})\s?%|(?<![\d.])(\d{1,2}(?:\.\d)?)\s?折(?!扣)", re.I)
RECURRING_RX = re.compile(r"\d+\s?%\s?(?:off\s+)?(?:recurring|lifetime|for life|forever)|recurring\s+(?:\d+\s?%|discount)|"
                          r"(?:off|discount)\s+(?:for life|forever|lifetime)|discount is recurring|for life\b|"
                          r"(?:applies to )?every renewal|as long as (?:your )?(?:plan|service) (?:stays|remains) active|"
                          r"price freeze|renewal prices?(?: \w+){0,3} unchanged|续费同价|永久|循环", re.I)
FIRST_TERM_RX = re.compile(r"first (?:month|year|invoice|term|payment|billing cycle)|one-?time discount|"
                           r"renews? at (?:the )?(?:regular|normal|standard|full) price|renewals? (?:are|is) at (?:the )?"
                           r"(?:regular|normal|standard) price|discount duration|首期|首月|首年|仅首次", re.I)
CODE_LABELLED = re.compile(r"(?:coupon|promo|discount|voucher)?\s*codes?\s*[:：]?\s*(?:is\s+)?[\"'“]?"
                           r"(?P<code>[A-Za-z0-9][A-Za-z0-9_-]{3,24})|(?:coupon|promo)\s*[:：]\s*(?P<code2>[A-Za-z0-9][A-Za-z0-9_-]{3,24})"
                           r"|优惠码\s*[:：]?\s*(?P<code3>[A-Za-z0-9_-]{4,24})", re.I)
CODE_FREE = re.compile(r"(?i:\b(?:use|with|w/|using|enter|apply))\s+(?P<code>[A-Z][A-Z0-9_-]{3,24})\b(?!\.\w)")
CODE_HARDWARE = re.compile(r"^(?:LP)?DDR\d|^PCIE|^GEN\d|^IPV\d|^TLS|^HTTP|^NVME|^RAID|^E\d-|^\d+(?:GB|TB|MB)$|^[A-Z]\d{1,2}$", re.I)
CODE_STOP = {"OFF", "BLACK", "FRIDAY", "VPS", "HTTPS", "HTTP", "NVME", "SSD", "HDD", "USD", "EUR", "GBP", "KVM", "IPV4",
             "IPV6", "DDOS", "LET", "LES", "RAM", "CPU", "EPYC", "RYZEN", "AMD", "INTEL", "SATA", "RAID", "BGP", "CN2",
             "GIA", "CAD", "AUD", "SOLD", "OUT", "FREE", "SALE", "DEAL", "DEALS", "CODE", "COUPON", "PROMO", "LINUX",
             "WINDOWS", "BELOW", "ABOVE", "HERE", "THIS", "THAT", "YOUR", "WITH", "LIMITED", "STOCK", "ORDER", "ORDERS",
             "THE", "AND", "FOR", "CHECKOUT", "INVOICE", "TICKET", "PAYPAL", "STRIPE", "CRYPTO", "UPI", "LTD", "LLC",
             "NEEDED", "REQUIRED", "APPLIED", "AUTOMATICALLY", "TOTAL", "FIRST", "MONTH", "YEAR", "RECURRING"}
LANDING = re.compile(r"black-?friday|blackfriday|cyber|promo|promotion|deals?\b|sale|special|offer|campaign|halloween|"
                     r"christmas|birthday|anniversary|lowendtalk|\blet\b", re.I)
NOT_MERCHANT = re.compile(r"//(?:[\w-]+\.)*(?:lowendtalk\.com|lowendspirit\.com|lowendbox\.com|nodeseek\.com|reddit\.com|"
                          r"facebook\.com|twitter\.com|x\.com|t\.me|discord\.gg|discord\.com|linkedin\.com|github\.com|"
                          r"youtube\.com|instagram\.com|tumblr\.com|trustpilot\.com|geekbench\.com|wikipedia\.org|"
                          r"getrewardful\.com|imgur\.com)(?:[/:?#]|$)|/aff\b|aff\.php|affiliate|^mailto:|^denied:", re.I)
GENERIC_TITLE_WORDS = re.compile(r"^(?:black friday|cyber monday|early|pre|sale|giveaway|halloween|christmas|special|"
                                 r"deals?|offers?|new|hot|flash|limited|exclusive|us|uk|eu|nl|de|dedicated|servers?|vps|"
                                 r"kvm|storage|hosting|summer|winter|budget|cheap|big ram)\b", re.I)
HOST_PREFIX = re.compile(r"^(?:www|my|billing|members|clients?|panel|manager|portal|order|store|shop|cp|dash|account)\.")


def domain_label(href: str) -> str | None:
    host = (urlsplit(href).hostname or "").lower()
    if not host or NOT_MERCHANT.search(href):
        return None
    host = HOST_PREFIX.sub("", host)
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org") and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else None


def extract_vendor(title: str, text: str, links: list[Link], author: str | None, source: str) -> str | None:
    """商家：下单 / 官网链接的域名在标题或正文中的自称写法 → 标题前缀 → 发帖人（LET / LES）。"""
    prose = re.sub(r"https?://\S+|\S+@\S+|\S+\.(?:com|net|org|host|io|co|uk|eu|cloud)\S*/\S*", " ", f"{title}\n{text}")
    labels: list[str] = []
    for link in sorted(links, key=lambda lk: not (kw.PRICE.search(lk.anchor) or "order" in lk.anchor.lower())):
        if (label := domain_label(link.href)) and label not in labels:
            labels.append(label)
    for label in labels[:3]:
        exact = re.findall(rf"(?<![\w./@-])({re.escape(label)}(?:\.[a-z]{{2,6}})?)(?![\w-])", prose, re.I)
        if exact and all(e.islower() for e in exact) and not any(e.lower() in title.lower() for e in exact):
            exact = []  # 只以小写出现（邮箱、裸域名），不是商家自称
        if exact:
            best = max(set(exact), key=lambda e: (exact.count(e), any(c.isupper() for c in e)))
            if best.lower().endswith((".com", ".net", ".org")) and any(e.lower() == best.lower().rsplit(".", 1)[0] for e in exact):
                best = best.rsplit(".", 1)[0]
            return best
        anchors = [a for lk in links if domain_label(lk.href) == label
                   and (a := re.sub(r"[!.,:]+$", "", " ".join(lk.anchor.split())))
                   and len(a.split()) <= 3 and re.search(r"[A-Z]", a) and not re.search(
                       r"^(?:web ?site|here|order|buy|click|learn|visit|home|link|view|more)\b|https?:", a, re.I)]
        if anchors:  # 指向该域名的链接锚文本（"Mango Mail!"）
            return max(set(anchors), key=anchors.count)
        partial = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9]{4,}", prose)
                   if label.startswith(w.lower()) and (not w.islower() or w.lower() in title.lower())]
        if partial:
            return max(set(partial), key=lambda w: (len(w), partial.count(w)))
    prefix = re.match(r"^\s*\[([^\]]{2,30})\]|^\s*([A-Za-z][\w.&' -]{1,30}?)\s*(?:\||–|—|-\s|:)", title)
    if prefix:
        cand = (prefix.group(1) or prefix.group(2)).strip()
        while (m := GENERIC_TITLE_WORDS.search(cand.split()[-1] if cand.split() else "")) and len(cand.split()) > 1:
            cand = " ".join(cand.split()[:-1])
        if cand and not GENERIC_TITLE_WORDS.match(cand) and len(cand.split()) <= 3:
            return cand
    if source in ("lowendtalk", "lowendspirit") and author:
        return author
    return None


def extract_discount(title: str, text: str) -> tuple[int | None, str | None, Recurring]:
    best: tuple[int, str] | None = None
    for blob in (title, text):
        for m in DISCOUNT_PCT.finditer(blob):
            if m.group(3):  # 中文折扣：5 折 → 50，85 折 / 8.5 折 → 15
                v = float(m.group(3))
                pct = round(100 - (v * 10 if v < 10 else v))
            else:
                pct = int(m.group(1) or m.group(2))
            if 1 <= pct <= 100 and (best is None or pct > best[0]):
                start = max(0, m.start() - 0)
                best = (pct, blob[start:m.end()].strip())
    blob = f"{title}\n{text}"
    rec, first = bool(RECURRING_RX.search(blob)), bool(FIRST_TERM_RX.search(blob))
    recurring = (Recurring.MIXED if rec and first else Recurring.RECURRING if rec
                 else Recurring.FIRST_TERM if first else Recurring.UNKNOWN)
    return (best[0], best[1][:60]) if best else (None, None), recurring


def _plausible_code(code: str) -> bool:
    return not (code.upper() in CODE_STOP or CODE_HARDWARE.search(code)
                or not re.search(r"\d", code) and not (code.isupper() and len(code) >= 6) or re.fullmatch(r"\d+", code))


CODE_COLUMN = re.compile(r"coupon|promo ?code|\bcode\b|优惠码|折扣码", re.I)
CODE_CELL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{3,24}")


def column_codes(segs: list[Segment]) -> list[str]:
    """表头为 Coupon / Code 的那一列，单元格就是码（"Starter VPS | … | SNOWSTARTER1445 | Order"，D57）。"""
    out: list[str] = []
    for seg in segs:
        for line in seg.lines if seg.anchor == "row" and seg.header else ():
            for cell, label in labelled_cells(line, seg.header):
                if CODE_COLUMN.search(label) and CODE_CELL.fullmatch(cell) and _plausible_code(cell) and cell not in out:
                    out.append(cell)
    return out


def extract_codes(title: str, text: str) -> list[str]:
    blob = f"{title}\n{text}"
    found: list[tuple[int, str]] = []
    for rx in (CODE_LABELLED, CODE_FREE):
        for m in rx.finditer(blob):
            code = next(g for g in (m.groupdict().get(k) for k in ("code", "code2", "code3")) if g)
            if not _plausible_code(code) or re.search(r"no (?:coupon|code) (?:needed|required)",
                                                      blob[m.start():m.end() + 20], re.I):
                continue
            found.append((m.start("code") if m.groupdict().get("code") else m.start(), code))
    out: list[str] = []
    for _, code in sorted(found):
        if code not in out:
            out.append(code)
    # "HALLOWEEN" 只是 "HALLOWEEN50" 的一部分（"Halloween Coupon: HALLOWEEN50"）
    return [c for c in out if re.search(r"\d", c) or not any(o != c and o.startswith(c) for o in out)]


def extract_purchase_url(links: list[Link]) -> str | None:
    """黑五专题页或活动落地页 → 第一个下单链接 → 第一个商家链接（金标集规范 5.10）。"""
    merchant = [lk for lk in links if lk.href.startswith("http") and not NOT_MERCHANT.search(lk.href)]

    def site_path(lk: Link) -> str:  # 不看查询串：utm_medium=offers、utm_source=let 之类的跟踪参数不代表落地页
        parts = urlsplit(lk.href)
        return f"{parts.hostname or ''}{parts.path}"

    landing = [lk for lk in merchant if (LANDING.search(site_path(lk)) or LANDING.search(lk.anchor))
               and not order_refs(f"[{lk.n}]", links)]
    if landing:
        return min(landing, key=lambda lk: (not LANDING.search(site_path(lk)), len(urlsplit(lk.href).path), lk.n)).href
    orders = [lk for lk in merchant if order_refs(f"[{lk.n}]", links)]
    if orders:
        return orders[0].href
    return merchant[0].href if merchant else None


# ---------------------------------------------------------------- 组装


@dataclass
class Extraction:
    vendor: str | None
    discount_pct: int | None
    discount_text: str | None
    recurring: Recurring
    coupon_codes: list[str]
    purchase_url: str | None
    plan_format: PlanFormat
    plans: list[Plan]
    price_list: list[dict]
    completeness: float
    start_prices: list[dict] = field(default_factory=list)  # 帖子级起价（"from $5/mo"），不是套餐价格
    starts_at: date | None = None  # 活动起止日期（O-15，dates.py）
    ends_at: date | None = None


POST_DEFAULTS_HEAD = re.compile(r"(?:all|every|each)\s+(?:\w+\s+){0,2}(?:plans?|servers?|vps|packages?)\b.{0,40}"
                                r"(?:include|come|feature|get|:)|included on (?:all|every)|all plans\s*:", re.I)


def post_defaults(lines: list[str]) -> Config:
    """"All plans: … / Every plan includes: …" 及其后的列表行、"Location: …" 行，作为全帖默认值。"""
    cfg = Config()
    bare: list[float] = []
    for i, line in enumerate(lines):
        block: list[str] = []
        if POST_DEFAULTS_HEAD.search(line):
            block = [line] + [x for x in lines[i + 1:i + 8] if re.match(r"^\s*-\s", x)]
        elif re.match(r"^\s*-?\s*(?:locations?|datacenter|data center|机房)\s*[:：]", line, re.I):
            block = [line]
        for unit in block:
            for part in re.split(r",\s+|;\s+", unit):
                _apply_unit(cfg, part, "", bare)
        cfg.net += [t for t in net_in(" ".join(block)) if t not in cfg.net]
    return cfg


def _plan_code(seg: Segment, hit: PriceHit, links: list[Link], codes: list[str]) -> str | None:
    if hit.code and hit.code in codes:
        return hit.code
    for ref in seg.link_refs:
        if 1 <= ref <= len(links):
            query = parse_qs(urlsplit(links[ref - 1].href).query)
            for key in ("promocode", "coupon", "promo", "code"):
                for value in query.get(key, []):
                    if value in codes:
                        return value
    mentioned = [c for c in codes if c in seg.text]
    return mentioned[0] if len(mentioned) == 1 else None


def _order_url(seg: Segment, hit: PriceHit, links: list[Link]) -> str | None:
    after = hit.line[hit.end:hit.end + 25]
    for ref in [int(r) for r in LINK_REF.findall(after)] + seg.link_refs:
        if order_refs(f"[{ref}]", links):
            return links[ref - 1].href
    return None


def _clauses(seg: Segment) -> list[Segment]:
    """段落兜底块里一句写了多个套餐（"100GB plan for $8.70/mo or 30GB for $3.50/mo"）时按分句拆开。"""
    if seg.anchor != "spec_line" or len(PRICE_AMOUNT.findall(seg.lines[0])) < 2:
        return [seg]
    parts = [p for p in re.split(r"\s+or\s+|;\s*|，|。|!\s+", seg.lines[0]) if PRICE_AMOUNT.search(p)]
    if len(parts) < 2 or not all(SIZE_RX.search(p) for p in parts):
        return [seg]
    return [Segment(seg.anchor, seg.start, seg.end, [p], seg.context, seg.header, seg.link_refs) for p in parts]


def _plan_net(seg: Segment, cfg: Config, lines: list[str], defaults: Config, title_net) -> list[str]:
    """一个套餐的线路标签（O-16）：先看套餐自己的块与名称；没有就往上找最近的区块标题（12 行内、不超过 140 字、不带价格与列表符号）；
    再没有用"All plans"里写的，最后用标题里写的（整帖共用的线路）。"""
    if found := net_in("\n".join(seg.lines) + " " + (cfg.name or "")):
        return found
    for line in reversed(lines[max(0, seg.start - 12):seg.start]):
        heading = (len(line) <= 140 and not ROW_SEP.search(line) and not PRICE_AMOUNT.search(line)
                   and not re.match(r"\s*[-•●*]", line))  # 别的套餐的行、带价格的行、列表项都不是标题
        if heading and (found := net_in(line)):
            return found
    return list(defaults.net) or list(title_net)


def extract_plans(text: str, links: list[Link], segs: list[Segment], lang: str, codes: list[str],
                  storage_like: bool = False, free_ok: bool = False, title_net: list[str] = ()) -> list[Plan]:
    lines = text.splitlines()
    post_period = default_period(text)
    defaults = post_defaults(lines)
    plans: list[Plan] = []
    prev_cfg: Config | None = None
    for seg0 in segs:
        for seg in _clauses(seg0):
            header_period = None
            if seg.header:
                header_period = next((p for h in seg.header if re.search(r"price|cost|价", h, re.I)
                                      and (p := find_period(h))), None)
            hint = header_period or next((p for x in seg.lines + seg.context if not PRICE_AMOUNT.search(x)
                                          and (p := default_period(x))), None) or post_period
            hits: list[PriceHit] = []
            for line in seg.lines:
                if len(ROW_SEP.findall(line)) >= 2:  # 行内按单元格找价格，避免周期词串到别的单元格
                    for cell, label in labelled_cells(line, seg.header):
                        # 表头的列名给出该列的周期（"Monthly | Annual*"）与角色（"Regular | With SNOW50"），单元格自身写明的优先（D57）
                        role = column_role(label)
                        for h in price_hits(cell, lang, find_period(label) or hint):
                            if role and h.kind == "plain":
                                h.kind = role
                            if role == "promo" and h.code is None:
                                h.code = next((c for c in codes if c in label), None)
                            h.line = line
                            pos = line.find(cell)
                            h.start, h.end = h.start + pos, h.end + pos
                            hits.append(h)
                else:
                    hits += price_hits(line, lang, hint)
            selected = select_prices(hits)
            if not selected and free_ok and is_free_plan(seg):  # 免费产品块：0 价，币种与周期留空（D55）
                line = next(x for x in seg.lines if kw.FREE_PLAN.search(x))
                selected = [PriceHit(0.0, None, None, "plain", line, 0, len(line))]
            if not selected:
                continue
            cfg = parse_config(seg, lines[:seg.start], storage_like)
            if cfg.name is None and prev_cfg and seg.anchor != "row" and not seg.has_spec:
                cfg.name = prev_cfg.name  # 只有价格的块（第二个付费周期单独一行）沿用上一块的名称
            prev_cfg = cfg
            cfg.net = _plan_net(seg, cfg, lines, defaults, title_net)
            for name in ("bandwidth_tb", "port_gbps", "disk_type"):
                if getattr(cfg, name) is None and getattr(defaults, name) is not None:
                    setattr(cfg, name, getattr(defaults, name))
            if not cfg.locations and defaults.locations:
                cfg.locations = defaults.locations
            regular = [h for h in hits if h.kind == "regular"]
            for h in selected:
                disk = cfg.disk_gb
                if storage_like and (own := line_size(LINK_REF.sub("", h.line))):
                    disk = own  # 存储类一块列多档容量时，各价格取同一行的容量
                plans.append(Plan(
                    name=cfg.name, cpu_cores=cfg.cpu_cores, cpu_desc=cfg.cpu_desc, ram_gb=cfg.ram_gb,
                    disk_gb=disk, disk_type=cfg.disk_type, bandwidth_tb=cfg.bandwidth_tb,
                    port_gbps=cfg.port_gbps, locations=list(cfg.locations), net=list(cfg.net), price=h.amount,
                    currency=h.currency, period=h.period,
                    price_note=(PriceNote.REGULAR if h.kind == "regular"  # 只写了原价（"Regular" 列）
                                else PriceNote.PROMO if h.kind == "promo" or regular else None),
                    order_url=_order_url(seg, h, links), coupon_code=_plan_code(seg, h, links, codes),
                    notes="unmetered" if cfg.unmetered else None,
                ))
    unique: list[Plan] = []  # 同一套餐在正文里写了两遍
    seen: set = set()
    for p in plans:
        key = (p.name, p.ram_gb, p.disk_gb, p.price, p.currency, p.period, tuple(p.locations), p.bandwidth_tb,
               p.port_gbps)
        if key not in seen:
            seen.add(key)
            unique.append(p)
    plans = unique
    # 全帖通用的码不写进套餐（金标集规范 6.12）
    if plans and len({p.coupon_code for p in plans}) == 1 and plans[0].coupon_code and len(codes) == 1:
        for p in plans:
            p.coupon_code = None
    for i, p in enumerate(plans):
        p.position = i
    return plans


def completeness(ex: "Extraction", has_price: bool) -> float:
    """第 4.2 节：价格 0.30、周期 0.15、配置 0.25、商家 0.10、购买链接 0.10、优惠码或折扣 0.10（D63 去掉库存后重新分配）。"""
    plans = ex.plans
    score = 0.30 * (bool(plans) or has_price)
    score += 0.15 * bool(plans and all(p.period for p in plans))
    score += 0.25 * bool(plans and all(p.ram_gb or p.disk_gb for p in plans))
    score += 0.10 * bool(ex.vendor) + 0.10 * bool(ex.purchase_url)
    score += 0.10 * bool(ex.coupon_codes or ex.discount_pct or ex.discount_text)
    return round(score, 2)


def extract(source: str, title: str, text: str, links: list[Link], author: str | None, lang: str,
            has_html_table: bool, published: datetime | str | None = None) -> Extraction:
    free_ok = bool(kw.FREE_PLAN.search(title))  # 标题写明送免费主机 / VPS（D55）
    segs = segment(text, links, free_ok)
    codes = extract_codes(title, text)
    codes += [c for c in column_codes(segs) if c not in codes]  # 表格的码列（D57）
    (pct, dtext), recurring = extract_discount(title, text)
    plans = extract_plans(text, links, segs, lang, codes, storage_like=is_storage_like(title), free_ok=free_ok,
                          title_net=net_in(title))
    price_list = [{"amount": p.price, "currency": p.currency, "period": str(p.period) if p.period else None}
                  for p in plans]
    ex = Extraction(
        vendor=extract_vendor(title, text, links, author, source), discount_pct=pct, discount_text=dtext,
        recurring=recurring, coupon_codes=codes, purchase_url=extract_purchase_url(links),
        plan_format=plan_format(segs, has_html_table, bool(kw.PRICE.search(text))),
        plans=plans, price_list=price_list, completeness=0.0, start_prices=start_prices(title, text, lang),
    )
    ex.completeness = completeness(ex, bool(kw.PRICE.search(text)))
    found = find_dates(f"{title}\n{text}", published)
    ex.starts_at, ex.ends_at = found.starts, found.ends
    return ex
