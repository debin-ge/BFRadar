"""数据模型与枚举（《需求与方案说明》第 4.2、11 节）。

枚举取值与数据库 CHECK 约束、金标集标注取值一致；字段名与数据库列名一致（JSON 列在模型中为 list / dict）。
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class RecordKind(StrEnum):
    OFFER = "offer"
    INTEL = "intel"
    IGNORE = "ignore"


class EventType(StrEnum):  # 按此顺序为多重命中时的优先级（金标集规范 5.3）
    BLACK_FRIDAY = "black_friday"
    CYBER_MONDAY = "cyber_monday"
    HOLIDAY = "holiday"
    ANNIVERSARY = "anniversary"
    RESTOCK = "restock"
    LAUNCH = "launch"
    GIVEAWAY = "giveaway"
    SALE = "sale"
    OTHER = "other"


class ProductType(StrEnum):  # 只收这四类；邮箱、域名、托管机柜等其他产品的帖子不入库（D62）
    VPS = "vps"
    DEDICATED = "dedicated"
    HOSTING = "hosting"
    STORAGE = "storage"


class Recurring(StrEnum):
    RECURRING = "recurring"
    FIRST_TERM = "first_term"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class PlanFormat(StrEnum):
    PIPE = "pipe"
    TABLE = "table"
    LIST = "list"
    FREETEXT = "freetext"
    NONE = "none"


class DiskType(StrEnum):
    NVME = "NVMe"
    SSD = "SSD"
    HDD = "HDD"


class Period(StrEnum):
    MONTH = "month"
    QUARTER = "quarter"
    SEMI = "semi"
    YEAR = "year"
    BIENNIAL = "biennial"
    TRIENNIAL = "triennial"
    ONETIME = "onetime"
    HOUR = "hour"


class PriceNote(StrEnum):
    PROMO = "promo"
    REGULAR = "regular"


class IntelCategory(StrEnum):
    RESALE = "resale"
    CARPOOL = "carpool"
    DISCUSSION = "discussion"
    QUESTION = "question"
    REVIEW = "review"
    NEWS = "news"
    OTHER = "other"


class LlmStatus(StrEnum):  # D33
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class ExtractedBy(StrEnum):
    RULES = "rules"
    LLM = "llm"


class SourceKind(StrEnum):  # 第 12.1 节采集源类型
    OFFERS = "offers"
    MIXED = "mixed"


LANGS = ("zh", "en")
CURRENCIES = ("USD", "EUR", "GBP", "CNY", "HKD", "CAD", "AUD", "SGD", "JPY")


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    """统一的时间存储格式：UTC，精确到秒，如 2026-11-02T15:22:27Z。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("拒绝无时区的时间，采集源须给出 UTC 或带时区的时间")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(slots=True)
class RawPost:
    """采集源插件的输出（第 12.1 节）。url 已规范化。"""

    source: str
    url: str
    title: str
    content_html: str
    author: str | None = None
    published_at: datetime | None = None
    categories: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # 采集源附带的字段，如 NodeSeek 的帖子 ID


@dataclass(slots=True)
class Vendor:
    slug: str
    name: str
    aliases: list[str] = field(default_factory=list)
    short_aliases: list[str] = field(default_factory=list)  # 仅标题词边界匹配（D29）
    website: str | None = None
    id: int | None = None


@dataclass(slots=True)
class Plan:
    """一个配置 × 一个付费周期（第 11 节、金标集规范 6.1）。前 17 个字段与金标集套餐字段同名。"""

    name: str | None = None
    cpu_cores: float | None = None
    cpu_desc: str | None = None
    ram_gb: float | None = None
    disk_gb: float | None = None
    disk_type: DiskType | None = None
    bandwidth_tb: float | None = None
    port_gbps: float | None = None
    locations: list[str] = field(default_factory=list)
    net: list[str] = field(default_factory=list)  # 线路与网络标签（keywords.NET_TAGS 的键，O-16）
    price: float | None = None
    currency: str | None = None
    period: Period | None = None
    price_note: PriceNote | None = None
    order_url: str | None = None
    coupon_code: str | None = None
    notes: str | None = None
    term_months: int | None = None  # 一次付清的合约月数（D85）
    renewal_price: float | None = None  # 续费价，与 price 同币种、同周期（D85）
    position: int = 0
    price_cny_year: float | None = None
    confidence: float | None = None
    extracted_by: ExtractedBy = ExtractedBy.RULES
    id: int | None = None
    deal_id: int | None = None


@dataclass(slots=True)
class Deal:
    url: str
    source: str
    title: str
    sources: list[str] = field(default_factory=list)
    vendor: str | None = None  # 商家名（不是数据库列）：保存时解析为 vendor_id，读取时为商家标准名
    vendor_id: int | None = None
    title_zh: str | None = None
    title_en: str | None = None
    author: str | None = None
    published_at: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    content_hash: str | None = None
    is_black_friday: bool = False
    event_type: EventType = EventType.OTHER
    product_types: list[ProductType] = field(default_factory=list)
    discount_pct: int | None = None
    discount_text: str | None = None
    recurring: Recurring = Recurring.UNKNOWN
    coupon_codes: list[str] = field(default_factory=list)
    specs_range: dict = field(default_factory=dict)
    price_list: list[dict] = field(default_factory=list)
    min_price_cny_year: float | None = None
    min_price: float | None = None  # 起价的原币种金额、币种与周期（D47），页面按语言格式化
    min_price_currency: str | None = None
    min_price_period: Period | None = None
    purchase_url: str | None = None
    plan_format: PlanFormat | None = None
    starts_at: str | None = None  # 活动起止日期（ISO 日期，O-15）：规则层从原文找；截止日期通过校验的 LLM 值优先
    ends_at: str | None = None
    ends_source: str | None = None  # rule / llm
    summary_zh: str | None = None
    summary_en: str | None = None
    excerpt: str | None = None
    lang: str | None = None
    extracted_by: ExtractedBy = ExtractedBy.RULES
    completeness: float = 0.0
    confidence: float | None = None
    llm_status: LlmStatus = LlmStatus.PENDING
    llm_attempts: int = 0
    llm_hash: str | None = None
    llm_output: dict | None = None  # D46：LLM 原始输出，重新合并时不必再调用
    tracked: dict = field(default_factory=dict)  # D46：变更检测的比对基准
    updated_at: str | None = None
    plans: list[Plan] = field(default_factory=list)
    id: int | None = None


@dataclass(slots=True)
class Intel:
    url: str
    source: str
    title: str
    category: IntelCategory = IntelCategory.OTHER
    lang: str | None = None
    published_at: str | None = None
    first_seen_at: str | None = None
    is_black_friday: bool = False
    vendor_ids: list[int] = field(default_factory=list)  # 写入 intel_vendors
    title_zh: str | None = None  # 机器翻译的标题（英文帖），translate.py 填写（O-14）
    title_en: str | None = None  # 机器翻译的标题（中文帖）
    translate_failed: int = 0
    id: int | None = None
