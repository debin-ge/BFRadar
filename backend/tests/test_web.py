"""后端的 Web 层（M2、M3）：范围与筛选的语义、参数白名单、给前端的各页数据（首页、套餐视图、详情、商家、状态、关于，
按语言格式化好）、空状态、字典完整；后端不出页面；RSS、JSON API、SEO、sitemap、分享图、手动刷新鉴权、内存缓存。
页面本身（React）的检查在 test_e2e_web.py 与 test_browser.py。"""

import io
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import replace
from email.utils import parsedate_to_datetime
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import feedparser
import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import main
from app.config import SOURCE_IDS, load_settings
from app.db import Database, connect
from app.http import HttpClient
from app.i18n import strings
from app.models import DiskType, EventType, Period, ProductType, Recurring
from app.pipeline import crawl
from app.sources import build_sources
from app.web import fmt, internal, og, seo
from app.web import view as web_view
from app.web.app import CSP, create_app
from app.web.queries import (MAX_PAGE, OFFER_SOURCES, PERIODS, PLAN_PAGE_SIZE, PLAN_PREVIEW, PLAN_SORTS, PRODUCTS,
                             DealQuery, PlanQuery, list_deals, list_plans, same_offer)
from app.web.view import View

from conftest import CJK, FixtureServer, data_cjk, noop_sleep

SEASON = "2026-10-01T00:00:00Z"
TODAY = "2026-11-03"  # 固定"今天"：夹具里的优惠有的写了截止日期，不固定的话测试会随日子变（O-15）
VM6 = "https://lowendtalk.com/discussion/222765"
CHANGE_FIELDS = ("title", "discount_pct", "coupon_codes", "plan_price")  # merge.diff 会产出的字段
BACKEND = Path(__file__).resolve().parents[1] / "app"  # 后端源码（扫 t("键")）
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "app"  # React 前端源码（D60）


@contextmanager
def copied_site(site, tmp_path, *, scheduler=None, **env):
    """模块共用库的副本上另开一个应用（各有各的内存缓存）：要改数据或换配置的测试用，不影响其他测试。"""
    _, conn, _ = site
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "copy.db"
    dest = sqlite3.connect(path)
    conn.backup(dest)
    dest.close()
    settings = load_settings({"DB_PATH": str(path), "SOURCES": "lowendtalk,nodeseek,reddit", "TODAY": TODAY, **env}, env_file=None)
    with TestClient(create_app(settings, scheduler=scheduler)) as client:
        yield path, client


def write(path, sql: str, params=()) -> None:
    db = sqlite3.connect(path)
    db.execute(sql, params)
    db.commit()
    db.close()


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    path = tmp_path_factory.mktemp("web") / "radar.db"
    settings = load_settings({"DB_PATH": str(path), "SOURCES": "lowendtalk,nodeseek,reddit", "TODAY": TODAY}, env_file=None)
    db = Database(path)
    db.migrate()
    with HttpClient(settings, transport=httpx.MockTransport(FixtureServer().handler), sleep=noop_sleep) as http:
        crawl(db, build_sources(settings, http, sleep=noop_sleep), reason="test")
    with TestClient(create_app(settings)) as client:
        yield settings, db.conn(), client


def deal_id(conn, url: str) -> int:
    return conn.execute("SELECT id FROM deals WHERE url = ?", (url,)).fetchone()[0]


# ---------------------------------------------------------------- 查询语义


def test_scopes_nest_and_bf_means_season_black_friday(site):
    _, conn, _ = site
    bf, promo, every = (list_deals(conn, DealQuery(scope=s), SEASON) for s in ("bf", "promo", "all"))
    assert 0 < bf.total < promo.total < every.total == 100
    assert all(d.is_black_friday and d.published_at >= SEASON for d in bf.deals)
    assert all(d.published_at >= SEASON for d in promo.deals)


def test_filters_search_sort_and_paging(site):
    _, conn, _ = site
    q = DealQuery(scope="all", products=("dedicated",))
    page = list_deals(conn, q, SEASON)
    assert page.total and all(ProductType.DEDICATED in d.product_types for d in page.deals)

    for text in ("VM6", "bfvps55", "UK VPS"):  # 商家、优惠码（不分大小写）、标题
        found = list_deals(conn, DealQuery(scope="all", q=text), SEASON).deals
        assert "https://lowendtalk.com/discussion/222765" in {d.url for d in found}, text
    assert list_deals(conn, DealQuery(scope="all", q="100%_"), SEASON).total == 0  # 通配符按字面
    assert list_deals(conn, DealQuery(scope="all", sources=("lowendbox",)), SEASON).total == 0

    cheap = list_deals(conn, DealQuery(scope="all", sort="price"), SEASON).deals
    prices = [d.min_price_cny_year for d in cheap]
    known = [p for p in prices if p is not None]
    assert known == sorted(known) and prices[:len(known)] == known  # 没有年价的排在最后

    last = list_deals(conn, DealQuery(scope="all", page=MAX_PAGE), SEASON)
    assert last.page == last.pages == 4 and len(last.deals) == 10  # 页码越界收回末页


def test_query_parsing_whitelist_and_links():
    q = DealQuery.parse({"scope": ["oops"], "source": ["lowendtalk", "evil"], "product": list(PRODUCTS),
                         "page": ["abc"], "q": ["  VM6   bf  " + "x" * 200], "sort": ["drop table"]})
    assert (q.scope, q.sources, q.products, q.page, q.sort) == ("bf", ("lowendtalk",), (), 1, "newest")
    assert q.q.startswith("VM6 bf x") and len(q.q) == 100
    assert DealQuery.parse({"source": list(OFFER_SOURCES)}).sources == ()  # 全勾等于不筛
    assert PRODUCTS == ("vps", "dedicated", "hosting", "storage")  # D62：只收这四类
    assert DealQuery.parse({"product": ["mail", "vps"]}).products == ("vps",)  # 旧链接里已不收的产品类型忽略
    assert DealQuery.parse({"product": ["mail"]}).products == () and DealQuery.parse({"in_stock": ["1"]}) == DealQuery()
    assert DealQuery.parse({"page": ["99999"]}).page == MAX_PAGE
    assert DealQuery().href() == "/"

    q = DealQuery(scope="all", sources=("lowendtalk", "lowendspirit"), products=("vps",), q="BF 2026", sort="price",
                  page=3)
    assert DealQuery.parse(parse_qs(urlsplit(q.href()).query)) == q  # 链接可复制分享


# ---------------------------------------------------------------- 页面


def test_home_data(site):
    """首页数据：标题、刚刚上架 8 张价签、当前范围、条数与分页、卡片里只展开前几个套餐；倒计时与更新时间在页头页脚数据里。"""
    _, _, client = site
    home = client.get("/internal/v1/zh/home").json()
    assert home["seo"]["title"] == "2026 黑五 VPS 优惠 · 黑五雷达"
    assert len(home["just"]) == 8 and all(re.fullmatch(r"/deal/\d+", x["href"]) for x in home["just"])
    assert [s["key"] for s in home["scopes"] if s["current"]] == ["bf"]
    total = int(re.search(r"共 (\d+) 条", home["total"]).group(1))
    assert len(home["deals"]) == min(total, internal.CARD_PAGE_SIZE)
    chrome = client.get("/internal/v1/zh/chrome").json()
    assert re.search(r"距黑五还有 \d+ 天 \d+ 小时|黑五 / 网一进行中|今年黑五已结束", chrome["countdown"]["text"])
    assert chrome["freshness"].startswith("上次更新") and "汇率为内置参考值估算" in chrome["footer"]

    page = client.get("/internal/v1/zh/home?scope=all").json()
    assert len(page["deals"]) == internal.CARD_PAGE_SIZE and page["pager"]["text"] == "第 1 / 5 页"
    assert page["pager"]["next"] == "/?scope=all&page=2"
    many = [d for d in page["deals"] if d["plans_more"]]  # 套餐多的卡片只展开最便宜的几个，其余看详情页
    assert many and all(len(d["plans"]) == internal.CARD_PLANS and f"最便宜的 {internal.CARD_PLANS} 个" in d["plans_more"]
                        for d in many)


@pytest.mark.parametrize("path", ["/", "/en/", "/plans", "/deal/10", "/status", "/about", "/static/style.css",
                                  "/favicon.ico", "/nope"])
def test_backend_serves_no_pages(site, path):
    """页面全由前端渲染（D60）：后端对页面地址一律给 JSON 404，带兜底的安全头。"""
    _, _, client = site
    r = client.get(path)
    assert (r.status_code, r.json()["error"]) == (404, "not_found"), path
    assert (r.headers["content-security-policy"], r.headers["x-frame-options"],
            r.headers["x-content-type-options"]) == (CSP, "DENY", "nosniff"), path


def test_home_ignores_the_source_param(site):
    """D59：页面不按来源筛选，旧链接里的 source 参数不起作用，规范地址与缓存键都不带它；/api/deals 仍认。"""
    settings, _, client = site
    old = client.get("/internal/v1/zh/home?scope=all&source=lowendbox").json()  # 库里没有 LEB 的帖子，按来源筛会是 0 条
    assert old["total"] == "共 100 条" and len(old["deals"]) == internal.CARD_PAGE_SIZE and old["form"]["reset"] is None
    assert old["seo"]["canonical"] == f"{settings.site_url}/?scope=all"
    assert client.get("/internal/v1/zh/home?scope=all").json() == old
    assert client.get("/api/deals?scope=all&source=lowendbox").json()["total"] == 0


def test_empty_states(tmp_path):
    settings = load_settings({"DB_PATH": str(tmp_path / "empty.db"), "SOURCES": "lowendtalk"}, env_file=None)
    with TestClient(create_app(settings)) as client:
        home = client.get("/internal/v1/zh/home").json()
        assert "还没有抓到黑五活动，下一轮采集后自动更新" in home["empty"] and home["just"] == home["intel"] == []
        assert "还没有采集过" in client.get("/internal/v1/zh/chrome").json()["freshness"]
        assert "没有符合条件的优惠" in client.get("/internal/v1/zh/home?q=nothing").json()["empty"]


def test_serve_is_the_default_command(monkeypatch):
    called = []
    monkeypatch.setattr(main, "cmd_serve", lambda settings, args: called.append(args.command) or 0)
    monkeypatch.setattr(main, "load_settings", lambda: load_settings({}, env_file=None))
    assert main.main([]) == 0 and called == [None]


# ---------------------------------------------------------------- 格式与字典


@pytest.mark.parametrize("value, expected", [(0.5, "512 MB"), (2, "2 GB"), (1.5, "1.5 GB"), (1000, "1 TB"),
                                             (2500, "2.5 TB"), (None, "")])
def test_size(value, expected):
    assert fmt.size_gb(value) == expected


def test_speed_span_and_time():
    assert (fmt.speed(0.1), fmt.speed(10), fmt.span(1, 1), fmt.span(1, 12)) == ("100 Mbps", "10 Gbps", "1", "1–12")
    now = datetime(2026, 11, 20, 4, tzinfo=UTC)
    assert fmt.ago("2026-11-20T03:59:30Z", now) == ("now", 0)
    assert fmt.ago("2026-11-20T01:00:00Z", now) == ("hours", 3)
    assert fmt.clock("2026-11-20T03:00:00Z", "Asia/Shanghai", now) == "11:00"
    assert fmt.clock("2026-11-18T03:00:00Z", "Asia/Shanghai", now) == "11-18 11:00"
    assert fmt.countdown(now) == ("before", 7, 1)  # 美东 11-27 零点 = UTC 05:00
    assert fmt.countdown(fmt.BLACK_FRIDAY + timedelta(hours=1))[0] == "during"
    assert fmt.countdown(fmt.CYBER_MONDAY_END)[0] == "after"
    assert fmt.remaining(now) == ("before", 7, 1, 0, 0)  # 首页大号倒计时：天、时、分、秒
    assert fmt.remaining(fmt.BLACK_FRIDAY - timedelta(seconds=1)) == ("before", 0, 0, 0, 1)
    assert fmt.remaining(fmt.BLACK_FRIDAY + timedelta(hours=1, seconds=5)) == ("during", 3, 22, 59, 55)  # 数到网一结束
    assert fmt.remaining(fmt.CYBER_MONDAY_END) == ("after", 0, 0, 0, 0)


def frontend_sources() -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8") for p in sorted(FRONTEND.rglob("*")) if p.suffix in (".ts", ".tsx")}


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_dictionary_covers_code_and_enums(lang):
    """后端代码的 t("键")（页面数据、RSS、分享图）与 React 前端的 t("键")、t(`前缀.${值}`) 用到的词条都在字典里；
    拼出来的键按前缀与枚举逐个查。"""
    table = strings(lang)
    backend, frontend, prefixes = set(), set(), set()
    for path in sorted(BACKEND.rglob("*.py")):
        source = path.read_text(encoding="utf-8")  # t("x")、t('x') 与 view.t("x")
        backend |= {key for _, key in re.findall(r"""(?<!\w)t\((["'])([\w.]+)\1""", source)}
    for source in frontend_sources().values():
        frontend |= set(re.findall(r'(?<![\w.])t\("([\w.]+)"', source))
        prefixes |= set(re.findall(r"(?<![\w.])t\(`([\w.]+\.)\$\{", source))
    assert len(backend) > 80 and len(frontend) > 50  # 扫描本身没失灵：写法变了、正则没跟上时会扫到空集而照样通过
    prefixes |= {k for k in backend if k.endswith(".")}  # t("event." + 值) 这类拼出来的键
    missing = [k for k in backend | frontend if not k.endswith(".") and k not in table]
    enums = (("event.", EventType), ("product.", ProductType), ("recurring.", Recurring),
             ("scope.", ("bf", "promo", "all")),
             ("sort.", ("newest", "price", "discount", "changed")),
             ("flag.", ("price_drop", "new_code")),
             ("source.", (*SOURCE_IDS, "llm")),
             ("plans.sort.", PLAN_SORTS), ("period.", PERIODS), ("change.", CHANGE_FIELDS),
             ("tier.", ("normal", "bf")), ("lang.", ("zh", "en")),
             ("countdown.title.", ("before", "during", "after")), ("countdown.when.", ("before", "during")),
             ("countdown.unit.", ("days", "hours", "minutes", "seconds")))
    assert prefixes and prefixes <= {prefix for prefix, _ in enums}  # 拼出来的键都有枚举可查
    for prefix, values in enums:
        missing += [prefix + str(v) for v in values if prefix + str(v) not in table]
    assert missing == []


# ---------------------------------------------------------------- 套餐视图（M2.4）


def all_plans(conn, query: PlanQuery) -> list:
    first = list_plans(conn, query, SEASON)
    out = list(first.rows)
    for n in range(2, first.pages + 1):
        out += list_plans(conn, replace(query, page=n), SEASON).rows
    assert len(out) == first.total
    return out


def test_plan_query_parsing_whitelist_and_links():
    q = PlanQuery.parse({"scope": ["oops"], "cpu_min": ["0"], "ram_min": ["abc"], "disk_min": ["-5"],
                         "price_max": ["1e9"], "period": ["weekly"], "sort": ["drop table"], "nvme": ["yes"],
                         "page": ["0"], "location": ["  " + "x" * 100]})
    assert q == PlanQuery(location="x" * 60)  # 不认识的值一律回到默认
    assert PlanQuery().href() == "/plans" and not PlanQuery().filtered
    q = PlanQuery(scope="all", cpu_min=4, ram_min=0.5, disk_min=40, price_max=100, location="Los Angeles",
                  period="year", nvme=True, sort="ram", page=2)
    assert q.href().startswith("/plans?scope=all&cpu_min=4&ram_min=0.5&disk_min=40&price_max=100&location=Los+Angeles&")
    assert PlanQuery(cpu_min=2).filtered and PlanQuery.parse({"cpu_min": ["2000"]}).cpu_min is None
    assert PlanQuery.parse(parse_qs(urlsplit(q.href()).query)) == q  # 链接可复制分享


def test_plan_filters_and_sorts(site):
    _, conn, _ = site
    every = all_plans(conn, PlanQuery(scope="all"))
    assert len(every) == conn.execute("SELECT COUNT(*) FROM plans WHERE price IS NOT NULL").fetchone()[0]
    prices = [r.plan.price_cny_year for r in every]
    known = [p for p in prices if p is not None]
    assert known == sorted(known) and prices[:len(known)] == known  # 默认按人民币年价升序，算不出的排最后

    assert PlanQuery.parse({"include_sold_out": ["1"]}) == PlanQuery()  # 库存不再收集（D63），旧参数忽略

    cores = all_plans(conn, PlanQuery(scope="all", cpu_min=4))  # 核数不明的套餐不算（D59）
    assert cores and all(r.plan.cpu_cores >= 4 for r in cores)
    assert len(cores) == sum((r.plan.cpu_cores or 0) >= 4 for r in every) < len(every)
    big = all_plans(conn, PlanQuery(scope="all", ram_min=2, sort="ram"))
    assert big and all(r.plan.ram_gb >= 2 for r in big)
    assert [r.plan.ram_gb for r in big] == sorted((r.plan.ram_gb for r in big), reverse=True)
    fast = all_plans(conn, PlanQuery(scope="all", disk_min=40, nvme=True))
    assert fast and all(r.plan.disk_gb >= 40 and r.plan.disk_type == DiskType("NVMe") for r in fast)
    cheap = all_plans(conn, PlanQuery(scope="all", price_max=100, period="year"))
    assert cheap and all(r.plan.price_cny_year <= 100 and r.plan.period == Period("year") for r in cheap)
    fra = all_plans(conn, PlanQuery(scope="all", location="Frankfurt"))
    assert fra and all("Frankfurt" in r.plan.locations for r in fra)

    bf, promo, total = (list_plans(conn, PlanQuery(scope=s), SEASON).total for s in ("bf", "promo", "all"))
    assert 0 < bf < promo < total == len(every)
    last = list_plans(conn, PlanQuery(scope="all", page=MAX_PAGE), SEASON)
    assert last.page == last.pages and len(last.rows) == total - (last.pages - 1) * PLAN_PAGE_SIZE


def test_plans_data(site):
    settings, conn, client = site
    data = client.get("/internal/v1/zh/plans").json()
    assert data["seo"]["title"] == "2026 黑五 VPS 套餐比价 · 黑五雷达"
    total = list_plans(conn, PlanQuery(), fmt.season_start_iso(settings.season_start)).total
    assert data["total"] == f"共 {total} 个套餐" and len(data["rows"]) == min(total, PLAN_PAGE_SIZE)
    assert any((r["vendor_href"] or "").startswith("/vendor/") for r in data["rows"])
    assert client.get("/internal/v1/zh/plans?scope=all&page=2").json()["pager"]["prev"] == "/plans?scope=all"
    form = client.get("/internal/v1/zh/plans?location=Frankfurt&nvme=1").json()["form"]
    assert {"value": "Frankfurt", "label": "法兰克福", "selected": True} in form["location"] and form["reset"]
    form = client.get("/internal/v1/zh/plans?cpu_min=4").json()["form"]
    assert {"value": "4", "label": "4 核", "selected": True} in form["cpu"] and form["reset"] == "/plans"


# ---------------------------------------------------------------- 优惠详情页、商家页（M2.5、M2.6）


def test_deal_data(site):
    _, conn, client = site
    vm6 = deal_id(conn, VM6)
    d = client.get(f"/internal/v1/zh/deal/{vm6}").json()
    assert d["seo"]["title"].startswith("BLACK FRIDAY | UK VPS") and d["url"] == VM6
    assert d["purchase_url"] == "https://www.vm6.co.uk/promotions"
    assert (d["vendor"], d["vendor_href"]) == ("VM6", "/vendor/vm6")
    assert len(d["plans"]) == 9 and all(p["order"] for p in d["plans"])  # 详情页列全部套餐，每个都能去下单
    other = conn.execute("SELECT id FROM deals WHERE vendor_id = (SELECT vendor_id FROM deals WHERE id = ?)"
                         " AND id != ?", (vm6, vm6)).fetchone()[0]
    assert d["others_heading"] == "VM6 的其他活动" and f"/deal/{other}" in [o["href"] for o in d["others"]]


def test_vendor_data(site):
    _, _, client = site
    vendor = client.get("/internal/v1/zh/vendor/vm6").json()
    assert vendor["seo"]["title"] == "VM6 的 VPS 优惠 · 黑五雷达" and vendor["lowest"]["price"]
    assert client.get("/internal/v1/zh/vendor/mailbig").json()["website"] == "https://mailbig.net"  # 表里没有就取购买链接最常见的域名
    bwh = client.get("/internal/v1/zh/vendor/bandwagonhost").json()
    assert bwh["deals"] == [] and bwh["aka"].startswith("也叫：")
    assert any("[六折出]搬瓦工正价机美西ECOMMERCE" in i["title"] for i in bwh["intel"])  # 情报按别名表匹配到商家（D52）


def test_home_links_vendor_pages_with_heat(site, tmp_path, monkeypatch):
    _, conn, _ = site
    monkeypatch.setattr(web_view, "utcnow", lambda: datetime(2026, 11, 3, tzinfo=UTC))  # 情报在最近 7 天里
    vm6 = deal_id(conn, VM6)
    with copied_site(site, tmp_path) as (path, client):
        write(path, "INSERT INTO intel_vendors (intel_id, vendor_id, matched_alias) SELECT i.id, v.id, 'VM6'"
                    " FROM intel i, vendors v WHERE i.title LIKE '%搬瓦工%' AND v.slug = 'vm6'")  # 启动同步之后再加
        card = next(c for c in client.get("/internal/v1/zh/home?scope=all&q=VM6").json()["deals"] if c["id"] == vm6)
        deal = client.get(f"/internal/v1/zh/deal/{vm6}").json()
    for data in (card, deal):
        assert (data["vendor_href"], data["heat"], data["heat_title"]) == ("/vendor/vm6", "7 天讨论 1", "最近 7 天社区讨论 1 次")


# ---------------------------------------------------------------- 显示格式


def test_change_and_location_labels():
    v = View("zh", "Asia/Shanghai", datetime(2026, 11, 20, tzinfo=UTC))
    assert v.change("discount_pct", None, "40") == ("折扣", "无", "40%")
    assert v.change("coupon_codes", '["BF1"]', '["BF1", "BF2"]') == ("优惠码", "BF1", "BF1、BF2")
    old = {"name": None, "ram_gb": 2, "disk_gb": 40, "price": 20, "currency": "USD", "period": "year"}
    assert v.change("plan_price", json.dumps(old), json.dumps({**old, "price": 15})) == ("2 GB 40 GB 价格", "$20/年",
                                                                                         "$15/年")
    named = {**old, "name": "KVM-2G", "price": 15, "currency": "EUR", "period": "month"}
    assert v.change("plan_price", json.dumps(old), json.dumps(named)) == ("KVM-2G 价格", "$20/年", "€15/月")
    assert v.locations(["Los Angeles", "Gotham", "Frankfurt"]) == "洛杉矶、Gotham、法兰克福"  # 认不出的照原文
    assert v.locations(["Frankfurt, Germany", "NYC", "Frankfurt"]) == "法兰克福、纽约"  # 先规范化、去重（O-12）
    assert v.locations(["Dallas", "Los Angeles", "Miami", "Tokyo"], limit=2) == "达拉斯、洛杉矶 +2"


# ---------------------------------------------------------------- 状态页、关于页（M2.7、M2.8）


class FakeScheduler:
    """应用只用到调度器的这几个方法；refresh 第一次给运行编号，之后算"已有一轮在跑"。"""

    def __init__(self):
        self.listeners, self.started = [], False

    def start(self):
        pass

    def shutdown(self, wait=True):
        pass

    def status(self):
        return {"tier": "normal", "intervals": {"lowendtalk": 60, "nodeseek": 5, "reddit": 60},
                "jobs": {"source:lowendtalk": "2026-11-20T05:00:00Z"}, "refreshing": self.started,
                "llm_running": False}

    def add_listener(self, fn):
        self.listeners.append(fn)

    def refresh(self):
        from app.scheduler import RefreshBusy

        if self.started:
            raise RefreshBusy("busy")
        self.started = True
        return 7


def test_status_data(site):
    _, conn, client = site
    r = client.get("/internal/v1/zh/status")
    data = r.json()
    assert r.headers["cache-control"] == "no-store" and data["seo"]["title"] == "采集状态 · 黑五雷达"
    facts = {f["label"]: f["value"] for f in data["facts"]}
    total = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
    assert facts["调度器"] == "本进程没有运行调度器，数据由其他进程更新" and data["refreshing"] is False
    assert facts["LLM 抽取"] == "未配置，只用规则抽取" and facts["优惠"].startswith(f"共 {total} 条")
    assert facts["汇率"].startswith("库里还没有汇率") and "NodeSeek 覆盖率" in facts
    assert [s["name"] for s in data["sources"]] == ["LET", "NodeSeek", "Reddit"]  # 没配 LLM，不列 LLM 队列
    assert all([(row["bad"], row["result"]) for row in s["rows"]] == [(False, "✓")] for s in data["sources"])  # 每源一轮
    assert data["sources"][1]["columns"][-2:] == ["覆盖率", "结果"]


def test_status_shows_failures_without_secrets(site, tmp_path):
    with copied_site(site, tmp_path, LLM_BASE_URL="https://llm.example.com/v1", LLM_API_KEY="k",
                     LLM_MODEL="m") as (path, client):
        for n in range(2):
            write(path, "INSERT INTO runs (started_at, reason) VALUES (?, 'scheduled')", (f"2099-01-0{n + 1}T00:00:00Z",))
            write(path, "INSERT INTO run_sources (run_id, source, started_at, finished_at, error) VALUES"
                        " ((SELECT MAX(id) FROM runs), 'lowendtalk', ?, ?, ?)",
                  (f"2099-01-0{n + 1}T00:00:00Z", f"2099-01-0{n + 1}T00:00:05Z",
                   "ProxyError: http://user:hunter2@proxy.example:8080 refused"))
        write(path, "INSERT INTO runs (started_at, reason) VALUES ('2099-01-03T00:00:00Z', 'llm')")
        write(path, "INSERT INTO run_sources (run_id, source, started_at, finished_at, llm_calls, llm_rejected,"
                    " updated, extra_json) VALUES ((SELECT MAX(id) FROM runs), 'llm', '2099-01-03T00:00:00Z',"
                    " '2099-01-03T00:01:00Z', 4, 3, 4, ?)",
              (json.dumps({"failed": 0, "units": 60, "tokens": {"total_tokens": 31000},
                           "errors": ["https://llm.example.com/v1: HTTP 400"]}),))
        data = client.get("/internal/v1/zh/status").json()
        api = client.get("/api/runs?limit=3").json()
    text = json.dumps(data, ensure_ascii=False)
    let = next(s for s in data["sources"] if s["key"] == "lowendtalk")
    assert sum(r["bad"] for r in let["rows"]) == 2 and let["failing"] == "连续失败 2 轮"  # 任一源失败在页面可见（FR-13）
    assert "http://***@proxy.example:8080" in let["rows"][0]["result"] and "hunter2" not in text
    llm = next(s for s in data["sources"] if s["key"] == "llm")
    assert llm["name"] == "LLM 队列" and {"3/60", "31,000"} <= set(llm["rows"][0]["cells"])
    assert "llm.example.com" not in text
    assert all("errors" not in s["extra"] and "hunter2" not in (s["error"] or "")
               for run in api["items"] for s in run["sources"])


def test_about_data(site, tmp_path):
    _, _, client = site
    about = client.get("/internal/v1/zh/about").json()
    assert about["seo"]["title"] == "关于本站 · 黑五雷达" and "contact" not in about
    assert set(about) == {"seo", "scope_bf"}  # 数据来源、更新频率两节去掉后，间隔、更新方式、User-Agent 不再下发（2026-11-05）
    with copied_site(site, tmp_path, CONTACT_EMAIL="ops@example.com") as (_, other):  # 邮箱只进 User-Agent，页面上不出现（D69）
        data = other.get("/internal/v1/zh/about").json()
        assert "contact" not in data and "ops@example.com" not in str(data)


# ---------------------------------------------------------------- RSS（M2.9）


def test_feed(site):
    settings, conn, client = site
    r = client.get("/feed.xml")
    assert r.status_code == 200 and r.headers["content-type"] == "application/rss+xml; charset=utf-8"
    assert r.headers["cache-control"] == "public, max-age=600"
    rss = ET.fromstring(r.content)
    channel = rss.find("channel")
    atom = "{http://www.w3.org/2005/Atom}link"
    assert rss.get("version") == "2.0" and channel.findtext("title") == "黑五雷达 · 黑五/网一"
    assert channel.findtext("link") == settings.site_url + "/"
    assert channel.find(atom).attrib == {"href": settings.site_url + "/feed.xml", "rel": "self",
                                         "type": "application/rss+xml"}
    items = channel.findall("item")
    assert len(items) == list_deals(conn, DealQuery(scope="bf"), SEASON).total
    dates = [parsedate_to_datetime(i.findtext("pubDate")) for i in items]
    assert dates == sorted(dates, reverse=True)
    for item in items:
        link = item.findtext("link")
        assert re.fullmatch(re.escape(settings.site_url) + r"/deal/\d+", link)
        assert item.find("guid").text == link and item.find("guid").get("isPermaLink") == "true"
        assert "看原帖" in item.findtext("description") and "库存" not in item.findtext("description")
    parsed = feedparser.parse(r.content)
    assert not parsed.bozo and parsed.version == "rss20"

    every = ET.fromstring(client.get("/feed.xml?scope=all").content).find("channel")
    assert len(every.findall("item")) == 50 and every.find(atom).get("href").endswith("/feed.xml?scope=all")
    assert client.get("/feed.xml?scope=evil").content == r.content  # 不认识的范围按默认


def test_feed_item_escapes_text():
    from app.models import Deal
    from app.web.feed import item_html, item_title

    v = View("zh", "Asia/Shanghai", datetime(2026, 11, 20, tzinfo=UTC))
    d = Deal(url="https://x.test/?a=1&b=<2>", source="lowendtalk", title="<b>50% off</b> & more", vendor="A&B",
             excerpt="<script>alert(1)</script> & co")
    html = item_html(d, v, "https://site/deal/1")
    assert "<script>" not in html and "<p>&lt;script&gt;alert(1)&lt;/script&gt; &amp; co</p>" in html
    assert "<strong>A&amp;B</strong>" in html and 'href="https://x.test/?a=1&amp;b=&lt;2&gt;"' in html
    assert item_title(d, v) == "A&B：<b>50% off</b> & more"  # 标题是纯文本，由 XML 层转义


# ---------------------------------------------------------------- JSON API（M2.10）


def test_api_deals_matches_the_page(site):
    _, conn, client = site
    r = client.get("/api/deals?scope=all&page=2&page_size=10")
    data = r.json()
    assert r.headers["cache-control"] == "public, max-age=60" and r.headers["access-control-allow-origin"] == "*"
    assert (data["total"], data["page"], data["pages"], data["page_size"], len(data["items"])) == (100, 2, 10, 10, 10)
    for item in data["items"]:
        assert item["page_url"].endswith(f"/deal/{item['id']}") and item["url"].startswith("https://")
        assert len(item["plans"]) == min(item["plan_count"], PLAN_PREVIEW)
        if item["vendor_slug"]:
            assert conn.execute("SELECT name FROM vendors WHERE slug = ?", (item["vendor_slug"],)).fetchone()[0] \
                == item["vendor"]
    q = client.get("/api/deals?scope=all&q=VM6&source=lowendtalk").json()
    assert q["total"] == list_deals(conn, DealQuery(scope="all", q="VM6", sources=("lowendtalk",)), SEASON).total
    assert VM6 in {i["url"] for i in q["items"]}
    assert client.get("/api/deals?page_size=1000").json()["page_size"] == 100
    assert client.get("/api/deals?page_size=abc").json()["page_size"] == 30


def test_api_plans(site):
    _, _, client = site
    data = client.get("/api/plans?scope=all&ram_min=2&period=year&page_size=100").json()
    assert data["total"] and all(i["ram_gb"] >= 2 and i["period"] == "year" for i in data["items"])
    assert all(i["deal"]["page_url"].endswith(f"/deal/{i['deal']['id']}") for i in data["items"])
    cores = client.get("/api/plans?scope=all&cpu_min=4&page_size=100").json()
    assert cores["total"] and all(i["cpu_cores"] >= 4 for i in cores["items"])
    a = client.get("/api/plans?scope=all&price_max_cny_year=100").json()
    assert a["total"] == client.get("/api/plans?scope=all&price_max=100").json()["total"] > 0


def test_api_deal_vendor_and_errors(site, tmp_path):
    _, conn, client = site
    vm6 = deal_id(conn, VM6)
    deal = client.get(f"/api/deals/{vm6}").json()
    assert deal["plan_count"] == len(deal["plans"]) == 9 and deal["vendor_slug"] == "vm6"
    assert deal["min_price"]["currency"] and isinstance(deal["changes"], list) and isinstance(deal["intel"], list)
    assert "stock" not in deal and all("stock_status" not in p for p in deal["plans"])  # D63
    with copied_site(site, tmp_path) as (path, other):
        write(path, "INSERT INTO changes (deal_id, changed_at, field, old_value, new_value)"
                    " VALUES (?, '2026-11-02T10:00:00Z', 'discount_pct', '50', '60')", (vm6,))
        assert {"field": "discount_pct", "old": 50, "new": 60}.items() <= \
            other.get(f"/api/deals/{vm6}").json()["changes"][0].items()

    vendor = client.get("/api/vendors/vm6").json()
    assert (vendor["name"], vendor["aliases"], vendor["website"]) == ("VM6", ["VM6 Networks"], "https://www.vm6.co.uk")
    assert len(vendor["deals"]) == 2 and all(d["plans"] == [] and d["plan_count"] for d in vendor["deals"])

    for path, status, code in (("/api/deals/999999", 404, "not_found"), ("/api/deals/abc", 404, "not_found"),
                               ("/api/vendors/nope", 404, "not_found"), ("/api/nope", 404, "not_found")):
        r = client.get(path)
        assert (r.status_code, r.json()["error"]) == (status, code), path
        assert set(r.json()) == {"error", "message"} and r.headers["access-control-allow-origin"] == "*"
    r = client.post("/api/deals")
    assert (r.status_code, r.json()["error"]) == (405, "method_not_allowed") and "GET" in r.headers["allow"]


def test_api_stats_runs_and_openapi(site):
    _, conn, client = site
    stats = client.get("/api/stats").json()
    assert stats["deals"]["total"] == 100 == sum(stats["deals"]["by_source"].values())
    assert stats["season_start"] == SEASON and stats["next_update"] is None and stats["last_update"]
    runs = client.get("/api/runs?limit=500").json()["items"]
    assert 0 < len(runs) <= 50 and {s["source"] for run in runs for s in run["sources"]} == {
        "lowendtalk", "nodeseek", "reddit"}
    assert len(client.get("/api/runs?limit=1").json()["items"]) == 1

    spec = client.get("/api/openapi.json").json()
    assert set(spec["paths"]) == {"/api/deals", "/api/plans", "/api/deals/{deal_id}", "/api/vendors/{slug}",
                                  "/api/stats", "/api/runs", "/healthz", "/api/refresh"}  # 页面不进接口说明
    params = {p["name"]: p for p in spec["paths"]["/api/deals"]["get"]["parameters"]}
    assert params["scope"]["schema"]["enum"] == ["bf", "promo", "all"] and params["source"]["schema"]["type"] == "array"
    assert "cpu_min" in {p["name"] for p in spec["paths"]["/api/plans"]["get"]["parameters"]}
    header = spec["paths"]["/api/refresh"]["post"]["parameters"][0]
    assert (header["in"], header["name"]) == ("header", "x-refresh-token")


def test_healthz(site, tmp_path):
    _, _, client = site
    r = client.get("/healthz")
    data = r.json()
    assert r.status_code == 200 and data["ok"] and r.headers["cache-control"] == "no-store"
    assert set(data["sources"]) == {"lowendtalk", "nodeseek", "reddit"} and data["failing"] == []
    assert client.head("/healthz").status_code == 200  # 探活服务常用 HEAD
    with copied_site(site, tmp_path) as (path, other):
        for n in range(3):
            write(path, "INSERT INTO runs (started_at, reason) VALUES ('2099-01-01T00:00:00Z', 'scheduled')")
            write(path, "INSERT INTO run_sources (run_id, source, started_at, finished_at, error) VALUES"
                        " ((SELECT MAX(id) FROM runs), 'nodeseek', ?, ?, 'boom')",
                  (f"2099-01-01T00:0{n}:00Z", f"2099-01-01T00:0{n}:01Z"))
        r = other.get("/healthz")
    assert r.status_code == 503 and r.json()["failing"] == ["nodeseek"]


# ---------------------------------------------------------------- 内部页面数据（D60）

HOME_KEYS = {"seo", "stats", "just", "scopes", "form", "total", "deals", "pager", "more", "empty", "intel"}
CARD_KEYS = {"id", "href", "bf", "event", "vendor", "vendor_href", "heat", "heat_title", "title", "same", "same_href",
             "flags", "offer", "specs", "codes", "codes_more", "price", "date", "published_at", "expand", "plans",
             "cols", "plans_more", "excerpt", "expiry", "net"}
PLAN_KEYS = {"name", "ip", "net", "cpu", "ram", "disk", "bw", "port", "loc", "price", "term", "cny", "code", "order"}
HIDDEN_KEYS = {"source", "sources", "evidence", "seen_at", "stock", "stock_key", "stock_source", "stock_evidence",
               "stock_status", "dim", "in_stock", "include_sold_out"}  # 页面不显示来源（D59），库存不再收集（D63）


def all_keys(value) -> set:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in all_keys(v)}
    return {k for v in value for k in all_keys(v)} if isinstance(value, list) else set()


def test_internal_home_is_formatted_and_hides_sources(site):
    """键集合是前端的契约，改动要同步 frontend/app/lib/types.ts；页面上不出现来源名（D59）与库存（D63）。"""
    settings, _, client = site
    r = client.get("/internal/v1/zh/home?scope=all&page=2&source=lowendtalk")  # 页面不按来源筛选，source 忽略
    data = r.json()
    assert r.headers["cache-control"] == "no-store" and set(data) == HOME_KEYS
    assert data["seo"]["canonical"] == f"{settings.site_url}/?scope=all&page=2"
    assert data["seo"]["alternates"] == {"zh": "/?scope=all&page=2", "en": "/en/?scope=all&page=2"}
    assert len(data["deals"]) == internal.CARD_PAGE_SIZE and data["total"] == "共 100 条"
    assert (data["pager"]["prev"], data["pager"]["next"]) == ("/?scope=all", "/?scope=all&page=3")
    assert [s["href"] for s in data["scopes"]] == ["/", "/?scope=promo", "/?scope=all"]
    for card in data["deals"]:
        assert set(card) == CARD_KEYS and card["href"] == f"/deal/{card['id']}"
        assert len(card["plans"]) <= internal.CARD_PLANS and all(set(p) == PLAN_KEYS for p in card["plans"])
    assert not all_keys(data) & HIDDEN_KEYS

    en = client.get("/internal/v1/en/home").json()
    assert en["form"]["action"] == "/en/" and all(s["href"].startswith("/en/") for s in en["scopes"])
    assert en["seo"]["canonical"] == f"{settings.site_url}/en/" and en["total"].endswith("deals")


def test_internal_chrome_and_errors(site):
    _, _, client = site
    chrome = client.get("/internal/v1/zh/chrome").json()
    assert set(chrome) == {"countdown", "freshness", "footer"} and "汇率" in chrome["footer"]
    countdown = chrome["countdown"]
    assert set(countdown) == {"target", "end", "phase", "days", "hours", "minutes", "seconds", "text", "when"}
    assert countdown["phase"] in ("before", "during", "after") and set(countdown["when"]) == {"before", "during"}
    assert "11-27 13:00" in countdown["when"]["before"] and "美东 11-27 零点" in countdown["when"]["before"]  # 北京时间
    assert "12-01 13:00" in countdown["when"]["during"]
    en = client.get("/internal/v1/en/chrome").json()["countdown"]["when"]
    assert "11-27 05:00 UTC" in en["before"] and "US Eastern, 11-27" in en["before"]
    for path in ("/internal/v1/fr/home", "/internal/v1/zh/nope"):
        r = client.get(path)
        assert r.status_code == 404 and set(r.json()) == {"error", "message"}, path  # 内部数据出错也给 JSON，不给页面
    assert internal._url("javascript:alert(1)") is None and internal._url("https://x.test/a") == "https://x.test/a"


def test_internal_plans_deal_vendor_and_about(site, tmp_path):
    settings, conn, client = site
    plans = client.get("/internal/v1/zh/plans?scope=all&ram_min=2&period=year&_=1").json()  # 多余参数不进规范地址
    assert set(plans) == {"seo", "scopes", "presets", "form", "total", "rows", "pager"}
    assert plans["seo"]["canonical"] == f"{settings.site_url}/plans?scope=all&ram_min=2&period=year"
    row_keys = PLAN_KEYS | {"loc_title", "vendor", "vendor_href", "deal_title", "deal_href"}
    assert plans["rows"] and all(set(r) == row_keys for r in plans["rows"])
    form = plans["form"]
    assert (form["action"], form["reset"]) == ("/plans", "/plans?scope=all")
    assert [o["value"] for o in form["ram"] if o["selected"]] == ["2"] and {"value": "0.5", "label": "512 MB",
                                                                           "selected": False} in form["ram"]
    assert [o["value"] for o in form["period"] if o["selected"]] == ["year"]
    assert {"value": "4", "label": "4 核", "selected": False} in form["cpu"]
    en = client.get("/internal/v1/en/plans?cpu_min=4").json()
    assert en["form"]["action"] == "/en/plans"
    assert {"value": "4", "label": "4 cores", "selected": True} in en["form"]["cpu"]

    vm6 = deal_id(conn, VM6)
    deal = client.get(f"/internal/v1/zh/deal/{vm6}").json()
    assert deal["seo"]["canonical"] == f"{settings.site_url}/deal/{vm6}" and deal["vendor_href"] == "/vendor/vm6"
    assert deal["plans_heading"] == "套餐（9 个）" and len(deal["plans"]) == 9
    assert deal["others_heading"] == "VM6 的其他活动" and deal["others"][0]["href"].startswith("/deal/")
    with copied_site(site, tmp_path) as (path, other):
        write(path, "INSERT INTO changes (deal_id, changed_at, field, old_value, new_value)"
                    " VALUES (?, '2026-11-02T10:00:00Z', 'discount_pct', '50', '60')", (vm6,))
        assert "折扣：50% → 60%" in [c["text"] for c in other.get(f"/internal/v1/zh/deal/{vm6}").json()["changes"]]

    vendor = client.get("/internal/v1/zh/vendor/vm6").json()
    assert (vendor["name"], vendor["aka"], vendor["website"]) == ("VM6", "也叫：VM6 Networks", "https://www.vm6.co.uk")
    assert vendor["deals_heading"] == "本季优惠（2 条）" and vendor["past_heading"] is None and vendor["past"] == []
    assert vendor["lowest"]["href"].startswith("/deal/")

    about = client.get("/internal/v1/en/about").json()
    assert "contact" not in about
    assert "2026-10-01" in about["scope_bf"]
    for data in (plans, deal, vendor, about):
        assert not all_keys(data) & HIDDEN_KEYS
    for path in ("/internal/v1/zh/deal/999999", "/internal/v1/zh/deal/abc", "/internal/v1/zh/vendor/nope"):
        r = client.get(path)
        assert r.status_code == 404 and set(r.json()) == {"error", "message"}, path


def test_vendor_directory_lists_active_vendors_only(site, tmp_path):
    """商家目录（O-17）：本季有优惠、或近 30 天有讨论的商家，不列空壳；默认按本季优惠数排，可按名称 A–Z；中英文都有。"""
    _, conn, client = site
    data = client.get("/internal/v1/zh/vendors").json()
    assert set(data) == {"seo", "total", "sorts", "rows", "empty"} and data["empty"] == ""
    rows = data["rows"]
    all_vendors = conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]
    assert 0 < len(rows) < all_vendors and data["total"] == f"共 {len(rows)} 家"  # 别名表里没有优惠、没有讨论的不列
    assert all(set(r) == {"name", "href", "aliases", "deals", "has_deals", "low", "latest", "regions"} for r in rows)
    counts = [int(re.match(r"\d+", r["deals"])[0]) for r in rows if r["has_deals"]]
    assert counts == sorted(counts, reverse=True)
    vm6 = next(r for r in rows if r["href"] == "/vendor/vm6")
    assert vm6["deals"] == "2 条" and vm6["aliases"] == "VM6 Networks" and vm6["low"].startswith("≈ ¥") and vm6["regions"] == "欧洲"
    assert data["seo"]["canonical"].endswith("/vendors")
    assert [(s["value"], s["current"], s["href"]) for s in data["sorts"]] == [("deals", True, "/vendors"), ("name", False, "/vendors?sort=name")]
    by_name = client.get("/internal/v1/zh/vendors?sort=name").json()
    names = [r["name"].casefold() for r in by_name["rows"]]
    assert names == sorted(names) and len(names) == len(rows)
    assert by_name["seo"]["canonical"].endswith("/vendors") and by_name["sorts"][1]["current"] is True
    assert client.get("/internal/v1/zh/vendors?sort=nonsense").json()["sorts"][0]["current"] is True
    en = client.get("/internal/v1/en/vendors").json()
    assert en["total"] == f"{len(rows)} providers" and en["sorts"][1]["href"] == "/en/vendors?sort=name"
    assert next(r for r in en["rows"] if r["href"] == "/en/vendor/vm6")["regions"] == "Europe"
    assert client.get("/internal/v1/fr/vendors").status_code == 404


def test_vendor_page_lists_plans_without_duplicates_places_and_earlier_deals(site, tmp_path):
    """商家页（O-17）：本季在售套餐去重（核数、内存、存储、价格、币种、周期都一样的只留最新帖里的）、最便宜的在前、最多 20 个；
    结束了的活动里的套餐不算在售；机房按地区 → 城市带套餐数；线路标签汇总；本季与往季的优惠分开。"""
    _, conn, _ = site
    vm6 = deal_id(conn, VM6)
    other = conn.execute("SELECT id FROM deals WHERE vendor_id = (SELECT vendor_id FROM deals WHERE id = ?) AND id != ?",
                         (vm6, vm6)).fetchone()[0]
    _, _, client = site
    data = client.get("/internal/v1/zh/vendor/vm6").json()
    assert set(data) >= {"places", "net", "plans_heading", "plans", "cols", "deals", "past", "past_heading"}
    years = [float(p["cny"].lstrip("¥").replace(",", "")) for p in data["plans"] if p["cny"]]
    assert data["plans"] and all(p["href"].startswith("/deal/") for p in data["plans"]) and years == sorted(years)  # 年价从低到高
    assert data["plans_heading"].startswith("在售套餐（本季，去重后") and f"列出最便宜的 {len(data['plans'])} 个" in data["plans_heading"]
    assert data["places"][0]["region"] == "欧洲" and re.search(r"诺里奇 \d+", data["places"][0]["cities"])
    total = int(re.search(r"去重后 (\d+) 个", data["plans_heading"])[1])
    # 同一个套餐在另一帖里再写一遍：只留一个（页面有内存缓存，改了库要换一个新应用再取）
    with copied_site(site, tmp_path / "dup") as (path, fresh):
        write(path, "INSERT INTO plans (deal_id, position, name, cpu_cores, ram_gb, disk_gb, price, currency, period, price_cny_year)"
                    " SELECT ?, 99, name, cpu_cores, ram_gb, disk_gb, price, currency, period, price_cny_year FROM plans"
                    " WHERE deal_id = ? LIMIT 1", (other, vm6))
        assert int(re.search(r"去重后 (\d+) 个", fresh.get("/internal/v1/zh/vendor/vm6").json()["plans_heading"])[1]) == total
    # 活动结束了，里面的套餐不算在售；往季的优惠单列
    with copied_site(site, tmp_path / "ended") as (path, fresh):
        write(path, "UPDATE deals SET ends_at = '2026-10-01' WHERE id = ?", (vm6,))
        write(path, "UPDATE deals SET published_at = '2025-11-20T00:00:00Z' WHERE id = ?", (other,))
        later = fresh.get("/internal/v1/zh/vendor/vm6").json()
        assert later["plans"] == [] and later["plans_heading"] is None and later["places"] == []
        assert later["deals_heading"] == "本季优惠（1 条）" and later["past_heading"] == "往季优惠（1 条）"
        assert [d["href"] for d in later["deals"]] == [f"/deal/{vm6}"] and later["deals"][0]["expiry"]["state"] == "ended"
        assert later["past"][0]["href"] == f"/deal/{other}"


def test_codes_page_lists_this_seasons_codes_newest_first_and_ended_last(site, tmp_path):
    """优惠码汇总（O-18）：本季带码的优惠，每行商家、码、折扣、续费性质、有效期，链到详情页；最新的在前，已结束的排到最后（照样列出）。"""
    _, conn, client = site
    data = client.get("/internal/v1/zh/codes").json()
    assert set(data) == {"seo", "total", "rows", "empty"} and data["empty"] == ""
    rows = data["rows"]
    season = conn.execute("SELECT COUNT(*) FROM deals WHERE published_at >= ? AND coupon_codes_json != '[]'", (SEASON,)).fetchone()[0]
    assert len(rows) == season > 0 and data["total"] == f"共 {season} 条"
    assert all(set(r) == {"id", "href", "vendor", "vendor_href", "title", "codes", "discount", "recurring", "expiry", "date"}
               and r["codes"] and r["href"] == f"/deal/{r['id']}" for r in rows)
    vm6 = next(r for r in rows if r["vendor"] == "VM6")
    assert vm6["vendor_href"] == "/vendor/vm6" and {"BFVPS55", "BFVPS60"} <= set(vm6["codes"])
    assert vm6["discount"] == "最高 60%" and vm6["recurring"] == "仅首期"
    assert data["seo"]["canonical"].endswith("/codes")
    en = client.get("/internal/v1/en/codes").json()
    assert en["total"] == f"{season} deals" and next(r for r in en["rows"] if r["vendor"] == "VM6")["href"].startswith("/en/deal/")
    assert client.get("/internal/v1/fr/codes").status_code == 404
    with copied_site(site, tmp_path) as (path, fresh):  # 页面有内存缓存：先改库再取
        newest = rows[0]["id"]
        write(path, "UPDATE deals SET ends_at = '2026-10-01', ends_source = 'rule' WHERE id = ?", (newest,))
        write(path, "UPDATE deals SET ends_at = '2026-11-05', ends_source = 'rule' WHERE id = ?", (rows[1]["id"],))
        later = fresh.get("/internal/v1/zh/codes").json()["rows"]
        states = [r["expiry"] and r["expiry"]["state"] for r in later]
        n = states.count("ended")
        assert n >= 1 and states[-n:] == ["ended"] * n and newest in {r["id"] for r in later[-n:]}  # 已结束的都在末尾
        assert later[0]["id"] == rows[1]["id"] and later[0]["expiry"]["state"] == "soon"


def test_plans_quick_filters_are_ready_made_queries(site, tmp_path):
    """套餐视图的快捷筛选（O-19）：每个按钮是一条现成的查询；当前查询与它一致（不计排序与页码）时高亮，再点一下回到没有筛选；
    线路按钮只在当前范围里真有带这个标签的套餐时给。"""
    _, conn, client = site
    data = client.get("/internal/v1/zh/plans?scope=all").json()
    presets = {x["key"]: x for x in data["presets"]}
    assert list(presets) == ["cheap", "ram2", "bigdisk", "dedicated", "asia"]  # 库里没有线路标签：不给 CN2 GIA
    assert presets["cheap"]["label"] == "年价 ¥100 内" and presets["cheap"]["href"] == "/plans?scope=all&price_max=100"
    assert presets["ram2"]["href"] == "/plans?scope=all&ram_min=2&price_max=200"
    assert presets["bigdisk"]["href"] == "/plans?scope=all&disk_min=1000"
    assert presets["dedicated"]["href"] == "/plans?scope=all&product=dedicated"
    assert presets["asia"]["href"] == "/plans?scope=all&region=asia" and not any(x["current"] for x in presets.values())
    # 点进去：列表确实是那条查询的结果，按钮高亮，链接变成"取消"
    for key in ("cheap", "dedicated", "asia"):
        href = presets[key]["href"]
        on = client.get("/internal/v1/zh" + href + "&sort=ram&page=1").json()  # 排序与页码不影响高亮
        marked = {x["key"]: x for x in on["presets"]}
        assert marked[key]["current"] is True and marked[key]["href"] == "/plans?scope=all", key
        assert [k for k, x in marked.items() if x["current"]] == [key] and on["rows"], key
    cheap = client.get("/internal/v1/zh/plans?scope=all&price_max=100").json()
    assert cheap["rows"] and all(float(r["cny"].lstrip("¥").replace(",", "")) <= 100 for r in cheap["rows"] if r["cny"])
    extra = client.get("/internal/v1/zh/plans?scope=all&price_max=100&nvme=1").json()  # 多了别的筛选：不再是这一条
    assert not any(x["current"] for x in extra["presets"])
    assert client.get("/internal/v1/en/plans").json()["presets"][0]["label"] == "Under ¥100/yr"
    assert client.get("/internal/v1/en/plans").json()["presets"][0]["href"] == "/en/plans?price_max=100"
    with copied_site(site, tmp_path) as (path, fresh):
        write(path, "UPDATE plans SET net_json = '[\"cn2gia\"]' WHERE id = (SELECT MIN(id) FROM plans WHERE price IS NOT NULL)")
        with_net = {x["key"]: x for x in fresh.get("/internal/v1/zh/plans?scope=all").json()["presets"]}
        assert with_net["cn2gia"]["href"] == "/plans?scope=all&net=cn2gia"


def test_seo_meta_and_structured_data(site):
    """M3.4：各页数据里的 seo（前端照着出 meta 与 JSON-LD）；详情页 Product + AggregateOffer，商家页 Organization，
    首页 WebSite + ItemList，各页 BreadcrumbList；地址都以 SITE_URL 开头。"""
    settings, conn, client = site
    base, vm6 = settings.site_url, deal_id(conn, VM6)
    data = client.get(f"/internal/v1/zh/deal/{vm6}").json()["seo"]
    og = data["og"]
    assert (og["type"], og["url"], og["site_name"]) == ("article", f"{base}/deal/{vm6}", "黑五雷达")
    product, crumbs = data["jsonld"]
    offers = product["offers"]
    assert (product["@type"], product["brand"]["name"], offers["@type"]) == ("Product", "VM6", "AggregateOffer")
    assert offers["lowPrice"] <= offers["highPrice"] and offers["offerCount"] >= 1 and offers["priceCurrency"]
    assert [i["item"] for i in crumbs["itemListElement"]] == [f"{base}/", f"{base}/vendor/vm6", f"{base}/deal/{vm6}"]
    en = client.get(f"/internal/v1/en/deal/{vm6}").json()["seo"]
    assert (en["og"]["site_name"], en["og"]["locale"]) == ("BlackFridayRadar", "en_US")
    assert en["jsonld"][1]["itemListElement"][0]["item"] == f"{base}/en/"

    home = client.get("/internal/v1/zh/home?scope=all").json()
    website, items = home["seo"]["jsonld"]
    assert website["@type"] == "WebSite" and len(items["itemListElement"]) == len(home["deals"])
    assert client.get("/internal/v1/zh/vendor/vm6").json()["seo"]["jsonld"][0] == {
        "@context": "https://schema.org", "@type": "Organization", "name": "VM6", "url": "https://www.vm6.co.uk",
        "alternateName": ["VM6 Networks"]}



def test_sitemap_and_robots(site):
    """M3.5：中英文各一条，每条带 zh-CN、en、x-default；有优惠的商家才列；状态页不列（D59）。"""
    settings, conn, client = site
    base = settings.site_url
    r = client.get("/sitemap.xml")
    assert r.headers["content-type"] == "application/xml; charset=utf-8"
    assert r.headers["cache-control"] == "public, max-age=3600"
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9", "x": "http://www.w3.org/1999/xhtml"}
    urls = ET.fromstring(r.content).findall("s:url", ns)
    locs = [u.findtext("s:loc", namespaces=ns) for u in urls]
    deals = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
    vendors = conn.execute("SELECT COUNT(DISTINCT vendor_id) FROM deals WHERE vendor_id IS NOT NULL").fetchone()[0]
    assert len(locs) == len(set(locs)) == 2 * (5 + deals + vendors)  # 首页、套餐视图、商家目录、优惠码、关于页
    assert {f"{base}/", f"{base}/en/", f"{base}/en/vendor/vm6", f"{base}/plans", f"{base}/vendors", f"{base}/en/vendors", f"{base}/codes"} <= set(locs)
    assert not [loc for loc in locs if "/status" in loc]
    assert [(link.get("hreflang"), link.get("href")) for link in urls[1].findall("x:link", ns)] == [
        ("zh-CN", f"{base}/"), ("en", f"{base}/en/"), ("x-default", f"{base}/")]
    vm6 = deal_id(conn, VM6)
    assert len(urls[locs.index(f"{base}/deal/{vm6}")].findtext("s:lastmod", namespaces=ns)) == 10  # YYYY-MM-DD

    robots = client.get("/robots.txt")
    assert robots.text == f"User-agent: *\nAllow: /\n\nSitemap: {base}/sitemap.xml\n"
    assert robots.headers["cache-control"] == "public, max-age=86400"


# ---------------------------------------------------------------- 分享预览图（M3.6）


def png_size(r) -> tuple[int, int]:
    assert (r.status_code, r.headers["content-type"], r.headers["cache-control"]) == (
        200, "image/png", "public, max-age=86400")
    return Image.open(io.BytesIO(r.content)).size


def test_og_images(site):
    """M3.6：og:image 地址带内容摘要；图为 1200×630 PNG，画一次存到数据目录的 og/ 下。"""
    settings, conn, client = site
    base, vm6 = settings.site_url, deal_id(conn, VM6)
    image = client.get(f"/internal/v1/zh/deal/{vm6}").json()["seo"]["og"]["image"]
    assert image.startswith(f"{base}/og/zh/deal/{vm6}.png?v=")
    r = client.get(image.removeprefix(base))
    assert png_size(r) == (og.WIDTH, og.HEIGHT)
    cached = list((Path(settings.db_path).parent / "og" / "zh").glob(f"{vm6}-*.png"))
    assert len(cached) == 1 and cached[0].read_bytes() == r.content
    assert client.get(f"/og/zh/deal/{vm6}.png").content == r.content

    for lang, prefix in (("zh", ""), ("en", "/en")):  # 其余页面用站点图
        image = client.get(f"/internal/v1/{lang}/about").json()["seo"]["og"]["image"]
        assert image.startswith(f"{base}/og/{lang}/site.png?v=")
        assert png_size(client.get(f"/og/{lang}/site.png")) == (og.WIDTH, og.HEIGHT)
    for path in ("/og/zh/deal/999999.png", f"/og/fr/deal/{vm6}.png", "/og/fr/site.png"):
        r = client.get(path)
        assert (r.status_code, r.headers["cache-control"]) == (404, "public, max-age=60"), path
    assert client.get("/og/zh/deal/abc.png").status_code == 404  # 编号不是数字：对不上路由，一般的 404


def test_og_text_and_cache(tmp_path):
    """字体里没有的字（emoji）去掉；折行不超宽、超出行数加省略号、西文词不拆开；内容变了换新图并删旧图。"""
    assert og.clean("🔥 黑五\t大促  VPS ✓") == "黑五 大促 VPS ✓"
    width = 600
    lines = og.wrap("GreenCloud 黑五 Budget KVM 大促 " * 8, 50, width, 3)
    assert len(lines) == 3 and lines[-1].endswith("…")
    assert all(og.font(50).getlength(line) <= width for line in lines)
    assert {word for line in lines for word in re.findall(r"[A-Za-z]+", line)} <= {"GreenCloud", "Budget", "KVM"}
    long = og.wrap("x" * 80, 50, width, 2)  # 比整行还宽的词逐字断
    assert len(long) == 2 and long[1].endswith("…") and all(og.font(50).getlength(line) <= width for line in long)

    first = og._cached(tmp_path, "7", {"title": "a"}, lambda data: b"one")
    other = og._cached(tmp_path, "70", {"title": "a"}, lambda data: b"x")
    second = og._cached(tmp_path, "7", {"title": "b"}, lambda data: b"two")
    assert not first.exists() and second.read_bytes() == b"two" and other.exists()
    assert og._cached(tmp_path, "7", {"title": "b"}, lambda data: pytest.fail("已有就不再画")) == second


# ---------------------------------------------------------------- 手动刷新（M2.11）


def test_refresh_needs_a_token(site, tmp_path):
    _, _, client = site
    r = client.post("/api/refresh")
    assert (r.status_code, r.json()["error"]) == (403, "refresh_disabled")  # 没设 REFRESH_TOKEN 就不开
    with copied_site(site, tmp_path, REFRESH_TOKEN="s3cret") as (_, other):
        assert other.post("/api/refresh").status_code == 401
        assert other.post("/api/refresh", headers={"X-Refresh-Token": "nope"}).status_code == 401
        r = other.post("/api/refresh", headers={"X-Refresh-Token": "s3cret"})
        assert (r.status_code, r.json()["error"]) == (503, "scheduler_unavailable")
    fake = FakeScheduler()
    with copied_site(site, tmp_path / "b", scheduler=fake, REFRESH_TOKEN="s3cret") as (_, other):
        r = other.post("/api/refresh", headers={"X-Refresh-Token": "s3cret"})
        assert (r.status_code, r.json(), r.headers["cache-control"]) == (200, {"run_id": 7}, "no-store")
        r = other.post("/api/refresh", headers={"X-Refresh-Token": "s3cret"})
        assert (r.status_code, r.json()["error"]) == (409, "busy")  # 重复触发不并发
        status = other.get("/internal/v1/zh/status").json()
        assert status["refreshing"] is True and "正在手动刷新" in status["facts"][1]["value"]


# ---------------------------------------------------------------- 内存缓存（M2.12）


def test_page_cache_ttl_lru_and_generations():
    from app.web.cache import Cached, PageCache

    now = {"t": 0.0}
    cache = PageCache(max_entries=2, clock=lambda: now["t"])
    one = Cached(b"1", {})
    _, gen = cache.lookup("a")
    cache.store("a", one, 10, gen)
    assert cache.lookup("a")[0] is one
    now["t"] = 10.0
    assert cache.lookup("a")[0] is None  # 到期
    for key in ("a", "b", "c"):
        cache.store(key, one, 10, cache.lookup(key)[1])
    assert cache.lookup("a")[0] is None and len(cache) == 2  # 最久没用的先出
    _, gen = cache.lookup("d")
    cache.clear()
    cache.store("d", one, 10, gen)  # 清除前开始的渲染不写回
    assert len(cache) == 0


def test_pages_are_cached_until_data_changes(site, tmp_path):
    fake = FakeScheduler()
    vendor = "/internal/v1/zh/vendor/vm6"
    with copied_site(site, tmp_path, scheduler=fake) as (path, client):
        cache = client.app.state.cache
        assert client.get(vendor).json() == client.get(vendor).json() and cache.hits == 1
        write(path, "UPDATE vendors SET name = 'VM6 Renamed' WHERE slug = 'vm6'")
        assert client.get(vendor).json()["name"] == "VM6"  # 缓存里的旧数据
        for listener in fake.listeners:  # 调度器跑完一轮会通知
            listener()
        assert client.get(vendor).json()["name"] == "VM6 Renamed"

        before = len(cache)
        for query in ("scope=all&_=1", "_=2&scope=all&page=1", "scope=bf", ""):  # 多余、乱序、取默认值的参数归到同一条
            client.get(f"/internal/v1/zh/home?{query}")
        assert len(cache) == before + 2
        size = len(cache)
        assert client.get("/internal/v1/zh/status").headers["cache-control"] == "no-store"
        client.get("/healthz")
        client.post("/api/refresh")
        assert len(cache) == size  # 状态页、健康检查、刷新不进缓存

# ---------------------------------------------------------------- 优化方案第一组（O-01 至 O-06，D66）


def copy_deal(db: sqlite3.Connection, source_id: int, url: str, days: int, price_bump: float = 0) -> int:
    """把一条优惠连同套餐复制一份（另一处的帖子），发布时间挪 days 天，套餐价格加 price_bump。"""
    cols = [r[1] for r in db.execute("PRAGMA table_info(deals)") if r[1] not in ("id", "url", "published_at")]
    db.execute(f"INSERT INTO deals (url, published_at, {', '.join(cols)}) SELECT ?,"
               f" strftime('%Y-%m-%dT%H:%M:%SZ', published_at, '{days:+d} days'), {', '.join(cols)}"
               f" FROM deals WHERE id = ?", (url, source_id))
    new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    cols = [r[1] for r in db.execute("PRAGMA table_info(plans)") if r[1] not in ("id", "deal_id", "price")]
    db.execute(f"INSERT INTO plans (deal_id, price, {', '.join(cols)}) SELECT ?, price + ?, {', '.join(cols)}"
               f" FROM plans WHERE deal_id = ?", (new_id, price_bump, source_id))
    return new_id


def test_same_offer_is_marked_not_hidden(site, tmp_path):
    """同一活动在别处又发了一帖（套餐一样、发布相差 ≤ 21 天）：两条都照常显示，卡片写"同一活动另有 N 帖"，
    详情页单列"同一活动的其他帖子"且不在"其他活动"里重复；套餐不一样或相隔太久的不算（D66）。"""
    _, conn, _ = site
    vm6 = deal_id(conn, VM6)
    with copied_site(site, tmp_path) as (path, client):
        db = sqlite3.connect(path)
        repost = copy_deal(db, vm6, "https://lowendspirit.com/discussion/90001", days=3)
        later = copy_deal(db, vm6, "https://lowendspirit.com/discussion/90002", days=60)
        other = copy_deal(db, vm6, "https://lowendspirit.com/discussion/90003", days=1, price_bump=1)
        db.commit()
        db.close()
        cards = {d["id"]: d for n in (1, 2, 3, 4, 5, 6) for d in
                 client.get(f"/internal/v1/zh/home?scope=all&page={n}").json()["deals"]}
        assert {vm6, repost, later, other} <= set(cards)  # 全部照常显示
        assert (cards[vm6]["same"], cards[vm6]["same_href"]) == ("同一活动另有 1 帖", f"/deal/{vm6}#same")
        assert cards[repost]["same"] == "同一活动另有 1 帖"
        assert cards[later]["same"] is None and cards[other]["same"] is None
        detail = client.get(f"/internal/v1/zh/deal/{vm6}").json()
        assert detail["same_heading"] == "同一活动的其他帖子"
        assert [x["href"] for x in detail["same"]] == [f"/deal/{repost}"]
        assert f"/deal/{repost}" not in [o["href"] for o in detail["others"]]
        assert f"/deal/{other}" in [o["href"] for o in detail["others"]]
        en = client.get(f"/internal/v1/en/deal/{vm6}").json()
        assert en["same_heading"] == "Same offer, other posts" and en["same"][0]["href"] == f"/en/deal/{repost}"
        assert client.get(f"/internal/v1/zh/deal/{later}").json()["same"] == []


def test_same_offer_groups_chains_of_reposts(site, tmp_path):
    """每两周重发一次的帖子：首尾相隔超过 21 天，经中间一帖连成一组，组内互相标注。"""
    _, conn, _ = site
    vm6 = deal_id(conn, VM6)
    with copied_site(site, tmp_path) as (path, _):
        db = sqlite3.connect(path)
        a = copy_deal(db, vm6, "https://lowendspirit.com/discussion/90011", days=14)
        b = copy_deal(db, vm6, "https://lowendspirit.com/discussion/90012", days=28)
        db.commit()
        db.close()
        found = same_offer(connect(path), [vm6, a, b])
        assert {x.id for x in found[vm6]} == {a, b} and {x.id for x in found[b]} == {vm6, a}
        assert [x.id for x in found[vm6]] == [b, a]  # 新的在前


def test_card_preview_shows_the_cheapest_plans_and_only_filled_columns(site):
    """卡片展开的预览是最便宜的几个套餐；全空的列不画，卡片里不画"下单"列（O-02、O-03）。"""
    _, conn, client = site
    checked = 0
    for n in (1, 2, 3, 4, 5):
        for card in client.get(f"/internal/v1/zh/home?scope=all&page={n}").json()["deals"]:
            plans, cols = card["plans"], card["cols"]
            assert "price" in cols and "order" not in cols, card["id"]
            assert list(cols) == [c for c in internal.PLAN_COLS if c in cols]  # 顺序固定
            for col in set(internal.PLAN_COLS) - {"price", "order"}:
                filled = any(p[col] or (col == "name" and p["ip"]) for p in plans)
                assert (col in cols) == filled, (card["id"], col)
            got = [float(p["cny"].lstrip("¥").replace(",", "")) if p["cny"] else None for p in plans]
            if plans and None not in got:
                db = [r[0] for r in conn.execute("SELECT price_cny_year FROM plans WHERE deal_id = ? AND"
                                                 " price_cny_year IS NOT NULL ORDER BY price_cny_year", (card["id"],))]
                assert got == [float(f"{x:.0f}") for x in db[:len(got)]], card["id"]
                checked += len(db) > len(got)
    assert checked  # 至少核对过一张"套餐比预览多"的卡片
    detail = client.get(f"/internal/v1/zh/deal/{deal_id(conn, VM6)}").json()
    assert "order" in detail["cols"] and detail["cols"] == [c for c in internal.PLAN_COLS if c in detail["cols"]]


def test_plans_nat_filter_and_labels(site, tmp_path):
    """NAT（套餐名或备注里单独的 NAT）：表里带标签，"不含 NAT"时不出现；"alternative"这类词里的 nat 不算（O-04）。
    商家默认都给 IPv4，没有"无独立 IPv4"这一档：不记、不标、不筛（D69）。"""
    _, conn, _ = site
    vm6 = deal_id(conn, VM6)
    second, third = [r[0] for r in conn.execute(
        "SELECT id FROM plans WHERE deal_id = ? ORDER BY position LIMIT 2 OFFSET 1", (vm6,))]
    with copied_site(site, tmp_path) as (path, client):
        write(path, "UPDATE plans SET name = 'LXC NAT VPS' WHERE id = ?", (second,))
        write(path, "UPDATE plans SET notes = 'alternative: unlimited transfer' WHERE id = ?", (third,))
        db = connect(path)  # 带 is_nat 函数的连接
        every = {r.plan.id for r in all_plans(db, PlanQuery(scope="all"))}
        no_nat = {r.plan.id for r in all_plans(db, PlanQuery(scope="all", no_nat=True))}
        assert second in every - no_nat and third in no_nat  # 其余带 NAT 的套餐也被去掉，不只这一条
        labels = {p["name"]: p["ip"] for p in client.get(f"/internal/v1/zh/deal/{vm6}").json()["plans"]}
        assert labels["LXC NAT VPS"] == "NAT" and set(labels.values()) == {"NAT", None}
        en = client.get(f"/internal/v1/en/deal/{vm6}").json()["plans"]
        assert {p["ip"] for p in en} == {"NAT", None}
        data = client.get("/internal/v1/zh/plans?scope=all&nonat=1").json()
        assert data["form"]["no_nat"] is True and data["form"]["reset"] == "/plans?scope=all"
        assert data["seo"]["canonical"].endswith("/plans?scope=all&nonat=1")
        assert "ipv4" not in client.get(f"/api/deals/{vm6}").json()["plans"][0]
    assert PlanQuery.parse({"nonat": ["1"]}).href() == "/plans?nonat=1" and PlanQuery.parse({"nonat": ["1"]}).filtered
    assert not PlanQuery.parse({"ipv4": ["1"]}).filtered  # 旧链接里的 ipv4 参数不再起作用


def test_home_points_to_the_rest_of_the_season(site):
    """首页默认黑五/网一（D66）：最后一页末尾提示本季另有多少条其他优惠，链到"本季全部"；筛选中或别的范围不提示。"""
    _, conn, client = site
    cutoff = fmt.ended_cutoff(datetime.fromisoformat(TODAY).date())  # 页面上不列已结束的活动（O-15）
    bf = list_deals(conn, DealQuery(scope="bf"), SEASON, ended_before=cutoff)
    promo = list_deals(conn, DealQuery(scope="promo"), SEASON, ended_before=cutoff)
    last = f"?page={bf.pages}" if bf.pages > 1 else ""
    home = client.get(f"/internal/v1/zh/home{last}").json()
    assert home["more"] == {"text": f"本季另有 {promo.total - bf.total} 条其他优惠（促销、节日、新品等），看本季全部",
                            "href": "/?scope=promo"}
    assert client.get(f"/internal/v1/en/home{last}").json()["more"]["href"] == "/en/?scope=promo"
    for path in ("/internal/v1/zh/home?scope=promo", "/internal/v1/zh/home?q=vps", "/internal/v1/zh/home?product=vps"):
        assert client.get(path).json()["more"] is None, path
    if bf.pages > 1:
        assert client.get("/internal/v1/zh/home").json()["more"] is None  # 只在最后一页


def test_network_tags_show_on_cards_tables_and_filter_the_plans_view(site, tmp_path):
    """线路与网络标签（O-16）：套餐名后与卡片上显示标签，套餐视图的"线路"多选只列当前范围里有的，套餐带其中任何一个就算；接口带标签键。"""
    _, conn, _ = site
    vm6 = deal_id(conn, VM6)
    first, second, third = [r[0] for r in conn.execute(
        "SELECT id FROM plans WHERE deal_id = ? ORDER BY position LIMIT 3", (vm6,))]
    with copied_site(site, tmp_path) as (path, client):
        write(path, "UPDATE plans SET net_json = '[\"cn2gia\"]' WHERE id = ?", (first,))
        write(path, "UPDATE plans SET net_json = '[\"9929\", \"4837\"]' WHERE id = ?", (second,))
        write(path, "UPDATE deals SET specs_range_json = json_set(specs_range_json, '$.net', json('[\"cn2gia\",\"9929\",\"4837\"]'))"
                    " WHERE id = ?", (vm6,))
        nets = [p["net"] for p in client.get(f"/internal/v1/zh/deal/{vm6}").json()["plans"]]  # 按套餐顺序
        assert nets[:2] == [["CN2 GIA"], ["9929", "4837"]] and not any(nets[2:])
        assert [p["net"] for p in client.get(f"/internal/v1/en/deal/{vm6}").json()["plans"]][:2] == [
            ["CN2 GIA"], ["AS9929", "AS4837"]]
        card = next(c for c in client.get("/internal/v1/zh/home?scope=all&page=1").json()["deals"] if c["id"] == vm6)
        assert card["net"] == ["CN2 GIA", "9929", "4837"]
        # 套餐视图：只列有的标签，选中后只剩带这个标签的套餐
        form = client.get("/internal/v1/zh/plans?scope=all").json()["form"]
        assert [n["value"] for n in form["net"]] == ["cn2gia", "9929", "4837"] and not any(n["checked"] for n in form["net"])
        data = client.get("/internal/v1/zh/plans?scope=all&net=9929&net=cn2gia").json()
        assert data["seo"]["canonical"].endswith("/plans?scope=all&net=cn2gia&net=9929")  # 按词表顺序规范化
        assert {r["deal_href"] for r in data["rows"]} == {f"/deal/{vm6}"} and len(data["rows"]) == 2
        assert [n["value"] for n in data["form"]["net"] if n["checked"]] == ["cn2gia", "9929"]
        assert data["form"]["reset"] == "/plans?scope=all"
        only = client.get("/internal/v1/zh/plans?scope=all&net=4837").json()
        assert [r["net"] for r in only["rows"]] == [["9929", "4837"]]
        assert client.get("/internal/v1/zh/plans?scope=all&net=nonsense").json()["form"]["reset"] is None  # 不认识的标签丢掉
        api = client.get("/api/plans?scope=all&net=cn2gia").json()
        assert [p["net"] for p in api["items"]] == [["cn2gia"]]
    assert PlanQuery.parse({"net": ["9929", "cn2gia", "x"]}).net == ("cn2gia", "9929")
    assert PlanQuery.parse({"net": ["cn2gia"]}).href() == "/plans?net=cn2gia"


def test_ended_deals_are_hidden_by_default_and_sink_when_included(site, tmp_path):
    """有效期（O-15）：原帖写明的截止日期过了（宽限一天）算已结束——首页默认不列，勾"含已结束"后排在最后；
    剩 3 天以内标 soon；含往季（scope=all）不处理；接口默认也不含，加 ended=1 才有；没写截止日期的照常显示。"""
    _, conn, _ = site
    first = list_deals(conn, DealQuery(scope="promo"), SEASON, page_size=12).deals  # 按"最新"排
    open_ = next(d.id for d in first if d.min_price is not None)  # 详情页的 JSON-LD 要有价格
    ended, grace, soon = [d.id for d in first if d.id != open_ and d.ends_at is None][:3]
    with copied_site(site, tmp_path) as (path, client):
        write(path, "UPDATE deals SET ends_at = '2026-11-01', ends_source = 'rule' WHERE id = ?", (ended,))  # 今天 11-03：过了两天
        write(path, "UPDATE deals SET ends_at = '2026-11-02', ends_source = 'rule' WHERE id = ?", (grace,))  # 宽限一天内
        write(path, "UPDATE deals SET ends_at = '2026-11-05', ends_source = 'llm' WHERE id = ?", (soon,))
        write(path, "UPDATE deals SET ends_at = '2026-12-02', ends_source = 'rule' WHERE id = ?", (open_,))

        def listing(query: str, lang: str = "zh") -> list[dict]:  # 所有页的卡片，按显示顺序
            cards, page = [], 1
            while True:
                data = client.get(f"/internal/v1/{lang}/home?{query}&page={page}").json()
                cards += data["deals"]
                if not data["pager"]["next"]:
                    return cards
                page += 1

        default = listing("scope=promo")
        shown = {c["id"]: c["expiry"] for c in default}
        assert ended not in shown and not any(c["expiry"] and c["expiry"]["state"] == "ended" for c in default)
        assert shown[grace] == {"state": "soon", "text": "截止 11-02（即将结束）", "date": "2026-11-02"}
        assert shown[soon] == {"state": "soon", "text": "截止 11-05（还剩 2 天）", "date": "2026-11-05"}
        assert shown[open_] == {"state": "open", "text": "截止 12-02（还剩 29 天）", "date": "2026-12-02"}
        assert client.get("/internal/v1/zh/home?scope=promo").json()["form"]["ended"] is False
        with_ended = listing("scope=promo&ended=1")
        states = [c["expiry"] and c["expiry"]["state"] for c in with_ended]
        n_ended = states.count("ended")
        assert n_ended >= 1 and states[-n_ended:] == ["ended"] * n_ended and ended in {c["id"] for c in with_ended[-n_ended:]}
        assert [c["id"] for c in with_ended[:-n_ended]] == [c["id"] for c in default]  # 其余顺序不变，已结束的全在末尾
        flagged = client.get("/internal/v1/zh/home?scope=promo&ended=1").json()
        assert flagged["form"]["ended"] is True and flagged["form"]["reset"] == "/?scope=promo"
        assert flagged["seo"]["canonical"].endswith("/?scope=promo&ended=1")
        assert client.get("/internal/v1/zh/home?scope=all").json()["form"]["ended"] is None  # 含往季不处理
        english = {c["id"]: c["expiry"]["text"] for c in listing("scope=promo&ended=1", "en") if c["expiry"]}
        assert (english[soon], english[open_], english[ended]) == ("Ends Nov 5 (2 days left)", "Ends Dec 2 (29 days left)", "Ended")
        # 接口：默认不含，ended=1 才有；带截止日期与来源
        api = {d["id"]: d for d in client.get("/api/deals?scope=promo&page_size=100").json()["items"]}
        assert ended not in api and api[soon]["ends_at"] == "2026-11-05" and api[soon]["ends_source"] == "llm"
        assert ended in {d["id"] for d in client.get("/api/deals?scope=promo&ended=1&page_size=100").json()["items"]}
        # 详情页：截止日期与 JSON-LD 的 priceValidUntil
        detail = client.get(f"/internal/v1/zh/deal/{open_}").json()
        assert detail["facts"]["expiry"]["text"] == "截止 12-02（还剩 29 天）"
        product = next(i for i in detail["seo"]["jsonld"] if i["@type"] == "Product")
        assert product["offers"]["priceValidUntil"] == "2026-12-02"
        assert client.get(f"/internal/v1/zh/deal/{ended}").json()["facts"]["expiry"]["state"] == "ended"


def test_just_listed_skips_one_time_prices(site, tmp_path):
    """"刚刚上架"不放起价是一次性付款的优惠（O-04）。"""
    _, conn, client = site
    newest = client.get("/internal/v1/zh/home").json()["just"][0]["id"]
    with copied_site(site, tmp_path) as (path, other):
        write(path, "UPDATE deals SET min_price_period = 'onetime' WHERE id = ?", (newest,))
        just = other.get("/internal/v1/zh/home").json()["just"]
        assert len(just) == 8 and newest not in [x["id"] for x in just]


def test_deal_facts_codes_discount_note_and_posted(site, tmp_path):
    """详情页：没有码时不出"优惠码"一格；折扣说明与页面不同语言时标"原文："（O-06）。卡片的发布时间写相对时间或日期。"""
    _, conn, _ = site
    vm6 = deal_id(conn, VM6)
    with copied_site(site, tmp_path) as (path, client):
        write(path, "UPDATE deals SET coupon_codes_json = '[]', discount_text = 'Up to 60% off' WHERE id = ?", (vm6,))
        zh = client.get(f"/internal/v1/zh/deal/{vm6}").json()["facts"]
        assert zh["codes"] == [] and zh["discount_note"] == "原文：Up to 60% off" and "（" not in zh["discount"]
        en = client.get(f"/internal/v1/en/deal/{vm6}").json()["facts"]
        assert en["discount_note"] == "Up to 60% off" and zh["seen"]
    now = datetime(2026, 11, 5, 12, tzinfo=UTC)
    zh_view, en_view = View("zh", "Asia/Shanghai", now), View("en", "UTC", now)
    assert zh_view.posted("2026-11-03T12:00:00Z") == "2 天前发布" and en_view.posted("2026-11-03T12:00:00Z") == "Posted 2 d ago"
    assert zh_view.posted("2026-10-20T12:00:00Z") == "10-20 发布" and en_view.posted("2026-10-20T12:00:00Z") == "Posted 10-20"
    assert zh_view.posted("2026-11-05T09:00:00Z") == "3 小时前发布" and zh_view.posted(None) == ""


def test_status_shows_launch_settings(site, tmp_path):
    """状态页列出上线前还没设的配置（O-05）；都设了写"必填项都已设置"。"""
    _, _, client = site
    facts = {f["label"]: f["value"] for f in client.get("/internal/v1/zh/status").json()["facts"]}
    assert "SITE_URL 没设或是本机地址" in facts["上线配置"] and "CONTACT_EMAIL 没设" in facts["上线配置"]
    env = {"SITE_URL": "https://bfradar.example", "CONTACT_EMAIL": "hi@bfradar.example", "REFRESH_TOKEN": "x" * 32}
    with copied_site(site, tmp_path, **env) as (_, other):
        facts = {f["label"]: f["value"] for f in other.get("/internal/v1/en/status").json()["facts"]}
        assert facts["Launch settings"] == "All required settings are set"


# ---------------------------------------------------------------- 优化方案第二组（O-07 至 O-14，D67）


def test_offer_chips_have_kinds_and_codes_are_separate(site, tmp_path):
    """卡片标签带含义（O-08）；优惠码单列、原样给出，页面加"码"字样与复制按钮（O-07），卡片上最多 3 个。"""
    _, conn, _ = site
    vm6 = deal_id(conn, VM6)
    with copied_site(site, tmp_path) as (path, client):
        write(path, "UPDATE deals SET discount_pct = 60, recurring = 'first_term',"
                    " coupon_codes_json = '[\"A1\", \"B2\", \"C3\", \"D4\"]' WHERE id = ?", (vm6,))
        write(path, "UPDATE plans SET coupon_code = 'PLAN9' WHERE id = (SELECT MIN(id) FROM plans WHERE deal_id = ?)",
              (vm6,))  # 页面数据有缓存：先改完再取
        cards = {d["id"]: d for n in (1, 2, 3, 4, 5) for d in
                 client.get(f"/internal/v1/zh/home?scope=all&page={n}").json()["deals"]}
        card = cards[vm6]
        assert card["offer"] == [{"kind": "discount", "text": "最高 60%"}, {"kind": "rec-first_term", "text": "仅首期"}]
        assert card["codes"] == ["A1", "B2", "C3"] and card["codes_more"] == "共 4 个码，见详情"
        assert {c["kind"] for d in cards.values() for c in d["offer"]} <= {
            "discount", "rec-recurring", "rec-first_term", "rec-mixed", "rec-unknown"}
        detail = client.get(f"/internal/v1/zh/deal/{vm6}").json()
        assert detail["facts"]["codes"] == ["A1", "B2", "C3", "D4"]
        assert "PLAN9" in [p["code"] for p in detail["plans"]]  # 原样，不带"码"字样


def test_recent_activity_lists_new_deals_and_changes(site, tmp_path):
    """"24 小时动态"（O-10）：24 小时内新上架的与降价、新码的优惠，按时间倒序；不足 3 条时用本季较早的补满 8 条。"""
    _, conn, _ = site
    vm6 = deal_id(conn, VM6)
    now = datetime.now(UTC)
    recent = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    later = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with copied_site(site, tmp_path) as (path, client):
        other = next(i for (i,) in conn.execute("SELECT id FROM deals WHERE published_at >= ? AND id != ?"
                                                " AND COALESCE(min_price_period, '') != 'onetime'"
                                                " ORDER BY published_at", (SEASON, vm6)))  # 本季最早的一条
        write(path, "UPDATE deals SET published_at = ? WHERE id = ?", (recent, other))
        write(path, "INSERT INTO changes (deal_id, changed_at, field, old_value, new_value) VALUES (?, ?, 'plan_price',"
                    " ?, ?)", (vm6, later, json.dumps({"ram_gb": 2, "price": 20, "currency": "GBP", "period": "year"}),
                               json.dumps({"ram_gb": 2, "price": 15, "currency": "GBP", "period": "year"})))
        just = client.get("/internal/v1/zh/home").json()["just"]
        assert [(x["id"], x["kind"], x["kind_label"]) for x in just[:2]] == [(vm6, "price_drop", "降价"),
                                                                             (other, "new", "新")]
        assert len(just) == 8 and {x["kind"] for x in just[2:]} == {"earlier"} and just[2]["kind_label"] == "较早"
        assert just[0]["ago"] == "1 小时前" and just[0]["at"] == later
        en = client.get("/internal/v1/en/home").json()["just"]
        assert [x["kind_label"] for x in en[:3]] == ["Price drop", "New", "Earlier"]


def test_plans_filters_by_product_search_and_region(site, tmp_path):
    """套餐视图（O-13）：产品类型、商家与套餐名搜索、地区（O-12）；机房按规范名筛，旧写法也认。"""
    _, conn, client = site
    data = client.get("/internal/v1/zh/plans?scope=all&q=vm6&product=vps&region=eu").json()
    assert data["seo"]["canonical"].endswith("/plans?scope=all&product=vps&q=vm6&region=eu")
    assert data["rows"] and {r["vendor"] for r in data["rows"]} == {"VM6"}
    assert data["form"]["q"] == "vm6" and [p["value"] for p in data["form"]["products"] if p["checked"]] == ["vps"]
    assert [o["value"] for o in data["form"]["region"] if o["selected"]] == ["eu"]
    assert data["form"]["reset"] == "/plans?scope=all"
    assert all(o["value"] not in ("Los Angeles", "Dallas") for o in data["form"]["location"])  # 只列欧洲的机房
    every = PlanQuery.parse({"product": list(PRODUCTS)})
    assert every.products == () and PlanQuery.parse({"region": ["mars"]}).region == ""
    assert PlanQuery.parse({"location": ["Frankfurt, Germany"]}).location == "Frankfurt"
    vm6 = deal_id(conn, VM6)
    with copied_site(site, tmp_path) as (path, other):
        write(path, "UPDATE plans SET locations_json = '[\"London, UK\", \"NYC\", \"Tokyo\", \"Gotham\"]'"
                    " WHERE deal_id = ? AND position = 0", (vm6,))
        rows = other.get("/internal/v1/zh/plans?scope=all&location=London").json()["rows"]
        assert any(r["loc"] == "伦敦、纽约 +2" and r["loc_title"] == "伦敦、纽约、东京、Gotham" for r in rows)
        facts = {f["label"]: f["value"] for f in other.get("/internal/v1/zh/status").json()["facts"]}
        assert re.fullmatch(r"本季套餐里 \d+ 种写法，1 种认不出", facts["机房写法"])


def test_pager_has_page_numbers():
    assert internal._page_numbers(1, 1) == [1]
    assert internal._page_numbers(1, 14) == [1, 2, 3, None, 14]
    assert internal._page_numbers(7, 14) == [1, None, 5, 6, 7, 8, 9, None, 14]
    assert internal._page_numbers(13, 14) == [1, None, 11, 12, 13, 14]
    assert internal._page_numbers(4, 6) == [1, 2, 3, 4, 5, 6]


def test_pager_data(site):
    _, _, client = site
    pager = client.get("/internal/v1/zh/home?scope=all&page=2").json()["pager"]
    assert [(p["n"], p["current"]) for p in pager["pages"] if p] == [(1, False), (2, True), (3, False), (4, False),
                                                                      (5, False)]
    assert pager["pages"][0]["href"] == "/?scope=all" and pager["pages"][2]["href"] == "/?scope=all&page=3"


def test_community_titles_follow_the_page_language():
    """社区讨论标题按页面语言取机器译名（O-14），没有译名时显示原标题。"""
    now = datetime(2026, 11, 20, tzinfo=UTC)
    rows = [{"title": "黑五买什么", "title_zh": None, "title_en": "What to buy on Black Friday", "url": "https://x.test/1",
             "published_at": "2026-11-19T00:00:00Z"},
            {"title": "Best BF deal?", "title_zh": "最好的黑五优惠？", "title_en": None, "url": "https://x.test/2",
             "published_at": "2026-11-19T00:00:00Z"},
            {"title": "没有译名", "title_zh": None, "title_en": None, "url": "https://x.test/3",
             "published_at": "2026-11-19T00:00:00Z"}]
    en = [x["title"] for x in internal._intel(View("en", "UTC", now), rows)]
    zh = [x["title"] for x in internal._intel(View("zh", "Asia/Shanghai", now), rows)]
    assert en == ["What to buy on Black Friday", "Best BF deal?", "没有译名"]
    assert zh == ["黑五买什么", "最好的黑五优惠？", "没有译名"]


def test_free_offer_display():
    """免费套餐（D55）：起价与套餐价格写"免费"，不附人民币估算；RSS 写"免费"。"""
    from app.models import Deal, Plan
    from app.web.feed import item_html

    v = View("zh", "Asia/Shanghai", datetime(2026, 11, 20, tzinfo=UTC))
    plan = Plan(name="Free NVMe Shared Hosting - USA", disk_gb=1, price=0.0, price_cny_year=0.0)
    d = Deal(url="https://lowendspirit.com/discussion/1", source="lowendspirit", title="Free Web Hosting",
             vendor="Dewlance", min_price=0.0, min_price_cny_year=0.0, plans=[plan])
    assert v.start(d) == ("免费", "") and (v.plan_price(plan), v.plan_cny(plan)) == ("免费", "")
    assert "<p>免费</p>" in item_html(d, v, "https://site/deal/1") and "起价" not in item_html(d, v, "https://site/deal/1")


# ---------------------------------------------------------------- 英文版（M3.1–M3.3）

EN_DATA = ["/internal/v1/en/" + p for p in ("chrome", "home", "home?scope=all&sort=price", "home?scope=all&page=2",
                                             "plans", "plans?scope=all&ram_min=2", "deal/{vm6}", "vendor/vm6", "status",
                                             "about")]
LINK_KEYS = {"href", "vendor_href", "deal_href", "action", "reset", "prev", "next"}  # 站内链接


def visible_text(html: str) -> str:
    from html import unescape

    return unescape(re.sub(r"<[^>]+>", " ", html))


def test_dictionaries_match():
    """英文字典与中文字典键一一对应（机房译名 loc.* 只有中文），占位符相同；英文值里只有站名与"中文"两处中文。"""
    zh, en = strings("zh"), strings("en")
    assert set(en) == {k for k in zh if not k.startswith("loc.")}
    assert {k: sorted(re.findall(r"\{(\w+)\}", en[k])) for k in en} == {
        k: sorted(re.findall(r"\{(\w+)\}", zh[k])) for k in en}
    assert sorted(k for k, value in en.items() if CJK.search(value)) == ["footer.names", "lang.zh"]


def test_dictionary_placeholders_are_plain_names():
    """占位符只用 {name}：Python 用 str.format 填，前端 t() 只认 {name}，其他写法（{0}、{x:.2f}、{{）两边填出来不一样。"""
    for lang in ("zh", "en"):
        odd = {k: v for k, v in strings(lang).items() if re.search(r"[{}]", re.sub(r"\{[a-z_][a-z0-9_]*\}", "", v))}
        assert odd == {}, lang


def test_frontend_has_no_chinese_literals():
    """界面文字一律走字典（中英两套）：React 前端源码里除注释与开发日志（console.*）外不出现中文。"""
    hits = []
    for path, source in frontend_sources().items():
        code = re.sub(r"/\*.*?\*/", lambda m: "\n" * m[0].count("\n"), source, flags=re.S)  # 保留行号
        for n, line in enumerate(code.splitlines(), 1):
            line = re.sub(r"(?<![:\"'`])//.*", "", line).strip()  # 行尾注释；网址里的 // 前面是冒号
            if CJK.search(line) and not line.startswith("console."):
                hits.append(f"{path.relative_to(FRONTEND)}:{n}: {line}")
    assert hits == []


def texts(value, key=None):
    """JSON 里的全部（键, 字符串）。"""
    if isinstance(value, dict):
        return [x for k, v in value.items() for x in texts(v, k)]
    if isinstance(value, list):
        return [x for v in value for x in texts(v, key)]
    return [(key, value)] if isinstance(value, str) else []


def test_english_data_has_no_chinese(site):
    """FR-17：英文页的数据里，中文只能来自内容本身与页脚的中文站名；站内链接都带 /en（hreflang 的中文版除外）。"""
    _, conn, client = site
    allowed = data_cjk(conn) | {"黑五雷达", "中文"}
    vm6 = deal_id(conn, VM6)
    for path in EN_DATA:
        r = client.get(path.format(vm6=vm6))
        assert r.status_code == 200, path
        found = texts(r.json())
        assert [x for _, s in found for x in CJK.findall(s) if x not in allowed] == [], path
        links = [s for k, s in found if k in LINK_KEYS and s]
        assert [x for x in links if not x.startswith("/en/")] == [], path


def test_english_content_and_times(site):
    _, conn, client = site
    price, cny = client.get(f"/internal/v1/en/deal/{deal_id(conn, VM6)}").json()["facts"]["price"]
    assert re.search(r"/yr|/mo", price) and cny.startswith("≈ ¥")  # 英文页同样写原币种与人民币估算（"≈ ¥156/yr"）
    chrome = client.get("/internal/v1/en/chrome").json()
    assert re.search(r"Updated \d\d:\d\d UTC|Updated \d\d-\d\d \d\d:\d\d UTC", chrome["freshness"])
    assert "All times are UTC" in chrome["footer"]
    assert re.search(r"\d+ days \d+ hours to Black Friday|Black Friday / Cyber Monday is on|Black Friday 2026 is over",
                     chrome["countdown"]["text"])


def test_canonical_and_hreflang_keep_filters(site):
    """canonical 与语言切换（hreflang 的两个版本）都保留规范化后的筛选参数，多余参数不进链接。"""
    settings, _, client = site
    zh = client.get("/internal/v1/zh/plans?ram_min=2&period=year&_=junk").json()["seo"]
    assert zh["canonical"] == f"{settings.site_url}/plans?ram_min=2&period=year"
    assert zh["alternates"] == {"zh": "/plans?ram_min=2&period=year", "en": "/en/plans?ram_min=2&period=year"}
    en = client.get("/internal/v1/en/plans?ram_min=2&period=year").json()["seo"]
    assert en["canonical"] == f"{settings.site_url}/en/plans?ram_min=2&period=year"
    assert en["alternates"] == zh["alternates"]


def test_english_feed(site):
    settings, conn, client = site
    r = client.get("/en/feed.xml?scope=all")
    channel = ET.fromstring(r.content).find("channel")
    assert channel.findtext("language") == "en" and channel.findtext("title") == "BFRadar · All time"
    assert channel.findtext("link") == settings.site_url + "/en/?scope=all"
    assert channel.find("{http://www.w3.org/2005/Atom}link").get("href") == settings.site_url + "/en/feed.xml?scope=all"
    items = channel.findall("item")
    assert len(items) == 50 and all(re.fullmatch(re.escape(settings.site_url) + r"/en/deal/\d+", i.findtext("link"))
                                    for i in items)
    allowed = data_cjk(conn) | {"黑五雷达"}
    assert [x for i in items for x in CJK.findall(visible_text(i.findtext("description"))) if x not in allowed] == []
    assert not feedparser.parse(r.content).bozo
    assert client.get("/en/feed.xml").content != client.get("/feed.xml").content


def test_separators_follow_the_language():
    zh = View("zh", "Asia/Shanghai", datetime(2026, 11, 20, tzinfo=UTC))
    en = View("en", "UTC", datetime(2026, 11, 20, tzinfo=UTC))
    assert (zh.join(["A", "B"]), en.join(["A", "B"])) == ("A、B", "A, B")
    assert (zh.sentences("甲", "乙"), en.sentences("A", "B")) == ("甲。乙。", "A. B.")
    assert (zh.prefix, en.prefix, zh.html_lang, en.html_lang) == ("", "/en", "zh-CN", "en")
    assert en.locations(["Los Angeles", "Frankfurt"]) == "Los Angeles, Frankfurt"  # 英文页机房照原文
    assert en.change("coupon_codes", '["A"]', '["A", "B"]') == ("Coupon codes", "A", "A, B")
