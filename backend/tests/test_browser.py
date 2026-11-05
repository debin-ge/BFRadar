"""浏览器检查（D60）：Playwright 驱动本机安装的 Chrome（channel="chrome"，不另下载浏览器），对着已构建的 React 前端。
开脚本：水合无报错、无 CSP 违规，筛选一改即提交、不整页刷新，客户端导航，主题选择记住且首帧前生效；
关脚本：筛选、分页、展开套餐照常可用；375 px 宽不出横向滚动、套餐表变卡片，深色模式跟随系统。现场见 live.py。

前端要先构建（cd frontend && pnpm build）；没构建、没有 node 或本机没有 Chrome 时整个模块跳过，设 REQUIRE_WEB_E2E=1 时改为失败。
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from playwright.sync_api import Error, Page, expect, sync_playwright

from live import live_site, require, web_site

VM6 = "https://lowendtalk.com/discussion/222765"
PHONE = {"width": 375, "height": 812}
# 首帧前的主题：<body> 出现那一刻 html[data-theme] 已经是访客的选择（<head> 里带 nonce 的内联脚本写的），不会先白后黑
THEME_AT_BODY = """new MutationObserver((_, observer) => {
  if (!document.body) return;
  window.__themeAtBody = document.documentElement.getAttribute("data-theme");
  observer.disconnect();
}).observe(document, { childList: true, subtree: true });"""


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_site(tmp_path_factory) as env:
        yield env


@pytest.fixture(scope="module")
def web(live, tmp_path_factory):
    with web_site(live.origin, tmp_path_factory.mktemp("web")) as site:
        yield site


@pytest.fixture(scope="module")
def browser(web):
    with sync_playwright() as p:
        try:
            chrome = p.chromium.launch(channel="chrome")
        except Error as e:
            require(f"本机没有可用的 Chrome：{e.message.splitlines()[0]}")
        yield chrome
        chrome.close()


@pytest.fixture(scope="module")
def vm6(live) -> int:
    return next(d["id"] for d in live.json("/api/deals?scope=all&q=VM6")["items"] if d["url"] == VM6)


@contextmanager
def opened(browser, web, path: str, **options) -> Iterator[tuple[Page, list[str]]]:
    """新的浏览器上下文里打开 path；收集控制台的错误、警告与页面异常（CSP 拦下的脚本或样式、水合不一致都会出现在这里）。"""
    context = browser.new_context(base_url=str(web.http.base_url), **options)
    page = context.new_page()
    problems: list[str] = []
    page.on("console", lambda m: problems.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    try:
        page.goto(path)
        yield page, problems
    finally:
        context.close()


def hydrated(page: Page) -> None:
    page.wait_for_selector("html[data-hydrated]", state="attached")


def documents(page: Page) -> list[str]:
    """之后整页加载（文档请求）的地址；单页导航只取 .data，这里应一直为空。"""
    urls: list[str] = []
    page.on("request", lambda r: urls.append(r.url) if r.resource_type == "document" else None)
    return urls


def background(page: Page) -> tuple[int, ...]:
    color = page.evaluate("getComputedStyle(document.body).backgroundColor")
    return tuple(int(x) for x in re.findall(r"\d+", color)[:3])


# ---------------------------------------------------------------- 开脚本


@pytest.mark.parametrize("path", ["/", "/en/", "/plans?ram_min=2", "/en/plans", "/deal/{vm6}", "/en/vendor/vm6",
                                  "/vendors", "/en/vendors?sort=name", "/codes", "/en/codes", "/about", "/status", "/nope"])
def test_pages_hydrate_cleanly(browser, web, vm6, path):
    with opened(browser, web, path.format(vm6=vm6)) as (page, problems):
        hydrated(page)
        if path == "/nope":  # 404 页本身的状态码，浏览器会记一条
            problems = [p for p in problems if "status of 404" not in p]
        assert problems == []


def test_csp_blocks_injected_inline_script(browser, web):
    """CSP 真的生效：页面里后加的、不带 nonce 的内联脚本不执行。"""
    with opened(browser, web, "/") as (page, problems):
        hydrated(page)
        ran = page.evaluate("""() => {
          const s = document.createElement("script");
          s.textContent = "window.__injected = true";
          document.head.append(s);
          return window.__injected === true;
        }""")
        assert not ran and any("Content Security Policy" in p for p in problems)


def test_filters_submit_on_change(browser, live, web):
    """下拉与勾选一改就按新条件取数据（单页导航，地址里只留有值的参数），不用点"筛选"。"""
    total = live.json("/api/plans?ram_min=2")["total"]
    with opened(browser, web, "/plans") as (page, problems):
        hydrated(page)
        loads = documents(page)
        page.select_option("select[name=ram_min]", "2")
        expect(page).to_have_url(re.compile(r"/plans\?(.+&)?ram_min=2(&|$)"))
        expect(page.locator(".total")).to_have_text(f"共 {total} 个套餐")
        assert "cpu_min=" not in page.url and "price_max=" not in page.url
        page.goto("/?scope=all")
        hydrated(page)
        loads.clear()
        page.fill("input[name=q]", "VM6")  # 搜索框回车提交
        page.press("input[name=q]", "Enter")
        expect(page).to_have_url(re.compile(r"q=VM6"))
        expect(page.locator("li.deal").first).to_contain_text("VM6")
        page.uncheck("input[name=product][value=vps]")  # 默认全选；去掉一项，地址里列出其余各项
        expect(page).not_to_have_url(re.compile(r"product=vps"))
        assert "product=dedicated" in page.url and "q=VM6" in page.url
        assert loads == [] and problems == []


def test_client_side_navigation(browser, web):
    """详情、栏目、语言切换与后退都在页面内完成：只取 .data，不整页加载；<html lang> 与标题跟着变。"""
    with opened(browser, web, "/") as (page, problems):
        hydrated(page)
        loads = documents(page)
        link = page.locator("li.deal a[href^='/deal/']").first
        href = link.get_attribute("href")
        link.click()
        expect(page).to_have_url(re.compile(re.escape(href) + "$"))
        expect(page.locator("h1")).to_be_visible()
        page.locator("header a[href='/plans']").click()
        expect(page).to_have_url(re.compile(r"/plans$"))
        page.locator("header a[hreflang=en]").click()
        expect(page).to_have_url(re.compile(r"/en/plans$"))
        expect(page.locator("html")).to_have_attribute("lang", "en")
        expect(page).to_have_title(re.compile(r" · BFRadar · BlackFridayRadar$"))
        page.go_back()
        expect(page).to_have_url(re.compile(r"/plans$"))
        expect(page.locator("html")).to_have_attribute("lang", "zh-CN")
        assert loads == [] and problems == []


def test_countdown_block_ticks_every_second(browser, web):
    """首页倒计时是单独一块的大号读数（天、时、分、秒）：水合后秒位每秒走字，首次渲染与服务端一致、没有水合错误。"""
    with opened(browser, web, "/") as (page, problems):
        hydrated(page)
        if page.locator(".countdown-block[data-phase=after]").count():
            pytest.skip("黑五已过，倒计时区块不再走字")
        cells = page.locator(".countdown-block .cd-cell b")
        expect(cells).to_have_count(4)
        assert all(len(t) >= 2 for t in cells.all_inner_texts())
        seconds = cells.last
        before = seconds.inner_text()
        expect(seconds).not_to_have_text(before, timeout=3000)
        assert page.locator("header .countdown, header [role=timer]").count() == 0  # 页头不再有倒计时
        assert problems == []


def test_theme_choice_is_remembered_and_applied_before_paint(browser, web):
    """跟随系统 → 浅色 → 深色 → 跟随系统；选择存在 localStorage，重新打开时首帧前就生效。"""
    with opened(browser, web, "/", color_scheme="light") as (page, problems):
        page.add_init_script(THEME_AT_BODY)
        hydrated(page)
        root, toggle = page.locator("html"), page.locator(".theme-toggle")
        light = background(page)
        toggle.click()
        expect(root).to_have_attribute("data-theme", "light")
        toggle.click()
        expect(root).to_have_attribute("data-theme", "dark")
        assert page.evaluate("localStorage.getItem('theme')") == "dark" and background(page) != light
        page.reload()
        hydrated(page)
        assert page.evaluate("window.__themeAtBody") == "dark"
        toggle.click()  # 回到跟随系统
        assert root.get_attribute("data-theme") is None and page.evaluate("localStorage.getItem('theme')") is None
        assert background(page) == light and problems == []


# ---------------------------------------------------------------- 关脚本、手机宽度、深色


def test_works_without_javascript(browser, live, web):
    total = live.json("/api/plans?ram_min=2")["total"]
    with opened(browser, web, "/plans", java_script_enabled=False) as (page, _):
        submit = page.locator("form.filters button[type=submit]")
        expect(submit).to_be_visible()  # 没有脚本时"筛选"按钮在
        page.select_option("select[name=ram_min]", "2")
        expect(page).to_have_url(re.compile(r"/plans$"))  # 改了不会自己提交
        submit.click()
        expect(page).to_have_url(re.compile(r"ram_min=2"))
        expect(page.locator(".total")).to_have_text(f"共 {total} 个套餐")
        page.goto("/?scope=all")
        page.locator("nav.pager a[rel=next]").first.click()
        expect(page).to_have_url(re.compile(r"page=2"))
        more = page.locator("details.more", has=page.locator("table")).first
        more.locator("summary").click()
        expect(more).to_have_js_property("open", True)
        expect(more.locator("tbody tr").first).to_be_visible()


@pytest.mark.parametrize("path", ["/", "/plans", "/deal/{vm6}", "/en/vendor/vm6", "/vendors", "/codes", "/about", "/status"])
def test_phone_width_in_dark_mode(browser, web, vm6, path):
    """375 px 宽不出横向滚动条，套餐表变成一行一张卡片；系统是深色时页面跟着深色。"""
    with opened(browser, web, path.format(vm6=vm6), viewport=PHONE, color_scheme="dark") as (page, problems):
        hydrated(page)
        assert page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]
        assert max(background(page)) < 60
        if path == "/plans":
            assert page.evaluate("getComputedStyle(document.querySelector('table.pv tbody tr')).display") != "table-row"
        assert problems == []


@pytest.mark.parametrize("width", [375, 900, 1280])
def test_plan_tables_never_scroll_sideways(browser, web, vm6, width):
    """套餐表按自己的可用宽度折叠（优化方案 O-02）：详情页与卡片展开的表在手机、平板、桌面宽度下都不横向滚动；
    外框窄于 800 px 时每行折成小卡片，价格在第一行，空格不显示。"""
    viewport = {"width": width, "height": 800}
    with opened(browser, web, f"/deal/{vm6}", viewport=viewport) as (page, problems):
        hydrated(page)
        wraps = page.evaluate("[...document.querySelectorAll('.pt-wrap')]"
                              ".map(e => [e.clientWidth, e.scrollWidth - e.clientWidth])")
        assert wraps and all(over <= 0 for _, over in wraps), wraps
        folded = page.evaluate("getComputedStyle(document.querySelector('table.pt tbody tr')).display") != "table-row"
        assert folded == (wraps[0][0] < 800), wraps
        if folded:
            price = page.locator("table.pt td[data-c=price]").first.bounding_box()
            name = page.locator("table.pt td[data-c=name]").first.bounding_box()
            assert abs(price["y"] - name["y"]) < 4  # 套餐名与价格同一行
        assert problems == []
    with opened(browser, web, "/?scope=all", viewport=viewport) as (page, problems):
        hydrated(page)
        page.evaluate("document.querySelectorAll('details.more').forEach(d => d.open = true)")
        over = page.evaluate("[...document.querySelectorAll('.pt-wrap')].map(e => e.scrollWidth - e.clientWidth)")
        assert over and max(over) <= 0, over
        assert problems == []


def test_coupon_codes_copy_with_one_click(browser, web, vm6):
    """优惠码（O-07）：水合后码旁有复制按钮，点了码进剪贴板、按钮变对勾；没有脚本时没有按钮，码点一下就全选。"""
    with opened(browser, web, f"/deal/{vm6}", permissions=["clipboard-read", "clipboard-write"]) as (page, problems):
        hydrated(page)
        chip = page.locator(".hero-codes .code").first
        code = chip.locator("code").inner_text()
        button = chip.locator("button.copy")
        expect(button).to_be_visible()
        button.click()
        expect(button).to_have_attribute("data-copied", "")
        assert page.evaluate("navigator.clipboard.readText()") == code
        expect(chip.locator(".sr-only")).to_have_text("已复制")
        assert problems == []
    with opened(browser, web, f"/deal/{vm6}", java_script_enabled=False) as (page, _):
        chip = page.locator(".hero-codes .code").first
        expect(chip.locator("button.copy")).to_be_hidden()
        assert chip.locator("code").evaluate("e => getComputedStyle(e).userSelect") == "all"


@pytest.mark.parametrize("viewport", [{"width": 1280, "height": 900}, PHONE])
def test_about_page_reads_like_a_rendered_document(browser, web, viewport):
    """关于页（D69）：标题与列表是真正的排版，不带 "> "、"## "、"-" 这类没渲染的 Markdown 源码符号；
    正文收在一栏里（≤ 820 px），两侧留边，没有"更正、移除与联系"一节。"""
    with opened(browser, web, "/about", viewport=viewport) as (page, problems):
        hydrated(page)
        marks = page.evaluate("""() => ['.prose h1', '.prose h2', '.prose li'].map(sel =>
            getComputedStyle(document.querySelector(sel), '::before').content)""")
        assert all(m in ("none", "normal") for m in marks), marks
        assert page.evaluate("getComputedStyle(document.querySelector('.prose li')).listStyleType") == "disc"
        box = page.locator(".prose").bounding_box()
        assert box["width"] <= 820 and box["x"] >= 14 and box["x"] + box["width"] <= viewport["width"] - 14, box
        assert page.locator(".prose h2").count() == 6
        assert "更正、移除与联系" not in page.locator(".prose").inner_text()
        assert page.evaluate("document.documentElement.scrollWidth") <= viewport["width"]
        assert problems == []


def test_home_first_card_is_near_the_top(browser, web):
    """首屏（O-09、D68）：通栏首屏（倒计时、读数）之后第一条优惠仍在第一屏内——桌面 1280×800 下 ≤ 720 px、手机 ≤ 720 px；
    读数在倒计时块里，"24 小时动态"在右侧栏。"""
    for viewport, limit in (({"width": 1280, "height": 800}, 720), (PHONE, 720)):
        with opened(browser, web, "/", viewport=viewport) as (page, problems):
            hydrated(page)
            top = page.locator("ol.deals > li").first.bounding_box()["y"]
            assert top <= limit, (viewport, top)
            assert page.locator(".countdown-block .stats dd").count() == 4
            assert page.locator(".rail .shelf .tag").count() >= 1
            assert problems == []
