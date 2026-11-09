"""每页的 meta 与结构化数据（M3.4，第 16 节）：title、description、canonical、hreflang、Open Graph、JSON-LD。

React 前端经 internal.py 取用，JSON-LD 由前端写进 <script type="application/ld+json">（React Router 负责转义）。
地址只用 SITE_URL 拼，不看请求的 Host：Cloudflare 缓存的页面不能被伪造的 Host 污染。结构化数据按 Google 现行支持取舍：
详情页 Product + AggregateOffer，商家页 Organization，首页 WebSite 与本页优惠的 ItemList，各页加 BreadcrumbList；
页面上没有可见的问答，所以不放 FAQPage。
"""

from collections.abc import Iterable

from ..config import Settings
from ..models import Deal
from . import og
from .view import HTML_LANG, PREFIX, View

SCHEMA = "https://schema.org"
OG_LOCALE = {"zh": "zh_CN", "en": "en_US"}


def site_name(settings: Settings, view: View) -> str:
    """站名：中文页"黑五雷达"，英文页用全称 BlackFridayRadar（D18，OG site_name）。"""
    return settings.site_name_zh if view.lang == "zh" else settings.site_fullname_en


def absolute(settings: Settings, view: View, path: str) -> str:
    return settings.site_url + view.prefix + path


def page(settings: Settings, view: View, *, title: str, description: str, path: str, og_title: str | None = None,
         og_type: str = "website", image: str | None = None, jsonld: Iterable[dict | None] = ()) -> dict:
    """path 为不带语言前缀的规范地址（"/plans?ram_min=2"）；jsonld 里的 None 跳过。"""
    alternates = {lang: prefix + path for lang, prefix in PREFIX.items()}
    canonical = settings.site_url + alternates[view.lang]
    return {
        "title": title, "description": description, "canonical": canonical, "alternates": alternates,
        "site_url": settings.site_url,
        "og": {"type": og_type, "title": og_title or title, "description": description, "url": canonical,
               "site_name": site_name(settings, view), "locale": OG_LOCALE[view.lang], "image": image,
               "image_alt": og_title or title},
        "jsonld": [{"@context": SCHEMA, **item} for item in jsonld if item],
    }


def breadcrumbs(settings: Settings, view: View, *trail: tuple[str, str]) -> dict:
    """首页 → trail；trail 每项为（名称, 不带语言前缀的地址）。"""
    items = [(site_name(settings, view), "/"), *trail]
    return {"@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": n, "name": name, "item": absolute(settings, view, path)}
        for n, (name, path) in enumerate(items, 1)]}


def product(settings: Settings, view: View, d: Deal) -> dict | None:
    """优惠 → Product。offers 只取与起价同币种、同周期的套餐，免得把月付和年付混成一个价格区间；没有价格就不给。"""
    if d.min_price is None or not d.min_price_currency:
        return None
    prices = [p.price for p in d.plans if p.price is not None and p.currency == d.min_price_currency
              and p.period == d.min_price_period] or [d.min_price]
    offers = {"@type": "AggregateOffer", "priceCurrency": d.min_price_currency, "lowPrice": min(prices),
              "highPrice": max(prices), "offerCount": len(prices), "url": absolute(settings, view, f"/deal/{d.id}")}
    if d.ends_at:  # 原帖写明的有效期（O-15）：Google 商品摘要认 priceValidUntil
        offers["priceValidUntil"] = d.ends_at
    if d.starts_at:
        offers["validFrom"] = d.starts_at
    item = {"@type": "Product", "name": view.title(d), "url": absolute(settings, view, f"/deal/{d.id}"),
            "offers": offers}
    if description := view.summary(d) or d.excerpt:
        item["description"] = description[:500]
    if d.vendor:
        item["brand"] = {"@type": "Brand", "name": d.vendor}
    return item


# ---------------------------------------------------------------- 各页


def home(settings: Settings, view: View, path: str, deals: list[Deal]) -> dict:
    t = view.t
    return page(settings, view, title=t("site.title"), description=t("site.description"), path=path,
                image=og.site_url(settings, view), jsonld=[
        {"@type": "WebSite", "name": site_name(settings, view), "url": absolute(settings, view, "/"),
         "inLanguage": HTML_LANG[view.lang]},
        {"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": n, "url": absolute(settings, view, f"/deal/{d.id}"),
             "name": view.title(d)} for n, d in enumerate(deals, 1)]},
    ])


def plans(settings: Settings, view: View, path: str) -> dict:
    t = view.t
    return page(settings, view, title=t("plans.title"), description=t("plans.description"), path=path,
                image=og.site_url(settings, view), jsonld=[breadcrumbs(settings, view, (t("nav.plans"), "/plans"))])


def deal(settings: Settings, view: View, d: Deal, vendor_slug: str | None) -> dict:
    t, title = view.t, view.title(d)
    trail = [(d.vendor, f"/vendor/{vendor_slug}")] if d.vendor and vendor_slug else []
    return page(settings, view, title=t("deal.title", title=title), og_title=title, og_type="article",
                image=og.deal_url(settings, view, d),
                description=(view.summary(d) or d.excerpt or title)[:150], path=f"/deal/{d.id}",
                jsonld=[product(settings, view, d), breadcrumbs(settings, view, *trail, (title, f"/deal/{d.id}"))])


def codes(settings: Settings, view: View) -> dict:
    t = view.t
    return page(settings, view, title=t("codes.title"), description=t("codes.description"), path="/codes",
                image=og.site_url(settings, view), jsonld=[breadcrumbs(settings, view, (t("nav.codes"), "/codes"))])


def vendors(settings: Settings, view: View, path: str) -> dict:
    t = view.t
    return page(settings, view, title=t("vendors.title"), description=t("vendors.description"), path=path,
                image=og.site_url(settings, view), jsonld=[breadcrumbs(settings, view, (t("nav.vendors"), "/vendors"))])


def vendor(settings: Settings, view: View, row, website: str | None, aliases: list[str]) -> dict:
    t, name, path = view.t, row["name"], f"/vendor/{row['slug']}"
    org = {"@type": "Organization", "name": name}
    if website:
        org["url"] = website
    if aliases:
        org["alternateName"] = aliases
    return page(settings, view, title=t("vendor.title", name=name), description=t("vendor.description", name=name),
                path=path, image=og.site_url(settings, view), jsonld=[org, breadcrumbs(settings, view, (name, path))])


def about(settings: Settings, view: View) -> dict:
    t = view.t
    return page(settings, view, title=t("about.title"), description=t("about.description"), path="/about",
                image=og.site_url(settings, view), jsonld=[breadcrumbs(settings, view, (t("footer.about"), "/about"))])


def status(settings: Settings, view: View) -> dict:
    """状态页不在页面上链接（D59），只给基本的 meta。"""
    t = view.t
    return page(settings, view, title=t("statuspage.title"), description=t("statuspage.description"), path="/status",
                image=og.site_url(settings, view))
