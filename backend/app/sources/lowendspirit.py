"""LowEndSpirit Offers：父分类 offers 与 6 个子分类各一个 feed，按 URL 去重（D36）。"""

from collections.abc import Mapping

from ..models import SourceKind
from .base import FetchResult, Source, feed_posts, register

FEED_URL = "https://lowendspirit.com/categories/{}/feed.rss"


@register
class LowEndSpirit(Source):
    id = "lowendspirit"
    name = "LowEndSpirit"
    kind = SourceKind.OFFERS
    lang = "en"

    def fetch(self, state: Mapping | None = None) -> FetchResult:
        urls = [FEED_URL.format(slug) for slug in self.settings.les_categories]
        return self.fetch_many(urls, lambda data, url: feed_posts(self.id, data))
