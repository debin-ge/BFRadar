"""文本工具：HTML 结构化（第 12.2 节）、帖子 URL 规范化（第 12.1 节）、内容哈希与摘录。"""

import hashlib
import re
from html.parser import HTMLParser
from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class Link(NamedTuple):
    n: int
    anchor: str
    href: str


class StructuredText(NamedTuple):
    text: str
    links: list[Link]


class _Structurer(HTMLParser):
    """表格每行一行、单元格以 ` | ` 分隔；列表项按层级缩进并以 `- ` 开头；链接替换为 `锚文本 [n]`；<pre> 保留换行。"""

    BLOCK = {"p", "div", "br", "hr", "table", "tr", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6",
             "section", "article", "header", "footer", "figure", "figcaption", "dl", "dt", "dd"}
    SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines = [""]
        self.links: list[Link] = []
        self.depth = 0
        self.pre = 0
        self.skip = 0
        self.href: str | None = None
        self.anchor: list[str] = []

    def _newline(self) -> None:
        if self.lines[-1].strip():
            self.lines.append("")

    def _write(self, text: str) -> None:
        self.lines[-1] += text

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
            return
        a = dict(attrs)
        if tag in ("ul", "ol"):
            self.depth += 1
            self._newline()
        elif tag == "li":
            self._newline()
            self.lines[-1] = "  " * max(0, self.depth - 1) + "- "
        elif tag in ("td", "th"):
            if self.lines[-1].strip():
                self._write(" | ")
        elif tag == "a":
            self.href = (a.get("href") or "").strip()
            self.anchor = []
        elif tag == "img":
            alt = " ".join((a.get("alt") or "").split())
            if alt and not re.fullmatch(r":\w+:", alt):  # 论坛表情 :star: 之类不保留
                self._write(f"[img: {alt}]")
        elif tag in self.BLOCK:
            self._newline()
            if tag == "pre":
                self.pre += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in ("ul", "ol"):
            self.depth = max(0, self.depth - 1)
            self._newline()
        elif tag == "li":
            self._newline()
        elif tag == "a" and self.href is not None:
            if self.href and not self.href.startswith("#"):
                self.links.append(Link(len(self.links) + 1, " ".join("".join(self.anchor).split()), self.href))
                self._write(f" [{len(self.links)}]")
            self.href = None
        elif tag in self.BLOCK:
            if tag == "pre":
                self.pre = max(0, self.pre - 1)
            self._newline()

    def handle_data(self, data):
        if self.skip:
            return
        if self.href is not None:
            self.anchor.append(data)
        if self.pre:
            first, *rest = data.split("\n")
            self._write(first)
            self.lines.extend(rest)
            return
        text = re.sub(r"\s+", " ", data)
        if not self.lines[-1].strip(" -"):  # 行首（含列表前缀）不保留源码换行带来的空格
            text = text.lstrip()
        self._write(text)


def structure_html(html: str) -> StructuredText:
    """HTML → 结构化文本与链接表。规则层、LLM 输入与摘录共用这份文本。"""
    parser = _Structurer()
    parser.feed(html)
    parser.close()
    lines: list[str] = []
    for line in parser.lines:
        indent = len(line) - len(line.lstrip(" "))
        line = (" " * indent + re.sub(r" {2,}", " ", line.strip())).rstrip()
        if line.strip() or (lines and lines[-1].strip()):  # 最多保留一个空行
            lines.append(line)
    return StructuredText("\n".join(lines).strip(), parser.links)


def collapse_ws(text: str) -> str:
    return " ".join(text.split())


def content_hash(*parts: str) -> str:
    """内容哈希：空白差异不改变哈希，用于判断原帖是否被编辑。"""
    h = hashlib.sha256()
    for part in parts:
        h.update(collapse_ws(part).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def make_excerpt(text: str, limit: int = 300) -> str:
    """正文摘录（FR-03，≤ limit 字符）：按行合并，尽量在句末或空白处截断。"""
    flat = " / ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(flat) <= limit:
        return flat
    cut = flat[: limit - 1]
    for sep in ("。", "！", "？", ". ", "! ", "? ", " / ", " "):
        pos = cut.rfind(sep)
        if pos >= limit * 0.6:
            cut = cut[: pos + len(sep.rstrip())]
            break
    return cut.rstrip(" /") + "…"


# ---------------------------------------------------------------- URL 规范化

TRACKING_PARAM = re.compile(r"utm_\w+|fbclid|gclid|yclid|msclkid|mc_cid|mc_eid|_ga|_gl", re.I)
HOST_ALIASES = {
    "www.lowendtalk.com": "lowendtalk.com",
    "www.lowendspirit.com": "lowendspirit.com",
    "nodeseek.com": "www.nodeseek.com",
    "reddit.com": "www.reddit.com",
    "old.reddit.com": "www.reddit.com",
    "new.reddit.com": "www.reddit.com",
    "www.lowendbox.com": "lowendbox.com",
}
HTTPS_HOSTS = {"lowendtalk.com", "lowendspirit.com", "www.nodeseek.com", "www.reddit.com", "lowendbox.com"}
PATH_RULES = (  # (主机, 路径模式, 规范路径)
    (("lowendtalk.com", "lowendspirit.com"), re.compile(r"/discussion/(\d+)(?:/.*)?"), "/discussion/{}"),
    (("www.reddit.com",), re.compile(r"((?:/r/[^/]+)?/comments/[a-z0-9]+)(?:/.*)?", re.I), "{}"),
    (("www.nodeseek.com",), re.compile(r"/post-(\d+)-\d+/?"), "/post-{}-1"),
)


def normalize_url(url: str) -> str:
    """帖子 URL 规范化，作为去重主键：去跟踪参数与锚点，统一主机名，论坛帖去掉 slug 与分页。"""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower()
    host = HOST_ALIASES.get(host, host)
    if host in HTTPS_HOSTS:
        scheme = "https"
    netloc = host
    if parts.port and not ((scheme == "https" and parts.port == 443) or (scheme == "http" and parts.port == 80)):
        netloc = f"{host}:{parts.port}"
    path = parts.path or "/"
    for hosts, pattern, template in PATH_RULES:
        if host in hosts and (m := pattern.fullmatch(path)):
            path = template.format(m.group(1))
            break
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not TRACKING_PARAM.fullmatch(k)])
    return urlunsplit((scheme, netloc, path, query, ""))
