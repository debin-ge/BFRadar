"""页面查询（只读，D25）。查询参数先经白名单与长度校验（第 15.4 节）；scope 的含义与 RSS、API 一致（D31）。"""

import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import urlencode, urlsplit

from ..extract.keywords import NET_TAGS
from ..locations import REGIONS
from ..locations import normalize as normalize_location
from ..locations import region as location_region
from ..models import Deal, Plan, ProductType
from ..pipeline import LLM_SOURCE, llm_candidates
from ..repo import latest_runs, load_deal, plan_from_row

SCOPES = ("bf", "promo", "all")
SORTS = ("newest", "price", "discount", "changed")
OFFER_SOURCES = ("lowendtalk", "lowendspirit", "lowendbox", "telegram", "official", "nodeseek", "reddit")  # 会产出优惠的源
PRODUCTS = tuple(p.value for p in ProductType)
PAGE_SIZE = 30  # 页面体积 ≤ 150 KB（第 9 节）：每条优惠带前 PLAN_PREVIEW 个套餐
PLAN_PREVIEW = 10
MAX_PAGE = 200
Q_MAX = 100
ORDER = {
    "newest": "d.published_at DESC, d.id DESC",
    "price": "d.min_price_cny_year IS NULL, d.min_price_cny_year, d.published_at DESC",
    "discount": "d.discount_pct IS NULL, d.discount_pct DESC, d.published_at DESC",
    "changed": "COALESCE((SELECT MAX(c.changed_at) FROM changes c WHERE c.deal_id = d.id), d.updated_at) DESC,"
               " d.published_at DESC",
}


@dataclass(frozen=True)
class DealQuery:
    scope: str = "bf"
    sources: tuple[str, ...] = ()  # 空为全部
    products: tuple[str, ...] = ()
    q: str = ""
    sort: str = "newest"
    ended: bool = False  # 含已结束的活动（O-15）：默认不含，含时排在最后
    page: int = 1

    @classmethod
    def parse(cls, params) -> "DealQuery":
        """params 为 Starlette QueryParams 或 {名: [值, …]}；不认识的取值丢掉，越界的收回默认或边界。"""
        def values(name: str) -> list[str]:
            if hasattr(params, "getlist"):
                return params.getlist(name)
            raw = params.get(name, [])
            return raw if isinstance(raw, list) else [raw]

        def first(name: str) -> str:
            got = values(name)
            return got[0].strip() if got else ""

        scope, sort = first("scope"), first("sort")
        try:
            page = min(max(int(first("page") or 1), 1), MAX_PAGE)
        except ValueError:
            page = 1
        sources = tuple(s for s in OFFER_SOURCES if s in values("source"))
        products = tuple(p for p in PRODUCTS if p in values("product"))
        return cls(
            scope=scope if scope in SCOPES else "bf",
            sources=() if len(sources) == len(OFFER_SOURCES) else sources,  # 全勾等于不筛
            products=() if len(products) == len(PRODUCTS) else products,
            q=" ".join(first("q").split())[:Q_MAX],
            sort=sort if sort in SORTS else "newest",
            ended=first("ended") == "1",
            page=page,
        )

    def href(self, **changes) -> str:
        """当前筛选（可改其中几项）对应的地址；默认值不写进查询串，链接可复制分享。"""
        query = replace(self, **changes)
        params = [("scope", query.scope)] if query.scope != "bf" else []
        params += [("source", s) for s in query.sources] + [("product", p) for p in query.products]
        params += [("q", query.q)] if query.q else []
        params += [("sort", query.sort)] if query.sort != "newest" else []
        params += [("ended", "1")] if query.ended else []
        params += [("page", str(query.page))] if query.page > 1 else []
        return "/?" + urlencode(params) if params else "/"

    @property
    def filtered(self) -> bool:
        return bool(self.sources or self.products or self.q or self.ended)


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _where(query: DealQuery, season_start: str, ended_before: str | None = None) -> tuple[str, list]:
    sql, params = ["1 = 1"], []
    if ended_before and not query.ended:  # 截止日期早于 ended_before 的已结束；没写截止日期的照常显示
        sql.append("(d.ends_at IS NULL OR d.ends_at >= ?)")
        params.append(ended_before)
    if query.scope in ("bf", "promo"):
        sql.append("d.published_at >= ?")
        params.append(season_start)
    if query.scope == "bf":
        sql.append("d.is_black_friday = 1")
    if query.sources:
        sql.append(f"d.source IN ({', '.join('?' * len(query.sources))})")
        params += query.sources
    if query.products:
        sql.append(f"EXISTS (SELECT 1 FROM json_each(d.product_types_json) j"
                   f" WHERE j.value IN ({', '.join('?' * len(query.products))}))")
        params += query.products
    if query.q:
        fields = ("d.title", "d.title_zh", "v.name", "v.aliases_json", "d.coupon_codes_json")
        sql.append("(" + " OR ".join(f"{f} LIKE ? ESCAPE '\\'" for f in fields) + ")")
        params += [_like(query.q)] * len(fields)
    return " AND ".join(sql), params


@dataclass
class DealPage:
    deals: list[Deal]
    total: int
    page: int
    pages: int


def list_deals(conn: sqlite3.Connection, query: DealQuery, season_start: str, page_size: int = PAGE_SIZE, *,
               ended_before: str | None = None) -> DealPage:
    """ended_before（见 fmt.ended_cutoff）：给了才处理已结束的活动——默认不列，query.ended 时列出并排到最后。
    "含往季"（scope=all）本来就是翻旧帖的，不处理：已结束的照常按时间列出。"""
    if query.scope == "all":
        ended_before = None
    where, params = _where(query, season_start, ended_before)
    base = f"FROM deals d LEFT JOIN vendors v ON v.id = d.vendor_id WHERE {where}"
    total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
    pages = max(1, math.ceil(total / page_size))
    page = min(query.page, pages)
    order, order_params = ORDER[query.sort], []
    if ended_before and query.ended:
        order, order_params = f"(d.ends_at IS NOT NULL AND d.ends_at < ?), {order}", [ended_before]
    rows = conn.execute(f"SELECT d.url {base} ORDER BY {order} LIMIT ? OFFSET ?",
                        [*params, *order_params, page_size, (page - 1) * page_size]).fetchall()
    return DealPage([load_deal(conn, r["url"]) for r in rows], total, page, pages)


@dataclass(frozen=True)
class Activity:
    """"24 小时动态"的一行：new 新上架、price_drop 降价、new_code 新码，earlier 是不足时补上的本季较早的优惠。"""

    kind: str
    at: str | None
    deal: Deal


ACTIVITY_FLOOR = 3  # 24 小时内的动态少于这么多条时，用本季最新的优惠补满


def recent_activity(conn: sqlite3.Connection, season_start: str, since: str, flags: dict,
                    limit: int = 8) -> list[Activity]:
    """首页"24 小时动态"（优化方案 O-10）：since 之后新上架的优惠，与有降价、新码的优惠（flags 即 repo.recent_flags），
    按时间倒序；同一条只出一次，有降价或新码的按变更算。起价是一次性付款的不进来（O-04）。
    不足 ACTIVITY_FLOOR 条时用本季最新的优惠补满 limit 条，标为 earlier。"""
    usable = "d.published_at >= ? AND COALESCE(d.min_price_period, '') != 'onetime'"  # 本季、不是一次性付款
    found: dict[int, tuple[str, str | None, str]] = {}  # id → (kind, at, url)
    for r in conn.execute(f"SELECT d.id, d.url, d.published_at FROM deals d WHERE {usable} AND d.published_at >= ?",
                          (season_start, since)):
        found[r["id"]] = ("new", r["published_at"], r["url"])
    if flags:
        marks = ", ".join("?" * len(flags))
        for r in conn.execute(f"SELECT d.id, d.url, MAX(c.changed_at) AS at FROM changes c JOIN deals d ON d.id = c.deal_id"
                              f" WHERE {usable} AND c.changed_at >= ? AND d.id IN ({marks}) GROUP BY d.id",
                              [season_start, since, *flags]):
            kind = "price_drop" if "price_drop" in flags[r["id"]] else "new_code"
            found[r["id"]] = (kind, r["at"], r["url"])
    rows = sorted(found.items(), key=lambda x: (x[1][1] or "", x[0]), reverse=True)[:limit]
    if len(rows) < ACTIVITY_FLOOR:
        have = {i for i, _ in rows}
        for r in conn.execute(f"SELECT d.id, d.url, d.published_at FROM deals d WHERE {usable}"
                              " ORDER BY d.published_at DESC, d.id DESC LIMIT ?", (season_start, limit + len(have))):
            if len(rows) >= limit:
                break
            if r["id"] not in have:
                rows.append((r["id"], ("earlier", r["published_at"], r["url"])))
    return [Activity(kind, at, load_deal(conn, url)) for _, (kind, at, url) in rows]


def community(conn: sqlite3.Connection, season_start: str, limit: int = 10) -> list[sqlite3.Row]:
    """社区情报侧栏：默认只显示命中黑五词的本季情报（第 21 节风险应对）。"""
    return conn.execute("SELECT url, source, category, title, title_zh, title_en, published_at FROM intel"
                        " WHERE is_black_friday = 1"
                        " AND published_at >= ? ORDER BY published_at DESC LIMIT ?", (season_start, limit)).fetchall()


def last_update(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(finished_at) FROM run_sources WHERE error IS NULL AND source != 'llm'").fetchone()
    return row[0]


# ---------------------------------------------------------------- 套餐视图（FR-09）

PLAN_SORTS = ("price", "ram", "disk", "newest")
PERIODS = ("month", "quarter", "semi", "year", "biennial", "triennial", "onetime", "hour")
PLAN_PAGE_SIZE = 50
CPU_STEPS = (2, 4, 6, 8, 12, 16, 24, 32)  # 套餐视图下拉的下限；核数从 2 起：≥ 1 只会筛掉核数不明的套餐
RAM_STEPS = (0.5, 1, 2, 4, 8, 16, 32, 64)
DISK_STEPS = (10, 20, 40, 80, 160, 320, 500, 1000, 2000)
PLAN_ORDER = {
    "price": "p.price_cny_year IS NULL, p.price_cny_year, d.published_at DESC",
    "ram": "p.ram_gb IS NULL, p.ram_gb DESC, p.price_cny_year IS NULL, p.price_cny_year",
    "disk": "p.disk_gb IS NULL, p.disk_gb DESC, p.price_cny_year IS NULL, p.price_cny_year",
    "newest": "d.published_at DESC, d.id DESC, p.position",
}
LIMITS = {"cpu_min": 1024, "ram_min": 1024, "disk_min": 200000, "price_max": 1_000_000}


def _number(raw: str, cap: float) -> float | None:
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if 0 < value <= cap else None


@dataclass(frozen=True)
class PlanQuery:
    scope: str = "promo"  # 套餐视图用来跨帖比价，默认本季全部（D52）
    cpu_min: float | None = None  # 核数（D59）
    ram_min: float | None = None
    disk_min: float | None = None
    price_max: float | None = None  # ≈人民币/年
    products: tuple[str, ...] = ()  # 产品类型（O-13）；空为全部
    q: str = ""  # 商家名、别名、套餐名（O-13）
    region: str = ""  # 机房所在地区（O-12）
    location: str = ""
    period: str = ""
    nvme: bool = False
    no_nat: bool = False  # 不含 NAT 套餐（优化方案 O-04；D69 起不再有“无独立 IPv4”这一档，商家默认都给 IPv4）
    net: tuple[str, ...] = ()  # 线路标签（O-16，NET_TAGS 的键），多选：套餐带其中任何一个
    sort: str = "price"
    page: int = 1

    @classmethod
    def parse(cls, params) -> "PlanQuery":
        def first(name: str) -> str:
            got = params.getlist(name) if hasattr(params, "getlist") else params.get(name, [])
            got = got if isinstance(got, list) else [got]
            return got[0].strip() if got else ""

        scope, sort, period, area = first("scope"), first("sort"), first("period"), first("region")
        got = params.getlist("product") if hasattr(params, "getlist") else params.get("product", [])
        products = tuple(x for x in PRODUCTS if x in (got if isinstance(got, list) else [got]))
        nets = params.getlist("net") if hasattr(params, "getlist") else params.get("net", [])
        net = tuple(x for x in NET_TAGS if x in (nets if isinstance(nets, list) else [nets]))
        try:
            page = min(max(int(first("page") or 1), 1), MAX_PAGE)
        except ValueError:
            page = 1
        return cls(
            scope=scope if scope in SCOPES else "promo",
            cpu_min=_number(first("cpu_min"), LIMITS["cpu_min"]),
            ram_min=_number(first("ram_min"), LIMITS["ram_min"]),
            disk_min=_number(first("disk_min"), LIMITS["disk_min"]),
            price_max=_number(first("price_max") or first("price_max_cny_year"), LIMITS["price_max"]),  # 后者为 API 名
            products=() if len(products) == len(PRODUCTS) else products,  # 全勾等于不筛
            q=" ".join(first("q").split())[:Q_MAX],
            region=area if area in REGIONS else "",
            location=normalize_location(first("location")[:60]),
            period=period if period in PERIODS else "",
            nvme=first("nvme") == "1",
            no_nat=first("nonat") == "1",
            net=net,
            sort=sort if sort in PLAN_SORTS else "price",
            page=page,
        )

    def href(self, **changes) -> str:
        q = replace(self, **changes)
        params = [("scope", q.scope)] if q.scope != "promo" else []
        for name in ("cpu_min", "ram_min", "disk_min", "price_max"):
            if (value := getattr(q, name)) is not None:
                params.append((name, f"{value:g}"))
        params += [("product", x) for x in q.products]
        params += [("q", q.q)] if q.q else []
        params += [("region", q.region)] if q.region else []
        params += [("location", q.location)] if q.location else []
        params += [("period", q.period)] if q.period else []
        params += [("nvme", "1")] if q.nvme else []
        params += [("nonat", "1")] if q.no_nat else []
        params += [("net", x) for x in q.net]
        params += [("sort", q.sort)] if q.sort != "price" else []
        params += [("page", str(q.page))] if q.page > 1 else []
        return "/plans?" + urlencode(params) if params else "/plans"

    @property
    def filtered(self) -> bool:
        return any((self.cpu_min, self.ram_min, self.disk_min, self.price_max, self.products, self.q, self.region,
                    self.location, self.period, self.nvme, self.no_nat, self.net))


@dataclass
class PlanRow:
    plan: Plan
    deal_id: int
    deal_title: str  # 原标题；页面按语言用 View.pick_title 选译名
    deal_title_zh: str | None
    deal_title_en: str | None
    deal_url: str
    purchase_url: str | None
    vendor: str | None
    vendor_slug: str | None


@dataclass
class PlanPage:
    rows: list[PlanRow]
    total: int
    page: int
    pages: int


def _plan_scope(scope: str, season_start: str) -> tuple[list[str], list]:
    sql, params = [], []
    if scope in ("bf", "promo"):
        sql.append("d.published_at >= ?")
        params.append(season_start)
    if scope == "bf":
        sql.append("d.is_black_friday = 1")
    return sql, params


# 套餐视图上方的快捷筛选（O-19）：每个按钮就是一条现成的查询。"大盘"只限容量、不限产品类型：存储型 VPS 归 vps，
# 写 product=storage 只会剩下对象存储。
PLAN_PRESETS = (
    ("cheap", {"price_max": 100.0}),  # ≈人民币年价 100 元以内
    ("ram2", {"ram_min": 2.0, "price_max": 200.0}),
    ("bigdisk", {"disk_min": 1000.0}),
    ("dedicated", {"products": ("dedicated",)}),
    ("asia", {"region": "asia"}),
    ("cn2gia", {"net": ("cn2gia",)}),
)


def plan_presets(query: PlanQuery, net_tags: list[str]) -> list[dict]:
    """快捷按钮：（键, 地址, 当前是否就是这一条）。当前查询（不计排序与页码）与某个按钮一致时，按钮高亮，再点一下回到没有筛选；
    线路按钮只在当前范围里真有带这个标签的套餐时给。"""
    here = replace(query, sort="price", page=1)
    out = []
    for key, filters in PLAN_PRESETS:
        if "net" in filters and not set(filters["net"]) <= set(net_tags):
            continue
        target = PlanQuery(scope=query.scope, **filters)
        current = here == target
        out.append({"key": key, "href": (PlanQuery(scope=query.scope) if current else target).href(), "current": current})
    return out


def net_tags_in_scope(conn: sqlite3.Connection, query: PlanQuery, season_start: str) -> list[str]:
    """套餐视图"线路"勾选项：当前范围里有套餐带的标签，加上已经选了的（词表顺序）。"""
    sql, params = _plan_scope(query.scope, season_start)
    rows = conn.execute("SELECT DISTINCT j.value FROM plans p JOIN deals d ON d.id = p.deal_id, json_each(p.net_json) j"
                        f" WHERE {' AND '.join(sql) or '1 = 1'}", params)
    present = {r[0] for r in rows}
    return [t for t in NET_TAGS if t in present or t in query.net]


def list_plans(conn: sqlite3.Connection, query: PlanQuery, season_start: str,
               page_size: int = PLAN_PAGE_SIZE) -> PlanPage:
    sql, params = _plan_scope(query.scope, season_start)
    sql.append("p.price IS NOT NULL")
    for column, op, value in (("p.cpu_cores", ">=", query.cpu_min), ("p.ram_gb", ">=", query.ram_min),
                              ("p.disk_gb", ">=", query.disk_min), ("p.price_cny_year", "<=", query.price_max)):
        if value is not None:
            sql.append(f"{column} {op} ?")
            params.append(value)
    if query.products:
        sql.append(f"EXISTS (SELECT 1 FROM json_each(d.product_types_json) j"
                   f" WHERE j.value IN ({', '.join('?' * len(query.products))}))")
        params += query.products
    if query.q:
        fields = ("v.name", "v.aliases_json", "v.short_aliases_json", "p.name")
        sql.append("(" + " OR ".join(f"{f} LIKE ? ESCAPE '\\'" for f in fields) + ")")
        params += [_like(query.q)] * len(fields)
    if query.region:  # loc_region 见 db.py（app/locations.region）
        sql.append("EXISTS (SELECT 1 FROM json_each(p.locations_json) j WHERE loc_region(j.value) = ?)")
        params.append(query.region)
    if query.location:
        sql.append("EXISTS (SELECT 1 FROM json_each(p.locations_json) j WHERE loc_norm(j.value) = ?)")
        params.append(query.location)
    if query.period:
        sql.append("p.period = ?")
        params.append(query.period)
    if query.nvme:
        sql.append("p.disk_type = 'NVMe'")
    if query.no_nat:  # is_nat 见 db.py，与页面上的 NAT 标签同一口径
        sql.append("NOT is_nat(p.name, p.notes)")
    if query.net:
        sql.append(f"EXISTS (SELECT 1 FROM json_each(p.net_json) j WHERE j.value IN ({', '.join('?' * len(query.net))}))")
        params += query.net
    base = ("FROM plans p JOIN deals d ON d.id = p.deal_id LEFT JOIN vendors v ON v.id = d.vendor_id WHERE "
            + " AND ".join(sql))
    total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
    pages = max(1, math.ceil(total / page_size))
    page = min(query.page, pages)
    rows = conn.execute(
        f"SELECT p.*, d.id AS d_id, d.title AS d_title, d.title_zh AS d_title_zh, d.title_en AS d_title_en,"
        f" d.url AS d_url,"
        f" d.purchase_url AS d_purchase, v.name AS v_name, v.slug AS v_slug {base}"
        f" ORDER BY {PLAN_ORDER[query.sort]} LIMIT ? OFFSET ?", [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    out = [PlanRow(plan_from_row(r), r["d_id"], r["d_title"], r["d_title_zh"], r["d_title_en"], r["d_url"],
                   r["d_purchase"], r["v_name"], r["v_slug"]) for r in rows]
    return PlanPage(out, total, page, pages)


def location_options(conn: sqlite3.Connection, scope: str, season_start: str, region: str = "",
                     limit: int = 40) -> list[str]:
    """机房下拉：当前范围（选了地区时只看该地区）内套餐最多的机房，按规范名合并计数（O-12）。"""
    sql, params = _plan_scope(scope, season_start)
    where = " AND ".join(sql) or "1 = 1"
    counts: Counter = Counter()
    for value, n in conn.execute(f"SELECT j.value, COUNT(*) FROM plans p JOIN deals d ON d.id = p.deal_id,"
                                 f" json_each(p.locations_json) j WHERE {where} GROUP BY j.value", params):
        name = normalize_location(value)
        if not region or location_region(name) == region:
            counts[name] += n
    return [name for name, _ in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:limit]]


def location_stats(conn: sqlite3.Connection, season_start: str) -> tuple[int, int]:
    """状态页：本季套餐里机房写法的种数，与其中认不出的种数（照着补 app/locations.py）。"""
    values = [r[0] for r in conn.execute("SELECT DISTINCT j.value FROM plans p JOIN deals d ON d.id = p.deal_id,"
                                          " json_each(p.locations_json) j WHERE d.published_at >= ?", (season_start,))]
    return len(values), sum(location_region(v) is None for v in values)


# ---------------------------------------------------------------- 同一活动的多个帖子（优化方案 O-01）

SAME_DAYS = 21  # 发布时间相差不超过这么多天
SAME_PLANS = 0.9  # 套餐指纹集合的 Jaccard 相似度下限（两边各至少 2 个套餐）
SAME_TITLE = 0.85  # 两边都没有套餐时：规范化标题的相似度下限，且优惠码相同
_TITLE_NOISE = re.compile(r"[^0-9a-z一-鿿]+")


@dataclass(frozen=True)
class SameOffer:
    """与某条优惠是同一活动的另一个帖子（全部照常显示，只互相标注，D66）。"""

    id: int
    title: str
    title_zh: str | None
    title_en: str | None
    published_at: str | None


def _days_apart(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds()) / 86400


def same_offer(conn: sqlite3.Connection, deal_ids: list[int]) -> dict[int, list[SameOffer]]:
    """同一商家、发布相差 ≤ SAME_DAYS 天，且套餐几乎一样（或都没有套餐而标题与优惠码一样）的其他优惠。
    商家在 LET、LES、LEB 各发一帖是常事；不合并、不隐藏，只在卡片与详情页互相标注。读时现算，按页缓存。"""
    if not deal_ids:
        return {}
    marks = ", ".join("?" * len(deal_ids))
    rows = conn.execute(
        f"SELECT id, vendor_id, title, title_zh, title_en, published_at, coupon_codes_json FROM deals WHERE vendor_id IN"
        f" (SELECT vendor_id FROM deals WHERE id IN ({marks}) AND vendor_id IS NOT NULL)", deal_ids).fetchall()
    if not rows:
        return {}
    plans: dict[int, set] = defaultdict(set)
    all_marks = ", ".join("?" * len(rows))
    for p in conn.execute(f"SELECT deal_id, cpu_cores, ram_gb, disk_gb, price, currency, period FROM plans"
                          f" WHERE deal_id IN ({all_marks})", [r["id"] for r in rows]):
        plans[p["deal_id"]].add(tuple(p)[1:])
    by_vendor: dict[int, list] = defaultdict(list)
    for r in rows:
        by_vendor[r["vendor_id"]].append(r)
    by_id = {r["id"]: r for r in rows}

    def same(a, b) -> bool:
        if _days_apart(a["published_at"], b["published_at"]) > SAME_DAYS:
            return False
        pa, pb = plans.get(a["id"], set()), plans.get(b["id"], set())
        if len(pa) >= 2 and len(pb) >= 2:
            return len(pa & pb) / len(pa | pb) >= SAME_PLANS
        if pa or pb:
            return False
        ta, tb = (_TITLE_NOISE.sub(" ", x["title"].lower()).strip() for x in (a, b))
        return (json.loads(a["coupon_codes_json"]) == json.loads(b["coupon_codes_json"])
                and SequenceMatcher(None, ta, tb).ratio() >= SAME_TITLE)

    # 两两相连的归成一组（每两周重发一次的帖子，首尾两帖可能相隔超过 SAME_DAYS，经中间一帖连上）
    group = {r["id"]: r["id"] for r in rows}

    def root(x: int) -> int:
        while group[x] != x:
            group[x] = x = group[group[x]]
        return x

    for vendor_rows in by_vendor.values():
        for i, a in enumerate(vendor_rows):
            for b in vendor_rows[i + 1:]:
                if root(a["id"]) != root(b["id"]) and same(a, b):
                    group[root(a["id"])] = root(b["id"])
    members: dict[int, list] = defaultdict(list)
    for r in rows:
        members[root(r["id"])].append(r)
    out: dict[int, list[SameOffer]] = {}
    for deal_id in deal_ids:
        if deal_id not in by_id:
            continue
        found = [SameOffer(r["id"], r["title"], r["title_zh"], r["title_en"], r["published_at"])
                 for r in members[root(deal_id)] if r["id"] != deal_id]
        if found:
            out[deal_id] = sorted(found, key=lambda x: (x.published_at or "", x.id), reverse=True)  # 新的在前
    return out


# ---------------------------------------------------------------- 详情页与商家页

HEAT_DAYS = 7


def deal_by_id(conn: sqlite3.Connection, deal_id: int) -> Deal | None:
    if not 0 < deal_id < 2**63:  # 超出 SQLite 整数范围的编号当作不存在，不让它报错
        return None
    row = conn.execute("SELECT url FROM deals WHERE id = ?", (deal_id,)).fetchone()
    return load_deal(conn, row["url"]) if row else None


VENDOR_SORTS = ("deals", "name")
DIRECTORY_INTEL_DAYS = 30  # 本季没有优惠、但近这些天有讨论的商家也列
OVERVIEW_PLANS = 20  # 商家页"在售套餐"最多列这么多个


@dataclass(frozen=True)
class VendorRow:
    slug: str
    name: str
    aliases: list[str]
    deals: int  # 本季优惠数
    low_cny_year: float | None  # 本季优惠里最低的起价（人民币估算年价）
    latest: str | None  # 最近发帖时间（没有优惠时是最近一条讨论的时间）
    regions: list[str]  # 本季套餐的机房地区键，按套餐数从多到少


def vendor_directory(conn: sqlite3.Connection, season_start: str, intel_since: str, sort: str = "deals") -> list[VendorRow]:
    """商家目录（O-17）：本季有优惠的商家，加上近 DIRECTORY_INTEL_DAYS 天有讨论的；不列空壳（搜索引擎会判成低质页面）。
    默认按本季优惠数排（同数按最近发帖），也可按名称 A–Z。"""
    found: dict[int, sqlite3.Row] = {r["id"]: r for r in conn.execute(
        "SELECT v.id, v.slug, v.name, v.aliases_json, v.short_aliases_json, COUNT(d.id) AS deals,"
        " MIN(d.min_price_cny_year) AS low, MAX(d.published_at) AS latest FROM vendors v"
        " JOIN deals d ON d.vendor_id = v.id WHERE d.published_at >= ? GROUP BY v.id", (season_start,))}
    for r in conn.execute(
            "SELECT v.id, v.slug, v.name, v.aliases_json, v.short_aliases_json, 0 AS deals, NULL AS low,"
            " MAX(i.published_at) AS latest FROM vendors v JOIN intel_vendors iv ON iv.vendor_id = v.id"
            " JOIN intel i ON i.id = iv.intel_id WHERE i.published_at >= ? GROUP BY v.id", (intel_since,)):
        found.setdefault(r["id"], r)
    places: dict[int, Counter] = defaultdict(Counter)
    for vendor_id, raw in conn.execute(
            "SELECT d.vendor_id, p.locations_json FROM plans p JOIN deals d ON d.id = p.deal_id"
            " WHERE d.published_at >= ? AND d.vendor_id IS NOT NULL", (season_start,)):
        for loc in json.loads(raw):
            if area := location_region(loc):
                places[vendor_id][area] += 1
    rows = [VendorRow(r["slug"], r["name"], (json.loads(r["aliases_json"]) + json.loads(r["short_aliases_json"]))[:3],
                      r["deals"], r["low"], r["latest"], [a for a, _ in places[r["id"]].most_common(3)])
            for r in found.values()]
    if sort == "name":
        return sorted(rows, key=lambda x: x.name.casefold())
    return sorted(rows, key=lambda x: (-x.deals, _neg(x.latest), x.name.casefold()))


def _neg(text: str | None) -> tuple:
    """时间倒序的排序键：晚的在前，没有的放最后。"""
    return (1, ()) if not text else (0, tuple(-ord(c) for c in text))


@dataclass
class VendorOverview:
    plans: list[PlanRow]  # 本季在售套餐去重后最便宜的几个
    plan_total: int  # 去重后的套餐总数
    places: list[tuple[str, list[tuple[str, int]]]]  # （地区键, [（机房, 套餐数）…]），地区按套餐数从多到少
    net: list[str]  # 线路标签（O-16），按词表顺序


def vendor_overview(conn: sqlite3.Connection, vendor_id: int, season_start: str, ended_before: str,
                    limit: int = OVERVIEW_PLANS) -> VendorOverview:
    """商家页（O-17）：本季在售套餐——没结束的活动里的套餐，核数、内存、存储、价格、币种、周期都一样的只留最新帖里的那个，
    按人民币估算年价排序；机房分布（地区 → 城市，带套餐数）与线路标签按去重后的套餐统计。"""
    rows = conn.execute(
        "SELECT p.*, d.id AS d_id, d.title AS d_title, d.title_zh AS d_title_zh, d.title_en AS d_title_en,"
        " d.url AS d_url, d.purchase_url AS d_purchase, v.name AS v_name, v.slug AS v_slug FROM plans p"
        " JOIN deals d ON d.id = p.deal_id JOIN vendors v ON v.id = d.vendor_id"
        " WHERE d.vendor_id = ? AND d.published_at >= ? AND p.price IS NOT NULL"
        " AND (d.ends_at IS NULL OR d.ends_at >= ?) ORDER BY d.published_at DESC, d.id DESC, p.position",
        (vendor_id, season_start, ended_before)).fetchall()
    seen: set = set()
    unique: list[PlanRow] = []
    for r in rows:
        key = (r["cpu_cores"], r["ram_gb"], r["disk_gb"], r["price"], r["currency"], r["period"])
        if key not in seen:
            seen.add(key)
            unique.append(PlanRow(plan_from_row(r), r["d_id"], r["d_title"], r["d_title_zh"], r["d_title_en"],
                                  r["d_url"], r["d_purchase"], r["v_name"], r["v_slug"]))
    area_cities: dict[str, Counter] = defaultdict(Counter)
    tags: set[str] = set()
    for row in unique:
        tags.update(row.plan.net)
        for loc in row.plan.locations:
            if area := location_region(loc):
                area_cities[area][loc] += 1
    places = sorted(((a, c.most_common()) for a, c in area_cities.items()), key=lambda x: -sum(n for _, n in x[1]))
    unique.sort(key=lambda r: (r.plan.price_cny_year is None, r.plan.price_cny_year or 0))
    return VendorOverview(unique[:limit], len(unique), places, [t for t in NET_TAGS if t in tags])


CODES_LIMIT = 300


def deals_with_codes(conn: sqlite3.Connection, season_start: str, ended_before: str, limit: int = CODES_LIMIT) -> list[Deal]:
    """优惠码汇总页（O-18）：本季带优惠码的优惠，按发布时间倒序；已结束的（见 fmt.ended_cutoff）排到最后，照样列出。"""
    rows = conn.execute(
        "SELECT d.url FROM deals d WHERE d.published_at >= ? AND d.coupon_codes_json != '[]'"
        " ORDER BY (d.ends_at IS NOT NULL AND d.ends_at < ?), d.published_at DESC, d.id DESC LIMIT ?",
        (season_start, ended_before, limit)).fetchall()
    return [load_deal(conn, r["url"]) for r in rows]


def vendor_by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM vendors WHERE slug = ?", (slug,)).fetchone()


def vendor_deals(conn: sqlite3.Connection, vendor_id: int, *, exclude: int | None = None, season_start: str = "",
                 limit: int = 100) -> list[Deal]:
    rows = conn.execute("SELECT url FROM deals WHERE vendor_id = ? AND id != ? AND COALESCE(published_at, '') >= ?"
                        " ORDER BY published_at DESC, id DESC LIMIT ?",
                        (vendor_id, exclude or 0, season_start, limit)).fetchall()
    return [load_deal(conn, r["url"]) for r in rows]


def vendor_intel(conn: sqlite3.Connection, vendor_id: int, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT i.url, i.source, i.category, i.title, i.title_zh, i.title_en, i.published_at, iv.matched_alias FROM intel i"
        " JOIN intel_vendors iv ON iv.intel_id = i.id WHERE iv.vendor_id = ?"
        " ORDER BY i.published_at DESC LIMIT ?", (vendor_id, limit)).fetchall()


def vendor_website(conn: sqlite3.Connection, vendor: sqlite3.Row) -> str | None:
    """官网：别名表里填了就用；否则取该商家优惠的购买链接里最常见的主机名。"""
    if vendor["website"]:
        return vendor["website"]
    hosts = Counter(urlsplit(r[0]).hostname for r in conn.execute(
        "SELECT purchase_url FROM deals WHERE vendor_id = ? AND purchase_url IS NOT NULL", (vendor["id"],)))
    hosts.pop(None, None)
    return f"https://{hosts.most_common(1)[0][0]}" if hosts else None


def vendor_badges(conn: sqlite3.Connection, vendor_ids: set[int], since: str) -> dict[int, tuple[str, int]]:
    """首页用：商家 slug 与最近 HEAT_DAYS 天被情报提到的次数（热度，FR-12）。"""
    if not vendor_ids:
        return {}
    marks = ", ".join("?" * len(vendor_ids))
    heat = dict(conn.execute(
        f"SELECT iv.vendor_id, COUNT(*) FROM intel_vendors iv JOIN intel i ON i.id = iv.intel_id"
        f" WHERE iv.vendor_id IN ({marks}) AND i.published_at >= ? GROUP BY iv.vendor_id", [*vendor_ids, since]))
    return {r["id"]: (r["slug"], heat.get(r["id"], 0))
            for r in conn.execute(f"SELECT id, slug FROM vendors WHERE id IN ({marks})", list(vendor_ids))}


# ---------------------------------------------------------------- 采集状态页（FR-13）与运行记录

STATUS_RUNS = 10
CREDENTIALS = re.compile(r"(\w+://)[^/@\s]+@")


def public_error(text: str | None, limit: int = 200) -> str | None:
    """公开页面与接口上的错误信息：去掉地址里的用户名与密码（代理地址可能带上），截到 limit 字。"""
    if not text:
        return None
    text = CREDENTIALS.sub(r"\1***@", text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def public_extra(extra: dict) -> dict:
    """运行记录的附加统计去掉逐条错误（LLM 的错误可能带服务商地址），只留计数。"""
    return {k: v for k, v in extra.items() if k != "errors"}


@dataclass
class RunInfo:
    source: str
    started_at: str
    finished_at: str | None
    seconds: float | None
    fetched: int
    new: int
    updated: int
    llm_calls: int
    llm_rejected: int
    error: str | None
    extra: dict


@dataclass
class SourceRuns:
    source: str
    runs: list[RunInfo]  # 新的在前
    last_ok_at: str | None
    failing: int  # 从最近一轮往前连续失败的轮数


def _seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())


def run_info(r: sqlite3.Row) -> RunInfo:
    return RunInfo(r["source"], r["started_at"], r["finished_at"], _seconds(r["started_at"], r["finished_at"]), r["fetched"],
                   r["new_items"], r["updated"], r["llm_calls"], r["llm_rejected"], public_error(r["error"]),
                   public_extra(json.loads(r["extra_json"])))


def source_runs(conn: sqlite3.Connection, sources: list[str], per_source: int = STATUS_RUNS) -> list[SourceRuns]:
    """各源最近 per_source 轮，以及最近一次成功的时间与连续失败轮数（状态页、/healthz）。"""
    latest = latest_runs(conn, per_source=per_source)
    out = []
    for source in sources:
        rows = latest.get(source, [])
        ok = conn.execute("SELECT finished_at FROM run_sources WHERE source = ? AND error IS NULL"
                          " ORDER BY started_at DESC LIMIT 1", (source,)).fetchone()
        failing = next((i for i, r in enumerate(rows) if r["error"] is None), len(rows))
        out.append(SourceRuns(source, [run_info(r) for r in rows], ok[0] if ok else None, failing))
    return out


@dataclass
class LlmSummary:
    runs: int = 0
    calls: int = 0
    done: int = 0
    failed: int = 0
    units: int = 0  # 校验单元
    rejected: int = 0  # 被拒单元
    tokens: int = 0
    pending: int = 0  # 队列里等着调用的优惠

    @property
    def reject_rate(self) -> float | None:
        return self.rejected / self.units if self.units else None


def llm_summary(conn: sqlite3.Connection, since: str) -> LlmSummary:
    out = LlmSummary(pending=len(llm_candidates(conn)))
    for r in conn.execute("SELECT * FROM run_sources WHERE source = ? AND started_at >= ?", (LLM_SOURCE, since)):
        extra = json.loads(r["extra_json"])
        out.runs += 1
        out.calls += r["llm_calls"]
        out.done += r["updated"]
        out.rejected += r["llm_rejected"]
        out.failed += extra.get("failed") or 0
        out.units += extra.get("units") or 0
        out.tokens += (extra.get("tokens") or {}).get("total_tokens") or 0
    return out


@dataclass
class Coverage:
    ratio: float | None  # 看到的新帖 / 应有的新帖
    gap: int  # 缺的帖子数
    runs: int
    unlinked: int  # 窗口没接上上一轮（两轮之间整段漏抓）的轮数


def nodeseek_coverage(conn: sqlite3.Connection, since: str) -> Coverage:
    """NodeSeek ID 缺口覆盖率（D33）：since 之后成功的各轮合计。"""
    seen = expected = gap = runs = unlinked = 0
    for (extra_json,) in conn.execute("SELECT extra_json FROM run_sources WHERE source = 'nodeseek'"
                                      " AND error IS NULL AND started_at >= ?", (since,)):
        extra = json.loads(extra_json)
        runs += 1
        unlinked += extra.get("overlap") is False
        if isinstance(extra.get("expected"), int) and extra["expected"] > 0:
            seen += extra.get("new_ids") or 0
            expected += extra["expected"]
            gap += extra.get("gap") or 0
    return Coverage(round(seen / expected, 4) if expected else None, gap, runs, unlinked)


def recent_runs(conn: sqlite3.Connection, limit: int) -> list[tuple[sqlite3.Row, list[RunInfo]]]:
    """最近 limit 次运行（新的在前）与其中各源的统计（/api/runs）。"""
    runs = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    if not runs:
        return []
    marks = ", ".join("?" * len(runs))
    by_run: dict[int, list[RunInfo]] = {}
    for r in conn.execute(f"SELECT * FROM run_sources WHERE run_id IN ({marks}) ORDER BY run_id, source",
                          [r["id"] for r in runs]):
        by_run.setdefault(r["run_id"], []).append(run_info(r))
    return [(r, by_run.get(r["id"], [])) for r in runs]


def vendor_slugs(conn: sqlite3.Connection, vendor_ids: set[int]) -> dict[int, str]:
    if not vendor_ids:
        return {}
    marks = ", ".join("?" * len(vendor_ids))
    return dict(conn.execute(f"SELECT id, slug FROM vendors WHERE id IN ({marks})", list(vendor_ids)))
