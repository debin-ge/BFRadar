"""LowEndTalk Offers 分类 RSS：100 条滚动窗口，按最后活跃排序（D23），全文在 description。"""

from collections.abc import Mapping

from ..models import SourceKind
from .base import FetchResult, Source, feed_posts, register

FEED_URL = "https://lowendtalk.com/categories/offers/feed.rss"


@register
class LowEndTalk(Source):
    id = "lowendtalk"
    name = "LowEndTalk"
    kind = SourceKind.OFFERS
    lang = "en"

    def fetch(self, state: Mapping | None = None) -> FetchResult:
        return FetchResult(feed_posts(self.id, self.http.get(FEED_URL).content), {"feeds": 1})
