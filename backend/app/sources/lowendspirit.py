"""LowEndSpirit Offers：父分类 offers 与 6 个子分类各一个 feed，再加全站讨论 feed（D82），按 URL 去重（D36）。

讨论 feed 里 Offers 各子分类以外的帖子由分类器判为情报或无关（第 12.3 节）。
"""

from collections.abc import Mapping

from ..models import SourceKind
from .base import FetchResult, Source, feed_posts, register

FEED_URL = "https://lowendspirit.com/categories/{}/feed.rss"
DISCUSSIONS_URL = "https://lowendspirit.com/discussions/feed.rss"


@register
class LowEndSpirit(Source):
    id = "lowendspirit"
    name = "LowEndSpirit"
    kind = SourceKind.OFFERS
    lang = "en"

    def fetch(self, state: Mapping | None = None) -> FetchResult:
        urls = [FEED_URL.format(slug) for slug in self.settings.les_categories] + [DISCUSSIONS_URL]
        return self.fetch_many(urls, lambda data, url: feed_posts(self.id, data))
