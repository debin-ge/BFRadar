"""RSS 2.0（FR-14，第 14 节）：中英文各一路（/feed.xml、/en/feed.xml），按发布时间最新的 50 条，
scope 与首页同义（D31）。

条目链接与 guid 指向本站同语言的详情页，描述里写商家、起价、优惠、摘要或摘录与原帖链接（HTML 转义后放进
description）；标题、摘要、标点随语言（第 7 节）。
用 ElementTree 生成，转义与命名空间声明由它负责；频道带 atom:link rel="self"（W3C Feed Validator 的建议项）。
"""

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import format_datetime
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..config import Settings
from ..models import Deal
from . import fmt
from .cache import DETAIL_TTL, cached_response, max_age
from .queries import DealQuery, list_deals
from .view import View, for_lang

ITEMS = 50
ATOM = "http://www.w3.org/2005/Atom"
MEDIA_TYPE = "application/rss+xml; charset=utf-8"
ET.register_namespace("atom", ATOM)

router = APIRouter(include_in_schema=False)


def rfc822(value: str | datetime) -> str:
    when = fmt.parse_ts(value) if isinstance(value, str) else value
    return format_datetime(when.astimezone(UTC), usegmt=True)


def _scoped(url: str, scope: str) -> str:
    return url if scope == "bf" else f"{url}?scope={scope}"


def item_title(d: Deal, v: View) -> str:
    title = v.title(d)
    return title if not d.vendor or d.vendor.casefold() in title.casefold() else f"{d.vendor}{v.sep('colon')}{title}"


def item_html(d: Deal, v: View, page: str) -> str:
    t = v.t
    event = t("event." + str(d.event_type))
    lines = [f"<p><strong>{escape(v.vendor(d))}</strong>{escape(v.sep('comma'))}{escape(event)}</p>"]
    if d.min_price == 0:
        lines.append(f"<p>{escape(t('feed.free'))}</p>")
    elif price := v.start(d):
        lines.append(f"<p>{escape(t('feed.price', price=' '.join(x for x in price if x)))}</p>")
    if offer := v.offer(d):
        lines.append(f"<p>{escape(v.sep('comma').join(offer))}</p>")
    if text := v.summary(d) or d.excerpt:
        lines.append(f"<p>{escape(text)}</p>")
    lines.append(f'<p><a href="{escape(page)}">{escape(t("feed.detail"))}</a>{escape(v.sep("comma"))}'
                 f'<a href="{escape(d.url)}">{escape(t("feed.original"))}</a></p>')
    return "".join(lines)


def _text(parent: ET.Element, tag: str, text: str, **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrs)
    el.text = text
    return el


def render(deals: list[Deal], *, view: View, settings: Settings, scope: str, now: datetime) -> bytes:
    t, site = view.t, settings.site_url + view.prefix
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    _text(channel, "title", t("feed.title", scope=t(f"scope.{scope}")))
    _text(channel, "link", _scoped(f"{site}/", scope))
    _text(channel, "description", t("feed.description"))
    ET.SubElement(channel, f"{{{ATOM}}}link",
                  {"href": _scoped(f"{site}/feed.xml", scope), "rel": "self", "type": "application/rss+xml"})
    _text(channel, "language", "zh-cn" if view.lang == "zh" else "en")
    _text(channel, "lastBuildDate", rfc822(now))
    _text(channel, "ttl", "10")
    for d in deals:
        page = f"{site}/deal/{d.id}"
        item = ET.SubElement(channel, "item")
        _text(item, "title", item_title(d, view))
        _text(item, "link", page)
        _text(item, "description", item_html(d, view, page))
        _text(item, "category", t(f"event.{d.event_type}"))
        _text(item, "guid", page, isPermaLink="true")
        if when := d.published_at or d.first_seen_at:
            _text(item, "pubDate", rfc822(when))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode") + "\n").encode()


# ---------------------------------------------------------------- 接口


@router.get("/feed.xml")
@router.get("/en/feed.xml")
def feed_xml(request: Request) -> Response:
    """中英文各一路，默认 scope=bf；经内存缓存，键为"语言前缀 + 规范地址"。"""
    lang = "en" if request.url.path.startswith("/en/") else "zh"
    scope, view = DealQuery.parse(request.query_params).scope, for_lang(request.app.state.settings, lang)
    return cached_response(request, f"{view.prefix}/feed.xml?scope={scope}", DETAIL_TTL,
                           lambda: _feed(request, view, scope))


def _feed(request: Request, view: View, scope: str) -> Response:
    settings, conn = request.app.state.settings, request.app.state.db.conn()
    page = list_deals(conn, DealQuery(scope=scope), fmt.season_start_iso(settings.season_start), page_size=ITEMS,
                      ended_before=fmt.ended_cutoff(view.today))
    body = render(page.deals, view=view, settings=settings, scope=scope, now=view.now)
    return Response(body, media_type=MEDIA_TYPE, headers={"Cache-Control": max_age(DETAIL_TTL)})
