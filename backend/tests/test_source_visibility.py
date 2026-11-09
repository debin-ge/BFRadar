"""不依赖第三方快照：用户页面隐藏出处，内容、购买入口与公开 API/RSS/维护页保留。"""

import threading
import xml.etree.ElementTree as ET

import lxml.html
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.config import load_settings
from app.db import Database
from app.i18n import strings
from app.models import Deal, EventType, Intel, Period, Plan, ProductType, iso, utcnow
from app.repo import save_deal, save_intel
from app.vendors import prepare
from app.web.app import create_app
from live import wait_for, web_site

POST = "https://lowendtalk.com/discussion/990001"
THREAD = "https://www.nodeseek.com/post-990002-1"
BUY = "https://www.vm6.co.uk/promotions"
ORDER = "https://www.vm6.co.uk/order?pid=1"
TITLE = "VM6 Black Friday offer"
EXCERPT = "Offer details with monthly billing."
DISCUSSION = "VM6 service discussion"


@pytest.fixture(scope="module")
def visibility_site(tmp_path_factory):
    settings = load_settings({"DB_PATH": str(tmp_path_factory.mktemp("visibility") / "site.db"),
                              "SOURCES": "lowendtalk,nodeseek", "SITE_URL": "https://bfradar.test"}, env_file=None)
    db = Database(settings.db_path)
    prepare(db)
    conn, now = db.conn(), iso(utcnow())
    common = {"source": "lowendtalk", "sources": ["lowendtalk"], "vendor": "VM6",
              "published_at": now, "last_seen_at": now, "content_hash": "visibility-test",
              "is_black_friday": True, "event_type": EventType.BLACK_FRIDAY,
              "product_types": [ProductType.VPS]}
    full = save_deal(conn, Deal(
        url=POST, title=TITLE, title_zh="VM6 黑五优惠", title_en="VM6 hosting offer",
        summary_zh="VM6 套餐摘要", summary_en="VM6 plan summary", excerpt=EXCERPT,
        purchase_url=BUY, coupon_codes=["VISIBILITY"], min_price=10, min_price_currency="USD",
        min_price_period=Period.MONTH, plans=[
            Plan(name="Direct order", price=10, currency="USD", period=Period.MONTH, order_url=ORDER),
            Plan(name="Purchase fallback", price=20, currency="USD", period=Period.MONTH, position=1),
        ], **common), now=now).deal_id
    empty = save_deal(conn, Deal(url=POST + "-empty", title="VM6 offer pending details", **common), now=now).deal_id
    buy_only = save_deal(conn, Deal(url=POST + "-buy", title="VM6 checkout offer",
                                   purchase_url=BUY, **common), now=now).deal_id
    intel_id, _ = save_intel(conn, Intel(url=THREAD, source="nodeseek", title=DISCUSSION, is_black_friday=True,
                                       published_at=now, first_seen_at=now))
    vendor_id = conn.execute("SELECT vendor_id FROM deals WHERE id = ?", (full,)).fetchone()[0]
    conn.execute("INSERT INTO intel_vendors VALUES (?, ?, 'VM6')", (intel_id, vendor_id))
    db.close()
    with TestClient(create_app(settings)) as client:
        yield settings, client, {"full": full, "empty": empty, "buy_only": buy_only}


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_page_data_omits_source_addresses_and_preserves_content(visibility_site, lang):
    _, client, ids = visibility_site
    home = client.get(f"/internal/v1/{lang}/home").json()
    deal = client.get(f"/internal/v1/{lang}/deal/{ids['full']}").json()
    vendor = client.get(f"/internal/v1/{lang}/vendor/vm6").json()
    assert "url" not in deal and deal["purchase_url"] == BUY
    assert TITLE in deal["original_title"] and deal["excerpt"] == EXCERPT
    assert deal["summary"] == ("VM6 套餐摘要" if lang == "zh" else "VM6 plan summary")
    assert [p["order"] for p in deal["plans"]] == [ORDER, BUY]
    assert vendor["website"] == "https://www.vm6.co.uk"
    for page in (home, deal, vendor):
        assert page["intel"] and page["intel"][0]["title"] == DISCUSSION
        assert set(page["intel"][0]) == {"title", "ago"}
    empty = client.get(f"/internal/v1/{lang}/deal/{ids['empty']}").json()
    assert empty["purchase_url"] is None and empty["facts"]["price"] is None and empty["plans"] == []


def test_api_rss_and_maintenance_status_keep_sources(visibility_site):
    _, client, ids = visibility_site
    deal = client.get(f"/api/deals/{ids['full']}").json()
    assert (deal["url"], deal["source"], deal["sources"]) == (POST, "lowendtalk", ["lowendtalk"])
    assert deal["intel"][0]["url"] == THREAD and deal["intel"][0]["source"] == "nodeseek"
    assert any(d["url"] == POST for d in client.get("/api/deals").json()["items"])
    assert any(p["deal"]["url"] == POST for p in client.get("/api/plans").json()["items"])
    assert any(d["url"] == POST for d in client.get("/api/vendors/vm6").json()["deals"])
    for prefix, lang in (("", "zh"), ("/en", "en")):
        feed = ET.fromstring(client.get(f"{prefix}/feed.xml").content)
        assert any(POST in i.findtext("description") and strings(lang)["feed.original"] in i.findtext("description")
                   for i in feed.findall("channel/item"))
        status = client.get(f"/internal/v1/{lang}/status").json()
        assert {s["key"] for s in status["sources"]} == {"lowendtalk", "nodeseek"}


@pytest.fixture(scope="module")
def visibility_web(visibility_site, tmp_path_factory):
    settings, _, ids = visibility_site
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        wait_for(lambda: server.started, "页面出处测试后端启动", timeout=15)
        port = server.servers[0].sockets[0].getsockname()[1]
        with web_site(f"http://127.0.0.1:{port}", tmp_path_factory.mktemp("visibility-web")) as web:
            yield web, ids
    finally:
        server.should_exit = True
        thread.join(15)
    assert not thread.is_alive()


@pytest.mark.parametrize("prefix,lang", [("", "zh"), ("/en", "en")])
def test_user_pages_hide_source_links_and_keep_purchase_links(visibility_web, prefix, lang):
    web, ids = visibility_web
    t = strings(lang)
    for path in ("/", f"/deal/{ids['full']}", "/vendor/vm6"):
        response = web.get(prefix + path)
        assert response.status_code == 200
        page = lxml.html.fromstring(response.content)
        assert page.xpath("//ul[@class='intel']/li") and DISCUSSION in page.text_content()
        assert not page.xpath("//ul[@class='intel']//a")
        assert POST not in response.text and THREAD not in response.text
    page = lxml.html.fromstring(web.get(f"{prefix}/deal/{ids['full']}").content)
    assert page.xpath("//a[@href=$url]", url=BUY) and page.xpath("//a[@href=$url]", url=ORDER)
    assert TITLE in page.text_content() and EXCERPT in page.text_content()
    assert t["deal.auto_summary"] in page.text_content() and t["deal.excerpt"] in page.text_content()
    for kind in ("empty", "buy_only"):
        page = lxml.html.fromstring(web.get(f"{prefix}/deal/{ids[kind]}").content)
        assert t["row.price_unknown"] in page.text_content() and t["deal.no_plans"] in page.text_content()
        assert bool(page.xpath("//p[@class='actions']")) == (kind == "buy_only")
    for path in ("/about", "/codes", "/"):
        page = lxml.html.fromstring(web.get(prefix + path).content)
        visible = page.xpath("//main")[0].text_content() + " ".join(page.xpath("//meta[@name='description']/@content"))
        assert not any(name in visible for name in ("LowEndTalk", "LowEndSpirit", "LowEndBox", "NodeSeek"))
        assert "原帖" not in visible and "original post" not in visible.lower()
    data = web.get(f"{prefix}/deal/{ids['full']}.data")
    assert data.status_code == 200 and POST not in data.text and THREAD not in data.text
    status = web.get(prefix + "/status")
    assert status.status_code == 200 and "NodeSeek" in lxml.html.fromstring(status.content).text_content()
