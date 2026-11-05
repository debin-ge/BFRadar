"""商家标准化与别名表（《需求与方案说明》第 12.7 节，D29、D52）。

优惠的商家名在保存时归并到 vendors 表：去掉空白、标点、大小写与常见域名后缀（.com / .net / .org / .io）后，与标准名
或别名相同即为同一商家，否则新建；"Hosting""Terms of Service"之类的泛词不当商家。别名表在 app/vendors.json，
启动时同步进库（sync_vendors）：写入标准名与别名，把同一商家的重复记录并到一起，删掉泛词商家，再重新匹配全部情报。

情报匹配商家（match_vendors）：普通别名在标题与正文中匹配，短别名（≤ 3 个字符或常用词，如 rn、cc、大妈）只在标题中
匹配（D29）。边界（D52）：英文别名前后不能紧挨字母或数字；两个字的中文别名前后不能紧挨汉字（避免"广场舞大妈"）；三个字
以上的中文别名（腾讯云、搬瓦工）直接按子串匹配，因为中文不空格，"腾讯云轻量"这类写法很常见。只按别名表里的商家匹配：
规则层从帖子里认出的商家名有不少是普通词（"Take""USA"），拿来匹配会把"take over""USA VPS"误挂上去。
"""

import json
import re
import sqlite3
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from .db import Database, transaction

TABLE = Path(__file__).with_name("vendors.json")
TLD = re.compile(r"\.(?:com|net|org|io)$")
HAN = r"一-鿿"
GENERIC = {  # 规则层偶尔把标题前缀、链接文字当成商家名；这些词不当商家
    "hosting", "host", "vps", "vds", "server", "servers", "cloud", "dedicated", "storagevps", "termsofservice",
    "freewordpress", "plusaddressing", "livedangerously", "yabs", "bgp", "blackfriday", "cybermonday", "sale", "deal",
    "deals", "offer", "offers", "special", "giveaway", "lesbot",
}


def vendor_key(name: str) -> str:
    """归并用的键：Godlike.host、godlike host 相同；SmokyHosts.com 与 SmokyHosts 相同；汉字保留。"""
    return re.sub(r"[\W_]+", "", TLD.sub("", name.casefold().strip()))


def prepare(db: Database) -> dict[str, int]:
    """启动时：迁移数据库并同步别名表。"""
    db.migrate()
    with transaction(db.conn()):
        return sync_vendors(db.conn())


def slugify(name: str) -> str:
    return re.sub(r"[\W_]+", "-", name.casefold()).strip("-")


MARKETING = re.compile(r"(?<![a-z])(?:sale|deals?|offers?|promo(?:tion)?s?|halloween|black ?friday|cyber ?monday|"
                       r"giveaways?|page)(?![a-z])", re.I)


def is_generic(name: str) -> bool:
    """泛词或活动文字（"Halloween sale page"）不当商家名。"""
    return vendor_key(name) in GENERIC or bool(MARKETING.search(name))


@dataclass(frozen=True)
class Curated:
    slug: str
    name: str
    website: str | None
    aliases: tuple[str, ...]
    short: tuple[str, ...]


@cache
def curated() -> tuple[Curated, ...]:
    data = json.loads(TABLE.read_text(encoding="utf-8"))
    return tuple(Curated(v["slug"], v["name"], v.get("website"), tuple(v.get("aliases", ())), tuple(v.get("short", ())))
                 for v in data["vendors"])


def _aliases(row: sqlite3.Row) -> list[str]:
    return json.loads(row["aliases_json"]) + json.loads(row["short_aliases_json"])


def resolve_vendor(conn: sqlite3.Connection, name: str | None) -> int | None:
    """商家名 → vendors.id，找不到时新建（标准名取第一次见到的写法）；泛词返回 None。调用方负责事务。"""
    if not name or not (key := vendor_key(name)) or is_generic(name):
        return None
    for row in conn.execute("SELECT id, name, aliases_json, short_aliases_json FROM vendors"):
        if key == vendor_key(row["name"]) or key in {vendor_key(a) for a in _aliases(row)}:
            return row["id"]
    return _insert(conn, slugify(name) or key, name.strip())


def _insert(conn: sqlite3.Connection, slug: str, name: str) -> int:
    base, n = slug, 1
    while conn.execute("SELECT 1 FROM vendors WHERE slug = ?", (slug,)).fetchone():
        n += 1
        slug = f"{base}-{n}"
    return conn.execute("INSERT INTO vendors (slug, name) VALUES (?, ?)", (slug, name)).lastrowid


def _merge(conn: sqlite3.Connection, keep: int, drop: int) -> None:
    conn.execute("UPDATE deals SET vendor_id = ? WHERE vendor_id = ?", (keep, drop))
    conn.execute("INSERT OR IGNORE INTO intel_vendors (intel_id, vendor_id, matched_alias)"
                 " SELECT intel_id, ?, matched_alias FROM intel_vendors WHERE vendor_id = ?", (keep, drop))
    conn.execute("DELETE FROM vendors WHERE id = ?", (drop,))


def sync_vendors(conn: sqlite3.Connection) -> dict[str, int]:
    """把别名表同步进库并整理商家记录，返回各项计数。可重复运行。调用方负责事务。"""
    stats = {"curated": 0, "merged": 0, "generic": 0}
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM vendors")}
    for c in curated():
        keys = {vendor_key(x) for x in (c.name, *c.aliases, *c.short)}
        same = [r for r in rows.values() if r["slug"] == c.slug or vendor_key(r["name"]) in keys]
        target = next((r["id"] for r in same if r["slug"] == c.slug), None)
        if target is None and same:
            target = same[0]["id"]
        if target is None:
            target = _insert(conn, c.slug, c.name)
        conn.execute("UPDATE vendors SET slug = ?, name = ?, aliases_json = ?, short_aliases_json = ?, website = ?"
                     " WHERE id = ?", (c.slug, c.name, json.dumps(list(c.aliases), ensure_ascii=False),
                                       json.dumps(list(c.short), ensure_ascii=False), c.website, target))
        for r in same:
            if r["id"] != target:
                _merge(conn, target, r["id"])
                stats["merged"] += 1
        stats["curated"] += 1
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM vendors")}
    # 名字归并键相同的其余商家（SmokyHosts 与 SmokyHosts.com）：并到优惠最多的那条
    groups: dict[str, list[int]] = {}
    for r in conn.execute("SELECT v.id, v.name, COUNT(d.id) AS n FROM vendors v LEFT JOIN deals d ON d.vendor_id = v.id"
                          " GROUP BY v.id ORDER BY n DESC, v.id"):
        groups.setdefault(vendor_key(r["name"]), []).append(r["id"])
    for ids in groups.values():
        for drop in ids[1:]:
            _merge(conn, ids[0], drop)
            stats["merged"] += 1
    for r in conn.execute("SELECT id, name FROM vendors").fetchall():
        if is_generic(r["name"]):
            conn.execute("UPDATE deals SET vendor_id = NULL WHERE vendor_id = ?", (r["id"],))
            conn.execute("DELETE FROM vendors WHERE id = ?", (r["id"],))
            stats["generic"] += 1
    stats["intel_links"] = relink_all_intel(conn)
    return stats


# ---------------------------------------------------------------- 情报匹配


def _pattern(alias: str) -> re.Pattern:
    """中文写法按子串（中文不用空格分词，"出绿云日本""除了大妈"都要认）；英文两侧不能紧挨字母或数字（D52）。"""
    if re.search(f"[{HAN}]", alias):
        return re.compile(re.escape(alias))
    return re.compile(f"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", re.I)


def _in_body(alias: str) -> bool:
    """普通别名在正文里也匹配的长度：三个汉字以上，或四个字符以上的英文（D29 的"≤ 3 个字符"按英文算，D52）。"""
    return len(alias) >= 3 if re.search(f"[{HAN}]", alias) else len(alias) > 3


@dataclass
class AliasIndex:
    title: list[tuple[int, str, re.Pattern]]  # 全部别名（标题里匹配）
    body: list[tuple[int, str, re.Pattern]]  # 普通别名（正文里也匹配）


def build_index(conn: sqlite3.Connection) -> AliasIndex:
    slugs = [c.slug for c in curated()]
    title, body = [], []
    for r in conn.execute(f"SELECT id, name, aliases_json, short_aliases_json FROM vendors"
                          f" WHERE slug IN ({', '.join('?' * len(slugs))})", slugs):
        normal = [r["name"], *json.loads(r["aliases_json"])]
        short = json.loads(r["short_aliases_json"])
        for alias in dict.fromkeys(a for a in normal + short if a and len(vendor_key(a)) >= 2):
            pat = _pattern(alias)
            title.append((r["id"], alias, pat))
            if alias in normal and _in_body(alias):  # 短别名与短名字只在标题里匹配（D29）
                body.append((r["id"], alias, pat))
    return AliasIndex(title, body)


def match_vendors(index: AliasIndex, title: str, text: str) -> list[tuple[int, str]]:
    """一条情报提到的商家（可多个），每个商家只记第一个命中的别名。"""
    found: dict[int, str] = {}
    for vendor_id, alias, pat in index.title:
        if vendor_id not in found and pat.search(title):
            found[vendor_id] = alias
    for vendor_id, alias, pat in index.body:
        if vendor_id not in found and pat.search(text):
            found[vendor_id] = alias
    return list(found.items())


def link_intel(conn: sqlite3.Connection, intel_id: int, matches: list[tuple[int, str]]) -> None:
    conn.execute("DELETE FROM intel_vendors WHERE intel_id = ?", (intel_id,))
    conn.executemany("INSERT INTO intel_vendors (intel_id, vendor_id, matched_alias) VALUES (?, ?, ?)",
                     [(intel_id, vendor_id, alias) for vendor_id, alias in matches])


def relink_all_intel(conn: sqlite3.Connection) -> int:
    """别名表变化后重新匹配全部情报，返回关联条数。"""
    index = build_index(conn)
    links = 0
    for r in conn.execute("SELECT i.id, i.title, r.text_struct FROM intel i LEFT JOIN raw_posts r ON r.url = i.url")\
            .fetchall():
        matches = match_vendors(index, r["title"], r["text_struct"] or "")
        link_intel(conn, r["id"], matches)
        links += len(matches)
    return links
