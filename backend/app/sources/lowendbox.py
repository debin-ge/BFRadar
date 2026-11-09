"""LowEndBox RSS：20 篇，content:encoded 为全文；优惠文与资讯混合，由分类器区分（第 12.3 节）。

另抓黑五标签 feed（D82）：黑五周的汇总长文都打这个标签，往年的文章按发布时间只进"含往季"。
"""

from collections.abc import Mapping

from ..models import SourceKind
from .base import FetchResult, Source, feed_posts, register

FEED_URL = "https://lowendbox.com/feed/"
FEED_URLS = (FEED_URL, "https://lowendbox.com/tag/black-friday/feed/")


@register
class LowEndBox(Source):
    id = "lowendbox"
    name = "LowEndBox"
    kind = SourceKind.MIXED
    lang = "en"

    def fetch(self, state: Mapping | None = None) -> FetchResult:
        return self.fetch_many(FEED_URLS, lambda data, url: feed_posts(self.id, data))
