"""HostingDiscussion（XenForo）全站 RSS：20 条，按情报处理（D82⑤、D88）。

正文是截断的摘要（末尾"…Read more"），套餐列表常被截掉，所以优惠版块的帖子也只当情报，不抽价格；条目自带版块名，
分类器据此分资讯与提问。robots.txt 404（无限制）。
"""

import re
from collections.abc import Mapping

from ..models import RawPost, SourceKind
from .base import FetchResult, Source, feed_posts, register

FEED_URL = "https://hostingdiscussion.com/forums/-/index.rss"
READ_MORE = re.compile(r"\s*<a [^>]*>Read more</a>(?=\s*</div>\s*$)")


def parse(data: bytes) -> list[RawPost]:
    posts = feed_posts(HostingDiscussion.id, data)
    for post in posts:
        post.content_html = READ_MORE.sub("", post.content_html)
    return posts


@register
class HostingDiscussion(Source):
    id = "hostingdiscussion"
    name = "HostingDiscussion"
    kind = SourceKind.MIXED
    lang = "en"

    def fetch(self, state: Mapping | None = None) -> FetchResult:
        return self.fetch_many((FEED_URL,), lambda data, url: parse(data))
