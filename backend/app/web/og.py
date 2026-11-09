"""分享预览图：1200×630 深色雷达卡片，站点图说明用途，优惠图显示商家、标题与起价。

用 Pillow 画，字体是仓库内置的 Noto Sans SC 子集（D27，fonts/，由 tests/tools/subset_font.py 生成）；字体里没有的字
（emoji 等）去掉，免得出现方框。画好的图按"语言 + 编号 + 内容摘要"存到数据目录的 og/ 下：内容变了摘要就变，
og:image 地址里带上摘要（?v=），摘要包含绘图版本，修改版式后不会继续命中旧图片；写新图时删掉同一条优惠的旧图。
"""

import hashlib
import io
import json
import math
import os
import re
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageDraw, ImageFont

from ..config import Settings
from ..models import Deal
from .queries import deal_by_id
from .view import View, for_lang

router = APIRouter(include_in_schema=False)

WIDTH, HEIGHT = 1200, 630
FONTS = Path(__file__).with_name("fonts")
FONT = FONTS / "NotoSansSC-Bold-subset.otf"
BG, PANEL, INK, MUTED, SIGNAL, LINE = "#050b09", "#0c1d16", "#e8f3ed", "#9cbbac", "#2ee6b6", "#1b3b2e"
RENDER_VERSION = "radar-1"  # 改版式或绘图文案时递增，同时更新公开图片 URL 和磁盘缓存。
CACHE = "public, max-age=86400"
LANGS = ("zh", "en")
TOKEN = re.compile(r"\s+|[⺀-鿿＀-￯]|[^\s⺀-鿿＀-￯]+")  # 汉字逐字、西文按词


@cache
def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT), size)


@cache
def covered() -> frozenset[str]:
    return frozenset((FONTS / "chars.txt").read_text(encoding="utf-8"))


def clean(text: str) -> str:
    """去掉字体里没有的字，空白合并成一个空格。"""
    return " ".join("".join(ch for ch in text if ch in covered() or ch.isspace()).split())


def wrap(text: str, size: int, width: int, lines: int) -> list[str]:
    """按宽度折行：汉字可在任意处断，西文在空格处断，比整行还宽的词逐字断；最多 lines 行，放不下时末行加省略号。"""
    f, out, line = font(size), [], ""
    for token in TOKEN.findall(text):
        if f.getlength(line + token) <= width:
            line += token
            continue
        if line.strip():
            out.append(line.strip())
        line = ""
        if token.isspace():
            continue
        for ch in token:  # 放得下就是整词，放不下逐字断
            if line and f.getlength(line + ch) > width:
                out.append(line)
                line = ""
            line += ch
    if line.strip():
        out.append(line.strip())
    if len(out) > lines:
        last = out[lines - 1]
        while last and f.getlength(last + "…") > width:
            last = last[:-1]
        out = out[:lines - 1] + [last.rstrip() + "…"]
    return out


def deal_fields(settings: Settings, view: View, d: Deal) -> dict:
    price = view.start(d)
    return {"lang": view.lang, "site": settings.site_name_zh if view.lang == "zh" else settings.site_name_en,
            "vendor": clean(view.vendor(d)), "title": clean(view.title(d)),
            "price": clean(price[0]) if price else clean(view.t("row.price_unknown")),
            "cny": clean(price[1]) if price else "",
            "event": clean(view.t(f"event.{d.event_type}")) if d.is_black_friday else "",
            "domain": clean(urlsplit(settings.site_url).netloc)}


def site_fields(settings: Settings, view: View) -> dict:
    """不放实时价格与数量，避免分享图长缓存后出现过期促销；域名来自站点配置。"""
    return {"lang": view.lang, "site": clean(settings.site_name_zh if view.lang == "zh" else settings.site_name_en),
            "year": settings.bf_week_start.year, "domain": clean(urlsplit(settings.site_url).netloc)}


def version(fields: dict) -> str:
    payload = {"renderer": RENDER_VERSION, "fields": fields}
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:10]


def deal_url(settings: Settings, view: View, d: Deal) -> str:
    return f"{settings.site_url}/og/{view.lang}/deal/{d.id}.png?v={version(deal_fields(settings, view, d))}"


def site_url(settings: Settings, view: View) -> str:
    return f"{settings.site_url}/og/{view.lang}/site.png?v={version(site_fields(settings, view))}"


# ---------------------------------------------------------------- 画图


def _canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    for x in range(0, WIDTH, 48):
        draw.line((x, 0, x, HEIGHT), fill="#0c1913")
    for y in range(0, HEIGHT, 48):
        draw.line((0, y, WIDTH, y), fill="#0c1913")
    draw.line((64, 536, WIDTH - 64, 536), fill=LINE, width=2)
    return img, draw


def _text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, size: int, width: int, fill: str = INK) -> None:
    """单行字段（商家、域名、价格）缩小后仍放不下时省略，不侵入其他区域。"""
    while size > 18 and font(size).getlength(text) > width:
        size -= 1
    draw.text(xy, wrap(text, size, width, 1)[0] if text else "", font=font(size), fill=fill, anchor="lt")


def _radar(draw: ImageDraw.ImageDraw, cx: int, cy: int, radius: int) -> None:
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill="#081710")
    draw.pieslice((cx - radius, cy - radius, cx + radius, cy + radius), 285, 325, fill="#103c2b")
    for r in (radius, radius * 2 // 3, radius // 3):
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline="#24513c", width=2)
    draw.line((cx - radius, cy, cx + radius, cy), fill=LINE, width=2)
    draw.line((cx, cy - radius, cx, cy + radius), fill=LINE, width=2)
    end = (cx + int(radius * math.cos(math.radians(325))), cy + int(radius * math.sin(math.radians(325))))
    draw.line((cx, cy, *end), fill=SIGNAL, width=3)
    for dx, dy in ((-64, 52), (68, -78), (32, 90)):
        x, y = cx + dx * radius // 170, cy + dy * radius // 170
        halo, dot = max(2, radius * 11 // 170), max(1, radius * 4 // 170)
        draw.ellipse((x - halo, y - halo, x + halo, y + halo), fill="#133b2c")
        draw.ellipse((x - dot, y - dot, x + dot, y + dot), fill=SIGNAL)


def _header(draw: ImageDraw.ImageDraw, site: str, label: str) -> None:
    _radar(draw, 84, 80, 22)
    _text(draw, (122, 61), site, 30, 570)
    w = font(20).getlength(label)
    draw.text((WIDTH - 64 - w, 68), label, font=font(20), fill=MUTED, anchor="lt")


def _footer(draw: ImageDraw.ImageDraw, f: dict) -> None:
    label = "VPS · 独服 · 虚拟主机 · 存储" if f["lang"] == "zh" else "VPS · Dedicated · Hosting · Storage"
    _text(draw, (64, 565), label, 22, 620, MUTED)
    domain = f["domain"]
    size = 26
    while size > 18 and font(size).getlength(domain) > 390:
        size -= 1
    domain = wrap(domain, size, 390, 1)[0] if domain else ""
    draw.text((WIDTH - 64, 565), domain, font=font(size), fill=SIGNAL, anchor="rt")


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_deal(f: dict) -> bytes:
    img, draw = _canvas()
    _header(draw, f["site"], f["event"] or "HOSTING DEALS")
    _text(draw, (64, 133), f["vendor"], 28, WIDTH - 128, SIGNAL)
    for n, line in enumerate(wrap(f["title"], 46, WIDTH - 128, 3)):
        draw.text((64, 188 + n * 62), line, font=font(46), fill=INK, anchor="lt")
    draw.rounded_rectangle((64, 405, WIDTH - 64, 508), radius=12, fill=PANEL, outline=LINE, width=2)
    _text(draw, (90, 424), f["price"], 54, 620, SIGNAL)
    _text(draw, (762, 443), f["cny"], 28, 340, MUTED)
    _footer(draw, f)
    return _png(img)


def render_site(f: dict) -> bytes:
    img, draw = _canvas()
    zh = f["lang"] == "zh"
    _header(draw, f["site"], f"BLACK FRIDAY / {f['year']}")
    _radar(draw, 966, 295, 166)
    lines = ("黑五 VPS 优惠，", "放在一起比。") if zh else ("Black Friday VPS.", "Compare the deals.")
    for y, text, color in ((174, lines[0], INK), (270, lines[1], SIGNAL)):
        _text(draw, (64, y), text, 72 if zh else 64, 710, color)
    subtitle = "按预算、配置和地区筛选套餐" if zh else "Filter by budget, specs and location."
    _text(draw, (68, 373), subtitle, 28, 710, MUTED)
    x = 64
    for label in (("套餐比价", "优惠码", "续费条件") if zh else ("Compare plans", "Coupon codes", "Renewal terms")):
        w = int(font(23).getlength(label)) + 40
        draw.rounded_rectangle((x, 445, x + w, 495), radius=25, fill=PANEL, outline="#2b5942", width=2)
        draw.text((x + 20, 458), label, font=font(23), fill=INK, anchor="lt")
        x += w + 14
    _footer(draw, f)
    return _png(img)


# ---------------------------------------------------------------- 接口


def _cached(directory: Path, name: str, data: dict, render) -> Path:
    """data/og/{lang}/{name}-{摘要}.png：没有就画（先写临时文件再改名），同名的旧图删掉。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}-{version(data)}.png"
    if not path.exists():
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(render(data))
        os.replace(tmp, path)
        for old in directory.glob(f"{name}-*.png"):
            if old != path:
                old.unlink(missing_ok=True)
    return path


def _view(request: Request, lang: str) -> View:
    return for_lang(request.app.state.settings, lang)


def _dir(request: Request, lang: str) -> Path:
    return Path(request.app.state.settings.db_path).parent / "og" / lang


NOT_FOUND = {"Cache-Control": "public, max-age=60"}


@router.get("/og/{lang}/deal/{deal_id:int}.png")
def deal_image(request: Request, lang: str, deal_id: int) -> Response:
    if lang not in LANGS:
        return Response(status_code=404, headers=NOT_FOUND)
    d = deal_by_id(request.app.state.db.conn(), deal_id)
    if d is None:
        return Response(status_code=404, headers=NOT_FOUND)
    data = deal_fields(request.app.state.settings, _view(request, lang), d)
    path = _cached(_dir(request, lang), str(deal_id), data, render_deal)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": CACHE})


@router.get("/og/{lang}/site.png")
def site_image(request: Request, lang: str) -> Response:
    if lang not in LANGS:
        return Response(status_code=404, headers=NOT_FOUND)
    data = site_fields(request.app.state.settings, _view(request, lang))
    return FileResponse(_cached(_dir(request, lang), "site", data, render_site), media_type="image/png",
                        headers={"Cache-Control": CACHE})
