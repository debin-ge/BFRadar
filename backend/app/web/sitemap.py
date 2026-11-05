"""sitemap.xml 与 robots.txt（M3.5，第 14、16 节）。

sitemap 列出首页、套餐视图、商家目录、优惠码汇总、关于页、全部优惠详情页和有优惠的商家页，中英文各一条，每条都带 zh-CN、en、x-default
三个 hreflang alternates（Google 的做法：每个语言版本都列出全部对应版本）；状态页不在页面上链接（D59），不列。
robots.txt 允许全部并指向 sitemap。地址只用 SITE_URL 拼。
"""

import xml.etree.ElementTree as ET

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from .cache import cached_response
from .queries import last_update
from .view import PREFIX

router = APIRouter(include_in_schema=False)

SITEMAP_TTL = 3600  # 第 14 节
ROBOTS_TTL = 86400
NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
XHTML = "http://www.w3.org/1999/xhtml"
ALTERNATES = (("zh-CN", "zh"), ("en", "en"), ("x-default", "zh"))
ET.register_namespace("", NS)
ET.register_namespace("xhtml", XHTML)


@router.get("/sitemap.xml")
def sitemap(request: Request) -> Response:
    return cached_response(request, "sitemap.xml", SITEMAP_TTL, lambda: _sitemap(request))


def _sitemap(request: Request) -> Response:
    site, conn = request.app.state.settings.site_url, request.app.state.db.conn()
    updated = last_update(conn)
    pages = [("/", updated), ("/plans", updated), ("/vendors", updated), ("/codes", updated), ("/about", None)]
    pages += [(f"/deal/{r['id']}", r["updated_at"])
              for r in conn.execute("SELECT id, updated_at FROM deals ORDER BY id")]
    pages += [(f"/vendor/{r['slug']}", r["updated"]) for r in conn.execute(
        "SELECT v.slug, MAX(d.updated_at) AS updated FROM vendors v JOIN deals d ON d.vendor_id = v.id"
        " GROUP BY v.slug ORDER BY v.slug")]
    urlset = ET.Element(f"{{{NS}}}urlset")
    for path, modified in pages:
        for prefix in PREFIX.values():
            url = ET.SubElement(urlset, f"{{{NS}}}url")
            ET.SubElement(url, f"{{{NS}}}loc").text = site + prefix + path
            if modified:
                ET.SubElement(url, f"{{{NS}}}lastmod").text = modified[:10]
            for hreflang, lang in ALTERNATES:
                ET.SubElement(url, f"{{{XHTML}}}link",
                              {"rel": "alternate", "hreflang": hreflang, "href": site + PREFIX[lang] + path})
    body = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(urlset, encoding="unicode") + "\n"
    return Response(body.encode(), media_type="application/xml; charset=utf-8",
                    headers={"Cache-Control": f"public, max-age={SITEMAP_TTL}"})


@router.get("/robots.txt")
def robots(request: Request) -> PlainTextResponse:
    site = request.app.state.settings.site_url
    return PlainTextResponse(f"User-agent: *\nAllow: /\n\nSitemap: {site}/sitemap.xml\n",
                             headers={"Cache-Control": f"public, max-age={ROBOTS_TTL}"})
