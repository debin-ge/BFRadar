"""活动有效期（优化方案 O-15）：从帖子里找起止日期。

find_dates(text, published) → Dates(starts, ends, mentioned)：
  - 区间写法："from November 2 to December 2"、"Nov 24 - Dec 2"、"11月24日至12月1日"
  - 截止写法："valid until Nov 14"、"ends 01.11.2026"、"through December 31, 2023"、"截止 12 月 1 日"
  - 相对写法："48 hours only"、"限时 3 天"：按发布时间推算
  - mentioned 是原文里出现过的所有日期（不论上下文），LLM 给出的截止日期要落在这里面才采信（validate.py）

没有年份的日期按发布年份补；补出来比发布日期早一个多月的，进一年（"12 月发帖写 1 月 5 日"）。
结果落在发布日期前 60 天到后 400 天以外的丢掉：多半是价格锁定、历史事件，不是这次活动的期限。
一篇里有几个截止日期时取最晚的：宁可晚标"已结束"，也不要把还在进行的活动标成已结束。
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
          "nov": 11, "dec": 12}
PAST_DAYS = 60  # 截止日期最早可以比发布日期早这么多天
FUTURE_DAYS = 400  # 最晚可以比发布日期晚这么多天
ROLL_DAYS = 31  # 没写年份的日期，比发布日期早超过这么多天就算明年的
MAX_RELATIVE_HOURS = 24 * 14

# "May" 只认首字母大写或全大写：小写的 may 是助动词
_MON = (r"(?:(?i:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?"
        r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)|May|MAY)\.?")
_ORD = r"(?:st|nd|rd|th)?"
_YEAR = r"(?P<y>20\d\d)(?!\d)"

# 月名在前："November 2, 2026"、"Nov 30"、"Nov26,2026"
MON_DAY = re.compile(rf"(?<![A-Za-z])(?P<mon>{_MON})\s*(?P<d>\d{{1,2}}){_ORD}(?!\d)(?:\s*,?\s*{_YEAR})?")
# 日在前："30 Nov 2026"、"21st of December 2026"、"3 November"
DAY_MON = re.compile(rf"(?<![\w.])(?P<d>\d{{1,2}}){_ORD}\s+(?:of\s+)?(?P<mon>{_MON})(?![A-Za-z])(?:\s*,?\s*{_YEAR})?")
ISO = re.compile(r"(?<![\d.])(?P<y>20\d\d)[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})(?!\d)")
DMY_DOTTED = re.compile(r"(?<![\d.])(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>20\d\d)(?!\d)")
CN_DATE = re.compile(r"(?:(?P<y>20\d\d)\s*年\s*)?(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]?")
# 只在截止词后面认的短写法："until 11/30"、"截止 12.1"
SLASH = re.compile(r"(?<![\d/.])(?P<a>\d{1,2})/(?P<b>\d{1,2})(?:/(?P<y>(?:20)?\d\d))?(?![\d/])")
CN_DOTTED = re.compile(r"(?P<m>\d{1,2})\.(?P<d>\d{1,2})(?!\d)")

_FILLER = (r"[\s:,\-–—@]*(?:(?:\d{1,2}:\d{2}|\d{1,2}\s*(?:midnight|noon|[AaPp][Mm]))\s*(?:[AaPp][Mm])?\s*(?:[A-Z]{2,5}\b)?)?"
           r"[\s,]*(?:on\s+)?(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?,?\s*)?(?:the\s+)?")
END_CUE = re.compile(
    r"(?:\b(?:until|till|til|through|thru|ends?|ending|closes?|closing|expires?|expiring|expiry|deadline(?:\s+of)?|"
    r"(?:valid|available|good|runs?|running)\s+(?:until|till|through|thru|to))(?:\s+on)?|expireson|"
    r"截止(?:到|至|于)?|截至|活动至|优惠至|有效期(?:至|到)?|结束(?:时间|于|日期)|限时至|至|到)" + _FILLER + r"$",
    re.I)
SHORT_CUE = re.compile(r"(?:\b(?:until|till|through|thru|ends?|expires?|deadline)|截止|截至|活动至)" + _FILLER + r"$", re.I)
RANGE_GLUE = re.compile(r"^\s*(?:-|–|—|~|～|to|until|till|through|thru|and|至|到)\s*$", re.I)
RANGE_CUE = re.compile(r"(?:\b(?:from|between|runs?|running|valid|available|sale|promo(?:tion)?|event|offers?|period)\b"
                       r"|活动|时间|期间)[^.\n]{0,25}$", re.I)
BARE_DAY_END = re.compile(r"\s*(?:-|–|—|to|through|thru|至|到)\s*(?P<d>\d{1,2})(?:st|nd|rd|th)?(?!\d|\s*(?:GB|TB|MB|G\b|T\b|%|"
                          r"vcpu|cores?|gbps|mbps|usd|eur))", re.I)

# 相对写法：必须带"只、限时、闪购"之类的词，"24 hour trial""within 48 hours of order"不算
REL_HOURS = re.compile(
    r"(?:\b(?:only|for|in|next|within)\s+(?:the\s+)?(?:next\s+|last\s+)?(?P<a>\d{1,3})[\s-]*(?P<ua>hours?|days?)\b"
    r"(?!\s*(?:of|after|from|refund|trial|support|ticket|notice|money))"
    r"|\b(?P<b>\d{1,3})[\s-]*(?P<ub>hours?|days?)\s+(?:only|flash|sale|promo|promotion|deal|offer|left|remaining)\b"
    r"|限时\s*(?P<c>\d{1,3})\s*(?P<uc>小时|天|日)|(?P<d>\d{1,3})\s*(?P<ud>小时|天)\s*(?:限时|限定|内有效))", re.I)
RELATIVE_WORDS = re.compile(r"\b(?:only|flash|sale|promo|promotion|deal|offer|limited|special|ends?|expires?)\b|限时|限定|活动",
                            re.I)


@dataclass(frozen=True)
class Dates:
    starts: date | None = None
    ends: date | None = None
    mentioned: frozenset[date] = frozenset()


@dataclass
class _Token:
    start: int
    end: int
    month: int
    day: int
    year: int | None
    kind: str  # text / numeric / cn


def as_datetime(published: datetime | date | str | None) -> datetime | None:
    if published is None:
        return None
    if isinstance(published, str):
        try:
            published = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(published, datetime):
        published = datetime(published.year, published.month, published.day)
    return published


def _month(word: str) -> int:
    return MONTHS[word.lower()[:3]]


def _tokens(text: str) -> list[_Token]:
    """原文里的全部日期写法（不看上下文），按位置排序；互相重叠的留先出现、更长的一个。"""
    found: list[_Token] = []
    for m in MON_DAY.finditer(text):
        found.append(_Token(m.start(), m.end(), _month(m["mon"]), int(m["d"]), int(m["y"]) if m["y"] else None, "text"))
    for m in DAY_MON.finditer(text):
        found.append(_Token(m.start(), m.end(), _month(m["mon"]), int(m["d"]), int(m["y"]) if m["y"] else None, "text"))
    for m in ISO.finditer(text):
        found.append(_Token(m.start(), m.end(), int(m["m"]), int(m["d"]), int(m["y"]), "numeric"))
    for m in DMY_DOTTED.finditer(text):
        found.append(_Token(m.start(), m.end(), int(m["m"]), int(m["d"]), int(m["y"]), "numeric"))
    for m in CN_DATE.finditer(text):
        found.append(_Token(m.start(), m.end(), int(m["m"]), int(m["d"]), int(m["y"]) if m["y"] else None, "cn"))
    found.sort(key=lambda t: (t.start, -(t.end - t.start)))
    out: list[_Token] = []
    for t in found:
        if out and t.start < out[-1].end:
            continue
        if 1 <= t.month <= 12 and 1 <= t.day <= 31:
            out.append(t)
    return out


def _resolve(month: int, day: int, year: int | None, published: date | None) -> date | None:
    """补年份：写了就用；没写按发布年份，比发布日期早 ROLL_DAYS 天以上的进一年。"""
    try:
        if year is not None:
            return date(year, month, day)
        if published is None:
            return None
        d = date(published.year, month, day)
        if d < published - timedelta(days=ROLL_DAYS):
            d = date(published.year + 1, month, day)
        return d
    except ValueError:  # 2 月 30 日之类
        return None


def plausible(d: date | None, published: date | None) -> bool:
    if d is None:
        return False
    if published is None:
        return True
    return published - timedelta(days=PAST_DAYS) <= d <= published + timedelta(days=FUTURE_DAYS)


def _before(text: str, pos: int, width: int = 48) -> str:
    return text[max(0, pos - width):pos]


def find_dates(text: str, published: datetime | date | str | None = None) -> Dates:
    pub_dt = as_datetime(published)
    pub = pub_dt.date() if pub_dt else None
    tokens = _tokens(text)
    resolved = [_resolve(t.month, t.day, t.year, pub) for t in tokens]
    mentioned = {d for d in resolved if d}
    starts: list[date] = []
    ends: list[date] = []
    used: set[int] = set()

    # ---- 区间：两个日期之间只隔着 "-"、"to"、"至" 之类
    for i in range(len(tokens) - 1):
        a, b = tokens[i], tokens[i + 1]
        if RANGE_GLUE.match(text[a.end:b.start]):
            end = _resolve(b.month, b.day, b.year, pub)
            year = a.year if a.year is not None else (end.year if end else None)
            start = _resolve(a.month, a.day, year, pub if a.year is None and year is None else None)
            if start and end and start > end and a.year is None:  # "Dec 20 - Jan 5"：起点在上一年
                start = _resolve(a.month, a.day, end.year - 1, None)
            if start and end and start <= end and (end - start).days <= 120 and plausible(end, pub):
                starts.append(start)
                ends.append(end)
                used.update((i, i + 1))
    # "Nov 24-30"：后一个只有日，要有"从、期间、活动"之类的词才认
    for i, t in enumerate(tokens):
        if i in used or t.kind != "text":
            continue
        m = BARE_DAY_END.match(text, t.end)
        if m and RANGE_CUE.search(_before(text, t.start, 40)):
            day = int(m["d"])
            start = _resolve(t.month, t.day, t.year, pub)
            end = _resolve(t.month, day, start.year if start else t.year, None) if start else None
            if start and end and start < end and (end - start).days <= 120 and plausible(end, pub):
                starts.append(start)
                ends.append(end)
                mentioned.add(end)  # "Nov 24-30" 的 30 日也算原文出现过
                used.add(i)

    # ---- 截止的说法（起点只认区间里的："festival starts on Nov 8" 说的不是这次活动）
    for i, t in enumerate(tokens):
        if i in used:
            continue
        head = _before(text, t.start)
        d = resolved[i]
        if END_CUE.search(head) and plausible(d, pub):
            ends.append(d)
    # 短写法 "until 11/30"、"截止 12.1"：只在截止词后面认
    for m in SLASH.finditer(text):
        if not SHORT_CUE.search(_before(text, m.start())):
            continue
        a, b = int(m["a"]), int(m["b"])
        month, day = (b, a) if a > 12 else (a, b)  # 前一个大于 12 只能是日（欧式）
        year = m["y"]
        year = (2000 + int(year) if len(year) == 2 else int(year)) if year else None
        if 1 <= month <= 12 and 1 <= day <= 31 and plausible(d := _resolve(month, day, year, pub), pub):
            ends.append(d)
            mentioned.add(d)
    for m in CN_DOTTED.finditer(text):
        if re.search(r"(?:截止|截至|活动至)" + _FILLER + r"$", _before(text, m.start())):
            month, day = int(m["m"]), int(m["d"])
            if 1 <= month <= 12 and 1 <= day <= 31 and plausible(d := _resolve(month, day, None, pub), pub):
                ends.append(d)
                mentioned.add(d)

    # ---- 相对写法：按发布时间推算；原文已经写了日期的，以日期为准（推算出的日期仍算"出现过"，供校验 LLM 用）
    if pub_dt is not None:
        explicit = bool(ends)
        for m in REL_HOURS.finditer(text):
            n = m["a"] or m["b"] or m["c"] or m["d"]
            unit = (m["ua"] or m["ub"] or m["uc"] or m["ud"]).lower()
            hours = int(n) * (24 if unit in ("day", "days", "天", "日") else 1)
            if not 1 <= hours <= MAX_RELATIVE_HOURS:
                continue
            if m["a"] and not RELATIVE_WORDS.search(text[max(0, m.start() - 60):m.end() + 40]):
                continue  # "for 24 hours" 这类要附近有"限时、只、活动"之类的词
            d = (pub_dt + timedelta(hours=hours)).date()
            mentioned.add(d)
            if not explicit:
                ends.append(d)

    return Dates(min(starts) if starts else None, max(ends) if ends else None, frozenset(mentioned))
