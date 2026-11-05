"""记录分类（《需求与方案说明》第 4.2、12.3 节；《金标集标注规范》第 5.1–5.5、7 节）。

输入为标题、结构化正文与源自带的分类标签；输出记录类型、是否黑五、活动类型、产品类型、语言与情报类别。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..models import EventType, IntelCategory, ProductType, RecordKind
from . import keywords as kw

LEAD_CHARS = 400  # "主要主题"看标题与正文开头
# LEB 的文章分类直接区分优惠与资讯（11-02 实测：Low End Virtual / Special Offers / Giveaways 与 Editorial & News）
LEB_NEWS = {"editorial & news", "industry news", "tutorials", "reviews"}
LEB_OFFER = {"special offers", "giveaways", "low end virtual", "low end dedicated", "low end web hosting",
             "low end storage"}
# NodeSeek 分类：promotion 为商家推广，photo-share 为贴图（排除）
NS_RESALE, NS_CARPOOL, NS_REVIEW, NS_PROMOTION, NS_PHOTO = "trade", "carpool", "review", "promotion", "photo-share"


@dataclass
class Classification:
    record_kind: RecordKind
    is_black_friday: bool
    event_type: EventType
    product_types: list[ProductType]
    lang: str
    intel_category: IntelCategory | None = None
    evidence: dict = field(default_factory=dict)  # 判定依据，便于排查


def classify(source: str, title: str, text: str, categories: Sequence[str] = ()) -> Classification:
    cats = {c.strip().lower() for c in categories}
    lang = detect_lang(title, text)
    kind = record_kind(source, title, text, cats)
    evidence: dict = {}
    products: list[ProductType] = []
    if kind is RecordKind.OFFER:
        products = product_types(title, text, default=ProductType.VPS)
        if not products:  # 帖子卖的只是四类以外的产品（邮箱、域名、托管机柜…）：不收（D62）
            kind = RecordKind.IGNORE
            evidence["scope"] = "产品不在收录范围内"
        elif kw.HARDWARE_SALE.search(title):  # 卖断硬件，不是租用（优化方案 O-04）
            kind, products = RecordKind.IGNORE, []
            evidence["scope"] = "出售硬件，不是租用"
    if kind is RecordKind.OFFER:
        bf = black_friday_offer(title, text, evidence)
        event = event_type(title, text, bf, evidence)
        intel_cat = None
    else:
        # 情报只看标题是否含黑五词（金标集规范 5.2），活动类型一律 other
        bf = bool(_bf_hits(title)) and not _negated_title(title)
        event = EventType.OTHER
        intel_cat = intel_category(source, title, cats) if kind is RecordKind.INTEL else None
        if kind is not RecordKind.IGNORE:
            products = product_types(title, text, default=None)
    return Classification(kind, bf, event, products, lang, intel_cat, evidence)


# ---------------------------------------------------------------- 记录类型


def record_kind(source: str, title: str, text: str, cats: set[str]) -> RecordKind:
    if source in ("lowendtalk", "lowendspirit"):  # Offers 分类全部为厂商促销
        return RecordKind.OFFER
    if source == "lowendbox":
        if cats & LEB_NEWS:
            return RecordKind.INTEL
        if cats & LEB_OFFER or _has_offer_signal(f"{title}\n{text}"):
            return RecordKind.OFFER
        return RecordKind.INTEL
    blob = f"{title}\n{text}"
    if source == "nodeseek":
        if NS_PHOTO in cats:
            return RecordKind.IGNORE
        if NS_PROMOTION in cats and kw.PROMO.search(blob):
            return RecordKind.OFFER
        if NS_REVIEW in cats:  # 测评区基本都是主机测评，多用 NQ/TQ 与商家名而不写产品词
            return RecordKind.INTEL
        if cats & {NS_RESALE, NS_CARPOOL}:
            return RecordKind.INTEL if kw.PRODUCT.search(blob) else RecordKind.IGNORE
    # NodeSeek 其他分类与 Reddit：只有情报与无关两种
    return RecordKind.INTEL if kw.PRODUCT.search(blob) else RecordKind.IGNORE


def _has_offer_signal(blob: str) -> bool:
    """LEB 未知分类时的兜底：有价格且有折扣、优惠码或促销词。"""
    return bool(kw.PRICE.search(blob) and (kw.DISCOUNT.search(blob) or kw.OFFER_WORDS.search(blob)))


def intel_category(source: str, title: str, cats: set[str]) -> IntelCategory:
    if source == "lowendbox":
        return IntelCategory.NEWS
    if NS_RESALE in cats or re.match(r"^\s*[\[【(（]?\s*(?:出|收|转)", title):
        return IntelCategory.RESALE
    if NS_CARPOOL in cats or re.search(r"拼车|合租|车找人|人找车|上车", title):
        return IntelCategory.CARPOOL
    if NS_REVIEW in cats or re.search(r"测评|评测|跑分|review|benchmark|yabs", title, re.I):
        return IntelCategory.REVIEW
    if kw.QUESTION.search(title.strip()):
        return IntelCategory.QUESTION
    return IntelCategory.DISCUSSION


# ---------------------------------------------------------------- 黑五


def _bf_hits(text: str) -> list[re.Match]:
    hits = list(kw.BLACK_FRIDAY.finditer(text)) + list(kw.BF_SHORT.finditer(text)) + list(kw.CYBER_MONDAY.finditer(text))
    return sorted(hits, key=lambda m: m.start())


def _sentence(text: str, pos: int) -> tuple[str, int]:
    """包含 pos 的句子及 pos 在句中的偏移。"""
    start = max(text.rfind(c, 0, pos) for c in ".!?。！？\n") + 1
    ends = [i for c in ".!?。！？\n" if (i := text.find(c, pos)) != -1]
    end = min(ends) + 1 if ends else len(text)
    return text[start:end], pos - start


def _negated(sentence: str, offset: int) -> bool:
    """黑五词前 4 个词（中文 6 个字）内有否定词，或句中是"比黑五还…"。"""
    if kw.COMPARATIVE_ZH.search(sentence):
        return True
    before = sentence[:offset]
    window = " ".join(before.split()[-4:]) + before[-6:]
    if kw.NOT_NEGATION.search(window):
        return False
    return bool(kw.NEGATION.search(window))


def _negated_title(title: str) -> bool:
    return all(_negated(*_sentence(title, m.start())) for m in _bf_hits(title)) if _bf_hits(title) else False


def black_friday_offer(title: str, text: str, evidence: dict) -> bool:
    """标题含黑五词（未被否定）→ 是；否则看正文：去掉否定句与猜测句后仍有提及才算。

    标题已写明其他活动（节日、周年、补货、上新）时，正文提及还须与 offer / deal / 折扣同句，
    以排除"万圣节活动里顺带说黑五见"之类（金标集规范 5.2）。
    """
    title_hits = [m for m in _bf_hits(title) if not _negated(*_sentence(title, m.start()))]
    if title_hits:
        evidence["black_friday"] = f"标题：{title_hits[0].group(0)}"
        return True
    other_event = any(p.search(title) for p in (kw.HOLIDAY, kw.ANNIVERSARY, kw.RESTOCK, kw.LAUNCH))
    for m in _bf_hits(text):
        sentence, offset = _sentence(text, m.start())
        if _negated(sentence, offset) or kw.SPECULATIVE.search(sentence):
            continue
        if other_event and not kw.OFFER_WORDS.search(sentence):
            continue
        evidence["black_friday"] = f"正文：{sentence.strip()[:100]}"
        return True
    return False


# ---------------------------------------------------------------- 活动类型


def event_type(title: str, text: str, is_bf: bool, evidence: dict) -> EventType:
    """按金标集规范 5.3 的优先级取一个主类型。"""
    lead = f"{title}\n{text[:LEAD_CHARS]}"
    has_offer = bool(kw.PRICE.search(text) or kw.DISCOUNT.search(f"{title}\n{text}"))
    has_giveaway = bool(kw.GIVEAWAY.search(f"{title}\n{text}"))
    if is_bf:
        if not (has_offer or has_giveaway):  # 只有预告、没有价格与折扣
            evidence["event"] = "黑五预告帖"
            return EventType.OTHER
        bf_proper = kw.BLACK_FRIDAY.search(f"{title}\n{text}") or kw.BF_SHORT.search(f"{title}\n{text}")
        return EventType.BLACK_FRIDAY if bf_proper else EventType.CYBER_MONDAY
    for pattern, event in ((kw.HOLIDAY, EventType.HOLIDAY), (kw.ANNIVERSARY, EventType.ANNIVERSARY),
                           (kw.RESTOCK, EventType.RESTOCK), (kw.LAUNCH, EventType.LAUNCH)):
        if m := pattern.search(lead):
            evidence["event"] = m.group(0)
            return event
    if has_giveaway and not has_offer:
        return EventType.GIVEAWAY
    return EventType.SALE if has_offer else EventType.OTHER


# ---------------------------------------------------------------- 产品类型


def product_types(title: str, text: str, default: ProductType | None) -> list[ProductType]:
    """按"实际在售"打分：标题命中 3 分，带价格的行命中 2 分，其余正文命中 1 分（封顶 2）；≥ 3 分入选。

    只返回 VPS、独服、虚拟主机、存储四类（D62）：入选的取全部；都不够时取最高分的一个；完全没有命中时，优惠记录默认
    vps（LET/LES 最常见），情报为空。邮箱、域名、托管机柜等其他产品参与打分，但只有它们明确入选（≥ 3 分）而四类产品
    一点证据也没有时才返回空列表，调用方丢弃该帖；证据弱的（< 3 分）不算数，宁可多收，留给 LLM 判断。
    """
    price_lines = [line for line in text.splitlines() if kw.PRICE.search(line)]
    scores: dict[str, int] = {}
    for name, pattern in {**kw.PRODUCT_TYPES, **kw.OUT_OF_SCOPE_PRODUCTS}.items():
        score = 3 * bool(pattern.search(title))
        score += 2 * any(pattern.search(line) for line in price_lines)
        score += min(2, len(pattern.findall(text)))
        if score:
            scores[name] = score
    wanted = {n: s for n, s in scores.items() if n in kw.PRODUCT_TYPES}
    if chosen := [n for n, s in wanted.items() if s >= 3]:
        return sorted((ProductType(n) for n in chosen), key=list(ProductType).index)
    if wanted:
        return [ProductType(max(wanted, key=lambda n: (wanted[n], -list(kw.PRODUCT_TYPES).index(n))))]
    if any(s >= 3 for s in scores.values()):
        return []
    return [default] if default else []


# ---------------------------------------------------------------- 语言


def detect_lang(title: str, text: str) -> str:
    """中英混排以字符多者为准（金标集规范 5.5）：汉字按 1、拉丁字母按 1/3 计（一个汉字约等于一个词）。"""
    sample = f"{title}\n{text[:2000]}"
    return "zh" if len(kw.CJK.findall(sample)) * 3 >= len(kw.LATIN.findall(sample)) else "en"
