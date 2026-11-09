"""V2EX 节点 feed：推广节点与 VPS 节点，各 50 条，按情报处理（D82⑤、D88）。

Atom 条目没有分类标签：把节点名记在分类里，分类器据此对推广节点从严（推广节点大多是 AI 中转、代理 IP、机场，
只收标题明确是主机的）。条目链接带 `#replyN`（随回复数变化），URL 规范化时去掉。robots.txt 允许。
"""

from collections.abc import Mapping

from ..models import RawPost, SourceKind
from .base import FetchResult, Source, feed_posts, register

NODES = ("promotions", "vps")
FEED_URL = "https://www.v2ex.com/feed/{}.xml"


def parse(data: bytes, node: str) -> list[RawPost]:
    posts = feed_posts(V2ex.id, data)
    for post in posts:
        post.categories = [node]
    return posts


@register
class V2ex(Source):
    id = "v2ex"
    name = "V2EX"
    kind = SourceKind.MIXED
    lang = "zh"
    request_gap = 3.0

    def fetch(self, state: Mapping | None = None) -> FetchResult:
        urls = {FEED_URL.format(n): n for n in NODES}
        return self.fetch_many(urls, lambda data, url: parse(data, urls[url]))
