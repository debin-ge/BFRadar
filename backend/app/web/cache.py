"""应用内页面缓存（第 15.5 节）：按"语言 + 路径 + 规范化查询参数"缓存渲染结果，到期或数据更新即失效。

键由各页面的查询对象规范化（DealQuery.href() 之类），多余、乱序或取默认值的参数不会多占条目。调度器每跑完一轮
采集、一批 LLM 或一次汇率更新就调 clear()（Scheduler.add_listener）；没有调度器时（serve --no-scheduler、
另开进程跑 crawl）靠到期失效。只缓存 200 的响应。
"""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import Response

MAX_ENTRIES = 256  # 一页 5–130 KB，最多约 30 MB
PAGE_TTL = 300  # 首页与列表页（第 15.5 节）
DETAIL_TTL = 600  # 详情页、商家页、关于页、RSS


def max_age(seconds: int) -> str:
    return f"public, max-age={seconds}"


@dataclass(frozen=True)
class Cached:
    body: bytes
    headers: dict[str, str]


class PageCache:
    """线程安全的 LRU + TTL 缓存。clear() 之后，清除前开始渲染、清除后才写回的结果会被丢掉（不缓存旧数据）。"""

    def __init__(self, max_entries: int = MAX_ENTRIES, clock: Callable[[], float] = time.monotonic) -> None:
        self._data: OrderedDict[str, tuple[float, Cached]] = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_entries
        self._clock = clock
        self._generation = 0
        self.hits = self.misses = 0

    def __len__(self) -> int:
        return len(self._data)

    def lookup(self, key: str) -> tuple[Cached | None, int]:
        """（缓存值或 None, 当前代数）；代数传回 store()，用来识别渲染期间发生的清除。"""
        with self._lock:
            item = self._data.get(key)
            if item is not None and item[0] > self._clock():
                self._data.move_to_end(key)
                self.hits += 1
                return item[1], self._generation
            if item is not None:
                del self._data[key]
            self.misses += 1
            return None, self._generation

    def store(self, key: str, value: Cached, ttl: float, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._data[key] = (self._clock() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._generation += 1


def cached_response(request: Request, key: str, ttl: int, render: Callable[[], Response]) -> Response:
    """命中就直接回放；否则 render() 并在 200 时缓存 ttl 秒。响应头（含 Cache-Control）由 render 决定，原样回放。"""
    cache: PageCache = request.app.state.cache
    hit, generation = cache.lookup(key)
    if hit is not None:
        return Response(hit.body, headers=hit.headers)
    response = render()
    if response.status_code == 200:
        headers = {k: v for k, v in response.headers.items() if k != "content-length"}
        cache.store(key, Cached(bytes(response.body), headers), ttl, generation)
    return response
