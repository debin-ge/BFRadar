"""内部页面数据（D60）：给前端 Node 服务的页面 JSON，按语言格式化好；只走内网（前端不转发这一路径，对外是页面 404），不进 OpenAPI。

格式化全部复用 View（与 RSS 同一套）：带数据的文字（价格、时间、"共 N 条"、空状态说明等）由这里给出，
前端字典只管不带数据的固定文字。页面不显示的字段不放进来（来源名，D59；库存已不收集，D63），抓取来的链接只放行 http(s)。
经内存缓存，键为 "int:" + 语言 + 规范地址，调度器清缓存时一起失效；状态页不缓存，每次现算。
"""

import json
import sqlite3
from dataclasses import replace
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..config import Settings
from ..extract.keywords import CJK
from ..locations import REGIONS
from ..fx import latest_rates
from ..models import Deal, Plan
from ..repo import deal_changes, recent_flags, site_stats
from . import fmt, seo, site
from .api import error
from .cache import DETAIL_TTL, PAGE_TTL, cached_response
from .queries import (CPU_STEPS, DIRECTORY_INTEL_DAYS, DISK_STEPS, HEAT_DAYS, PERIODS, PLAN_SORTS, PRODUCTS, RAM_STEPS,
                      SCOPES, SORTS, VENDOR_SORTS, Activity, DealQuery, LlmSummary, PlanQuery, SameOffer, SourceRuns,
                      community, deal_by_id, deals_with_codes, list_deals, list_plans, llm_summary, location_options, location_stats,
                      net_tags_in_scope, nodeseek_coverage, plan_presets, recent_activity, same_offer, source_runs,
                      vendor_badges, vendor_by_slug, vendor_deals, vendor_directory, vendor_intel, vendor_overview,
                      vendor_slugs, vendor_website)
from .site import STATUS_SOURCES
from .view import View, for_lang

router = APIRouter(prefix="/internal/v1", include_in_schema=False)

LANGS = ("zh", "en")
HEADERS = {"Cache-Control": "no-store"}  # 只给 Node 读，不让任何中间层缓存
CARD_PAGE_SIZE = 20  # 首页每页卡片数
CARD_PLANS = 5  # 卡片里展开可见的套餐数（最便宜的几个），其余看详情页
CARD_CODES = 3  # 卡片上直接给出（可复制）的优惠码个数，其余看详情页
PLAN_COLS = ("name", "cpu", "ram", "disk", "bw", "port", "loc", "price", "cny", "order")  # 套餐表的列，前端按此顺序画


def _json(data: dict) -> JSONResponse:
    return JSONResponse(data, headers=HEADERS)


def _view(request: Request, lang: str) -> View:
    return for_lang(request.app.state.settings, lang)


def _url(value: str | None) -> str | None:
    """抓取来的链接只放行 http(s)；javascript: 之类当作没有。"""
    return value if value and urlsplit(value).scheme in ("http", "https") else None


def _plan(v: View, p: Plan, order_fallback: str | None) -> dict:
    return {
        "name": p.name or "", "ip": v.plan_ip(p), "net": v.plan_net(p),
        "cpu": v.t("spec.core_one" if p.cpu_cores == 1 else "spec.cores", v=v.num(p.cpu_cores)) if p.cpu_cores else "",
        "ram": v.size(p.ram_gb),
        "disk": " ".join(x for x in (v.size(p.disk_gb), str(p.disk_type or "")) if x),
        "bw": v.bandwidth(p), "port": v.speed(p.port_gbps), "loc": v.locations(p.locations),
        "price": v.plan_price(p), "term": v.plan_term(p), "cny": v.plan_cny(p),
        "code": p.coupon_code or None,  # 原样的码，页面加"码"字样与复制按钮（O-07）
        "order": _url(p.order_url) or order_fallback,
    }


def _cols(rows: list[dict], *, card: bool) -> list[str]:
    """套餐表要画的列：这批套餐里全空的列不画（窄卡片里放得下，优化方案 O-02）；价格总在，套餐名一格还放
    NAT 等 IP 提示。卡片里不画"下单"列：卡片只用来比价，下单去详情页或套餐视图（D64）。"""
    def has(c: str) -> bool:
        return any(r[c] or (c == "name" and (r["ip"] or r["net"])) for r in rows)
    return [c for c in PLAN_COLS if c == "price" or (has(c) and not (card and c == "order"))]


def _preview(plans: list[Plan]) -> list[Plan]:
    """卡片展开的预览：最便宜的 CARD_PLANS 个（按人民币估算年价，估不出的放最后，同价按原帖顺序）。"""
    return sorted(plans, key=lambda p: (p.price_cny_year is None, p.price_cny_year or 0, p.position))[:CARD_PLANS]


def _same_note(v: View, href: str, same: list[SameOffer]) -> dict:
    return {"same": v.t("row.same", n=len(same)) if same else None, "same_href": f"{href}#same" if same else None}


def _deal_card(v: View, d: Deal, flags: dict, badges: dict, same: list[SameOffer] | None = None) -> dict:
    """首页优惠卡片。"""
    t = v.t
    price = v.start(d)
    href = f"{v.prefix}/deal/{d.id}"
    plans = [_plan(v, p, _url(d.purchase_url)) for p in _preview(d.plans)]
    return {
        "id": d.id, "href": href,
        "bf": d.is_black_friday, "event": t(f"event.{d.event_type}"),
        "vendor": v.vendor(d), **_vendor_link(v, d.vendor_id, badges),
        "title": v.title(d), **_same_note(v, href, same or []),
        "flags": [{"key": str(f), "label": t(f"flag.{f}")} for f in sorted(flags.get(d.id, ()))],
        "offer": v.offer_chips(d), "specs": v.specs(d), "net": [v.net_label(x) for x in d.specs_range.get("net", [])],
        "codes": d.coupon_codes[:CARD_CODES],
        "codes_more": t("discount.codes_more", n=len(d.coupon_codes)) if len(d.coupon_codes) > CARD_CODES else None,
        "price": list(price) if price else None,
        "date": v.posted(d.published_at), "published_at": d.published_at, "expiry": v.expiry(d),
        "expand": (t("row.expand", n=len(d.plans)) if d.plans else t("row.expand_text"))
        if d.plans or d.excerpt else None,
        "plans": plans, "cols": _cols(plans, card=True),
        "plans_more": t("row.more_plans", shown=CARD_PLANS, n=len(d.plans)) if len(d.plans) > CARD_PLANS else None,
        "excerpt": d.excerpt,
    }


def _tag(v: View, a: Activity) -> dict:
    """"24 小时动态"的一行（O-10）：什么动静、多久以前、商家、起价。"""
    d, t = a.deal, v.t
    price = v.start(d)
    kind = t(f"flag.{a.kind}") if a.kind in ("price_drop", "new_code") else t(f"just.kind.{a.kind}")
    return {"id": d.id, "href": f"{v.prefix}/deal/{d.id}", "vendor": v.vendor(d),
            "price": price[0] if price else None, "cny": price[1] if price else "",
            "kind": a.kind, "kind_label": kind, "ago": v.ago(a.at), "at": a.at}


def _intel(v: View, rows) -> list[dict]:
    """社区讨论：标题、原帖与相对时间（不写来源名，D59）。"""
    return [{"title": v.pick_title(r["title"], r["title_zh"], r["title_en"]), "url": _url(r["url"]),
             "ago": v.ago(r["published_at"])} for r in rows]  # 标题按页面语言取译名（O-14），没有译名用原标题


def _page_numbers(current: int, last: int, around: int = 2) -> list[int | None]:
    """页码：首页、末页与当前页前后各 around 页，中间断开处为 None（页面上画省略号）。"""
    keep = {1, last, *range(current - around, current + around + 1)}
    out: list[int | None] = []
    for n in range(1, last + 1):
        if n in keep:
            out.append(n)
        elif out and out[-1] is not None:
            out.append(None)
    return out


def _pager(v: View, page, href) -> dict | None:
    """href(page=n) 给出规范地址（不带语言前缀）。带页码（O-13），没有脚本也是普通链接。"""
    if page.pages <= 1:
        return None
    return {"text": v.t("list.page", page=page.page, pages=page.pages),
            "prev": v.prefix + href(page=page.page - 1) if page.page > 1 else None,
            "next": v.prefix + href(page=page.page + 1) if page.page < page.pages else None,
            "pages": [{"n": n, "href": v.prefix + href(page=n), "current": n == page.page} if n else None
                      for n in _page_numbers(page.page, page.pages)]}


def _options(values, label, current) -> list[dict]:
    """下拉选项：值写成查询参数的样子（2、0.5，不写 2.0），显示名按语言格式化好。"""
    return [{"value": f"{x:g}" if isinstance(x, (int, float)) else x, "label": label(x), "selected": x == current}
            for x in values]


def _vendor_link(v: View, vendor_id: int | None, badges: dict) -> dict:
    slug, heat = badges.get(vendor_id, (None, 0))
    return {"vendor_href": f"{v.prefix}/vendor/{slug}" if slug else None,
            "heat": v.t("vendor.heat", n=heat) if heat else None,
            "heat_title": v.t("vendor.heat_title", n=heat) if heat else None}


@router.get("/{lang}/chrome")
def chrome(request: Request, lang: str) -> Response:
    """页头页脚：倒计时、更新时间、页脚说明（含汇率来源与日期）。"""
    if lang not in LANGS:
        return error(404, "not_found", "没有这种语言")
    view = _view(request, lang)
    return cached_response(request, f"int:{lang}:chrome", PAGE_TTL, lambda: _chrome(request, view))


def _chrome(request: Request, view: View) -> JSONResponse:
    settings, conn = request.app.state.settings, request.app.state.db.conn()
    data = site.chrome(settings, conn, view, site.next_crawl(request.app.state.scheduler))
    t = view.t
    data["footer"] = view.sentences(t("footer.disclaimer"), data.pop("fx_note"), t("footer.timezone"))
    return _json(data)


@router.get("/{lang}/home")
def home(request: Request, lang: str) -> Response:
    """首页（帖子视图）。页面不按来源筛选（D59），source 参数忽略。"""
    if lang not in LANGS:
        return error(404, "not_found", "没有这种语言")
    query, view = replace(DealQuery.parse(request.query_params), sources=()), _view(request, lang)
    return cached_response(request, f"int:{lang}:{query.href()}", PAGE_TTL, lambda: _home(request, view, query))


def _home(request: Request, view: View, query: DealQuery) -> JSONResponse:
    settings, conn = request.app.state.settings, request.app.state.db.conn()
    t, now, p = view.t, view.now, view.prefix
    season = fmt.season_start_iso(settings.season_start)
    cutoff = fmt.ended_cutoff(view.today)
    page = list_deals(conn, query, season, page_size=CARD_PAGE_SIZE, ended_before=cutoff)
    stats = site_stats(conn, season_start=season, since=fmt.since(now, 24))
    if page.total:
        empty = ""
    elif query.scope == "bf" and not query.filtered:
        upcoming = site.next_crawl(request.app.state.scheduler)
        when = t("empty.when_next", time=view.clock(upcoming)) if upcoming else t("empty.when_soon")
        empty = t("empty.bf", when=when)
    else:
        empty = t("empty.filtered")
    flags = recent_flags(conn, since=fmt.since(now, 24))
    badges = vendor_badges(conn, {d.vendor_id for d in page.deals if d.vendor_id}, since=fmt.since(now, 24 * HEAT_DAYS))
    same = same_offer(conn, [d.id for d in page.deals])
    more = None  # 首页默认只看黑五/网一（D66）：列表末尾提示本季其他优惠，链到"本季全部"
    if query.scope == "bf" and not query.filtered and page.page == page.pages:
        others = list_deals(conn, replace(query, scope="promo", page=1), season, page_size=1,
                            ended_before=cutoff).total - page.total
        if others > 0:
            more = {"text": t("home.more_promo", n=others), "href": p + query.href(scope="promo", page=1)}
    return _json({
        "seo": seo.home(settings, view, query.href(), page.deals),
        "stats": {"bf": fmt.num(stats["deals"]["bf"]), "new": fmt.num(stats["deals"]["new"]),
                  "plans": fmt.num(stats["plans"]), "vendors": fmt.num(stats["vendors"])},
        "just": [_tag(view, a) for a in recent_activity(conn, season, fmt.since(now, 24), flags)],
        "scopes": [{"key": s, "href": p + query.href(scope=s, page=1), "current": s == query.scope} for s in SCOPES],
        "form": {
            "action": p + "/", "scope": query.scope if query.scope != "bf" else None,
            "products": [{"value": x, "checked": not query.products or x in query.products} for x in PRODUCTS],
            "q": query.q,
            "sorts": [{"value": s, "selected": s == query.sort} for s in SORTS],
            "ended": query.ended if query.scope != "all" else None,  # 含往季不处理已结束的，页面上不给这个勾选项
            "reset": p + query.href(products=(), q="", ended=False, page=1) if query.filtered else None,
        },
        "total": t("list.total", n=page.total),
        "deals": [_deal_card(view, d, flags, badges, same.get(d.id)) for d in page.deals],
        "pager": _pager(view, page, query.href),
        "more": more,
        "empty": empty,
        "intel": _intel(view, community(conn, season)),
    })


@router.get("/{lang}/plans")
def plans(request: Request, lang: str) -> Response:
    """套餐视图（FR-09）：筛选项的值与显示名、套餐行（所属优惠按语言取标题）、分页。"""
    if lang not in LANGS:
        return error(404, "not_found", "没有这种语言")
    query, view = PlanQuery.parse(request.query_params), _view(request, lang)
    return cached_response(request, f"int:{lang}:{query.href()}", PAGE_TTL, lambda: _plans(request, view, query))


def _plans(request: Request, view: View, query: PlanQuery) -> JSONResponse:
    settings, conn = request.app.state.settings, request.app.state.db.conn()
    t, p = view.t, view.prefix
    season = fmt.season_start_iso(settings.season_start)
    page = list_plans(conn, query, season)
    rows = []
    for r in page.rows:
        row = _plan(view, r.plan, _url(r.purchase_url))
        row.update(loc=view.locations(r.plan.locations, limit=2),  # 一格最多两个机房，其余"+N"，全部在 loc_title
                   loc_title=view.locations(r.plan.locations) if len(r.plan.locations) > 2 else None,
                   vendor=r.vendor or t("row.vendor_unknown"),
                   vendor_href=f"{p}/vendor/{r.vendor_slug}" if r.vendor_slug else None,
                   deal_title=view.pick_title(r.deal_title, r.deal_title_zh, r.deal_title_en),
                   deal_href=f"{p}/deal/{r.deal_id}")
        rows.append(row)
    net_tags = net_tags_in_scope(conn, query, season)
    reset = query.href(cpu_min=None, ram_min=None, disk_min=None, price_max=None, products=(), q="", region="",
                       location="", period="", nvme=False, no_nat=False, net=(), page=1)
    return _json({
        "seo": seo.plans(settings, view, query.href()),
        "scopes": [{"key": s, "href": p + query.href(scope=s, page=1), "current": s == query.scope} for s in SCOPES],
        "presets": [{"key": x["key"], "label": t(f"preset.{x['key']}"), "href": p + x["href"], "current": x["current"]}
                    for x in plan_presets(query, net_tags)],
        "form": {
            "action": p + "/plans", "scope": query.scope if query.scope != "promo" else None,
            "cpu": _options(CPU_STEPS, lambda x: t("spec.cores", v=x), query.cpu_min),
            "ram": _options(RAM_STEPS, view.size, query.ram_min),
            "disk": _options(DISK_STEPS, view.size, query.disk_min),
            "price_max": str(int(query.price_max)) if query.price_max else "",
            "products": [{"value": x, "checked": not query.products or x in query.products} for x in PRODUCTS],
            "q": query.q,
            "region": _options(REGIONS, lambda x: t(f"region.{x}"), query.region),
            "location": _options(location_options(conn, query.scope, season, query.region), view.location,
                                 query.location),
            "period": _options(PERIODS, lambda x: t(f"period.{x}"), query.period),
            "nvme": query.nvme,
            "no_nat": query.no_nat,
            "net": [{"value": x, "label": view.net_label(x), "checked": x in query.net} for x in net_tags],
            "sorts": _options(PLAN_SORTS, lambda x: t(f"plans.sort.{x}"), query.sort),
            "reset": p + reset if query.filtered else None,
        },
        "total": t("plans.total", n=page.total),
        "rows": rows,
        "pager": _pager(view, page, query.href),
    })


@router.get("/{lang}/deal/{deal_id:int}")
def deal(request: Request, lang: str, deal_id: int) -> Response:
    """优惠详情（FR-10）：字段卡、全部套餐、摘录、变更时间线、同商家本季其他活动、相关讨论。"""
    if lang not in LANGS:
        return error(404, "not_found", "没有这种语言")
    view = _view(request, lang)
    return cached_response(request, f"int:{lang}:/deal/{deal_id}", DETAIL_TTL, lambda: _deal(request, view, deal_id))


def _deal(request: Request, view: View, deal_id: int) -> JSONResponse:
    settings, conn = request.app.state.settings, request.app.state.db.conn()
    d = deal_by_id(conn, deal_id)
    if d is None:
        return error(404, "not_found", "没有这条优惠")
    t, sep, p = view.t, view.sep, view.prefix
    title, summary, price = view.title(d), view.summary(d), view.start(d)
    discount = t("discount.max", pct=d.discount_pct) if d.discount_pct else t("deal.none")
    discount_note = None  # 原帖里的折扣说明：与页面同语言照写，否则标"原文："（中文页不夹没标注的英文，优化方案 O-06）
    if d.discount_text:
        same_lang = bool(CJK.search(d.discount_text)) == (view.lang == "zh")
        discount_note = d.discount_text if same_lang else t("deal.original_text", text=d.discount_text)
    badges = vendor_badges(conn, {d.vendor_id} if d.vendor_id else set(), since=fmt.since(view.now, 24 * HEAT_DAYS))
    same = same_offer(conn, [d.id]).get(d.id, [])
    same_ids = {x.id for x in same}
    others = [o for o in vendor_deals(conn, d.vendor_id, exclude=d.id,
                                      season_start=fmt.season_start_iso(settings.season_start), limit=10 + len(same))
              if o.id not in same_ids][:10] if d.vendor_id else []  # 同一活动的帖子单列，不在"其他活动"里重复
    plans = [_plan(view, x, _url(d.purchase_url)) for x in d.plans]
    changes = []
    for c in deal_changes(conn, d.id):
        label, old, new = view.change(c["field"], c["old_value"], c["new_value"])
        changes.append({"at": c["changed_at"], "time": view.clock(c["changed_at"]),
                        "text": f"{label}{sep('colon')}{old} → {new}"})
    return _json({
        "seo": seo.deal(settings, view, d, badges.get(d.vendor_id, (None, 0))[0]),
        "vendor": view.vendor(d), **_vendor_link(view, d.vendor_id, badges),
        "bf": d.is_black_friday, "event": t(f"event.{d.event_type}"),
        "title": title, "summary": summary,
        "original_title": t("deal.original_title") + sep("colon") + d.title if title != d.title else None,
        "url": _url(d.url), "purchase_url": _url(d.purchase_url),
        "facts": {
            "price": list(price) if price else None,
            "products": view.join(t(f"product.{x}") for x in d.product_types),
            "discount": discount, "discount_note": discount_note, "recurring": t(f"recurring.{d.recurring}"),
            "codes": d.coupon_codes,  # 放在标题卡的价格框里，可复制（O-07）；没有码时不出
            "published": view.clock(d.published_at), "seen": view.clock(d.last_seen_at),
            "expiry": view.expiry(d),  # 截止日期与还剩几天（O-15）；原帖没写为 null
        },
        "plans_heading": t("deal.plans", n=len(d.plans)),
        "plans": plans, "cols": _cols(plans, card=False),
        "excerpt": d.excerpt,
        "changes": changes,
        "same_heading": t("deal.same") if same else None,
        "same": [{"title": view.pick_title(x.title, x.title_zh, x.title_en), "href": f"{p}/deal/{x.id}",
                  "meta": view.day(x.published_at)} for x in same],
        "others_heading": t("deal.same_vendor", vendor=d.vendor) if others else None,
        "others": [{"title": view.title(o), "href": f"{p}/deal/{o.id}",
                    "meta": (start[0] + sep("comma") if (start := view.start(o)) else "") + view.day(o.published_at)}
                   for o in others],
        "intel": _intel(view, vendor_intel(conn, d.vendor_id, limit=10)) if d.vendor_id else [],
    })


@router.get("/{lang}/codes")
def codes(request: Request, lang: str) -> Response:
    """优惠码汇总（O-18）：本季带码的优惠，每行商家、码（可复制）、折扣、续费性质、有效期；已结束的排在最后。"""
    if lang not in LANGS:
        return error(404, "not_found", "没有这种语言")
    view = _view(request, lang)
    return cached_response(request, f"int:{lang}:/codes", PAGE_TTL, lambda: _codes(request, view))


def _codes(request: Request, view: View) -> JSONResponse:
    settings, conn = request.app.state.settings, request.app.state.db.conn()
    t, p = view.t, view.prefix
    deals = deals_with_codes(conn, fmt.season_start_iso(settings.season_start), fmt.ended_cutoff(view.today))
    slugs = vendor_slugs(conn, {d.vendor_id for d in deals if d.vendor_id})
    return _json({
        "seo": seo.codes(settings, view),
        "total": t("codes.total", n=len(deals)),
        "rows": [{"id": d.id, "href": f"{p}/deal/{d.id}", "vendor": view.vendor(d),
                  "vendor_href": f"{p}/vendor/{slugs[d.vendor_id]}" if d.vendor_id in slugs else None,
                  "title": view.title(d), "codes": d.coupon_codes,
                  "discount": t("discount.max", pct=d.discount_pct) if d.discount_pct else "",
                  "recurring": t(f"recurring.{d.recurring}"), "expiry": view.expiry(d), "date": view.day(d.published_at)}
                 for d in deals],
        "empty": "" if deals else t("codes.empty"),
    })


@router.get("/{lang}/vendors")
def vendors(request: Request, lang: str) -> Response:
    """商家目录（O-17）：本季有优惠或近 30 天有讨论的商家，默认按本季优惠数排，可按名称 A–Z。"""
    if lang not in LANGS:
        return error(404, "not_found", "没有这种语言")
    sort = request.query_params.get("sort", "")
    sort = sort if sort in VENDOR_SORTS else "deals"
    view = _view(request, lang)
    return cached_response(request, f"int:{lang}:/vendors?sort={sort}", PAGE_TTL, lambda: _vendors(request, view, sort))


def _vendors(request: Request, view: View, sort: str) -> JSONResponse:
    settings, conn = request.app.state.settings, request.app.state.db.conn()
    t, p = view.t, view.prefix
    rows = vendor_directory(conn, fmt.season_start_iso(settings.season_start),
                            fmt.since(view.now, 24 * DIRECTORY_INTEL_DAYS), sort)
    return _json({
        "seo": seo.vendors(settings, view, "/vendors"),  # 换排序只是换顺序，规范地址都用 /vendors
        "total": t("vendors.total", n=len(rows)),
        "sorts": [{"value": s, "label": t(f"vendors.sort.{s}"), "current": s == sort,
                   "href": p + ("/vendors" if s == "deals" else f"/vendors?sort={s}")} for s in VENDOR_SORTS],
        "rows": [{"name": r.name, "href": f"{p}/vendor/{r.slug}", "aliases": view.join(r.aliases),
                  "deals": t("vendors.deals_n", n=r.deals) if r.deals else t("vendors.no_deals"), "has_deals": bool(r.deals),
                  "low": f"≈ ¥{r.low_cny_year:,.0f}/{view.t_period('year')}" if r.low_cny_year is not None else "",
                  "latest": view.day(r.latest), "regions": view.join(t(f"region.{x}") for x in r.regions)}
                 for r in rows],
        "empty": "" if rows else t("vendors.empty"),
    })


@router.get("/{lang}/vendor/{slug}")
def vendor(request: Request, lang: str, slug: str) -> Response:
    """商家页（FR-11、O-17）：别名、官网、机房与线路、历史最低起价、本季在售套餐、本季与往季的优惠、社区讨论。"""
    if lang not in LANGS:
        return error(404, "not_found", "没有这种语言")
    slug, view = slug[:80], _view(request, lang)
    return cached_response(request, f"int:{lang}:/vendor/{slug}", DETAIL_TTL, lambda: _vendor(request, view, slug))


def _vendor(request: Request, view: View, slug: str) -> JSONResponse:
    settings, conn = request.app.state.settings, request.app.state.db.conn()
    v = vendor_by_slug(conn, slug)
    if v is None:
        return error(404, "not_found", "没有这个商家")
    t, sep, p = view.t, view.sep, view.prefix
    season = fmt.season_start_iso(settings.season_start)
    deals = vendor_deals(conn, v["id"])
    this_season = [d for d in deals if (d.published_at or "") >= season]
    earlier = [d for d in deals if (d.published_at or "") < season]
    overview = vendor_overview(conn, v["id"], season, fmt.ended_cutoff(view.today))
    plans = []
    for r in overview.plans:
        row = _plan(view, r.plan, _url(r.purchase_url))
        row["href"] = f"{p}/deal/{r.deal_id}"
        plans.append(row)
    priced = [d for d in deals if d.min_price_cny_year is not None]
    lowest = min(priced, key=lambda d: d.min_price_cny_year) if priced else None
    aliases = json.loads(v["aliases_json"]) + json.loads(v["short_aliases_json"])
    website = _url(vendor_website(conn, v))

    def item(d: Deal) -> dict:
        return {"title": view.title(d), "href": f"{p}/deal/{d.id}",
                "event": t(f"event.{d.event_type}") if d.is_black_friday else None, "expiry": view.expiry(d),
                "meta": (start[0] + sep("comma") if (start := view.start(d)) else "") + view.day(d.published_at)}

    return _json({
        "seo": seo.vendor(settings, view, v, website, aliases),
        "name": v["name"], "aka": t("vendor.aka") + sep("colon") + view.join(aliases) if aliases else None,
        "website": website,
        "places": [{"region": t(f"region.{area}"),
                    "cities": view.join(f"{view.location(c)} {n}" for c, n in cities[:6])}
                   for area, cities in overview.places],
        "net": [view.net_label(x) for x in overview.net],
        "lowest": {"price": list(view.start(lowest)), "title": view.title(lowest), "href": f"{p}/deal/{lowest.id}",
                   "date": view.day(lowest.published_at)} if lowest else None,
        "plans_heading": t("vendor.plans", n=overview.plan_total, shown=len(plans)) if plans else None,
        "plans": plans, "cols": _cols(plans, card=False),
        "deals_heading": t("vendor.deals_season", n=len(this_season)),
        "deals": [item(d) for d in this_season],
        "past_heading": t("vendor.past", n=len(earlier)) if earlier else None,
        "past": [item(d) for d in earlier],
        "intel": _intel(view, vendor_intel(conn, v["id"])),
    })


@router.get("/{lang}/about")
def about(request: Request, lang: str) -> Response:
    """关于页（FR-20、第 17 节）：只给带数据的部分（本季起点），其余是字典里的固定文字。"""
    if lang not in LANGS:
        return error(404, "not_found", "没有这种语言")
    view = _view(request, lang)
    return cached_response(request, f"int:{lang}:/about", DETAIL_TTL, lambda: _about(request, view))


def _about(request: Request, view: View) -> JSONResponse:
    s, t = request.app.state.settings, view.t
    return _json({
        "seo": seo.about(s, view),
        "scope_bf": t("about.scope.bf", season=s.season_start.strftime("%Y-%m-%d")),
    })


# ---------------------------------------------------------------- 状态页


@router.get("/{lang}/status")
def status(request: Request, lang: str) -> Response:
    """采集状态（FR-13）：档位、数据量、汇率、LLM 调用与校验拒绝率、NodeSeek 覆盖率、各源最近 10 轮。
    不进内存缓存，每次现算；页面不在别处链接（D59）。"""
    if lang not in LANGS:
        return error(404, "not_found", "没有这种语言")
    return _status(request, _view(request, lang))


def _fact(label: str, value: str, note: str | None = None) -> dict:
    return {"label": label, "value": value, "note": note}


def _status(request: Request, view: View) -> JSONResponse:
    settings, conn, t = request.app.state.settings, request.app.state.db.conn(), view.t
    scheduler = request.app.state.scheduler
    sched = scheduler.status() if scheduler else None
    today = fmt.local(view.now, settings.display_tz_zh).date()  # 黑五周档位按北京时间的日期（D50）
    since = fmt.since(view.now, 24)
    stats = site_stats(conn, season_start=fmt.season_start_iso(settings.season_start), since=since)
    llm = llm_summary(conn, since)
    sources = [s for s in STATUS_SOURCES if s in settings.sources]
    if settings.llm_enabled or llm.runs:
        sources.append("llm")
    tier = sched["tier"] if sched else ("bf" if settings.is_bf_week(today) else "normal")
    intervals = sched["intervals"] if sched else {s: settings.interval(s, today) for s in settings.sources}
    jobs = sched["jobs"] if sched else {}
    deals = stats["deals"]
    locations = location_stats(conn, fmt.season_start_iso(settings.season_start))
    facts = [
        _fact(t("statuspage.tier"), t(f"tier.{tier}"),
              t("statuspage.tier_note", start=settings.bf_week_start.strftime("%m-%d"),
                end=settings.bf_week_end.strftime("%m-%d"))),
        _fact(t("statuspage.scheduler"), _scheduler_state(view, sched)),
        _fact(t("statuspage.deals"), t("statuspage.deals_value", total=deals["total"], season=deals["season"],
                                       bf=deals["bf"], new=deals["new"])),
        _fact(t("statuspage.data"), t("statuspage.data_value", plans=stats["plans"], intel=stats["intel"]["total"],
                                      vendors=stats["vendors"])),
        _fact(t("statuspage.fx"), _fx_state(view, conn, settings)),
        _llm_fact(view, llm, stats["llm"].get("skipped"), settings.llm_enabled),
        _fact(t("statuspage.locations"), t("statuspage.locations_value", n=locations[0], unknown=locations[1])),
        _fact(t("statuspage.config"), view.sep("semi").join(t(k) for k in settings.launch_warnings())
              or t("statuspage.config_ok")),
    ]
    if "nodeseek" in settings.sources:
        coverage = nodeseek_coverage(conn, since)
        value = (t("statuspage.coverage_value", runs=coverage.runs, pct=view.pct(coverage.ratio, 1), gap=coverage.gap)
                 + (t("statuspage.coverage_unlinked", n=coverage.unlinked) if coverage.unlinked else "")
                 if coverage.ratio is not None else t("statuspage.coverage_none"))
        facts.append(_fact(t("statuspage.coverage"), value))
    facts.append(_fact(t("statuspage.generated"), view.clock(view.now)))
    return _json({
        "seo": seo.status(settings, view),
        "refreshing": bool(sched and sched["refreshing"]),
        "facts": facts,
        "sources": [_runs(view, s, intervals, jobs) for s in source_runs(conn, sources)],
    })


def _scheduler_state(view: View, sched: dict | None) -> str:
    t = view.t
    if sched is None:
        return t("statuspage.scheduler_off")
    return (t("statuspage.scheduler_on") + (t("statuspage.scheduler_refreshing") if sched["refreshing"] else "")
            + (t("statuspage.scheduler_llm") if sched["llm_running"] else ""))


def _fx_state(view: View, conn: sqlite3.Connection, settings: Settings) -> str:
    row = conn.execute("SELECT date, provider, fetched_at FROM fx_rates ORDER BY date DESC LIMIT 1").fetchone()
    if row:
        return view.t("statuspage.fx_value", provider="ECB" if row["provider"] == "ecb" else row["provider"],
                      date=row["date"], time=view.clock(row["fetched_at"]))
    fx = latest_rates(conn, static="static" in settings.fx_providers)
    return view.t("statuspage.fx_static", date=fx.date if fx else "")


def _llm_fact(view: View, llm: LlmSummary, skipped: int | None, enabled: bool) -> dict:
    t = view.t
    if not enabled and not llm.runs:
        return _fact(t("statuspage.llm"), t("statuspage.llm_off"))
    value = t("statuspage.llm_value", calls=llm.calls, done=llm.done, failed=llm.failed, rejected=llm.rejected,
              units=llm.units, rate=view.pct(llm.reject_rate, 1) or "—", tokens=f"{llm.tokens:,}")
    note = t("statuspage.llm_pending", n=llm.pending) + (t("statuspage.llm_skipped", n=skipped) if skipped else "")
    return _fact(t("statuspage.llm"), value, note)


def _runs(view: View, s: SourceRuns, intervals: dict, jobs: dict) -> dict:
    """一个源的一节：间隔、下次运行、最近成功、连续失败，与最近 10 轮的表（LLM 队列的列不同）。"""
    t, key = view.t, s.source
    meta = [view.every(intervals[key])] if key != "llm" and intervals.get(key) else []
    job = jobs.get(f"source:{key}") or (jobs.get("llm") if key == "llm" else None)
    if job:
        meta.append(t("statuspage.next", time=view.clock(job)))
    meta.append(t("statuspage.last_ok", time=view.clock(s.last_ok_at)) if s.last_ok_at else t("statuspage.never_ok"))
    if key == "llm":
        columns = ("pending", "calls", "done", "failed", "rejected", "tokens")
    else:
        columns = ("fetched", "new", "updated", *(("coverage",) if key == "nodeseek" else ()))
    rows = []
    for r in s.runs:
        if key == "llm":
            cells = [str(r.fetched), str(r.llm_calls), str(r.updated), str(r.extra.get("failed") or 0),
                     f"{r.llm_rejected}/{r.extra.get('units') or 0}",
                     f"{(r.extra.get('tokens') or {}).get('total_tokens') or 0:,}"]
        else:
            cells = [str(r.fetched), str(r.new), str(r.updated)]
            if key == "nodeseek":
                cov, gap = r.extra.get("coverage"), r.extra.get("gap")
                cells.append(" ".join(x for x in (view.pct(cov) if cov is not None else "",
                                                  t("run.gap", n=gap) if gap else "",
                                                  t("run.unlinked") if r.extra.get("overlap") is False else "") if x))
        rows.append({"bad": r.error is not None, "started_at": r.started_at, "time": view.clock(r.started_at),
                     "took": view.duration(r.seconds), "cells": cells,
                     "result": f"✗ {r.error}" if r.error else "✓"})
    return {"key": key, "name": t(f"source.{key}"), "meta": meta,
            "failing": t("statuspage.failing", n=s.failing) if s.failing else None,
            "columns": [t("run.started"), t("run.took"), *(t(f"run.{c}") for c in columns), t("run.result")],
            "rows": rows, "empty": t("statuspage.no_runs")}
