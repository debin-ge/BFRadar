"""React 前端端到端（D60）：已构建的前端（node frontend/server.js，生产模式）接到真实的后端上，沿页面链接走遍全站，
逐页断言体积、元素数、耗时、缓存头、安全头、CSP nonce、JSON-LD 与不链隐藏页；单点检查 .data、跳转、404、静态文件与
旧版本归档；核对页面与后端接口的数据一致，查状态页、关于页与英文页无中文残留（M2 验收清单的页面部分）。前端也是站点
唯一的入口：后端的公开地址经它原样转发，内部页面数据转不出去，后端连不上时页面与转发都不缓存。
现场（真实 HTTP、快照与假 LLM）见 live.py。

前端要先构建（cd frontend && pnpm build），没构建或没有 node 时整个模块跳过；设 REQUIRE_WEB_E2E=1 时改为失败。
"""

import http.client
import json
import re
import socket
import sqlite3
from urllib.parse import urlsplit

import lxml.html
import pytest
from lxml import etree

from app.i18n import strings

from conftest import CJK, data_cjk
from live import (FRONTEND, HTML_LANG, PAGE_BUDGET, SITE, SLOW, SOURCES, SUMMARY, TITLE_SUFFIX, TOKEN,
                  all_deals, cache_rule, live_site, split_lang, wait_for, walk, web_site)

MAX_ELEMENTS = 3000  # 第 9 节
CSP = re.compile(r"default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self' 'nonce-([\w+/=]+)'; "
                 r"frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
IMMUTABLE = "public, max-age=31536000, immutable"
HIDDEN = ("/status", "/en/status", "/feed.xml", "/en/feed.xml", "/healthz")
HSTS = "max-age=31536000"
# 经前端转给后端的公开地址（frontend/forward.js）；/healthz 的内容带当前时间，只比状态码与响应头
FORWARDED = ("/api/deals?page_size=1", "/api/openapi.json", "/feed.xml", "/en/feed.xml?scope=all", "/sitemap.xml",
             "/robots.txt", "/og/zh/site.png", "/healthz")
TIMED = ("/healthz",)
LABELS = {"lowendtalk": "LET", "lowendspirit": "LES", "lowendbox": "LEB", "nodeseek": "NodeSeek", "reddit": "Reddit"}
VM6 = "https://lowendtalk.com/discussion/222765"


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_site(tmp_path_factory) as env:
        yield env


@pytest.fixture(scope="module")
def web(live, tmp_path_factory):
    with web_site(live.origin, tmp_path_factory.mktemp("web")) as site:
        yield site


def nonce(r) -> str:
    match = CSP.fullmatch(r.headers["content-security-policy"])
    assert match, r.headers["content-security-policy"]
    return match[1]


def check_page(path: str, r) -> None:
    """一页 HTML 的全部要求：没有内联样式；没有 src 的脚本要么带本次响应的 nonce，要么是 JSON-LD 数据块。"""
    lang = split_lang(path)[0]
    assert r.headers["content-type"] == "text/html; charset=utf-8", path
    assert (r.headers["x-frame-options"], r.headers["x-content-type-options"]) == ("DENY", "nosniff"), path
    assert "x-powered-by" not in r.headers and r.headers["strict-transport-security"] == HSTS, path
    assert r.headers["cache-control"] == cache_rule(path), path
    assert len(r.content) <= PAGE_BUDGET, (path, len(r.content))
    assert r.elapsed.total_seconds() < SLOW, (path, r.elapsed)
    doc = lxml.html.fromstring(r.content)
    assert sum(1 for _ in doc.iter(etree.Element)) <= MAX_ELEMENTS, path
    title, suffix = doc.findtext("head/title"), TITLE_SUFFIX[lang]
    assert doc.get("lang") == HTML_LANG[lang] and title.endswith(suffix) and title != suffix, path
    assert doc.xpath("//@style | //style") == [], path
    n = nonce(r)
    scripts = [s for s in doc.iter("script") if s.get("src") is None and s.get("type") != "application/ld+json"]
    assert scripts and all(s.get("nonce") == n for s in scripts), path
    for ld in doc.xpath("//script[@type='application/ld+json']"):  # 抓取来的标题（带 < 的也有）关不掉数据块
        json.loads(ld.text)
    assert doc.xpath("//a[@href=$href]", href=("/en" if lang == "en" else "") + "/about"), path


# ---------------------------------------------------------------- 走遍全站


def test_every_linked_page_works(web):
    pages = walk(web)
    assert {p: r.status_code for p, r in pages.items() if r.status_code != 200} == {}  # 站内没有坏链接
    html = {p: r for p, r in pages.items() if r.headers["content-type"].startswith("text/html")}
    assert len(html) > 600 and sum(split_lang(p)[0] == "en" for p in html) * 2 == len(html)  # 中英文页各一半
    for path, r in html.items():
        check_page(path, r)
    assert [p for p in pages if p.split("?")[0] in HIDDEN or p.startswith(("/api/", "/internal/"))] == []  # D59
    assets = [p for p in pages if p.startswith("/assets/")]
    assert {p.rsplit(".", 1)[-1] for p in assets} == {"css", "js"}
    assert [p for p in assets if pages[p].headers["cache-control"] != IMMUTABLE] == []


def test_walk_reaches_every_deal_and_vendor(live, web):
    pages = walk(web)
    ids, slugs = all_deals(live)
    for prefix in ("", "/en"):  # 中英文各自都能翻页走到每一条优惠、每个有优惠的商家
        assert {int(m[1]) for p in pages if (m := re.fullmatch(prefix + r"/deal/(\d+)", p))} == ids, prefix
        listed = {m[1] for p in pages if (m := re.fullmatch(prefix + r"/vendor/([\w-]+)", p))}
        assert slugs <= listed, prefix  # 商家目录还会带出只在社区讨论里出现的商家（O-17），所以是包含而不是相等
        assert f"{prefix}/vendors" in pages, prefix


def test_nonce_is_new_for_every_response(web):
    first, second = web.get("/"), web.get("/")
    assert nonce(first) != nonce(second) and f'nonce="{nonce(second)}"' in second.text


# ---------------------------------------------------------------- 单点


def test_data_requests_redirects_and_not_found(live, web):
    vm6 = next(d["id"] for d in live.json("/api/deals?scope=all&q=VM6")["items"] if d["url"] == VM6)
    for page, data in (("/", "/_.data"), ("/en/", "/en/_.data"), ("/plans", "/plans.data"),
                       (f"/deal/{vm6}", f"/deal/{vm6}.data"), ("/en/vendor/vm6", "/en/vendor/vm6.data")):
        r = web.get(data)  # 单页导航取数据，缓存头与对应页面相同
        assert (r.status_code, r.headers["content-type"]) == (200, "text/x-script; charset=utf-8"), data
        assert r.headers["cache-control"] == cache_rule(page), data
    for path, target in (("/en", "/en/"), ("/plans/", "/plans"), ("/plans/?ram_min=2", "/plans?ram_min=2"),
                         ("/en/about/", "/en/about")):
        r = web.get(path)
        assert (r.status_code, r.headers["location"]) == (307, target), path
    for path in ("/nope", "/deal/abc", "/deal/999999", "/vendor/nope", "/en/deal/999999", "/deal/999999.data"):
        r = web.get(path)
        assert (r.status_code, r.headers["cache-control"]) == (404, "public, max-age=60"), path
        if not path.endswith(".data"):
            text = lxml.html.fromstring(r.content).text_content()
            assert strings(split_lang(path)[0])["notfound.text"] in text, path
    head = web.http.head("/")
    assert head.status_code == 200 and head.content == b""
    assert web.get("/_health").text == "ok"


def test_assets_and_archive_of_old_builds(web):
    """本版本的文件已归档；一天前被替换的旧文件还能取到，八天前的已删掉；归档记录不对外。"""
    current = sorted(p.name for p in (FRONTEND / "build" / "client" / "assets").iterdir())
    assert set(current) <= {p.name for p in web.archive.iterdir()}
    builds = json.loads((web.archive / ".builds.json").read_text())
    assert set(builds) == {next(n for n in current if n.startswith("manifest-")), "manifest-old.js"}
    for name in (current[0], "old-chunk.js"):
        r = web.get(f"/assets/{name}")
        assert (r.status_code, r.headers["cache-control"]) == (200, IMMUTABLE), name
    assert not (web.archive / "gone-chunk.js").exists()
    for name in ("gone-chunk.js", "nope.js", ".builds.json"):  # 缺的文件 404 且不缓存（发布中可能只是暂时没有）
        r = web.get(f"/assets/{name}")
        assert (r.status_code, r.headers["cache-control"]) == (404, "no-store"), name


def test_pages_match_the_api(live, web):
    """验收 3–7：首页按商家名与优惠码搜得到；套餐筛选的总数与接口一致；详情页带 LLM 的中文标题与摘要、原帖链接、
    结构化数据与分享图；商家页列出这条优惠。"""
    item = next(d for d in live.json("/api/deals?scope=all&q=VM6")["items"] if d["url"] == VM6)
    for q in ("VM6", "bfvps55"):
        assert f'href="/deal/{item["id"]}"' in web.get(f"/?scope=all&q={q}").text, q
    total = live.json("/api/plans?ram_min=2&period=year")["total"]
    assert f"共 {total} 个套餐" in web.get("/plans?ram_min=2&period=year").text
    home = lxml.html.fromstring(web.get("/?scope=all&q=VM6").content)
    # 卡片标题先进本站详情页（原帖链接在详情页里），不直接跳到原帖
    assert home.xpath("//li[@id=$id]//h3/a/@href", id=f"d{item['id']}") == [f"/deal/{item['id']}"]
    assert not home.xpath("//li[@id=$id]//a[@target='_blank' and not(ancestor::table)]", id=f"d{item['id']}")
    page = lxml.html.fromstring(web.get(f"/deal/{item['id']}").content)
    assert page.findtext(".//h1").startswith("【译】") and SUMMARY in page.text_content()
    assert page.xpath("//a[@href=$url]", url=item["url"])
    assert [json.loads(x.text)["@type"] for x in page.xpath("//script[@type='application/ld+json']")] == [
        "Product", "BreadcrumbList"]
    assert page.xpath("//meta[@property='og:image']/@content")[0].startswith(f"{SITE}/og/zh/deal/{item['id']}.png?v=")
    vendor = lxml.html.fromstring(web.get(f"/vendor/{item['vendor_slug']}").content)
    assert re.search(r"本季优惠（\d+ 条）", vendor.text_content()) and vendor.xpath("//a[@href=$href]",
                                                                                 href=f"/deal/{item['id']}")
    english = lxml.html.fromstring(web.get(f"/en/deal/{item['id']}").content)
    assert not english.findtext(".//h1").startswith("【译】")  # 英文页用原标题


def test_countdown_block_is_on_the_home_page_only(web):
    """倒计时是首页上单独一块的大号读数（天、时、分、秒四格），页头不再有；其他页面没有。"""
    home = lxml.html.fromstring(web.get("/").content)
    block = home.xpath("//section[contains(@class, 'countdown-block')]")
    assert len(block) == 1 and len(block[0].xpath(".//span[contains(@class, 'cd-cell')]")) in (0, 4)
    assert not home.xpath("//header//*[contains(@class, 'countdown')]")
    for path in ("/plans", "/about", "/en/plans"):
        assert not lxml.html.fromstring(web.get(path).content).xpath("//*[contains(@class, 'countdown')]"), path
    english = lxml.html.fromstring(web.get("/en/").content)
    assert english.xpath("//section[contains(@class, 'countdown-block')]//h2")


def test_status_page(web):
    """验收 8：状态页不在任何页面上链接（D59），每次现算、不缓存；运行中，各源一节、没有失败的轮次，LLM 队列也在。"""
    for path in ("/status", "/en/status"):
        check_page(path, web.get(path))
    assert web.get("/status.data").headers["cache-control"] == "no-store"
    page = lxml.html.fromstring(web.get("/status").content)
    text = page.text_content()
    assert "运行中" in text and "ECB 2026-11-02" in text and "NodeSeek 覆盖率" in text
    assert [h.text_content() for h in page.xpath("//section/h2")] == [*(LABELS[s] for s in SOURCES), "LLM 队列"]
    assert page.xpath("//tr[@class='bad']") == [] and "连续失败" not in text


def test_about_page(web):
    """验收 9：关于页有免责说明；没有"更正、移除与联系"一节，也不放邮箱链接（D69）；
    "数据从哪里来""多久更新一次"两节按用户要求去掉（2026-11-05），页面上不再写采集程序的 User-Agent。"""
    page = lxml.html.fromstring(web.get("/about").content)
    text = page.text_content()
    assert "不构成购买建议" in text and "更正、移除与联系" not in text
    assert [w for w in ("数据从哪里来", "多久更新一次", "BFRadar/0.1", "robots.txt") if w in text] == []
    assert not page.xpath("//a[starts-with(@href, 'mailto:')]")


def test_english_pages_have_no_chinese_ui(live, web):
    """FR-17：英文页上的中文只能来自内容本身、页脚的中文站名与语言切换里的"中文"。"""
    with sqlite3.connect(live.settings.db_path) as conn:
        allowed = data_cjk(conn) | {"黑五雷达", "中文"}
    pages = {p: r for p, r in walk(web).items()
             if split_lang(p)[0] == "en" and r.headers["content-type"].startswith("text/html")}
    pages["/en/status"] = web.get("/en/status")
    for path, r in pages.items():
        body = lxml.html.fromstring(r.content).find("body")
        for script in body.findall(".//script"):  # 水合数据不显示
            script.drop_tree()
        assert [x for x in CJK.findall(body.text_content()) if x not in allowed] == [], path


# ---------------------------------------------------------------- 唯一的入口


def raw_get(web, path: str) -> int:
    """按原样发出请求行（httpx 会先把 ../ 规范掉），返回状态码。"""
    url = urlsplit(str(web.http.base_url))
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    try:
        conn.request("GET", path)
        return conn.getresponse().status
    finally:
        conn.close()


def test_backend_paths_are_forwarded(live, web):
    """后端的公开地址经前端原样转发：状态码、类型、缓存头、CSP 与内容都与直接访问后端相同，另加 HSTS。"""
    for path in FORWARDED:
        mine, direct = web.get(path), live.get(path)
        assert mine.status_code == direct.status_code == 200, path
        for name in ("content-type", "cache-control", "content-security-policy"):
            assert mine.headers.get(name) == direct.headers.get(name), (path, name)
        assert path.startswith(TIMED) or mine.content == direct.content, path
        assert mine.headers["strict-transport-security"] == HSTS, path
    head = web.http.head("/healthz")
    assert head.status_code == 200 and head.content == b""


def test_internal_data_is_not_reachable(web):
    """内部页面数据只给前端走内网：直接访问、借 ../ 或编码过的 ../ 绕道，都是 404，拿不到 JSON。"""
    for path in ("/internal/v1/zh/home", "/api/../internal/v1/zh/home", "/api/%2e%2e/internal/v1/zh/home",
                 "/static/%2E%2E/internal/v1/zh/chrome", "/api/..%2finternal/v1/zh/home", "//internal/v1/zh/home"):
        assert raw_get(web, path) == 404, path


def test_backend_down_is_not_cached(tmp_path):
    """后端连不上时：页面 503、转发的地址 502，都不缓存；前端自己照常响应，日志里记下原因。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]  # 刚释放的端口，没有人在听
    with web_site(f"http://127.0.0.1:{closed}", tmp_path, quiet=False) as down:
        page, api = down.get("/"), down.get("/api/deals")
        assert (page.status_code, page.headers["cache-control"]) == (503, "no-store")
        assert (api.status_code, api.headers["cache-control"]) == (502, "no-store")
        assert down.get("/_health").text == "ok"
    assert any("后端不可用" in line for line in down.stderr)


def test_refresh_through_the_front(live, web):
    """写接口也经前端转发：方法与令牌头原样到达后端。放在后面：触发的这一轮采集跑完再往下。"""
    assert web.http.post("/api/refresh").status_code == 401
    r = web.http.post("/api/refresh", headers={"X-Refresh-Token": TOKEN})
    assert r.status_code == 200 and r.json()["run_id"] > 0
    wait_for(lambda: not live.json("/internal/v1/zh/status")["refreshing"], "手动刷新跑完")


def test_no_errors_logged(live, web):
    walk(web)
    assert live.errors.lines() == [] and web.stderr == []
