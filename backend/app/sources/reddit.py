"""Reddit 子版块 new/.rss（Atom）。

匿名请求的限额约为每分钟 1 次（11-02 实测 x-ratelimit-remaining 用 1 次即归零），所以把所有子版块合并成一个
请求：/r/VPS+webhosting/new/.rss?limit=50（D39）。
"""

from collections.abc import Mapping

from ..models import RawPost, SourceKind
from .base import FetchResult, Source, feed_posts, register

FEED_URL = "https://www.reddit.com/r/{}/new/.rss?limit=50"


def feed_url(subreddits: tuple[str, ...]) -> str:
    return FEED_URL.format("+".join(subreddits))


def parse(data: bytes) -> list[RawPost]:
    posts = feed_posts(Reddit.id, data)
    for post in posts:
        if post.author and post.author.startswith("/u/"):
            post.author = post.author[3:]
    return posts


@register
class Reddit(Source):
    id = "reddit"
    name = "Reddit"
    kind = SourceKind.MIXED
    lang = "en"

    def fetch(self, state: Mapping | None = None) -> FetchResult:
        return FetchResult(parse(self.http.get(feed_url(self.settings.reddit_subreddits)).content), {"feeds": 1})
