"""分享预览图（M3.6，第 16 节）：每条优惠一张 1200×630 PNG，转发到微信、Telegram 时显示商家、标题与起价；其余页面用站点图。

用 Pillow 画，字体是仓库内置的 Noto Sans SC 子集（D27，fonts/，由 tests/tools/subset_font.py 生成）；字体里没有的字
（emoji 等）去掉，免得出现方框。画好的图按"语言 + 编号 + 内容摘要"存到数据目录的 og/ 下：内容变了摘要就变，
og:image 地址里带上摘要（?v=），Cloudflare 与浏览器可以长缓存；写新图时删掉同一条优惠的旧图。
"""

import hashlib
import io
import json
import os
import re
from functools import cache
from pathlib import Path

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
SHELF, PAPER, INK, INK_2, INK_3, TAG = "#e9ebe6", "#ffffff", "#000000", "#4a4f55", "#686d74", "#ffd500"
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
            "event": clean(view.t(f"event.{d.event_type}")) if d.is_black_friday else ""}


def site_fields(settings: Settings, view: View) -> dict:
    """站点图：大字站名（英文用全称，价签上已有简称）、一行说明、黄色价签写简称 BFRadar。"""
    return {"lang": view.lang, "site": settings.site_name_zh if view.lang == "zh" else settings.site_fullname_en,
            "headline": clean(view.t("site.title").split(" · ")[0]), "tag": settings.site_name_en}


def version(fields: dict) -> str:
    return hashlib.sha1(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:10]


def deal_url(settings: Settings, view: View, d: Deal) -> str:
    return f"{settings.site_url}/og/{view.lang}/deal/{d.id}.png?v={version(deal_fields(settings, view, d))}"


def site_url(settings: Settings, view: View) -> str:
    return f"{settings.site_url}/og/{view.lang}/site.png?v={version(site_fields(settings, view))}"


# ---------------------------------------------------------------- 画图


def _canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (WIDTH, HEIGHT), SHELF)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((40, 40, WIDTH - 40, HEIGHT - 40), radius=18, fill=PAPER)
    return img, draw


def _tag(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, size: int) -> int:
    """黄色挂牌价签（左边切角、打孔），与网站上"刚刚上架"的价签同一造型；返回右边界。"""
    h, cut = size + 52, 18
    w = int(font(size).getlength(text)) + 88
    draw.polygon([(x + cut, y), (x + w, y), (x + w, y + h), (x + cut, y + h), (x, y + h - cut), (x, y + cut)], fill=TAG)
    draw.ellipse((x + 14, y + h // 2 - 7, x + 28, y + h // 2 + 7), fill=PAPER)
    draw.text((x + 50, y + h // 2), text, font=font(size), fill=INK, anchor="lm")
    return x + w


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_deal(f: dict) -> bytes:
    img, draw = _canvas()
    draw.text((84, 86), f["site"], font=font(30), fill=INK, anchor="lm")
    if f["event"]:
        w = font(28).getlength(f["event"])
        draw.rounded_rectangle((WIDTH - 84 - w - 28, 64, WIDTH - 84, 108), radius=6, fill=INK)
        draw.text((WIDTH - 84 - w - 14, 86), f["event"], font=font(28), fill=PAPER, anchor="lm")
    draw.text((84, 160), f["vendor"], font=font(38), fill=INK_2, anchor="lm")
    for n, line in enumerate(wrap(f["title"], 50, WIDTH - 168, 3)):
        draw.text((84, 236 + n * 66), line, font=font(50), fill=INK, anchor="lm")
    right = _tag(draw, 84, 436, f["price"], 60)
    if f["cny"]:
        draw.text((right + 28, 436 + 56), f["cny"], font=font(34), fill=INK_3, anchor="lm")
    return _png(img)


def render_site(f: dict) -> bytes:
    img, draw = _canvas()
    draw.text((84, 150), f["site"], font=font(76), fill=INK, anchor="lm")
    for n, line in enumerate(wrap(f["headline"], 46, WIDTH - 168, 2)):
        draw.text((84, 250 + n * 62), line, font=font(46), fill=INK_2, anchor="lm")
    _tag(draw, 84, 420, f["tag"], 60)
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
