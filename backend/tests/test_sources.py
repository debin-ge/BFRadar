import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models import SourceKind
from app.sources import REGISTRY, build_sources
from app.sources import nodeseek, telegram
from app.sources import official
from app.sources import reddit as reddit_mod
from app.sources.base import feed_posts
from app.textutil import structure_html

from conftest import FIXTURES, ROUTES

sys.path.insert(0, str(Path(__file__).parent / "tools"))
import pool  # noqa: E402


def make(source_id, settings, http, sleeps=None):
    return REGISTRY[source_id](settings, http, sleep=(sleeps.append if sleeps is not None else lambda s: None))


def test_registry_declares_ten_sources(settings, http):
    assert set(REGISTRY) == {"lowendtalk", "lowendspirit", "lowendbox", "nodeseek", "reddit", "telegram", "official",
                             "v2ex", "nodeloc", "hostingdiscussion"}
    assert {s: (c.kind, c.lang) for s, c in REGISTRY.items()} == {
        "lowendtalk": (SourceKind.OFFERS, "en"), "lowendspirit": (SourceKind.OFFERS, "en"),
        "lowendbox": (SourceKind.MIXED, "en"), "nodeseek": (SourceKind.MIXED, "zh"),
        "reddit": (SourceKind.MIXED, "en"), "telegram": (SourceKind.OFFERS, "en"),
        "official": (SourceKind.OFFERS, "en"), "v2ex": (SourceKind.MIXED, "zh"),
        "nodeloc": (SourceKind.MIXED, "zh"), "hostingdiscussion": (SourceKind.MIXED, "en"),
    }
    assert [s.id for s in build_sources(settings, http)] == list(settings.sources)
    assert [s.id for s in build_sources(settings, http, ["nodeseek"])] == ["nodeseek"]
    with pytest.raises(ValueError):
        build_sources(settings, http, ["hostloc"])


def test_lowendtalk(settings, http, server):
    result = make("lowendtalk", settings, http).fetch()
    assert server.requests == ["https://lowendtalk.com/categories/offers/feed.rss",
                               "https://lowendtalk.com/categories/offers/p2/feed.rss",
                               "https://lowendtalk.com/discussions/feed.rss"]  # D82：Offers 两页与全站讨论
    assert result.stats == {"feeds": 3}
    assert len(result.posts) == 100
    assert all(re.fullmatch(r"https://lowendtalk\.com/discussion/\d+", p.url) for p in result.posts)
    assert len({p.url for p in result.posts}) == 100
    assert all(p.published_at and p.published_at.tzinfo for p in result.posts)
    hdz = next(p for p in result.posts if p.url.endswith("/222164"))
    assert hdz.author == "HostDZire" and hdz.categories == ["Offers"]
    assert "AMD EPYC || 2vCore" in hdz.content_html


def test_lowendspirit_reads_seven_feeds_and_dedupes(settings, http, server):
    sleeps = []
    result = make("lowendspirit", settings, http, sleeps).fetch()
    les_urls = [u for u in ROUTES if "lowendspirit" in u]
    assert [u for u in server.requests if "lowendspirit" in u] == [*les_urls,
                                                                   "https://lowendspirit.com/discussions/feed.rss"]
    assert sleeps == [1.0] * 7
    expected = {p.url for u in les_urls for p in feed_posts("lowendspirit", (FIXTURES / ROUTES[u]).read_bytes())}
    assert {p.url for p in result.posts} == expected
    assert len(result.posts) == len(expected)
    vm6 = next(p for p in result.posts if p.url == "https://lowendspirit.com/discussion/11544")
    assert vm6.categories == ["VPS"]
    assert result.stats == {"feeds": 8}


def test_lowendspirit_partial_and_total_failure(settings, http, server):
    broken = "https://lowendspirit.com/categories/vps/feed.rss"
    server.overrides[broken] = 500
    result = make("lowendspirit", settings, http).fetch()
    assert result.posts and set(result.stats["errors"]) == {broken}
    for url in [*ROUTES, "https://lowendspirit.com/discussions/feed.rss"]:
        server.overrides[url] = 503
    with pytest.raises(RuntimeError, match="全部 8 个入口失败"):
        make("lowendspirit", settings, http).fetch()


def test_lowendbox_uses_full_content(settings, http, server):
    posts = make("lowendbox", settings, http).fetch().posts
    assert server.requests[1] == "https://lowendbox.com/tag/black-friday/feed/"  # D82：黑五标签
    assert len(posts) == 20
    ender = next(p for p in posts if "enderhost" in p.url)
    assert "LETSPECIAL96" in ender.content_html and len(ender.content_html) > 5000
    assert "Low End Virtual" in ender.categories


def test_nodeseek_post_ids_and_first_run_coverage(settings, http):
    result = make("nodeseek", settings, http).fetch()
    ids = [p.extra["post_id"] for p in result.posts]
    assert len(ids) == 20 and all(p.url == f"https://www.nodeseek.com/post-{i}-1" for p, i in zip(result.posts, ids))
    assert result.stats["max_id"] == max(ids) and result.stats["overlap"] is None


def test_nodeseek_coverage_against_previous_run(settings, http):
    ids = [p.extra["post_id"] for p in make("nodeseek", settings, http).fetch().posts]
    lo, hi = min(ids), max(ids)
    stats = make("nodeseek", settings, http).fetch({"max_id": lo + 4})
    assert stats.stats["overlap"] is True
    assert stats.stats["new_ids"] == sum(i > lo + 4 for i in ids)
    assert stats.stats["expected"] == hi - lo - 4
    missed = make("nodeseek", settings, http).fetch({"max_id": lo - 50}).stats
    assert missed["overlap"] is False and missed["gap"] == 49  # lo-49 … lo-1 整段漏抓


@pytest.mark.parametrize("ids, prev, expected", [
    ([], 100, {"count": 0, "max_id": 100}),
    ([101, 102, 104], 100, {"count": 3, "min_id": 101, "max_id": 104, "new_ids": 3, "expected": 4, "gap": 1,
                            "overlap": True, "coverage": 0.75}),
    ([90, 95], 100, {"count": 2, "min_id": 90, "max_id": 100, "new_ids": 0, "expected": 0, "gap": 0,
                     "overlap": True, "coverage": None}),
    ([110, 111], 100, {"count": 2, "min_id": 110, "max_id": 111, "new_ids": 2, "expected": 11, "gap": 9,
                       "overlap": False, "coverage": 0.1818}),
])
def test_coverage(ids, prev, expected):
    assert nodeseek.coverage(ids, prev) == expected


def test_reddit_reads_all_subreddits_in_one_request(settings, http, server):
    result = make("reddit", settings, http).fetch()
    assert server.requests == [
        "https://www.reddit.com/r/VPS+webhosting+hetzner+Hosting+HostingReport+SelfHosting/new/.rss?limit=100"]  # D39
    assert result.stats == {"entry": "community", "next": 1, "dropped": 0}
    assert len(result.posts) == 50
    assert {p.categories[0] for p in result.posts} == {"VPS", "webhosting"}
    assert all(re.fullmatch(r"https://www\.reddit\.com/r/\w+/comments/\w+", p.url) for p in result.posts)
    assert all(p.author and not p.author.startswith("/u/") for p in result.posts)


def test_reddit_rotates_entries_one_request_per_round(settings, http, server):
    """D83：社区、官方版块、全站搜索三个入口轮流，每轮一个请求。"""
    reddit = make("reddit", settings, http)
    stats = reddit.fetch({"next": 1}).stats
    assert server.requests[-1].startswith("https://www.reddit.com/r/Hostinger+CloudwaysbyDO+")
    assert stats["entry"] == "vendors" and stats["next"] == 2
    stats = reddit.fetch(stats).stats
    assert server.requests[-1].startswith("https://www.reddit.com/search.rss?q=title%3A")
    assert "t=month" in server.requests[-1] and stats == {"entry": "search", "next": 0, "dropped": 0}
    assert reddit.fetch(stats).stats["entry"] == "community"


def test_reddit_failed_entry_moves_on_but_two_in_a_row_fail_the_source(settings, http, server):
    reddit = make("reddit", settings, http)
    community = reddit.entries()[0][1]
    server.overrides[community] = 403
    result = reddit.fetch({})
    assert result.posts == [] and result.stats["next"] == 1 and list(result.stats["errors"]) == [community]
    server.overrides[reddit.entries()[1][1]] = 403
    with pytest.raises(Exception, match="403"):
        reddit.fetch(result.stats)


def test_reddit_drops_coupon_subreddits_and_profile_posts():
    entry = ('<entry><author><name>/u/{a}</name></author><category term="{s}" label="r/{s}"/>'
             '<id>t3_{i}</id><link href="https://www.reddit.com/r/{s}/comments/{i}/x/"/><title>{t}</title>'
             '<updated>2026-11-06T00:00:00+00:00</updated><content type="html">body</content></entry>')
    feed = ('<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
            + entry.format(a="a", s="VPS", i="aa1", t="Black Friday VPS deals?")
            + entry.format(a="b", s="PromoCodesDaily", i="aa2", t="Hostinger Black Friday coupon 90% off")
            + entry.format(a="c", s="u_somevendor", i="aa3", t="Black Friday hosting sale")
            + "</feed>")
    posts, dropped = reddit_mod.parse(feed.encode())
    assert [p.url for p in posts] == ["https://www.reddit.com/r/VPS/comments/aa1"] and dropped == 2


GOLD_SOURCES = {"lowendtalk", "lowendspirit", "lowendbox"}


def test_plugins_see_exactly_what_was_annotated():
    """插件解析 fixture 得到的原文，必须与金标集标注时看到的原文一致（URL、标题、时间、结构化文本）。"""
    import json

    checked = 0
    for path in sorted((Path(__file__).parent / "gold").glob("*.json")):
        sample = json.loads(path.read_text(encoding="utf-8"))
        if sample["source"] not in GOLD_SOURCES:
            continue
        gold_item = pool.load_item(sample["fixture"], sample["fixture_item_guid"], sample["source"])
        posts = feed_posts(sample["source"], (Path(__file__).parent / sample["fixture"]).read_bytes())
        post = next(p for p in posts if p.url == sample["url"])
        assert post.title == gold_item.title
        assert post.published_at == gold_item.published
        assert structure_html(post.content_html) == structure_html(gold_item.html)
        checked += 1
    assert checked == 25


# ---------------------------------------------------------------- Telegram @VPS_spiders（D81）


def tg_message(msg_id, body, *, vendor=None, origin="https://lowendtalk.com/discussion/comment/4623265/#Comment_4623265",
               tags=("let", "黑五2025", "黑五"), when="2025-11-28T18:36:31+00:00"):
    """按频道网页预览的结构拼一条消息（链接按频道的写法转义两次）。"""
    head = []
    if vendor:
        head.append(f'<a href="?q=%23{vendor}">#{vendor}</a>')
        head.append('商家评论：<a href="https://t.me/c/1589001900/859">点我查看</a>')
        head.append(f'注册或登录：<a href="https://{vendor}.net/?aff=7">点我跳转或长按复制</a>')
    if origin:
        head.append(f'原始地址：<a href="{origin}" onclick="return confirm(1)">查看</a>')
    head.append(" ".join(f'<a href="?q=%23{t}">#{t}</a>' for t in tags))
    text = "<br/>".join(head) + "<br/><br/>" + body
    return (f'<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" '
            f'data-post="VPS_spiders/{msg_id}"><div class="tgme_widget_message_text js-message_text">{text}</div>'
            f'<div class="tgme_widget_message_info"><a class="tgme_widget_message_date" '
            f'href="https://t.me/VPS_spiders/{msg_id}"><time datetime="{when}">18:36</time></a></div></div></div>')


TG_BODY = ('LAX Ryzen Special<br/>CPU: 2 vCPU AMD Ryzen 9 7950X<br/>Memory: 3 GB<br/>Price: &#036;30 USD/year<br/>'
           'Stock: Limited<br/><a href="https://crowncloud.net/clients/index.php?rp=/store/specials/lax&amp;amp;'
           'billingcycle=annually&amp;amp;aff=125&amp;amp;utm_source=tg">Order Here</a>')


def test_telegram_relay_points_at_the_original_comment():
    (m,) = telegram.parse_page(tg_message(26187, TG_BODY, vendor="crowncloud"))
    post = m.post
    assert post.url == "https://lowendtalk.com/discussion/comment/4623265"
    assert post.source == "telegram" and post.extra == {"tg_post": "VPS_spiders/26187"}
    assert post.title == "LAX Ryzen Special" and post.author == "crowncloud"
    assert post.categories == ["let", "黑五2025", "黑五"]
    assert post.published_at.isoformat() == "2025-11-28T18:36:31+00:00"
    html = post.content_html
    assert html.startswith("LAX Ryzen Special<br/>CPU: 2 vCPU")
    assert "Stock" not in html  # D63
    assert 'href="https://crowncloud.net/clients/index.php?rp=/store/specials/lax&amp;billingcycle=annually"' in html
    assert "aff=" not in html and "utm_" not in html and "t.me/c/" not in html and "点我" not in html


def test_telegram_vendor_tag_takes_the_casing_used_in_the_post():
    body = "DigitalFyre: $11.11/m 11GB Ryzen VPS<br/>CPU: 4 vCPU<br/>Price: $11.11 USD/month"
    (m,) = telegram.parse_page(tg_message(26216, body, vendor="digitalfyre"))
    assert m.post.author == "DigitalFyre"


def test_telegram_affiliate_in_path_drops_the_order_link():
    body = 'Box<br/>CPU: 1 vCPU<br/>Price: $5/mo<br/><a href="https://host.example/aff.php?aff=9">Order Here</a>'
    (m,) = telegram.parse_page(tg_message(1, body))
    assert "host.example" not in m.post.content_html and "Order Here" not in m.post.content_html


def test_telegram_title_from_specs_when_the_post_has_none():
    body = "CPU: 1 vCPU<br/>Memory: 1 GB<br/>Disk: 10 GB NVMe<br/>Price: £4/year"
    (m,) = telegram.parse_page(tg_message(26450, body))
    assert m.post.title == "1 vCPU / 1 GB / 10 GB NVMe" and m.post.content_html.startswith("CPU: 1 vCPU")


@pytest.mark.parametrize("origin, skip", [
    (None, "no_origin"),  # 补货监控没有原始地址（D63）
    ("https://lowendtalk.com/discussion/212154/black-friday-2025-megathread", "thread"),  # 整帖由 LET RSS 收
    ("https://www.nodeseek.com/post-1-1", "other_origin"),
])
def test_telegram_skips_messages_without_a_comment_origin(origin, skip):
    (m,) = telegram.parse_page(tg_message(5, "Box<br/>Price: $5/mo", origin=origin))
    assert m.post is None and m.skip == skip


def test_telegram_pages_back_to_the_previous_max_id(settings):
    pages = {
        telegram.PAGE_URL: "".join(tg_message(i, "Box<br/>Price: $5/mo",
                                              origin=f"https://lowendtalk.com/discussion/comment/{i}")
                                   for i in (130, 131, 140)),
        f"{telegram.PAGE_URL}?before=130": "".join(tg_message(i, "x", origin=None) for i in (100, 120)),
        f"{telegram.PAGE_URL}?before=100": "".join(tg_message(i, "x", origin=None) for i in (80, 90)),
        f"{telegram.PAGE_URL}?before=80": "",  # 频道开头：没有更早的消息
    }
    requested = []

    class Http:
        def get(self, url):
            requested.append(url)
            return type("R", (), {"content": pages[url].encode()})

    sleeps = []
    source = telegram.Telegram(settings, Http(), sleep=sleeps.append)
    result = source.fetch({"max_id": 120})
    assert requested == [telegram.PAGE_URL, f"{telegram.PAGE_URL}?before=130"] and sleeps == [2.0]
    assert [p.url.rsplit("/", 1)[1] for p in result.posts] == ["140", "131", "130"]
    assert result.stats == {"pages": 2, "messages": 5, "relayed": 3, "skipped": {"no_origin": 2}, "min_id": 100,
                            "max_id": 140, "overlap": True}
    requested.clear()
    first = source.fetch(None)  # 首轮只看最新一页
    assert requested == [telegram.PAGE_URL] and "overlap" not in first.stats
    gap = source.fetch({"max_id": 10})  # 停机太久：翻到最旧一页也没接上
    assert gap.stats["overlap"] is False and gap.stats["min_id"] == 80 and gap.stats["pages"] == 4


def test_telegram_fetch_with_an_empty_channel_page(settings, http):
    result = make("telegram", settings, http).fetch({"max_id": 26602})
    assert result.posts == [] and result.stats == {"pages": 1, "messages": 0, "relayed": 0, "skipped": {},
                                                   "max_id": 26602}



# ---------------------------------------------------------------- 主流品牌官网活动页（D84）

HOSTINGER_CARD = """<div class="h-pricing-card"><div class="h-pricing-card__discount-tag">Special offer • 64% off</div>
<div class="h-pricing-card__title">KVM 2</div><span class="hw-price">$24.49</span>
<span class="hw-price"><span>$</span><span>8.79</span><span>/mo</span></span>
<div>Today you pay $210.96 for 24 months. Renews at $14.99/mo for the same term. Cancel anytime.</div>
<div class="h-plan-feature__text"><b>2</b> vCPU cores</div><div class="h-plan-feature__text"><b>8 GB</b> RAM</div>
<div class="h-plan-feature__text">Priority 24/7 expert support</div><div class="h-plan-feature__text">Build with:</div></div>"""


def hostinger_page(title="Black Friday VPS Hosting Deals 2026 | Hostinger", cards=HOSTINGER_CARD * 2):
    faq = "<p>Our Black Friday VPS deals are available from October 19 to December 14, 2026.</p>"
    return f"<html><head><title>{title}</title></head><body>{cards}{faq}</body></html>"


def test_official_hostinger_cards_become_a_table():
    page = official.PAGES[0]
    post = official.parse_page(page, hostinger_page(), datetime(2026, 11, 7, tzinfo=UTC))
    assert post.url == "https://www.hostinger.com/black-friday-vps-deals" and post.source == "official"
    assert post.title == "Hostinger: Black Friday VPS Hosting Deals 2026" and post.author == "Hostinger"
    text = structure_html(post.content_html).text
    assert text.splitlines() == [
        "available from October 19 to December 14, 2026.",
        "Plan | Specs | Price | Discount | Term",
        "KVM 2 | 2 vCPU cores, 8 GB RAM | $8.79/mo | 64% off | 24-month term, renews at $14.99/mo",  # 两遍的卡片去重
        "Order at Hostinger [1]",
    ]


@pytest.mark.parametrize("html", [
    hostinger_page(title="Contabo Black Friday Deals Coming Soon!", cards=""),  # 预告页：没有套餐卡片
    hostinger_page(title="VPS Hosting | Hostinger"),  # 不是黑五页
])
def test_official_skips_pages_that_are_not_live_deals(html):
    assert official.parse_page(official.PAGES[0], html, datetime(2026, 11, 7, tzinfo=UTC)) is None


def test_official_publish_time_is_first_seen_this_season(settings):
    pages = {p.url: hostinger_page() for p in official.PAGES}

    class Http:
        def get(self, url):
            return type("R", (), {"content": pages[url].encode()})

    source = official.Official(settings, Http(), sleep=lambda s: None)
    first = source.fetch({"first_seen": {official.PAGES[0].url: "2026-10-20T08:00:00Z",
                                         "https://old.example/bf": "2025-11-01T00:00:00Z"}})  # 往季的作废
    assert first.posts[0].published_at == datetime(2026, 10, 20, 8, tzinfo=UTC)
    assert set(first.stats["first_seen"]) == {p.url for p in official.PAGES}
    again = source.fetch(first.stats)
    assert [p.published_at for p in again.posts] == [p.published_at for p in first.posts]


def test_official_hostinger_snapshot_extracts_plans_with_term_and_renewal():
    """2026-11-07 抓的 Hostinger 两页（不随仓库发布）：VPS 4 个套餐、虚拟主机 3 个，合约 24 / 48 个月。"""
    from app.extract.rules import extract

    expected = {"vps": [("KVM 1", 6.49, 24, 11.99), ("KVM 2", 8.79, 24, 14.99), ("KVM 4", 12.99, 24, 28.99),
                        ("KVM 8", 25.99, 24, 49.99)],
                "web-hosting": [("Premium", 1.99, 48, 10.99), ("Unlimited", 2.79, 48, 16.99),
                                ("Cloud Startup", 7.99, 48, 25.99)]}
    for page, key in zip(official.PAGES, expected):
        path = FIXTURES / "official" / f"hostinger_black-friday-{key}-deals_2026-11-07.html"
        if not path.exists():
            pytest.skip("缺官网活动页样本（tests/fixtures/official/，不随仓库发布）")
        post = official.parse_page(page, path.read_bytes(), datetime(2026, 11, 7, tzinfo=UTC))
        st = structure_html(post.content_html)
        ex = extract("official", post.title, st.text, st.links, post.author, "en", True, "2026-11-07")
        assert [(p.name, p.price, p.term_months, p.renewal_price) for p in ex.plans] == expected[key]
        assert ex.vendor == "Hostinger" and str(ex.ends_at) == "2026-12-14"


# ---------------------------------------------------------------- V2EX、NodeLoc、HostingDiscussion（D88）

ATOM = """<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>V2EX</title>
<entry><title>求 VPS 推荐</title><link rel="alternate" type="text/html" href="https://www.v2ex.com/t/1255409#reply3"/>
<id>tag:www.v2ex.com,2026-11-08:/t/1255409</id><published>2026-11-08T16:59:39Z</published>
<author><name>someone</name></author><content type="html">&lt;p&gt;预算 10 刀&lt;/p&gt;</content></entry></feed>"""
RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>
<item><title>{title}</title><link>{link}</link><category>{cat}</category>
<pubDate>Sun, 08 Nov 2026 13:00:32 +0000</pubDate><description><![CDATA[{body}]]></description></item></channel></rss>"""


def test_v2ex_records_the_node_as_category_and_drops_the_reply_anchor(settings, server):
    from app.sources import v2ex
    (post,) = v2ex.parse(ATOM.encode(), "vps")
    assert post.url == "https://www.v2ex.com/t/1255409" and post.categories == ["vps"] and post.source == "v2ex"


def test_v2ex_fetches_both_nodes(settings):
    from app.sources import v2ex
    requested, sleeps = [], []

    class Http:
        def get(self, url):
            requested.append(url)
            return type("R", (), {"content": ATOM.replace("1255409", url.rsplit("/", 1)[1][:3]).encode()})

    result = v2ex.V2ex(settings, Http(), sleep=sleeps.append).fetch()
    assert requested == ["https://www.v2ex.com/feed/promotions.xml", "https://www.v2ex.com/feed/vps.xml"]
    assert sleeps == [3.0] and result.stats == {"feeds": 2}
    assert {p.url: p.categories for p in result.posts} == {"https://www.v2ex.com/t/pro": ["promotions"],
                                                           "https://www.v2ex.com/t/vps": ["vps"]}


def test_nodeloc_strips_the_discourse_footer():
    from app.sources import nodeloc
    body = ("<p>9929 线路 3$/月</p>\n            <p><small>3 个帖子 - 2 位参与者</small></p>\n"
            '            <p><a href="https://www.nodeloc.com/t/topic/117013">阅读完整话题</a></p>\n')
    (post,) = nodeloc.parse(RSS.format(title="t", link="https://www.nodeloc.com/t/topic/117013", cat="VPS",
                                       body=body).encode())
    assert post.content_html == "<p>9929 线路 3$/月</p>" and post.categories == ["VPS"]


def test_hostingdiscussion_strips_read_more_and_slug():
    from app.sources import hostingdiscussion
    body = ('<div class="bbWrapper">- <b>2GB</b> disk space<br />\n-...<br />\n<br />\n'
            '<a href="https://hostingdiscussion.com/threads/x.90817/" class="link">Read more</a></div>')
    (post,) = hostingdiscussion.parse(RSS.format(
        title="cPanel Hosting From $6.99/mo", link="https://hostingdiscussion.com/threads/cpanel-hosting.90817/",
        cat="Shared Hosting Offers", body=body).encode())
    assert post.url == "https://hostingdiscussion.com/threads/90817/"
    assert "Read more" not in post.content_html and post.content_html.endswith("<br />\n<br /></div>")


@pytest.mark.parametrize("source, files, total, intel", [
    ("v2ex", ("v2ex_promotions", "v2ex_vps"), 100, 47),
    ("nodeloc", ("nodeloc_vps", "nodeloc_information", "nodeloc_deal-hunter"), 75, 21),
    ("hostingdiscussion", ("hostingdiscussion",), 20, 20),
])
def test_small_forum_snapshots_mostly_intel(source, files, total, intel):
    """11-08 实采：综合推广 / 羊毛版块从严后只留与主机有关的帖子（D88）；没有快照时跳过。"""
    from importlib import import_module

    from app.extract.classify import classify
    from conftest import snapshot
    mod = import_module(f"app.sources.{source}")
    posts = []
    for name in files:
        data = snapshot(f"forums/{name}_2026-11-08.xml").read_bytes()
        posts += mod.parse(data, name.split("_", 1)[1]) if source == "v2ex" else mod.parse(data)
    kinds = [classify(source, p.title, structure_html(p.content_html).text, p.categories).record_kind for p in posts]
    assert len(posts) == total and sum(k == "intel" for k in kinds) == intel and "offer" not in kinds
    assert all(p.published_at and p.url.startswith("https://") for p in posts)
