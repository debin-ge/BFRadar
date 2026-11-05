import re
import sys
from pathlib import Path

import pytest

from app.models import SourceKind
from app.sources import REGISTRY, build_sources
from app.sources import nodeseek
from app.sources.base import feed_posts
from app.textutil import structure_html

from conftest import FIXTURES, ROUTES

sys.path.insert(0, str(Path(__file__).parent / "tools"))
import pool  # noqa: E402


def make(source_id, settings, http, sleeps=None):
    return REGISTRY[source_id](settings, http, sleep=(sleeps.append if sleeps is not None else lambda s: None))


def test_registry_declares_five_sources(settings, http):
    assert set(REGISTRY) == {"lowendtalk", "lowendspirit", "lowendbox", "nodeseek", "reddit"}
    assert {s: (c.kind, c.lang) for s, c in REGISTRY.items()} == {
        "lowendtalk": (SourceKind.OFFERS, "en"), "lowendspirit": (SourceKind.OFFERS, "en"),
        "lowendbox": (SourceKind.MIXED, "en"), "nodeseek": (SourceKind.MIXED, "zh"),
        "reddit": (SourceKind.MIXED, "en"),
    }
    assert [s.id for s in build_sources(settings, http)] == list(settings.sources)
    assert [s.id for s in build_sources(settings, http, ["nodeseek"])] == ["nodeseek"]
    with pytest.raises(ValueError):
        build_sources(settings, http, ["hostloc"])


def test_lowendtalk(settings, http):
    result = make("lowendtalk", settings, http).fetch()
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
    assert [u for u in server.requests if "lowendspirit" in u] == les_urls
    assert sleeps == [1.0] * 6
    expected = {p.url for u in les_urls for p in feed_posts("lowendspirit", (FIXTURES / ROUTES[u]).read_bytes())}
    assert {p.url for p in result.posts} == expected
    assert len(result.posts) == len(expected)
    vm6 = next(p for p in result.posts if p.url == "https://lowendspirit.com/discussion/11544")
    assert vm6.categories == ["VPS"]
    assert result.stats == {"feeds": 7}


def test_lowendspirit_partial_and_total_failure(settings, http, server):
    broken = "https://lowendspirit.com/categories/vps/feed.rss"
    server.overrides[broken] = 500
    result = make("lowendspirit", settings, http).fetch()
    assert result.posts and set(result.stats["errors"]) == {broken}
    for url in ROUTES:
        server.overrides[url] = 503
    with pytest.raises(RuntimeError, match="全部 7 个入口失败"):
        make("lowendspirit", settings, http).fetch()


def test_lowendbox_uses_full_content(settings, http):
    posts = make("lowendbox", settings, http).fetch().posts
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
    assert server.requests == ["https://www.reddit.com/r/VPS+webhosting/new/.rss?limit=50"]  # D39
    assert len(result.posts) == 50
    assert {p.categories[0] for p in result.posts} == {"VPS", "webhosting"}
    assert all(re.fullmatch(r"https://www\.reddit\.com/r/\w+/comments/\w+", p.url) for p in result.posts)
    assert all(p.author and not p.author.startswith("/u/") for p in result.posts)


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
