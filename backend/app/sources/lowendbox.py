"""LowEndBox RSS：20 篇，content:encoded 为全文；优惠文与资讯混合，由分类器区分（第 12.3 节）。"""

from collections.abc import Mapping

from ..models import SourceKind
from .base import FetchResult, Source, feed_posts, register

FEED_URL = "https://lowendbox.com/feed/"


@register
class LowEndBox(Source):
    id = "lowendbox"
    name = "LowEndBox"
    kind = SourceKind.MIXED
    lang = "en"

    def fetch(self, state: Mapping | None = None) -> FetchResult:
        return FetchResult(feed_posts(self.id, self.http.get(FEED_URL).content), {"feeds": 1})
