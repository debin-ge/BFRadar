"""主流品牌官网的黑五活动页（D80、D84）：白名单里的页面，每页一条优惠。

只抓服务端直接渲染出价格的页面（不用无头浏览器、不绕过质询）；价格要靠 JS 才出现、或被 403 / 工作量证明拦住的品牌
（OVHcloud、InMotion、Bluehost、HostGator、Namecheap、Hetzner）不在这里，由 Reddit 官方版块覆盖。各家页面结构
不同，每家一个解析函数，把页面上的套餐卡片整理成统一的简短 HTML（套餐名与折扣、配置列表、"Price: … , 24-month
term, renews at …"），交给通用的分段与规则抽取（第 12.4 节）；同一页不收的品类（域名、邮箱、建站器等）解析时就不取。

活动页地址每年会变，每季开始前人工核对 PAGES（第 21 节风险）。页面没有发布时间：stats 记下每页本季第一次抓到的
时间，作为发布时间，免得每次内容一变就算新帖；早于 SEASON_START 的记录作废重记。
"""

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape

from bs4 import BeautifulSoup, Tag

from ..models import RawPost, SourceKind, iso, utcnow
from .base import FetchResult, Source, register

log = logging.getLogger(__name__)

BF_TITLE = re.compile(r"black\s*friday|cyber\s*(?:monday|week)", re.I)
VALIDITY = re.compile(r"(?:available|valid|runs?|lasts?)\s+(?:from\s+)?[A-Z][a-z]+ \d{1,2}(?:st|nd|rd|th)?"
                      r"(?:,? \d{4})?\s+(?:to|until|through|–|-)\s+[A-Z][a-z]+ \d{1,2}(?:st|nd|rd|th)?,? \d{4}")
TERM_TEXT = re.compile(r"for (\d{1,3}) months", re.I)
RENEW_TEXT = re.compile(r"renews? at\s*([$€£]\s?\d[\d.,]*\s*/\s*\w+)", re.I)


@dataclass(frozen=True)
class Page:
    brand: str
    url: str
    parser: str  # PARSERS 的键


@dataclass
class Card:
    name: str
    discount: str | None
    price: str  # "$6.49/mo"
    term_months: int | None
    renews: str | None
    features: list[str]


def _text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _price(node: Tag | None) -> str:
    return re.sub(r"\s+", "", node.get_text("", strip=True)) if node else ""


def hostinger_cards(soup: BeautifulSoup) -> list[Card]:
    """Hostinger 产品黑五页（/black-friday-vps-deals 等）的套餐卡片。页面上同一组卡片出现两遍（两个切换页），去重。"""
    cards, seen = [], set()
    for node in soup.select("div.h-pricing-card"):
        name = _text(node.select_one(".h-pricing-card__title"))
        prices = node.select(".hw-price")
        if not name or not prices:
            continue
        price = _price(prices[-1])  # 第一个是划掉的原价，最后一个是活动价
        blob = _text(node)
        term = TERM_TEXT.search(blob)
        renew = RENEW_TEXT.search(blob)
        tag = _text(node.select_one(".h-pricing-card__discount-tag"))
        discount = m.group(0) if (m := re.search(r"\d+% off", tag)) else None
        features = [t for f in node.select(".h-plan-feature__text") if (t := _text(f)) and not t.endswith(":")]
        key = (name, price)
        if key in seen or not re.search(r"\d", price):
            continue
        seen.add(key)
        cards.append(Card(name, discount, price, int(term.group(1)) if term else None,
                          re.sub(r"\s+", "", renew.group(1)) if renew else None, features))
    return cards


PARSERS: dict[str, Callable[[BeautifulSoup], list[Card]]] = {"hostinger": hostinger_cards}
PAGES = (
    Page("Hostinger", "https://www.hostinger.com/black-friday-vps-deals", "hostinger"),
    Page("Hostinger", "https://www.hostinger.com/black-friday-web-hosting-deals", "hostinger"),
)


SPEC_FEATURE = re.compile(r"\d|unlimited|unmetered", re.I)
NOT_SPEC = re.compile(r"24/7|support", re.I)
MAX_SPECS = 6


def render(cards: list[Card], validity: str | None) -> str:
    """套餐卡片 → 一张表：套餐 | 配置 | 价格 | 折扣 | 合约（合约期与续费价，D85）。

    表格行是规则层最稳的套餐写法（第 12.4 节）；配置只留带数字或 Unlimited 的卖点（"4 GB RAM"、"50 GB NVMe disk
    space"、"3 websites"），"Free weekly backups" 这类不进表。
    """
    rows = []
    for c in cards:
        specs = ", ".join(
            [f for f in c.features if SPEC_FEATURE.search(f) and not NOT_SPEC.search(f)][:MAX_SPECS])
        term = ", ".join(x for x in (f"{c.term_months}-month term" if c.term_months else "",
                                     f"renews at {c.renews}" if c.renews else "") if x)
        cells = (c.name, specs, c.price, c.discount or "", term)
        rows.append("<tr>" + "".join(f"<td>{escape(x)}</td>" for x in cells) + "</tr>")
    head = "<tr><th>Plan</th><th>Specs</th><th>Price</th><th>Discount</th><th>Term</th></tr>"
    intro = f"<p>{escape(validity)}.</p>\n" if validity else ""
    return f"{intro}<table>{head}{''.join(rows)}</table>"


def parse_page(page: Page, data: bytes | str, published: datetime) -> RawPost | None:
    """不是黑五活动页（标题没有黑五 / 网一）或解析不出套餐时返回 None（例如活动还没上线的预告页）。"""
    soup = BeautifulSoup(data, "lxml")
    title = _text(soup.title)
    title = re.sub(r"\s*[|–-]\s*" + re.escape(page.brand) + r"\s*$", "", title, flags=re.I)
    if not BF_TITLE.search(title):
        return None
    for t in soup(["script", "style", "svg", "noscript"]):
        t.decompose()
    cards = PARSERS[page.parser](soup)
    if not cards:
        return None
    validity = m.group(0) if (m := VALIDITY.search(soup.get_text(" "))) else None
    html = render(cards, validity) + f'\n<p><a href="{escape(page.url)}">Order at {escape(page.brand)}</a></p>'
    return RawPost(source=Official.id, url=page.url, title=f"{page.brand}: {title}", content_html=html,
                   author=page.brand, published_at=published, categories=["official"], extra={})


@register
class Official(Source):
    id = "official"
    name = "官网活动页"
    kind = SourceKind.OFFERS
    lang = "en"
    request_gap = 3.0

    def fetch(self, state: Mapping | None = None) -> FetchResult:
        season = datetime.combine(self.settings.season_start, datetime.min.time(), UTC)
        first_seen = {u: t for u, t in ((state or {}).get("first_seen") or {}).items()
                      if datetime.fromisoformat(t) >= season}
        now = utcnow()
        posts: list[RawPost] = []
        errors: dict[str, str] = {}
        skipped: list[str] = []
        for i, page in enumerate(PAGES):
            if i:
                self._sleep(self.request_gap)
            try:
                published = datetime.fromisoformat(first_seen.get(page.url) or iso(now))
                post = parse_page(page, self.http.get(page.url).content, published)
            except Exception as e:  # 单页失败不影响其他页
                log.warning("official 页面失败：%s：%s", page.url, e)
                errors[page.url] = str(e)
                continue
            if post is None:
                skipped.append(page.url)
                continue
            first_seen.setdefault(page.url, iso(published))
            posts.append(post)
        if errors and len(errors) == len(PAGES):
            raise RuntimeError(f"official 全部 {len(PAGES)} 个页面失败：{next(iter(errors.values()))}")
        stats: dict = {"pages": len(PAGES), "first_seen": first_seen}
        if skipped:
            stats["skipped"] = skipped
        if errors:
            stats["errors"] = errors
        return FetchResult(posts, stats)
