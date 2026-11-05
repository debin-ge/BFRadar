"""套餐分段（《需求与方案说明》第 12.4 节）：把结构化正文切成套餐块，供字段规则（M1.8）与 LLM 使用。

锚点按优先级：
  1. 行：含两个以上分隔符（| || · │）的行。表格与竖线写法在结构化文本里形态相同；带价格的行各成一块，
     同组内无价格的表头与共用配置行作为上下文。
  2. 下单链接：以下单链接所在行为块尾，向上收到套餐标题行、空行、长段落或上一个块为止。
  3. 套餐标题行：短行且其后紧跟至少两行规格（"UK EPYC E4"、"Budget VPS 1"），收到下一个标题行为止。
  4. 兜底（规范外补充）：同时有价格与规格的单行，如列表项 "Starter, 5GB: £1/mo" 或段落句子。
不含价格的块丢弃；块外的孤立价格只进帖子级价格列表。只有价格、缺少规格的块从紧邻的上一块继承规格作为上下文。
标题写明送免费主机 / VPS 时（free_ok），块里写着免费产品（"Free NVMe Shared Hosting"）也算有价格，记为 0 价（D55）。
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from ..models import PlanFormat
from ..textutil import Link
from . import keywords as kw

ROW_SEP = re.compile(r"\||·|│")
LINK_REF = re.compile(r"\[(\d+)\]")
LIST_ITEM = re.compile(r"^\s*-\s")
KEY_VALUE = re.compile(r"^[A-Za-z][\w /&+.-]{0,24}:\s*\S")  # "RAM: 13GB"、"Price: $123" 这类字段行
BOILERPLATE = re.compile(r"^The post .* appeared first on ")  # LEB 文末固定句，会重复标题里的价格
SPEC = re.compile(
    r"\d+(?:\.\d+)?\s?(?:[GMT]B|[GMT]iB|GHz|[GM]bps|[GM]bit|G\b|T\b)|vCPU|vCore|\bcores?\b|\bCPU\b|\bRAM\b|memory|"
    r"\bdisk\b|\bSSD\b|NVMe|\bHDD\b|bandwidth|traffic|transfer|\bport\b|IPv4|\bBW\b|核|内存|硬盘|流量|带宽", re.I)
RAM = re.compile(r"\bRAM\b|memory|内存|\d+\s?[GM]B\s+(?:ECC\s+)?(?:DDR\d)|\d\s?[hc核]\s?\d+\s?g", re.I)
ORDER_ANCHOR = re.compile(r"order|buy|purchase|\bcart\b|checkout|sign ?up|deploy|get it|click here|grab|"
                          r"立即购买|购买|下单|订购", re.I)
ORDER_HREF = re.compile(r"cart\.php|/cart\b|/cart/|/order|[?&]pid=|/product|/store/|checkout|[?&]add=|rp=/store", re.I)
NOT_ORDER = re.compile(r"looking glass|terms|\btos\b|status|faq|knowledgebase|test (?:ip|file)|benchmark|review|"
                       r"discord|telegram|facebook|twitter|linkedin|affiliate", re.I)
MAX_BACK = 15  # 下单链接向上最多收的行数
MAX_TITLE_BLOCK = 20
PROSE_LEN = 120
TITLE_LEN = 60


@dataclass
class Segment:
    anchor: str  # row / order_link / title / spec_line
    start: int  # 起止行号（含），对应结构化文本的行
    end: int
    lines: list[str]
    context: list[str] = field(default_factory=list)  # 表头、共用配置行或继承的规格行
    header: list[str] | None = None
    link_refs: list[int] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def has_spec(self) -> bool:
        return any(SPEC.search(line) for line in self.lines)

    @property
    def has_ram(self) -> bool:
        return any(RAM.search(line) for line in self.lines + self.context)


def has_plan_price(line: str) -> bool:
    """行内有不带限定语的价格（排除 from / over / up to / 每 GB 单价等）。"""
    for m in kw.PRICE.finditer(line):
        before = line[max(0, m.start() - 25):m.start()]
        after = line[m.end():m.end() + 15]
        if not kw.PRICE_QUALIFIER_BEFORE.search(before) and not kw.PRICE_PER_UNIT_AFTER.search(after):
            return True
    return False


def cells(line: str) -> list[str]:
    return [c.strip() for c in ROW_SEP.split(line) if c.strip()]


def _is_row(line: str) -> bool:
    return len(ROW_SEP.findall(line)) >= 2


def _looks_header(line: str) -> bool:
    parts = cells(line)
    return len(parts) >= 2 and sum(not re.search(r"\d", c) for c in parts) / len(parts) >= 0.6


HEADER_WORD = re.compile(r"plan|name|package|product|cpu|core|thread|ram|memory|storage|disk|ssd|nvme|hdd|space|"
                         r"bandwidth|traffic|transfer|\bbw\b|port|network|uplink|\bip|location|datacenter|price|cost|"
                         r"month|year|annual|order|buy|link|spec|config|套餐|内存|硬盘|流量|价格|机房|配置", re.I)


def header_score(parts: list[str]) -> float:
    """像字段名的单元格占比："Plan | vCPU | RAM | SSD" 为 1，"KVM virtualization · BGP sessions available" 为 0。"""
    return sum(bool(HEADER_WORD.search(p)) for p in parts) / len(parts) if parts else 0.0


def _is_prose(line: str) -> bool:
    """段落句子：很长且规格词少；或较长、以句末标点结尾、没有规格词也没有价格。块不跨过这类行。"""
    stripped = line.strip()
    if len(stripped) > PROSE_LEN and len(SPEC.findall(stripped)) < 2:
        return True
    return (len(stripped) > TITLE_LEN and stripped[-1:] in ".!?。！？)" and not SPEC.search(stripped)
            and not kw.PRICE.search(stripped))


def is_title(lines: list[str], k: int) -> bool:
    """短行、不是列表项或"字段: 值"行、自身最多一个规格词（"4GB Budget VPS"），且其后 3 行内至少两行是规格行。"""
    line = lines[k].strip()
    if (not line or len(line) > TITLE_LEN or LIST_ITEM.match(line) or KEY_VALUE.match(line)
            or len(SPEC.findall(line)) > 1 or _is_row(line)):
        return False
    following = [x for x in lines[k + 1:k + 4] if x.strip()]
    return sum(bool(SPEC.search(x)) and not kw.PRICE.search(x) for x in following) >= 2


def order_refs(line: str, links: list[Link]) -> list[int]:
    out = []
    for ref in LINK_REF.findall(line):
        n = int(ref)
        if 1 <= n <= len(links):
            link = links[n - 1]
            if (ORDER_ANCHOR.search(link.anchor) or ORDER_HREF.search(link.href)) and not NOT_ORDER.search(link.anchor):
                out.append(n)
    return out


def is_free_plan(seg: Segment) -> bool:
    """没有数字价格、写着免费产品且有规格的块（D55）。"""
    return (not any(has_plan_price(x) for x in seg.lines) and any(kw.FREE_PLAN.search(x) for x in seg.lines)
            and seg.has_spec)


def segment(text: str, links: list[Link], free_ok: bool = False) -> list[Segment]:
    """free_ok：帖子标题写明送免费主机 / VPS（由调用方判断），这时免费产品块也算套餐块。"""
    lines = text.splitlines()
    used = [bool(BOILERPLATE.match(line)) for line in lines]
    segs = _rows(lines, used)
    segs += _order_blocks(lines, used, links)
    segs += _title_blocks(lines, used)
    segs += _spec_lines(lines, used)
    segs = sorted((s for s in segs if any(has_plan_price(x) for x in s.lines) or (free_ok and is_free_plan(s))),
                  key=lambda s: s.start)
    _inherit_context(segs)
    for s in segs:
        s.link_refs = [int(r) for line in s.lines for r in LINK_REF.findall(line)]
    return segs


def _rows(lines: list[str], used: list[bool]) -> list[Segment]:
    segs: list[Segment] = []
    i, n = 0, len(lines)
    while i < n:
        if not _is_row(lines[i]):
            i += 1
            continue
        j = i
        while j < n and _is_row(lines[j]):
            j += 1
        header: list[str] | None = None
        config: str | None = None
        group: list[Segment] = []
        for k in range(i, j):
            line = lines[k]
            if not kw.PRICE.search(line):
                if not group and _looks_header(line) and (header is None or header_score(cells(line))
                                                          >= header_score(header)):
                    # 表头上方紧挨着一行用 · 分隔的特性（"KVM virtualization · … · Unlimited traffic"）也像表头，
                    # 取更像字段名的那行（D56）；被替下的若含规格词，作共用配置行
                    if header is not None and SPEC.search(" | ".join(header)):
                        config = " | ".join(header)
                    header = cells(line)
                elif SPEC.search(line):
                    config = line  # 共用配置行（竖线写法常把配置与各周期价格分行）
                continue
            context = [" | ".join(header)] if header else []
            if config and not SPEC.search(line):
                context.append(config)
            group.append(Segment("row", k, k, [line], context, header))
        if group:
            for k in range(i, j):
                used[k] = True
            segs += group
        i = j
    return segs


def _order_blocks(lines: list[str], used: list[bool], links: list[Link]) -> list[Segment]:
    segs: list[Segment] = []
    boundary = -1
    for i, line in enumerate(lines):
        if used[i] or not order_refs(line, links):
            continue
        start = i
        k = i - 1
        while k > boundary and i - k <= MAX_BACK and not used[k]:
            if not lines[k].strip() or _is_prose(lines[k]):
                break
            start = k
            if is_title(lines, k):
                break
            k -= 1
        segs.append(Segment("order_link", start, i, lines[start:i + 1]))
        for x in range(start, i + 1):
            used[x] = True
        boundary = i
    return segs


def _title_blocks(lines: list[str], used: list[bool]) -> list[Segment]:
    segs: list[Segment] = []
    k, n = 0, len(lines)
    while k < n:
        if used[k] or not is_title(lines, k):
            k += 1
            continue
        end = k
        while (end + 1 < n and end + 1 - k < MAX_TITLE_BLOCK and not used[end + 1] and lines[end + 1].strip()
               and not _is_prose(lines[end + 1]) and not is_title(lines, end + 1)):
            end += 1
        segs.append(Segment("title", k, end, lines[k:end + 1]))
        for x in range(k, end + 1):
            used[x] = True
        k = end + 1
    return segs


def _spec_lines(lines: list[str], used: list[bool]) -> list[Segment]:
    segs = []
    for k, line in enumerate(lines):
        if not used[k] and kw.PRICE.search(line) and SPEC.search(line):
            segs.append(Segment("spec_line", k, k, [line]))
            used[k] = True
    return segs


def _inherit_context(segs: list[Segment]) -> None:
    """缺规格的块从紧邻的上一块继承：无任何规格时继承全部规格行；缺内存且上一块有内存时也继承。"""
    for prev, seg in zip(segs, segs[1:]):
        if prev.end + 1 != seg.start and not (seg.anchor == "row" and prev.anchor == "row"):
            continue
        if seg.context and seg.has_spec:
            continue
        if not seg.has_spec or (not seg.has_ram and prev.has_ram):
            spec_lines = [x for x in prev.context + prev.lines if SPEC.search(x) and not kw.PRICE.search(x)]
            seg.context = seg.context + [x for x in spec_lines if x not in seg.context]


def plan_format(segs: list[Segment], has_html_table: bool, has_price: bool) -> PlanFormat:
    """帖子的套餐呈现形态（金标集规范 5.12）。

    行是最强的结构信号：行块占四成以上即为 table / pipe（KhanWebHost 竖线行 5 块、NAT 列表 6 块，金标为 pipe）；
    否则按块数取多数，并列时按 list > freetext。
    """
    if not segs:
        return PlanFormat.FREETEXT if has_price else PlanFormat.NONE
    rows = sum(s.anchor == "row" for s in segs)
    if rows / len(segs) >= 0.4:
        return PlanFormat.TABLE if has_html_table else PlanFormat.PIPE
    counts: Counter = Counter()
    for s in segs:
        if s.anchor == "row":
            counts[PlanFormat.TABLE if has_html_table else PlanFormat.PIPE] += 1
        elif s.anchor == "spec_line" and not LIST_ITEM.match(s.lines[0]):
            counts[PlanFormat.FREETEXT] += 1
        else:
            counts[PlanFormat.LIST] += 1
    order = [PlanFormat.TABLE, PlanFormat.PIPE, PlanFormat.LIST, PlanFormat.FREETEXT]
    return max(counts, key=lambda f: (counts[f], -order.index(f)))  # 行占比不足四成时 table / pipe 很少胜出
