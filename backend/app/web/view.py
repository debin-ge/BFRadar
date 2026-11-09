"""页面数据、RSS 与分享图上的取值（按语言：中文在根路径，英文在 /en/，第 7 节）。"""

import json
from datetime import date, datetime

from ..config import Settings
from ..db import is_nat
from ..locations import normalize as normalize_location
from ..locations import normalize_all as normalize_locations
from ..fx import format_price
from ..i18n import strings, translator
from ..models import Deal, Plan, utcnow
from . import fmt

LANGS = ("zh", "en")
HTML_LANG = {"zh": "zh-CN", "en": "en"}
PREFIX = {"zh": "", "en": "/en"}
# 代码里不写死标点：中文用全角，英文用半角加空格（FR-17 英文页无中文残留）
SEPARATORS = {
    "zh": {"list": "、", "comma": "，", "colon": "：", "semi": "；", "open": "（", "close": "）", "end": "。",
           "space": ""},
    "en": {"list": ", ", "comma": ", ", "colon": ": ", "semi": "; ", "open": " (", "close": ")", "end": ".",
           "space": " "},
}


class View:
    """一次请求里用到的格式化：语言、显示时区与"现在"固定下来。"""

    def __init__(self, lang: str, tz: str, now: datetime, today: date | None = None) -> None:
        self.lang, self.tz, self.now = lang, tz, now
        self.today = today or fmt.local(now, tz).date()  # 活动有没有结束、还剩几天按这一天算（Settings.today 可固定）
        self.t = translator(lang)
        self.prefix = PREFIX[lang]  # 站内链接的语言前缀
        self.html_lang = HTML_LANG[lang]

    # ---- 文字

    def sep(self, name: str) -> str:
        return SEPARATORS[self.lang][name]

    def join(self, items) -> str:
        return self.sep("list").join(str(x) for x in items)

    def sentences(self, *parts: str) -> str:
        """几句话连成一段，各句以句号收尾（中文"甲。乙。"，英文"A. B."）。"""
        return self.sep("space").join(p + self.sep("end") for p in parts if p)

    def title(self, d: Deal) -> str:
        return self.pick_title(d.title, d.title_zh, d.title_en)

    def pick_title(self, original: str, zh: str | None, en: str | None) -> str:
        """中文页优先 title_zh，英文页优先 title_en（中文源才有），都没有用原标题（第 7 节）。"""
        return (zh if self.lang == "zh" else en) or original

    def summary(self, d: Deal) -> str | None:
        return d.summary_zh if self.lang == "zh" else d.summary_en

    def vendor(self, d: Deal) -> str:
        return d.vendor or self.t("row.vendor_unknown")

    def offer(self, d: Deal) -> list[str]:
        parts = []
        if d.discount_pct:
            parts.append(self.t("discount.max", pct=d.discount_pct))
        if d.discount_pct or d.coupon_codes or str(d.recurring) != "unknown":
            parts.append(self.t(f"recurring.{d.recurring}"))
        if d.coupon_codes:
            codes = self.join(d.coupon_codes[:2])
            more = self.t("discount.more_codes", n=len(d.coupon_codes)) if len(d.coupon_codes) > 2 else ""
            parts.append(self.t("discount.codes", codes=codes + more))
        return parts

    def offer_chips(self, d: Deal) -> list[dict]:
        """卡片上的优惠标签，带含义（kind）供页面着色（优化方案 O-08）：折扣、续费性质；优惠码另给（可复制，O-07）。"""
        chips = []
        if d.discount_pct:
            chips.append({"kind": "discount", "text": self.t("discount.max", pct=d.discount_pct)})
        if d.discount_pct or d.coupon_codes or str(d.recurring) != "unknown":
            chips.append({"kind": f"rec-{d.recurring}", "text": self.t(f"recurring.{d.recurring}")})
        return chips

    def expiry(self, d: Deal) -> dict | None:
        """有效期标记（O-15）：state 为 ended 已结束、soon 剩 3 天以内、open 其余；没有截止日期为 None。"""
        if not d.ends_at:
            return None
        left = fmt.days_left(d.ends_at, self.today)
        when = fmt.day_label(d.ends_at, self.lang, self.today)
        if left < -fmt.ENDED_GRACE_DAYS:
            return {"state": "ended", "text": self.t("expiry.ended"), "date": d.ends_at}
        if left <= 0:
            key = "expiry.today" if left == 0 else "expiry.ending"
            return {"state": "soon", "text": self.t(key, date=when), "date": d.ends_at}
        key = "expiry.left_one" if left == 1 else "expiry.left"
        return {"state": "soon" if left <= 3 else "open", "text": self.t(key, date=when, n=left), "date": d.ends_at}

    def specs(self, d: Deal) -> list[str]:
        r = d.specs_range
        parts = []
        if "cpu_cores" in r:
            key = "spec.core_one" if list(r["cpu_cores"]) == [1, 1] else "spec.cores"  # 英文单数 "1 core"
            parts.append(self.t(key, v=fmt.span(*r["cpu_cores"])))
        if "ram_gb" in r:
            parts.append(self.t("spec.ram", v=fmt.span(*r["ram_gb"], unit=fmt.size_gb)))
        if "disk_gb" in r:
            parts.append(self.t("spec.disk", v=fmt.span(*r["disk_gb"], unit=fmt.size_gb)))
        return parts

    # ---- 价格

    def start(self, d: Deal) -> tuple[str, str] | None:
        """起价两行：原币种原周期；人民币估算年价（人民币原价不估算，第 8 节）。"""
        if d.min_price is None:
            return None
        original = format_price(d.min_price, d.min_price_currency, d.min_price_period, self.lang)
        estimate = ""
        if d.min_price_cny_year is not None and d.min_price_currency != "CNY" and d.min_price != 0:  # 免费不写估算
            estimate = f"≈ ¥{d.min_price_cny_year:,.0f}/{self.t_period('year')}"
        return original, estimate

    def t_period(self, period: str) -> str:
        return {"zh": {"year": "年"}, "en": {"year": "yr"}}[self.lang][period]

    def plan_price(self, p: Plan) -> str:
        return format_price(p.price, p.currency, p.period, self.lang) if p.price is not None else ""

    def plan_term(self, p: Plan) -> str:
        """合约期与续费价（D85）："24 个月预付 · 续费 $11.99/月"。"""
        parts = []
        if p.term_months:
            parts.append(self.t("plan.term", n=p.term_months))
        if p.renewal_price is not None and p.renewal_price != p.price:
            parts.append(self.t("plan.renews", price=format_price(p.renewal_price, p.currency, p.period, self.lang)))
        return self.join(parts) if parts else ""

    def plan_cny(self, p: Plan) -> str:
        if p.price_cny_year is None or p.currency == "CNY" or p.price == 0:
            return ""
        return f"¥{p.price_cny_year:,.0f}"

    def bandwidth(self, p: Plan) -> str:
        if p.bandwidth_tb is not None:
            return f"{fmt.num(p.bandwidth_tb)} TB"
        return self.t("plan.unmetered") if p.notes and "unmetered" in p.notes.lower() else ""

    def size(self, gb: float | None) -> str:
        return fmt.size_gb(gb)

    def speed(self, gbps: float | None) -> str:
        return fmt.speed(gbps)

    def num(self, value: float | None) -> str:
        return fmt.num(value)

    def location(self, name: str) -> str:
        """机房按语言显示：先规范化（旧数据里的"Frankfurt, Germany"等），中文名在字典的 loc.*，字典里没有的照原文。"""
        name = normalize_location(name)
        return strings(self.lang).get(f"loc.{name}", name)

    def locations(self, names: list[str], limit: int | None = None) -> str:
        """多个机房连起来；给了 limit 时只写前几个，后面写"+N"（套餐视图一格放不下六七个机房）。"""
        names = normalize_locations(names)
        shown = [self.location(n) for n in names[:limit]]
        return self.join(shown) + (f" +{len(names) - limit}" if limit and len(names) > limit else "")

    # ---- 变更记录

    def change(self, field: str, old: str | None, new: str | None) -> tuple[str, str, str]:
        """（字段名, 旧值, 新值）的显示文字；值的格式见 merge.Change。"""
        def show(value: str | None) -> str:
            if value is None:
                return self.t("deal.none")
            if field == "discount_pct":
                return f"{value}%"
            if field == "coupon_codes":
                return self.join(json.loads(value)) or self.t("deal.none")
            if field == "plan_price":
                item = json.loads(value)
                return format_price(item["price"], item.get("currency"), item.get("period"), self.lang)
            return value

        label = self.t(f"change.{field}")
        if field == "plan_price" and new:
            item = json.loads(new)
            spec = item.get("name") or " ".join(x for x in (fmt.size_gb(item.get("ram_gb")),
                                                            fmt.size_gb(item.get("disk_gb"))) if x)
            label = f"{spec} {label}".strip()
        return label, show(old), show(new)

    # ---- 时间

    def duration(self, seconds: float | None) -> str:
        if seconds is None:
            return ""
        n = round(seconds)
        return self.t("duration.seconds", n=n) if n < 60 else self.t("duration.minutes", m=n // 60, s=n % 60)

    def every(self, minutes: int) -> str:
        """采集间隔：每 5 分钟、每小时、每 6 小时。"""
        if minutes == 60:
            return self.t("every.hour")
        if minutes % 60 == 0:
            return self.t("every.hours", n=minutes // 60)
        return self.t("every.minutes", n=minutes)

    def pct(self, ratio: float | None, digits: int = 0) -> str:
        return "" if ratio is None else f"{ratio * 100:.{digits}f}%"

    def ago(self, ts: str | None) -> str:
        unit, n = fmt.ago(ts, self.now)
        return self.t(f"ago.{unit}", n=n) if unit != "now" else self.t("ago.now")

    def day(self, ts: str | None) -> str:
        return fmt.day(ts, self.tz)

    def posted(self, ts: str | None) -> str:
        """卡片上的发布时间：7 天以内写"2 天前发布"，更早的写"11-03 发布"（优化方案 O-06）。"""
        if not ts:
            return ""
        unit, n = fmt.ago(ts, self.now)
        if unit != "days" or n < 7:
            return self.t("row.posted_ago", ago=self.ago(ts))
        return self.t("row.posted_on", date=self.day(ts))

    def net_label(self, tag: str) -> str:
        return self.t(f"net.{tag}")

    def plan_net(self, p: Plan) -> list[str]:
        return [self.net_label(t) for t in p.net]

    def plan_ip(self, p: Plan) -> str | None:
        """IP 提示：套餐名或备注里写明 NAT 的标 NAT，其余不标。商家默认都给 IPv4，不单独标“无独立 IPv4”（D69）；
        与套餐视图的“不含 NAT”同一口径。"""
        return self.t("plan.nat") if is_nat(p.name, p.notes) else None

    def clock(self, ts: str | datetime | None) -> str:
        return fmt.clock(ts, self.tz, self.now)


def for_lang(settings: Settings, lang: str) -> View:
    """中文按北京时间、英文按 UTC 显示（第 7 节），"现在"取当前时刻。"""
    return View(lang, settings.display_tz_en if lang == "en" else settings.display_tz_zh, utcnow(), settings.today)


def ended_before(settings: Settings) -> str:
    """截止日期早于这一天的活动算已结束（接口与 RSS 用，没有 View 时）。"""
    return fmt.ended_cutoff(for_lang(settings, "zh").today)
