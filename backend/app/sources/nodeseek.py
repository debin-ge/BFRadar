"""NodeSeek 官方 RSS：20 条滚动窗口（约 10 分钟），按帖子 ID 统计覆盖率（D24、FR-13）。"""

import re
from collections.abc import Mapping

from ..models import RawPost, SourceKind
from .base import FetchResult, Source, feed_posts, register

FEED_URL = "https://rss.nodeseek.com/"
POST_ID = re.compile(r"/post-(\d+)-")


def parse(data: bytes) -> list[RawPost]:
    posts = []
    for post in feed_posts(NodeSeek.id, data):
        if m := POST_ID.search(post.url):
            post.extra["post_id"] = int(m.group(1))
            posts.append(post)
    return posts


def coverage(ids: list[int], prev_max: int | None) -> dict:
    """本轮覆盖统计。

    有上一轮最大 ID 时：本轮应看到 (prev_max, max_id] 区间内的全部帖子，缺口 = 区间长度 − 实际看到的新帖；
    overlap 为本轮窗口是否回溯到了上一轮最大 ID（为 False 说明两轮之间整段漏抓）。
    删帖与不进 RSS 的帖子也计入缺口，所以覆盖率是下限。首轮只统计窗口内的连续度。
    """
    if not ids:
        return {"count": 0, "max_id": prev_max}
    lo, hi = min(ids), max(ids)
    stats: dict = {"count": len(ids), "min_id": lo, "max_id": max(hi, prev_max or 0)}
    if prev_max is None:
        expected, seen, overlap = hi - lo + 1, len(set(ids)), None
    else:
        seen = len({i for i in ids if i > prev_max})
        expected = max(0, hi - prev_max)
        overlap = lo <= prev_max + 1
    stats.update(new_ids=seen, expected=expected, gap=expected - seen, overlap=overlap,
                 coverage=round(seen / expected, 4) if expected else None)
    return stats


@register
class NodeSeek(Source):
    id = "nodeseek"
    name = "NodeSeek"
    kind = SourceKind.MIXED
    lang = "zh"

    def fetch(self, state: Mapping | None = None) -> FetchResult:
        posts = parse(self.http.get(FEED_URL).content)
        prev_max = (state or {}).get("max_id")
        return FetchResult(posts, coverage([p.extra["post_id"] for p in posts], prev_max))
