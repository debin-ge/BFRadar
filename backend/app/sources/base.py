"""采集源插件接口与 RSS / Atom 解析（《需求与方案说明》第 12.1 节）。

新增采集源：在 app/sources/ 下新建文件，定义 Source 子类并加 @register，再在 app/sources/__init__.py 中导入。
"""

import calendar
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import ClassVar

import feedparser

from ..config import Settings
from ..http import HttpClient
from ..models import RawPost, SourceKind
from ..textutil import normalize_url

log = logging.getLogger(__name__)


@dataclass
class FetchResult:
    posts: list[RawPost]
    stats: dict = field(default_factory=dict)  # 写入 run_sources.extra_json，下一轮作为 state 传回 fetch()


class Source(ABC):
    id: ClassVar[str]
    name: ClassVar[str]
    kind: ClassVar[SourceKind]
    lang: ClassVar[str]
    request_gap: ClassVar[float] = 1.0  # 同一源多个请求之间的间隔秒数

    def __init__(self, settings: Settings, http: HttpClient, sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings = settings
        self.http = http
        self._sleep = sleep

    @abstractmethod
    def fetch(self, state: Mapping | None = None) -> FetchResult:
        """抓取一轮。state 为上一轮成功时返回的 stats。"""

    def interval(self, day: date) -> int:
        return self.settings.interval(self.id, day)

    def fetch_many(self, urls: Iterable[str], parse: Callable[[bytes, str], list[RawPost]]) -> FetchResult:
        """依次抓取多个入口并按 URL 去重；部分入口失败只记入 stats，全部失败才抛出。"""
        posts: dict[str, RawPost] = {}
        errors: dict[str, str] = {}
        urls = list(urls)
        for i, url in enumerate(urls):
            if i:
                self._sleep(self.request_gap)
            try:
                for post in parse(self.http.get(url).content, url):
                    posts.setdefault(post.url, post)
            except Exception as e:  # 单个入口失败不影响其他入口
                log.warning("%s 入口失败：%s", self.id, e)
                errors[url] = str(e)
        if errors and len(errors) == len(urls):
            raise RuntimeError(f"{self.id} 全部 {len(urls)} 个入口失败：{next(iter(errors.values()))}")
        stats = {"feeds": len(urls)}
        if errors:
            stats["errors"] = errors
        return FetchResult(list(posts.values()), stats)


REGISTRY: dict[str, type[Source]] = {}


def register(cls: type[Source]) -> type[Source]:
    if cls.id in REGISTRY:
        raise ValueError(f"采集源 id 重复：{cls.id}")
    REGISTRY[cls.id] = cls
    return cls


# ---------------------------------------------------------------- RSS / Atom


def parse_feed(data: bytes) -> feedparser.FeedParserDict:
    """容错解析；不做 HTML 清洗与相对链接改写（页面只输出转义后的文本，原文保持原样）。"""
    feed = feedparser.parse(data, sanitize_html=False, resolve_relative_uris=False)
    if not feed.entries and feed.get("bozo"):
        raise ValueError(f"无法解析 feed：{feed.get('bozo_exception')}")
    return feed


def entry_time(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime.fromtimestamp(calendar.timegm(parsed), UTC) if parsed else None


def entry_html(entry) -> str:
    """优先全文（content:encoded / Atom content），其次 description。"""
    for content in entry.get("content") or []:
        if content.get("value"):
            return content["value"]
    return entry.get("summary", "") or ""


def entry_post(source: str, entry, **extra) -> RawPost | None:
    link = entry.get("link")
    if not link:
        return None
    return RawPost(
        source=source,
        url=normalize_url(link),
        title=(entry.get("title") or "").strip(),
        content_html=entry_html(entry),
        author=(entry.get("author") or "").strip() or None,
        published_at=entry_time(entry),
        categories=[t["term"] for t in entry.get("tags") or [] if t.get("term")],
        extra=dict(extra),
    )


def feed_posts(source: str, data: bytes) -> list[RawPost]:
    return [p for e in parse_feed(data).entries if (p := entry_post(source, e)) is not None]
