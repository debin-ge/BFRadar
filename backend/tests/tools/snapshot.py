"""M0 快照脚本（D19）：把各采集源的 RSS / 页面原样保存到 tests/fixtures/，供金标集选样与解析测试。

只用标准库，不依赖 app/ 代码，M1 之前即可运行。

  uv run python tests/tools/snapshot.py                    # 全部源各抓一次（同日文件已存在则跳过）
  uv run python tests/tools/snapshot.py --sources nodeseek --loop --interval 300 --until 2026-11-03T12:00-08:00
  uv run python tests/tools/snapshot.py --pool lowendtalk  # 列出快照池中的本季帖子，供按配额选样
  uv run python tests/tools/snapshot.py --show let-222765  # 以结构化文本查看一条帖子
  uv run python tests/tools/snapshot.py --draft let-222765 les-12345   # 生成空白标注草稿

文件名中的 {date} 为 UTC 日期，同日只保存一份；NodeSeek 每次按 UTC 时间戳新存一份（窗口只有约 10 分钟，D24）。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from pool import FEEDS, TESTS, guess_format, load_pool, render

FIXTURES = TESTS / "fixtures"
GOLD = TESTS / "gold"
LOG_FILE = FIXTURES / "snapshot.log"
USER_AGENT = os.environ.get("BFRADAR_UA", "Mozilla/5.0 (compatible; BFRadar-dev/0.1)")
TIMEOUT = 30
REQUEST_GAP = 2  # 同一轮内相邻请求间隔秒数；Reddit 对连续匿名请求会返回 429
RETRY_STATUS = {429, 500, 502, 503, 504}
CHALLENGE_MARKERS = (b"<title>Just a moment", b"Attention Required! | Cloudflare")
ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class Target:
    source: str
    url: str
    filename: str
    kind: str  # xml / json / html


# LES 的 Offers 是父分类，新帖都发在子分类里，父分类 feed 只有直接发在父分类下的老帖（D36）
LES_OFFER_CATEGORIES = {
    "offers": "offers",
    "vps": "vps",
    "dedicated-server": "dedicated-server",
    "shared-hosting": "shared-hosting",
    "services": "services",
    "free-offers-and-stuff": "free-offers-and-stuff",
    "must-see-deals-": "must-see-deals",
}

TARGETS = [
    Target("lowendtalk", "https://lowendtalk.com/categories/offers/feed.rss", "lowendtalk_offers_{date}.xml", "xml"),
    *(
        Target("lowendspirit", f"https://lowendspirit.com/categories/{slug}/feed.rss", f"lowendspirit_{name}_{{date}}.xml", "xml")
        for slug, name in LES_OFFER_CATEGORIES.items()
    ),
    Target("lowendbox", "https://lowendbox.com/feed/", "lowendbox_feed_{date}.xml", "xml"),
    Target("lowendbox", "https://lowendbox.com/wp-json/wp/v2/posts?search=black%20friday&per_page=20", "lowendbox_rest_bf_{date}.json", "json"),
    Target("lowendbox", "https://lowendbox.com/wp-json/wp/v2/posts?per_page=20", "lowendbox_rest_recent_{date}.json", "json"),
    Target("nodeseek", "https://rss.nodeseek.com/", "nodeseek/nodeseek_{ts}.xml", "xml"),
    # 匿名限额约每分钟 1 次，合并子版块一次请求（D39）
    Target("reddit", "https://www.reddit.com/r/VPS+webhosting/new/.rss?limit=50", "reddit_multi_{date}.xml", "xml"),
]
SOURCES = sorted({t.source for t in TARGETS})


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(url: str) -> bytes:
    """GET，429 / 5xx / 网络错误时退避重试两次（5 秒、10 秒）。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUS or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(5 * 2**attempt)
    raise AssertionError("unreachable")


def check(body: bytes, kind: str) -> str:
    """确认响应是预期内容而不是质询页或空结果，返回一句摘要。"""
    if any(m in body[:20000] for m in CHALLENGE_MARKERS):
        raise ValueError("返回的是 Cloudflare 质询页")
    if kind == "xml":
        root = ET.fromstring(body)
        n = len(root.findall(".//item")) or len(root.findall(f"{ATOM}entry"))
        if not n:
            raise ValueError("feed 中没有条目")
        return f"{n} 条"
    if kind == "json":
        data = json.loads(body)
        if not isinstance(data, list):
            raise ValueError(f"预期 JSON 数组，实际为 {type(data).__name__}")
        return f"{len(data)} 篇"
    if b"<table" not in body:
        raise ValueError("页面中没有表格")
    return f"{body.count(b'<tr')} 行表格"


def target_path(t: Target, now: datetime) -> Path:
    path = FIXTURES / t.filename.format(date=f"{now:%Y-%m-%d}", ts=f"{now:%Y-%m-%dT%H%MZ}")
    if "{ts}" in t.filename and path.exists():
        path = FIXTURES / t.filename.format(ts=f"{now:%Y-%m-%dT%H%M%SZ}")
    return path


def snapshot(t: Target, force: bool) -> Path | None:
    path = target_path(t, datetime.now(timezone.utc))
    if path.exists() and not force:
        log(f"{t.source:<12} 跳过 {path.relative_to(FIXTURES)}（今日已存在，--force 覆盖）")
        return None
    body = fetch(t.url)
    summary = check(body, t.kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(body)
    tmp.replace(path)
    log(f"{t.source:<12} {path.relative_to(FIXTURES)}  {len(body) / 1024:.0f} KB  {summary}")
    return path


def nodeseek_items(path: Path) -> dict[int, datetime | None]:
    """帖子 ID → 发布时间。NodeSeek 的 guid 就是帖子 ID。"""
    items = {}
    for item in ET.parse(path).getroot().iter("item"):
        guid = (item.findtext("guid") or "").strip()
        if guid.isdigit():
            pub = item.findtext("pubDate")
            items[int(guid)] = parsedate_to_datetime(pub) if pub else None
    return items


class NodeSeekCoverage:
    """按帖子 ID 统计累计覆盖率 = 已见 ID 数 / (最大 ID − 最小 ID + 1)。

    删帖和不进 RSS 的帖子也会形成缺口，所以这是覆盖率的下限；
    相邻两份快照没有重叠则说明两次抓取之间一定漏了帖子。
    """

    def __init__(self) -> None:
        self.seen: set[int] = set()
        self.last: set[int] = set()
        for path in sorted((FIXTURES / "nodeseek").glob("nodeseek_*.xml")):
            self.last = set(nodeseek_items(path))
            self.seen |= self.last

    def update(self, path: Path) -> str:
        items = nodeseek_items(path)
        ids = set(items)
        overlap = len(ids & self.last) if self.last else None
        new = len(ids - self.seen)
        self.seen |= ids
        self.last = ids
        pubs = [p for p in items.values() if p]
        span = (max(pubs) - min(pubs)).total_seconds() / 60 if pubs else 0
        coverage = len(self.seen) / (max(self.seen) - min(self.seen) + 1)
        msg = (f"nodeseek     窗口 {span:.1f} 分钟，新帖 {new}，与上一份重叠 {overlap if overlap is not None else '-'}，"
               f"累计 {len(self.seen)} 帖，ID 覆盖率 {coverage:.0%}")
        return msg + ("  ⚠ 与上一份无重叠，两次抓取之间漏帖" if overlap == 0 else "")


def run_round(targets: list[Target], force: bool, coverage: NodeSeekCoverage | None) -> int:
    failures = 0
    for i, t in enumerate(targets):
        if i:
            time.sleep(REQUEST_GAP)
        try:
            path = snapshot(t, force)
            if path and coverage and t.source == "nodeseek":
                log(coverage.update(path))
        except Exception as e:  # 单个源失败不影响其他源
            failures += 1
            log(f"{t.source:<12} 失败 {t.url}：{type(e).__name__}: {e}")
    return failures


def show_pool(sources: list[str]) -> None:
    """列出本季（第 2.5 节）帖子：id、发布日、是否黑五标题、粗略形态、作者、标题，以及按来源的汇总。"""
    pool = sorted(load_pool(sources).values(), key=lambda i: (i.source, -i.published.timestamp()))
    for item in pool:
        fmt = guess_format(item.html)
        print(f"{item.id:<12} {item.published:%m-%d} {'BF' if item.bf else '  '} {fmt:<8} {item.author[:16]:<16} {item.title[:90]}")
    for source in sources:
        items = [i for i in pool if i.source == source]
        formats = Counter(guess_format(i.html) for i in items)
        print(f"\n{source}: 本季 {len(items)} 条，黑五标题 {sum(i.bf for i in items)} 条，粗略形态 {dict(formats)}")


def show_item(sample_id: str) -> None:
    item = load_pool(since=None).get(sample_id)
    if item is None:
        sys.exit(f"快照池中没有 {sample_id}")
    text, links = render(item.html)
    print(f"# {item.id}  {item.url}\n# {item.fixture_rel}  guid={item.guid}")
    print(f"# 发布 {item.published.isoformat()}  作者 {item.author}  分类 {', '.join(item.categories)}\n")
    print(f"标题：{item.title}\n\n{text}\n\n链接表：")
    for n, anchor, href in links:
        print(f"  [{n}] {anchor[:60]} → {href}")


def blank_labels() -> dict:
    return {
        "record_kind": None, "is_black_friday": None, "event_type": None, "product_types": [], "lang": None,
        "vendor": None, "discount": {"pct": None, "recurring": None, "text": None}, "coupon_codes": [],
        "stock_status": None, "stock_evidence": None, "purchase_url": None, "ends_at": None, "plan_format": None,
        "plans_truncated": False, "plans": [], "intel": None, "notes": None,
    }


def write_drafts(sample_ids: list[str]) -> None:
    """生成只含定位信息的空白草稿（D19），已存在的文件不覆盖。"""
    pool = load_pool(since=None)
    GOLD.mkdir(parents=True, exist_ok=True)
    for sample_id in sample_ids:
        item = pool.get(sample_id)
        path = GOLD / f"{sample_id}.json"
        if item is None:
            print(f"跳过 {sample_id}：快照池中没有")
            continue
        if path.exists():
            print(f"跳过 {sample_id}：{path.name} 已存在")
            continue
        draft = {
            "id": item.id, "source": item.source, "url": item.url, "fixture": item.fixture_rel,
            "fixture_item_guid": item.guid, "published_at": item.published.isoformat(), "annotator": "claude",
            "reviewed_by": None, "spec_version": "1.1", "split": None, "labels": blank_labels(),
        }
        path.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已生成 {path.relative_to(TESTS)}")


def parse_until(value: str) -> datetime:
    until = datetime.fromisoformat(value)
    return until.astimezone() if until.tzinfo is None else until


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", default=",".join(SOURCES), help=f"逗号分隔，可选：{','.join(SOURCES)}")
    ap.add_argument("--force", action="store_true", help="覆盖今日已存在的快照")
    ap.add_argument("--loop", action="store_true", help="循环抓取直到 --until")
    ap.add_argument("--interval", type=int, default=300, help="循环间隔秒数，默认 300，最小 60")
    ap.add_argument("--until", type=parse_until, help="循环截止时间，ISO 8601；不带时区按本机时区")
    ap.add_argument("--pool", nargs="*", metavar="SOURCE", help=f"列出快照池中的本季帖子，可选：{','.join(FEEDS)}")
    ap.add_argument("--show", metavar="ID", help="以结构化文本查看一条帖子，如 let-222765")
    ap.add_argument("--draft", nargs="+", metavar="ID", help="为选中的样本生成空白标注草稿")
    args = ap.parse_args(argv)

    if args.pool is not None:
        show_pool(args.pool or list(FEEDS))
        return 0
    if args.show:
        show_item(args.show)
        return 0
    if args.draft:
        write_drafts(args.draft)
        return 0

    wanted = set(args.sources.split(","))
    if unknown := wanted - set(SOURCES):
        ap.error(f"未知的源：{','.join(sorted(unknown))}")
    targets = [t for t in TARGETS if t.source in wanted]
    coverage = NodeSeekCoverage() if "nodeseek" in wanted else None

    if not args.loop:
        return 1 if run_round(targets, args.force, coverage) else 0

    if args.until is None:
        ap.error("--loop 需要 --until")
    if args.interval < 60:
        ap.error("--interval 不能小于 60 秒")
    log(f"开始循环：{','.join(sorted(wanted))}，每 {args.interval} 秒，截止 {args.until.isoformat()}")
    next_run = time.monotonic()
    try:
        while datetime.now(timezone.utc) < args.until:
            run_round(targets, args.force, coverage)
            next_run = max(next_run + args.interval, time.monotonic())  # 某轮超时后不连发补抓
            time.sleep(next_run - time.monotonic())
    except KeyboardInterrupt:
        log("手动停止")
    else:
        log("到达截止时间，结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
