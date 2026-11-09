"""NodeLoc（Discourse）分类 RSS：VPS、优惠情报、羊毛党三个分类，各 25 条，按情报处理（D82⑤、D88）。

条目自带分类名；优惠情报与羊毛党大多是 eSIM、银行开户、AI 额度一类，分类器对这两个分类从严（只收标题明确是
主机的）。Discourse 在正文末尾加"N 个帖子 - M 位参与者 / 阅读完整话题"，解析时去掉。robots.txt 允许。
"""

import re
from collections.abc import Mapping

from ..models import RawPost, SourceKind
from .base import FetchResult, Source, feed_posts, register

FEED_URLS = (
    "https://www.nodeloc.com/c/internet/vps/27.rss",
    "https://www.nodeloc.com/c/business/information/10.rss",
    "https://www.nodeloc.com/c/business/deal-hunter/43.rss",
)
FOOTER = re.compile(r"\s*<p><small>[^<]*</small></p>\s*<p><a href=\"[^\"]*\">[^<]*</a></p>\s*$")


def parse(data: bytes) -> list[RawPost]:
    posts = feed_posts(NodeLoc.id, data)
    for post in posts:
        post.content_html = FOOTER.sub("", post.content_html)
    return posts


@register
class NodeLoc(Source):
    id = "nodeloc"
    name = "NodeLoc"
    kind = SourceKind.MIXED
    lang = "zh"
    request_gap = 3.0

    def fetch(self, state: Mapping | None = None) -> FetchResult:
        return self.fetch_many(FEED_URLS, lambda data, url: parse(data))
