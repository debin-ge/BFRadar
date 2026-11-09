"""LowEndTalk Offers 分类 RSS：每页 100 条，按最后活跃排序（D23），全文在 description。

抓 Offers 第 1、2 页（D82）：窗口由 100 条扩到 200 条；再抓全站讨论 feed（最近活跃的 100 帖，各分类混在一起），
其中 Shared Hosting Offers、Giveaways & Freebies 两个分类的帖子是优惠（Offers feed 里没有），其余分类由分类器判为
情报或无关（第 12.3 节）。同一帖在几个 feed 里内容相同（11-07 实测），按 URL 去重。
"""

from collections.abc import Mapping

from ..models import SourceKind
from .base import FetchResult, Source, feed_posts, register

FEED_URL = "https://lowendtalk.com/categories/offers/feed.rss"
FEED_URLS = (FEED_URL, "https://lowendtalk.com/categories/offers/p2/feed.rss",
             "https://lowendtalk.com/discussions/feed.rss")


@register
class LowEndTalk(Source):
    id = "lowendtalk"
    name = "LowEndTalk"
    kind = SourceKind.OFFERS
    lang = "en"

    def fetch(self, state: Mapping | None = None) -> FetchResult:
        return self.fetch_many(FEED_URLS, lambda data, url: feed_posts(self.id, data))
