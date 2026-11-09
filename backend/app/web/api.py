"""JSON API（第 14 节，FR-15、FR-16）：只读（手动刷新除外）、允许跨域 GET、参数与页面相同、错误一律 {error, message}。

列表与详情经内存缓存 60 秒（第 15.5 节）；/healthz 与刷新不缓存。接口说明由 FastAPI 生成在 /api/openapi.json；
不开 Swagger 页面（它要从 CDN 加载脚本，与 CSP 和"不加载境外脚本"冲突，D26）。
"""

import hmac
import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response

from ..extract.keywords import NET_TAGS
from ..models import Deal, Plan, utcnow
from ..repo import deal_changes, site_stats
from ..scheduler import RefreshBusy
from . import fmt
from .cache import cached_response
from .queries import (OFFER_SOURCES, PAGE_SIZE, PERIODS, PLAN_PAGE_SIZE, PLAN_PREVIEW, PLAN_SORTS, PRODUCTS, SCOPES,
                      SORTS, DealQuery, PlanQuery, deal_by_id, last_update, list_deals, list_plans, recent_runs,
                      source_runs, vendor_by_slug, vendor_deals, vendor_intel, vendor_slugs, vendor_website)
from .site import STATUS_SOURCES, next_crawl
from .view import ended_before

log = logging.getLogger(__name__)
router = APIRouter()

API_TTL = 60
PAGE_SIZE_MAX = 100
RUNS_DEFAULT, RUNS_MAX = 20, 50
FAILING_RUNS = 3  # 连续失败这么多轮算故障（/healthz）
NO_STORE = "no-store"
CORS = {"Access-Control-Allow-Origin": "*"}
JSON_CHANGES = {"coupon_codes", "plan_price"}  # 变更记录里以 JSON 文本保存的字段
MESSAGES = {404: ("not_found", "没有这个接口"), 405: ("method_not_allowed", "不支持这个请求方法")}


def json_response(data, *, status: int = 200, cache: str = f"public, max-age={API_TTL}",
                  headers: dict | None = None) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={"Cache-Control": cache, **CORS, **(headers or {})})


def error(status: int, code: str, message: str, *, cache: str = NO_STORE, headers: dict | None = None) -> JSONResponse:
    return json_response({"error": code, "message": message}, status=status, cache=cache, headers=headers)


def http_error(status: int, detail: str, headers: dict | None = None) -> JSONResponse:
    """/api/* 下 Starlette 抛出的 HTTP 错误（未知路径、方法不对）也用 {error, message}。"""
    code, message = MESSAGES.get(status, ("error", detail))
    return error(status, code, message, headers=headers)


# ---------------------------------------------------------------- 序列化


def plan_json(p: Plan) -> dict:
    return {
        "name": p.name, "cpu_cores": p.cpu_cores, "cpu_desc": p.cpu_desc, "ram_gb": p.ram_gb, "disk_gb": p.disk_gb,
        "disk_type": p.disk_type, "bandwidth_tb": p.bandwidth_tb, "port_gbps": p.port_gbps, "net": p.net,
        "locations": p.locations, "price": p.price, "currency": p.currency, "period": p.period,
        "price_note": p.price_note, "price_cny_year": p.price_cny_year, "coupon_code": p.coupon_code,
        "term_months": p.term_months, "renewal_price": p.renewal_price,
        "order_url": p.order_url,
    }


def deal_json(d: Deal, site: str, vendor_slug: str | None, *, plans: int | None) -> dict:
    """plans 为 None 时给全部套餐，否则给前 plans 个（plan_count 总是全数）。"""
    shown = d.plans if plans is None else d.plans[:plans]
    start = None
    if d.min_price is not None:
        start = {"amount": d.min_price, "currency": d.min_price_currency, "period": d.min_price_period,
                 "cny_year": d.min_price_cny_year}
    return {
        "id": d.id, "page_url": f"{site}/deal/{d.id}", "url": d.url, "source": d.source, "sources": d.sources,
        "title": d.title, "title_zh": d.title_zh, "title_en": d.title_en,
        "vendor": d.vendor, "vendor_slug": vendor_slug,
        "event_type": d.event_type, "is_black_friday": d.is_black_friday, "product_types": d.product_types,
        "discount_pct": d.discount_pct, "discount_text": d.discount_text, "recurring": d.recurring,
        "coupon_codes": d.coupon_codes, "min_price": start, "specs_range": d.specs_range,
        "starts_at": d.starts_at, "ends_at": d.ends_at, "ends_source": d.ends_source,
        "purchase_url": d.purchase_url, "summary_zh": d.summary_zh, "summary_en": d.summary_en, "excerpt": d.excerpt,
        "published_at": d.published_at, "first_seen_at": d.first_seen_at, "last_seen_at": d.last_seen_at,
        "updated_at": d.updated_at, "plan_count": len(d.plans), "plans": [plan_json(p) for p in shown],
    }


def intel_json(r) -> dict:
    return {"url": r["url"], "source": r["source"], "category": r["category"], "title": r["title"],
            "published_at": r["published_at"], "matched_alias": r["matched_alias"]}


def _change_value(field: str, value: str | None):
    if value is None:
        return None
    if field in JSON_CHANGES:
        return json.loads(value)
    return int(value) if field == "discount_pct" else value


def _int(raw: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(max(int(raw), lo), hi)
    except ValueError:
        return default


def _ctx(request: Request):
    return request.app.state.settings, request.app.state.db.conn()


def _season(request: Request) -> str:
    return fmt.season_start_iso(request.app.state.settings.season_start)


# ---------------------------------------------------------------- 接口文档里的查询参数（解析仍用页面的白名单）


def _param(name: str, description: str, kind: str = "string", enum=None, array: bool = False) -> dict:
    schema: dict = {"type": kind}
    if enum:
        schema["enum"] = list(enum)
    if array:
        schema = {"type": "array", "items": schema}
    out = {"name": name, "in": "query", "required": False, "description": description, "schema": schema}
    if array:
        out.update(style="form", explode=True)
    return out


SCOPE = _param("scope", "bf：本季黑五/网一（默认）；promo：本季全部；all：不限发布时间（D31）", enum=SCOPES)
PAGING = [_param("page", "页码（≤ 200）", "integer"), _param("page_size", f"每页条数（≤ {PAGE_SIZE_MAX}）", "integer")]
DEAL_PARAMS = [
    SCOPE, _param("q", "搜索标题、中文标题、商家名与别名、优惠码（≤ 100 字）"),
    _param("source", "来源，可重复；全选等于不筛", enum=OFFER_SOURCES, array=True),
    _param("product", "产品类型，可重复；全选等于不筛", enum=PRODUCTS, array=True),
    _param("sort", "newest 最新（默认）、price 起价最低、discount 折扣最高、changed 最近变更", enum=SORTS),
    _param("ended", "1：含已结束的活动（原帖写明的截止日期已过，默认不含；含时排在最后）", enum=["1"]), *PAGING,
]
PLAN_PARAMS = [
    _param("scope", "同 /api/deals，默认 promo（D52）", enum=SCOPES),
    _param("cpu_min", "CPU 核数下限（核数不明的套餐不算）", "number"),
    _param("ram_min", "内存下限（GB）", "number"), _param("disk_min", "存储下限（GB）", "number"),
    _param("price_max_cny_year", "≈人民币年价上限（也可写 price_max）", "number"),
    _param("location", "机房（与套餐里的写法相同，如 Los Angeles）"), _param("period", "付费周期", enum=PERIODS),
    _param("nvme", "1：只看 NVMe", enum=["1"]),
    _param("net", "线路标签，可重复，套餐带其中任何一个", enum=tuple(NET_TAGS), array=True),
    _param("sort", "price ≈人民币年价升序（默认）、ram、disk 降序、newest", enum=PLAN_SORTS), *PAGING,
]


# ---------------------------------------------------------------- 只读接口


@router.get("/api/deals", summary="优惠列表", tags=["优惠"], openapi_extra={"parameters": DEAL_PARAMS})
def api_deals(request: Request) -> Response:
    """{total, page, pages, page_size, items}；每条带前 10 个套餐（plan_count 为全数）。"""
    query = DealQuery.parse(request.query_params)
    size = _int(request.query_params.get("page_size", ""), PAGE_SIZE, 1, PAGE_SIZE_MAX)
    return cached_response(request, f"api:deals:{query.href()}:{size}", API_TTL, lambda: _deals(request, query, size))


def _deals(request: Request, query: DealQuery, size: int) -> JSONResponse:
    settings, conn = _ctx(request)
    page = list_deals(conn, query, _season(request), page_size=size, ended_before=ended_before(settings))
    slugs = vendor_slugs(conn, {d.vendor_id for d in page.deals if d.vendor_id})
    return json_response({
        "total": page.total, "page": page.page, "pages": page.pages, "page_size": size,
        "items": [deal_json(d, settings.site_url, slugs.get(d.vendor_id), plans=PLAN_PREVIEW) for d in page.deals],
    })


@router.get("/api/plans", summary="套餐列表", tags=["套餐"], openapi_extra={"parameters": PLAN_PARAMS})
def api_plans(request: Request) -> Response:
    """{total, page, pages, page_size, items}；每个套餐带所属优惠的摘要。"""
    query = PlanQuery.parse(request.query_params)
    size = _int(request.query_params.get("page_size", ""), PLAN_PAGE_SIZE, 1, PAGE_SIZE_MAX)
    return cached_response(request, f"api:plans:{query.href()}:{size}", API_TTL, lambda: _plans(request, query, size))


def _plans(request: Request, query: PlanQuery, size: int) -> JSONResponse:
    settings, conn = _ctx(request)
    page = list_plans(conn, query, _season(request), page_size=size)
    items = [{**plan_json(r.plan), "deal": {
        "id": r.deal_id, "page_url": f"{settings.site_url}/deal/{r.deal_id}", "url": r.deal_url,
        "title": r.deal_title, "title_zh": r.deal_title_zh, "title_en": r.deal_title_en,
        "purchase_url": r.purchase_url, "vendor": r.vendor, "vendor_slug": r.vendor_slug,
    }} for r in page.rows]
    return json_response({"total": page.total, "page": page.page, "pages": page.pages, "page_size": size,
                          "items": items})


@router.get("/api/deals/{deal_id:int}", summary="一条优惠", tags=["优惠"])
def api_deal(request: Request, deal_id: int) -> Response:
    """全部字段与套餐、变更记录、该商家的社区讨论。"""
    return cached_response(request, f"api:deal:{deal_id}", API_TTL, lambda: _deal(request, deal_id))


def _deal(request: Request, deal_id: int) -> JSONResponse:
    settings, conn = _ctx(request)
    deal = deal_by_id(conn, deal_id)
    if deal is None:
        return error(404, "not_found", "没有这条优惠", cache=f"public, max-age={API_TTL}")
    slug = vendor_slugs(conn, {deal.vendor_id}).get(deal.vendor_id) if deal.vendor_id else None
    data = deal_json(deal, settings.site_url, slug, plans=None)
    data["changes"] = [{"changed_at": c["changed_at"], "field": c["field"],
                        "old": _change_value(c["field"], c["old_value"]),
                        "new": _change_value(c["field"], c["new_value"])} for c in deal_changes(conn, deal.id)]
    data["intel"] = [intel_json(r) for r in vendor_intel(conn, deal.vendor_id)] if deal.vendor_id else []
    return json_response(data)


@router.get("/api/vendors/{slug}", summary="商家", tags=["商家"])
def api_vendor(request: Request, slug: str) -> Response:
    """商家、别名、官网、全部优惠（不含套餐明细）与社区讨论。"""
    slug = slug[:80]
    return cached_response(request, f"api:vendor:{slug}", API_TTL, lambda: _vendor(request, slug))


def _vendor(request: Request, slug: str) -> JSONResponse:
    settings, conn = _ctx(request)
    vendor = vendor_by_slug(conn, slug)
    if vendor is None:
        return error(404, "not_found", "没有这个商家", cache=f"public, max-age={API_TTL}")
    return json_response({
        "slug": vendor["slug"], "name": vendor["name"], "page_url": f"{settings.site_url}/vendor/{vendor['slug']}",
        "aliases": json.loads(vendor["aliases_json"]), "short_aliases": json.loads(vendor["short_aliases_json"]),
        "website": vendor_website(conn, vendor),
        "deals": [deal_json(d, settings.site_url, vendor["slug"], plans=0) for d in vendor_deals(conn, vendor["id"])],
        "intel": [intel_json(r) for r in vendor_intel(conn, vendor["id"])],
    })


@router.get("/api/stats", summary="统计", tags=["状态"])
def api_stats(request: Request) -> Response:
    """总数、本季与黑五数、24 小时新增、按来源与活动类型统计、LLM 状态、汇率、上次与下次更新。"""
    return cached_response(request, "api:stats", API_TTL, lambda: _stats(request))


def _stats(request: Request) -> JSONResponse:
    _, conn = _ctx(request)
    season = _season(request)
    stats = site_stats(conn, season_start=season, since=fmt.since(utcnow(), 24))
    deals = stats["deals"]
    return json_response({
        "deals": {"total": deals["total"], "season": deals["season"], "black_friday": deals["bf"],
                  "new_24h": deals["new"],
                  "by_source": dict(conn.execute("SELECT source, COUNT(*) FROM deals GROUP BY source")),
                  "by_event_type": dict(conn.execute("SELECT event_type, COUNT(*) FROM deals WHERE published_at >= ?"
                                                     " GROUP BY event_type", (season,)))},
        "plans": stats["plans"], "intel": stats["intel"], "vendors": stats["vendors"], "llm": stats["llm"],
        "fx": stats["fx"], "season_start": season, "last_update": last_update(conn),
        "next_update": next_crawl(request.app.state.scheduler),
    })


@router.get("/api/runs", summary="最近的采集记录", tags=["状态"],
            openapi_extra={"parameters": [_param("limit", f"条数（≤ {RUNS_MAX}，默认 {RUNS_DEFAULT}）", "integer")]})
def api_runs(request: Request) -> Response:
    limit = _int(request.query_params.get("limit", ""), RUNS_DEFAULT, 1, RUNS_MAX)
    return cached_response(request, f"api:runs:{limit}", API_TTL, lambda: _runs(request, limit))


def _runs(request: Request, limit: int) -> JSONResponse:
    _, conn = _ctx(request)
    return json_response({"items": [{
        "id": r["id"], "started_at": r["started_at"], "finished_at": r["finished_at"],
        "ok": None if r["ok"] is None else bool(r["ok"]), "reason": r["reason"],
        "sources": [{"source": s.source, "started_at": s.started_at, "finished_at": s.finished_at,
                     "seconds": s.seconds, "fetched": s.fetched, "new": s.new, "updated": s.updated,
                     "llm_calls": s.llm_calls, "llm_rejected": s.llm_rejected, "error": s.error, "extra": s.extra}
                    for s in sources],
    } for r, sources in recent_runs(conn, limit)]})


@router.api_route("/healthz", methods=["GET", "HEAD"], summary="健康检查", tags=["状态"])
def healthz(request: Request) -> JSONResponse:
    """ok 为 false 时返回 503：有源连续失败 3 轮，或最近一轮采集离现在超过最短间隔的 3 倍（至少 30 分钟）。"""
    settings, conn = _ctx(request)
    now = utcnow()
    sources = [s for s in STATUS_SOURCES if s in settings.sources]
    runs = source_runs(conn, sources)
    last = conn.execute("SELECT MAX(finished_at) FROM run_sources WHERE source != 'llm'").fetchone()[0]
    scheduler = request.app.state.scheduler
    intervals = scheduler.status()["intervals"] if scheduler else {}
    limit = timedelta(minutes=max(30, 3 * min(intervals.values() or [settings.intervals[s] for s in sources] or [10])))
    fresh = last is not None and now - datetime.fromisoformat(last) <= limit
    failing = [r.source for r in runs if r.failing >= FAILING_RUNS]
    fx = conn.execute("SELECT date FROM fx_rates ORDER BY date DESC LIMIT 1").fetchone()
    ok = fresh and not failing
    return json_response({
        "ok": ok, "last_run_at": last, "stale": not fresh, "failing": failing,
        "sources": {r.source: {"last_ok_at": r.last_ok_at, "failing": r.failing} for r in runs},
        "fx_date": fx[0] if fx else None,
    }, status=200 if ok else 503, cache=NO_STORE)


# ---------------------------------------------------------------- 手动刷新（FR-16）


@router.post("/api/refresh", summary="手动刷新一轮采集", tags=["状态"])
def api_refresh(request: Request, x_refresh_token: str | None = Header(default=None)) -> JSONResponse:
    """需要 X-Refresh-Token；未设置 REFRESH_TOKEN 时关闭（403），已有一轮在跑时 409。返回 {run_id}。"""
    settings = request.app.state.settings
    if not settings.refresh_token:
        return error(403, "refresh_disabled", "服务端没有设置 REFRESH_TOKEN，手动刷新已关闭")
    if not x_refresh_token or not hmac.compare_digest(x_refresh_token.encode(), settings.refresh_token.encode()):
        return error(401, "unauthorized", "缺少或错误的 X-Refresh-Token")
    scheduler = request.app.state.scheduler
    if scheduler is None:
        return error(503, "scheduler_unavailable", "本进程没有运行调度器（serve --no-scheduler）")
    try:
        run_id = scheduler.refresh()
    except RefreshBusy:
        return error(409, "busy", "已有一轮手动刷新在进行")
    log.info("手动刷新：运行 %d", run_id)
    return json_response({"run_id": run_id}, cache=NO_STORE)
