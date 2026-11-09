"""Telegram 公开频道 @VPS_spiders 的网页预览（`t.me/s/<频道>`，不用 Bot 与账号；D80、D81）。

这个频道把 LET、LES 上商家发的优惠转成固定模板，黑五期间连 MegaThread 里按模板发的评论也转——LET 讨论帖的网页全在
Cloudflare 质询后面，这是不绕过防护就能拿到 MegaThread 的唯一通道。一条转发的样子：

    #vendor                      ← 商家标签（可选）
    商家评论：点我查看            ← 私有群 t.me/c/… 链接，忽略
    注册或登录：点我跳转…         ← 商家官网，忽略
    原始地址：查看                ← LET / LES 原评论的永久链接
    #let #黑五2025 #黑五          ← 频道打的标签
    （空行）
    套餐标题
    CPU: … / Memory: … / … / Price: … / Stock: …
    Order Here                   ← 下单链接，可能带推广参数

出处规则（D81）：记录的 URL 与去重键是原评论地址，不是 t.me 地址；只收原始地址指向 LET / LES 评论的消息——指向
整帖的由 LET、LES 自己的 RSS 收（全文更全），没有原始地址的多是补货监控（D63 不收库存），都跳过。链接里的推广参数
（aff= 等）与 utm_ 参数去掉，推广参数在路径里去不掉的下单链接整个不要；头部的私有群与官网链接不收；Stock 一行不收
（D63）。频道的 #黑五 标签记在分类里，分类器据此判黑五（模板正文往往不写 Black Friday）。

增量：消息 ID 递增但不连续（删掉的、只有图片的不出现），每轮从最新一页按 `?before=<最小 ID>` 往回翻，翻到上一轮
的最大 ID 为止（最多 MAX_PAGES 页）；stats 记本轮 ID 区间、翻了几页、是否接上了上一轮。
"""

import html
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from ..models import RawPost, SourceKind
from ..textutil import normalize_url
from .base import FetchResult, Source, register

CHANNEL = "VPS_spiders"
PAGE_URL = f"https://t.me/s/{CHANNEL}"
MAX_PAGES = 10  # 一页约 20 条可见消息；黑五高峰约 180 条/天，10 分钟一轮翻一两页就够，停机后补抓最多 200 条

ORIGIN_HOSTS = {"lowendtalk.com", "www.lowendtalk.com", "lowendspirit.com", "www.lowendspirit.com"}
COMMENT_PATH = re.compile(r"/discussion/comment/\d+")
BR = re.compile(r"<br\s*/?>", re.I)
HASHTAG_HREF = re.compile(r"^\?q=%23(.+)$")
STOCK_LINE = re.compile(r"^\s*(?:stock|库存|状态)\s*[:：]", re.I)
FIELD_LINE = re.compile(r"(?:CPU|vCPU|Cores?|Memory|RAM|Disk|Storage|SSD|NVMe|Traffic|Bandwidth|Transfer|Port\s*Speed|"
                        r"Location|IPv4|IPv6|Price|Coupon|Virtuali[sz]ation|商品|类别|内存|硬盘|流量|端口|位置|价格)\s*[:：]",
                        re.I)
ORDER_ANCHOR = re.compile(r"order|buy|purchase|购买|下单", re.I)
# 推广参数：WHMCS 的 aff=、常见的 ref= / referral= / affid= 等；路径里的 /aff.php、/aff/、/ref/ 同样算
AFF_PARAM = re.compile(r"aff|affid|aff_id|affiliate|ref|refid|ref_id|referral|referrer|partner|a_aid|via", re.I)
AFF_PATH = re.compile(r"/aff(?:\.php|/|$)|/ref/|/affiliate", re.I)


def unescape_href(href: str) -> str:
    """频道里的链接常被转义两次（`&amp;amp;`）：反复反转义到不再变化。"""
    while (plain := html.unescape(href)) != href:
        href = plain
    return href.strip()


def strip_affiliate(href: str) -> str | None:
    """去掉推广参数后的地址；推广参数在路径里（/aff.php、/ref/…）去不掉，返回 None。"""
    parts = urlsplit(href)
    if AFF_PATH.search(parts.path):
        return None
    query = parse_qsl(parts.query, keep_blank_values=True)
    clean = [(k, v) for k, v in query if not AFF_PARAM.fullmatch(k) and not k.lower().startswith("utm_")]
    if len(clean) == len(query):
        return href
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(clean, safe="/:"), parts.fragment))


@dataclass
class Message:
    id: int
    time: datetime | None
    post: RawPost | None  # 可收的转发；None 表示跳过
    skip: str | None = None  # 跳过原因：no_text / no_origin / thread / other_origin


def _hashtags(fragment: Tag) -> list[str]:
    tags = []
    for a in fragment.find_all("a", href=True):
        if m := HASHTAG_HREF.match(a["href"]):
            tags.append(unquote(m.group(1)))
    return tags


def _vendor_display(tag: str, title: str, body: str) -> str:
    """商家标签是小写（#digitalfyre）：正文或标题里有大小写不同的同一个词（DigitalFyre）就用它。"""
    for m in re.finditer(rf"(?<![\w.]){re.escape(tag)}(?![\w])", f"{title}\n{body}", re.I):
        if m.group(0) != tag:
            return m.group(0)
    return tag


SPEC_FIELD = re.compile(r"^\s*(?:CPU|Memory|RAM|Disk|Storage)\s*[:：]\s*(.+)$", re.I)


def _spec_title(body_text: str) -> str:
    """没有套餐标题时用 CPU / 内存 / 硬盘三项拼一个（"1 vCPU / 1 GB / 10 GB NVMe"）。"""
    values = [m.group(1).strip() for line in body_text.splitlines() if (m := SPEC_FIELD.match(line))]
    return " / ".join(values[:3])


def _clean_body(lines: list[str]) -> tuple[str, str]:
    """正文各行（HTML）→（标题, 正文 HTML）：标题取第一行（是配置行时为空）；去掉 Stock 行与推广参数。"""
    kept: list[str] = []
    for line in lines:
        frag = BeautifulSoup(line, "lxml")
        text = frag.get_text(" ", strip=True)
        if STOCK_LINE.match(text):
            continue
        for a in frag.find_all("a"):
            href = unescape_href(a.get("href") or "")
            if href.startswith("?q=%23"):  # 正文里的标签只留文字
                a.unwrap()
                continue
            clean = strip_affiliate(href)
            if clean is None:  # 去不掉推广参数：下单链接整个去掉，其他链接只留文字
                a.decompose() if ORDER_ANCHOR.search(a.get_text()) else a.unwrap()
                continue
            if a.get_text(strip=True) == href:  # 锚文本就是地址本身时一起换掉
                a.string = clean
            a.attrs = {"href": clean}
        body = frag.body
        markup = body.decode_contents().strip() if body else ""
        if kept or BeautifulSoup(markup, "lxml").get_text(strip=True):  # 去掉开头的空行
            kept.append(markup)
    while kept and not BeautifulSoup(kept[-1], "lxml").get_text(strip=True):
        kept.pop()
    if not kept:
        return "", ""
    title = " ".join(BeautifulSoup(kept[0], "lxml").get_text(" ", strip=True).split())
    if FIELD_LINE.match(title):  # 没有套餐标题，第一行就是配置
        title = ""
    return title, "<br/>".join(kept)  # 套餐标题留在正文里，分段时作套餐标题行


def parse_message(node: Tag, channel: str = CHANNEL) -> Message:
    msg_id = int(node["data-post"].rsplit("/", 1)[1])
    when = node.select_one(".tgme_widget_message_date time[datetime]")
    published = datetime.fromisoformat(when["datetime"]) if when else None
    box = node.select_one(".tgme_widget_message_text")
    if box is None:
        return Message(msg_id, published, None, "no_text")
    lines = BR.split(box.decode_contents())
    try:  # 头部到第一个空行为止
        split = next(i for i, line in enumerate(lines) if not BeautifulSoup(line, "lxml").get_text(strip=True))
    except StopIteration:
        split = len(lines)
    header = BeautifulSoup("<br/>".join(lines[:split]), "lxml")
    origin = next((a["href"] for a in header.find_all("a", href=True)
                   if "原始地址" in str(a.previous_sibling or "")), None)
    if origin is None:
        return Message(msg_id, published, None, "no_origin")
    parts = urlsplit(origin)
    if (parts.hostname or "").lower() not in ORIGIN_HOSTS:
        return Message(msg_id, published, None, "other_origin")
    if not COMMENT_PATH.match(parts.path):
        return Message(msg_id, published, None, "thread")
    tags = _hashtags(header)
    first = BeautifulSoup(lines[0], "lxml")
    first_tags = _hashtags(first)
    vendor_tag = first_tags[0] if len(first_tags) == 1 and first.get_text(strip=True) == f"#{first_tags[0]}" else None
    title, body = _clean_body(lines[split:])
    categories = [t for t in tags if t != vendor_tag]
    body_text = BeautifulSoup(BR.sub("\n", body), "lxml").get_text()
    author = _vendor_display(vendor_tag, title, body_text) if vendor_tag else None
    title = title or _spec_title(body_text) or author or "LowEndTalk"
    post = RawPost(
        source=Telegram.id, url=normalize_url(origin), title=title, content_html=body,
        author=author, published_at=published, categories=categories, extra={"tg_post": f"{channel}/{msg_id}"},
    )
    return Message(msg_id, published, post)


def parse_page(data: bytes | str) -> list[Message]:
    soup = BeautifulSoup(data, "lxml")
    return [parse_message(node) for node in soup.select("div.tgme_widget_message[data-post]")]


@register
class Telegram(Source):
    id = "telegram"
    name = "Telegram @VPS_spiders"
    kind = SourceKind.OFFERS
    lang = "en"
    request_gap = 2.0

    def fetch(self, state: Mapping | None = None) -> FetchResult:
        prev_max = (state or {}).get("max_id")
        messages: dict[int, Message] = {}
        url, pages = PAGE_URL, 0
        while True:
            page = parse_page(self.http.get(url).content)
            pages += 1
            for m in page:
                messages.setdefault(m.id, m)
            if not page:
                break
            low = min(m.id for m in page)
            if prev_max is None or low <= prev_max or pages >= MAX_PAGES:
                break
            self._sleep(self.request_gap)
            url = f"{PAGE_URL}?before={low}"
        return FetchResult(*collect(messages.values(), prev_max, pages))


def collect(messages, prev_max: int | None, pages: int) -> tuple[list[RawPost], dict]:
    """新到旧排列，同一条原评论被转发多次时留最新的一条；stats 写本轮 ID 区间与跳过原因。"""
    ordered = sorted(messages, key=lambda m: m.id, reverse=True)
    posts: dict[str, RawPost] = {}
    skipped = Counter(m.skip for m in ordered if m.skip)
    for m in ordered:
        if m.post is not None:
            posts.setdefault(m.post.url, m.post)
    ids = [m.id for m in ordered]
    stats: dict = {"pages": pages, "messages": len(ids), "relayed": len(posts), "skipped": dict(skipped)}
    if ids:
        stats.update(min_id=min(ids), max_id=max(max(ids), prev_max or 0))
        if prev_max is not None:  # 本轮最旧的一条回溯到了上一轮最大 ID：两轮之间没有漏抓
            stats["overlap"] = min(ids) <= prev_max
    elif prev_max is not None:
        stats["max_id"] = prev_max
    return list(posts.values()), stats
