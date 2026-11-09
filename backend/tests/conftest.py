import re
import sys
from pathlib import Path

import httpx
import pytest

from app.config import load_settings
from app.http import HttpClient

sys.path.insert(0, str(Path(__file__).parent / "tools"))
from pool import SnapshotMissing  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
LES = {"offers": "offers", "vps": "vps", "dedicated-server": "dedicated-server", "shared-hosting": "shared-hosting",
       "services": "services", "free-offers-and-stuff": "free-offers-and-stuff", "must-see-deals-": "must-see-deals"}
ROUTES = {
    "https://lowendtalk.com/categories/offers/feed.rss": "lowendtalk_offers_2026-11-02.xml",
    **{f"https://lowendspirit.com/categories/{slug}/feed.rss": f"lowendspirit_{name}_2026-11-02.xml"
       for slug, name in LES.items()},
    "https://lowendbox.com/feed/": "lowendbox_feed_2026-11-02.xml",
    "https://rss.nodeseek.com/": "nodeseek/nodeseek_2026-11-02T1522Z.xml",
    # 11-02 的快照是 r/VPS+webhosting 50 条；D83 起社区入口合并了更多版块、每页 100 条，测试照旧用这份快照
    "https://www.reddit.com/r/VPS+webhosting+hetzner+Hosting+HostingReport+SelfHosting/new/.rss?limit=100":
        "reddit_multi_2026-11-02.xml",
}
# D81、D82 新增的入口没有 11-02 的快照：返回空 feed / 空频道页（随仓库提供），各源的条数与断言照旧
EXTRA_ROUTES = {
    "https://lowendtalk.com/categories/offers/p2/feed.rss": "empty_feed.xml",
    "https://lowendtalk.com/discussions/feed.rss": "empty_feed.xml",
    "https://lowendspirit.com/discussions/feed.rss": "empty_feed.xml",
    "https://lowendbox.com/tag/black-friday/feed/": "empty_feed.xml",
    "https://t.me/s/VPS_spiders": "telegram_empty.html",
    # 官网活动页（D84）：返回一个不是黑五页的空页面，该源本轮收 0 条
    "https://www.hostinger.com/black-friday-vps-deals": "telegram_empty.html",
    "https://www.hostinger.com/black-friday-web-hosting-deals": "telegram_empty.html",
    # V2EX、NodeLoc、HostingDiscussion（D88）
    **{url: "empty_feed.xml" for url in (
        "https://www.v2ex.com/feed/promotions.xml", "https://www.v2ex.com/feed/vps.xml",
        "https://www.nodeloc.com/c/internet/vps/27.rss", "https://www.nodeloc.com/c/business/information/10.rss",
        "https://www.nodeloc.com/c/business/deal-hunter/43.rss", "https://hostingdiscussion.com/forums/-/index.rss",
    )},
    **{url: "empty_feed.xml" for url in (  # Reddit 官方版块与全站搜索（D83），按轮转在第 2、3 轮抓
        "https://www.reddit.com/r/Hostinger+CloudwaysbyDO+SiteGroundOfficial+OVHcloud+digital_ocean+Contabo+RackNerd"
        "+netcup+vultr+linode+Ionos+Namecheap+LightNodeVPS+hostney/new/.rss?limit=100",
        "https://www.reddit.com/search.rss?q=title%3A%28%22black+friday%22+OR+%22cyber+monday%22+OR+BFCM%29+%28vps+OR"
        "+vds+OR+hosting+OR+%22dedicated+server%22+OR+seedbox+OR+%22bare+metal%22%29+NOT+subreddit%3APromoCodesDaily+NOT"
        "+subreddit%3Acouponcodes+NOT+subreddit%3ACouponCodeBD+NOT+subreddit%3ALowestPriceDeals&sort=new&t=month&limit=100",
    )},
}


SNAPSHOT_HINT = "缺抓取样本（第三方帖子原文，不随仓库发布；见 tests/fixtures/README.md）"


def snapshots_available() -> bool:
    """各源 RSS 快照是否都在 tests/fixtures 里。"""
    return all((FIXTURES / name).exists() for name in ROUTES.values())


def snapshot(name: str) -> Path:
    """取一份抓取快照的路径；没有样本时跳过当前测试。"""
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"{SNAPSHOT_HINT}：{name}")
    return path


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """金标集等工具读不到快照时（SnapshotMissing）跳过当前测试，而不是报错。"""
    try:
        return (yield)
    except SnapshotMissing as e:
        pytest.skip(str(e))


CJK = re.compile(r"[　-〿一-鿿＀-￯]+")


def noop_sleep(seconds: float) -> None:
    pass


def data_cjk(conn) -> set[str]:
    """库里内容本身带的中文（帖子标题、商家名与别名、情报标题、摘录、套餐名）：英文页照原文显示，不算残留。"""
    found: set[str] = set()
    for (text,) in conn.execute(
            "SELECT title FROM deals UNION ALL SELECT excerpt FROM deals "
            "UNION ALL SELECT discount_text FROM deals UNION ALL SELECT name FROM vendors UNION ALL SELECT aliases_json "
            "FROM vendors UNION ALL SELECT short_aliases_json FROM vendors UNION ALL SELECT title FROM intel "
            "UNION ALL SELECT name FROM plans UNION ALL SELECT locations_json FROM plans"):
        found.update(CJK.findall(text or ""))
    return found


class FixtureServer:
    """把各源入口 URL 映射到 tests/fixtures 中的真实快照；overrides 可指定某个 URL 返回的状态码或字节。"""

    def __init__(self) -> None:
        if not snapshots_available():
            pytest.skip(SNAPSHOT_HINT)
        self.overrides: dict[str, int | bytes] = {}
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url in self.overrides:
            value = self.overrides[url]
            return httpx.Response(value) if isinstance(value, int) else httpx.Response(200, content=value)
        if url in ROUTES or url in EXTRA_ROUTES:
            return httpx.Response(200, content=(FIXTURES / (ROUTES.get(url) or EXTRA_ROUTES[url])).read_bytes())
        return httpx.Response(404)


@pytest.fixture
def settings():
    return load_settings({}, env_file=None)


@pytest.fixture
def server():
    return FixtureServer()


@pytest.fixture
def http(settings, server):
    with HttpClient(settings, transport=httpx.MockTransport(server.handler), sleep=noop_sleep) as client:
        yield client
