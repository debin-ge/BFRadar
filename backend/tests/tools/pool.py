"""金标集工具共用：从快照中取出单条帖子，结构化文本用 app.textutil（与正式流水线同一份实现）。"""

import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS.parent))
from app.textutil import structure_html as render  # noqa: E402
FIXTURES = TESTS / "fixtures"
SEASON_START = datetime(2026, 10, 1, tzinfo=timezone.utc)
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
CREATOR = "{http://purl.org/dc/elements/1.1/}creator"
BF_TITLE = re.compile(
    r"black[\s-]?friday|cyber[\s-]?monday|\bBFCM\b|\bBF\b|\bbf20\d\d\b|黑五|黑色星期五|黑5|网一|网络星期一", re.I
)
PRICE = re.compile(r"[$€£¥]\s?\d|\d\s?(?:USD|EUR|GBP|CNY|刀|元)\b|/\s?(?:mo|yr|month|year)\b|annually|monthly|年付|月付", re.I)


class SnapshotMissing(FileNotFoundError):
    """快照文件不存在：第三方帖子原文不随仓库发布，测试遇到它就跳过（见 tests/fixtures/README.md）。"""


@dataclass(frozen=True)
class Feed:
    prefix: str  # 样本 id 前缀
    glob: str  # 快照文件
    id_pattern: str  # 在 "link guid" 中取帖子 id
    url: str | None  # 规范化 URL 模板，None 表示用原 link


FEEDS = {
    "lowendtalk": Feed("let", "lowendtalk_offers_*.xml", r"/discussion/(\d+)", "https://lowendtalk.com/discussion/{id}"),
    "lowendspirit": Feed("les", "lowendspirit_*.xml", r"/discussion/(\d+)", "https://lowendspirit.com/discussion/{id}"),
    "lowendbox": Feed("leb", "lowendbox_feed_*.xml", r"[?&]p=(\d+)", None),
    "nodeseek": Feed("ns", "nodeseek/nodeseek_*.xml", r"/post-(\d+)", "https://www.nodeseek.com/post-{id}-1"),
}


@dataclass(frozen=True)
class Item:
    id: str
    source: str
    url: str
    fixture: Path
    guid: str
    published: datetime
    title: str
    author: str
    categories: tuple[str, ...]
    html: str

    @property
    def bf(self) -> bool:
        return bool(BF_TITLE.search(self.title))

    @property
    def fixture_rel(self) -> str:
        return self.fixture.relative_to(TESTS).as_posix()


def iter_items(fixture: Path, source: str):
    feed = FEEDS[source]
    for node in ET.parse(fixture).getroot().iter("item"):
        link = (node.findtext("link") or "").strip()
        guid = (node.findtext("guid") or "").strip()
        m = re.search(feed.id_pattern, f"{link} {guid}")
        if not m:
            continue
        post_id = m.group(1)
        yield Item(
            id=f"{feed.prefix}-{post_id}",
            source=source,
            url=feed.url.format(id=post_id) if feed.url else link,
            fixture=fixture,
            guid=guid,
            published=parsedate_to_datetime(node.findtext("pubDate")).astimezone(timezone.utc),
            title=(node.findtext("title") or "").strip(),
            author=(node.findtext(CREATOR) or "").strip(),
            categories=tuple(c.text.strip() for c in node.findall("category") if c.text and c.text.strip()),
            html=node.findtext(CONTENT) or node.findtext("description") or "",
        )


def _snapshot_order(path: Path) -> tuple[str, str]:
    m = re.search(r"\d{4}-\d{2}-\d{2}(?:T\d+Z)?", path.name)
    return (m.group(0) if m else "", path.name)


def load_pool(sources=FEEDS, since: datetime | None = SEASON_START) -> dict[str, Item]:
    """快照池：按帖子去重，保留最早出现的那份快照（标注以它为准，保证可复现）。"""
    pool: dict[str, Item] = {}
    for source in sources:
        for fixture in sorted(FIXTURES.glob(FEEDS[source].glob), key=_snapshot_order):
            for item in iter_items(fixture, source):
                if item.id not in pool and (since is None or item.published >= since):
                    pool[item.id] = item
    return pool


def load_item(fixture_rel: str, guid: str, source: str) -> Item:
    if not (TESTS / fixture_rel).exists():
        raise SnapshotMissing(f"缺抓取样本 {fixture_rel}（不随仓库发布，见 tests/fixtures/README.md）")
    for item in iter_items(TESTS / fixture_rel, source):
        if item.guid == guid:
            return item
    raise KeyError(f"{fixture_rel} 中没有 guid={guid}")


def guess_format(html_text: str) -> str:
    """粗略判断套餐形态（第 5.12 节），只用于选样列表，标注时以人工判断为准。"""
    text, _ = render(html_text)
    if not PRICE.search(text):
        return "none"
    counts = {
        "pipe": sum(1 for line in text.splitlines() if "|" in line and PRICE.search(line) and "<tr" not in html_text.lower()),
        "table": html_text.lower().count("<tr"),
        "list": len(re.findall(r"<li[\s>]", html_text, re.I)),
    }
    best = max(counts, key=counts.get)
    return best if counts[best] >= 2 else "freetext"
