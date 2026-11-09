"""Reddit：社区版块、主流品牌官方版块与全站黑五标题搜索，三个入口轮流抓（D39、D83）。

匿名请求的限额约每分钟 1 次（11-02 实测 x-ratelimit-remaining 用 1 次即归零；11-05 实测间隔 70 秒连续 14 次都成功），
所以每轮只请求一个入口，下一轮换下一个；常态 20 分钟一轮，每个入口约每小时抓一次。一个入口内的子版块合并成一个
请求（`/r/A+B/new/.rss`）。暂不考虑 2026-11-13 停止 RSS，停了再议。

轮转靠 state：stats 记下一轮该抓第几个入口（`next`）。某个入口出错时这一轮只记 `errors`、照样换下一个，免得一个
入口长期失败把轮转卡住；上一轮也出错（连续两个入口失败，多半是 Reddit 整体不可用）才算该源失败，状态页可见。

丢掉的帖子：优惠码与推荐链接版块（搜索结果里最常见）、用户个人主页帖（`u_…`）。
"""

import logging
import re
from collections.abc import Mapping
from urllib.parse import quote_plus

from ..config import REDDIT_VENDOR_SUBREDDITS
from ..models import RawPost, SourceKind
from .base import FetchResult, Source, feed_posts, register

log = logging.getLogger(__name__)

FEED_URL = "https://www.reddit.com/r/{}/new/.rss?limit=100"
# 优惠码、推荐链接与刷单版块：搜索"black friday hosting"时大量出现，内容是复制来的码与推广链接
BLOCKED_SUBREDDITS = {s.lower() for s in (
    "PromoCodesDaily", "couponcodes", "CouponCodeBD", "LowestPriceDeals", "PromoCodeShare", "Referrallinks",
    "DigitalEarnHubb", "referralcodes", "promocodes", "coupons", "dealsreddit", "AffiliateMarketing",
)}
SEARCH_TERMS = ('title:("black friday" OR "cyber monday" OR BFCM) '
                '(vps OR vds OR hosting OR "dedicated server" OR seedbox OR "bare metal")')
SEARCH_EXCLUDE = ("PromoCodesDaily", "couponcodes", "CouponCodeBD", "LowestPriceDeals")  # 查询长度有限，其余在本地丢
SUBREDDIT_IN_URL = re.compile(r"/r/([^/]+)/")


def feed_url(subreddits: tuple[str, ...]) -> str:
    return FEED_URL.format("+".join(subreddits))


def search_url() -> str:
    query = SEARCH_TERMS + "".join(f" NOT subreddit:{s}" for s in SEARCH_EXCLUDE)
    return f"https://www.reddit.com/search.rss?q={quote_plus(query)}&sort=new&t=month&limit=100"


def subreddit(post: RawPost) -> str:
    if post.categories:
        return post.categories[0]
    m = SUBREDDIT_IN_URL.search(post.url)
    return m.group(1) if m else ""


def parse(data: bytes) -> tuple[list[RawPost], int]:
    """（留下的帖子, 丢掉的条数）。"""
    kept, dropped = [], 0
    for post in feed_posts(Reddit.id, data):
        if post.author and post.author.startswith("/u/"):
            post.author = post.author[3:]
        sub = subreddit(post).lower()
        if sub in BLOCKED_SUBREDDITS or sub.startswith("u_") or "/user/" in post.url:
            dropped += 1
            continue
        kept.append(post)
    return kept, dropped


@register
class Reddit(Source):
    id = "reddit"
    name = "Reddit"
    kind = SourceKind.MIXED
    lang = "en"

    def entries(self) -> list[tuple[str, str]]:
        out = [("community", feed_url(self.settings.reddit_subreddits))]
        if REDDIT_VENDOR_SUBREDDITS:
            out.append(("vendors", feed_url(REDDIT_VENDOR_SUBREDDITS)))
        out.append(("search", search_url()))
        return out

    def fetch(self, state: Mapping | None = None) -> FetchResult:
        state = state or {}
        entries = self.entries()
        index = state.get("next", 0) if isinstance(state.get("next"), int) else 0
        name, url = entries[index % len(entries)]
        stats: dict = {"entry": name, "next": (index + 1) % len(entries)}
        try:
            posts, dropped = parse(self.http.get(url).content)
        except Exception as e:
            if state.get("errors"):  # 连续两个入口失败：算该源失败
                raise
            log.warning("reddit 入口 %s 失败：%s", name, e)
            return FetchResult([], {**stats, "errors": {url: str(e)}})
        stats["dropped"] = dropped
        return FetchResult(posts, stats)
