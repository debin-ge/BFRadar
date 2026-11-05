"""商家别名表与归并（M2.6，D29、D52）：同步与去重、泛词、名字归并、情报匹配的边界规则。"""

import json
from datetime import UTC, datetime

import pytest

from app.db import connect, migrate
from app.models import RawPost
from app.pipeline import crawl
from app.sources.base import FetchResult
from app.vendors import (GENERIC, build_index, curated, is_generic, match_vendors, resolve_vendor, sync_vendors,
                         vendor_key)


@pytest.fixture
def conn():
    c = connect(":memory:")
    migrate(c)
    return c


def add_vendor(conn, slug, name):
    return conn.execute("INSERT INTO vendors (slug, name) VALUES (?, ?)", (slug, name)).lastrowid


def add_deal(conn, n, vendor_id):
    conn.execute("INSERT INTO deals (url, source, title, first_seen_at, last_seen_at, content_hash, updated_at,"
                 " vendor_id) VALUES (?, 'lowendtalk', 't', 't', 't', 'h', 't', ?)", (f"https://x/{n}", vendor_id))


def slug_of(conn, vendor_id):
    return conn.execute("SELECT slug FROM vendors WHERE id = ?", (vendor_id,)).fetchone()[0]


def test_alias_table_is_consistent():
    table = curated()
    assert len(table) == 100  # M4.2：别名表扩到 100 家
    assert len({c.slug for c in table}) == len(table)
    owners = {}
    for c in table:  # 一个写法只属于一家（同一家的 "Liquid Web"/"Liquidweb" 归一后相同，正文匹配仍要两种写法）
        for key in {vendor_key(x) for x in (c.name, *c.aliases, *c.short)}:
            assert owners.setdefault(key, c.slug) == c.slug, key
    assert not [c.name for c in table if is_generic(c.name)]
    assert all(not c.website or c.website.startswith("https://") for c in table)


def test_sync_merges_duplicates_and_drops_generic_names(conn):
    vm6, vm6n = add_vendor(conn, "vm6", "VM6"), add_vendor(conn, "vm6-networks", "VM6 Networks")
    smoky, smoky_com = add_vendor(conn, "smokyhosts", "SmokyHosts"), add_vendor(conn, "smokyhosts-com", "SmokyHosts.com")
    junk = [add_vendor(conn, "hosting", "Hosting"), add_vendor(conn, "halloween-sale-page", "Halloween sale page")]
    other = add_vendor(conn, "caliber", "Caliber")
    for n, vid in enumerate([vm6, vm6n, vm6n, smoky, smoky_com, smoky_com, *junk, other]):
        add_deal(conn, n, vid)
    stats = sync_vendors(conn)
    assert (stats["curated"], stats["merged"], stats["generic"]) == (len(curated()), 2, 2)
    counts = dict(conn.execute("SELECT v.slug, COUNT(d.id) FROM vendors v LEFT JOIN deals d ON d.vendor_id = v.id"
                               " WHERE v.slug IN ('vm6', 'smokyhosts', 'caliber') GROUP BY v.slug"))
    assert counts == {"vm6": 3, "smokyhosts": 3, "caliber": 1}
    assert conn.execute("SELECT COUNT(*) FROM deals WHERE vendor_id IS NULL").fetchone()[0] == 2
    row = conn.execute("SELECT name, aliases_json, website FROM vendors WHERE slug = 'vm6'").fetchone()
    assert (row["name"], json.loads(row["aliases_json"]), row["website"]) == ("VM6", ["VM6 Networks"],
                                                                              "https://www.vm6.co.uk")
    again = sync_vendors(conn)
    assert (again["merged"], again["generic"]) == (0, 0)


def test_resolve_uses_aliases_suffixes_and_rejects_generic_names(conn):
    sync_vendors(conn)
    assert slug_of(conn, resolve_vendor(conn, "RackNerd LLC")) == "racknerd"
    assert slug_of(conn, resolve_vendor(conn, "瑞克")) == "racknerd"
    assert slug_of(conn, resolve_vendor(conn, "GreenCloudVPS")) == "greencloud"
    assert resolve_vendor(conn, "smokyhosts.com") == resolve_vendor(conn, "SmokyHosts")  # .com 后缀不算
    assert slug_of(conn, resolve_vendor(conn, "Godlike.host")) == "godlike-host"  # .host 是品牌的一部分
    for junk in ("Hosting", "Terms of Service", "Black Friday Deals", "Halloween sale page"):
        assert resolve_vendor(conn, junk) is None, junk
    assert {"vps", "hosting"} <= GENERIC


@pytest.mark.parametrize("title, text, expected", [
    ("奶糕 RS 1000 年付", "", {"netcup"}),
    ("出奶糕 RS 1000", "", {"netcup"}),  # 中文按子串：中文不用空格分词（D52）
    ("求求了，纯电信CN2gia除了大妈还有啥？", "", {"buyvm"}),
    ("【出】溢价100 出绿云日本 2222 软银内存流量翻倍", "", {"greencloud"}),
    ("出杭州阿里2c2g 40g硬盘 200m 带价来", "", {"aliyun"}),
    ("求推荐", "楼下大妈说阿里那家不错", set()),  # 两个字的中文俗称只看标题（D29）
    ("[NQ留档]腾讯云轻量服务器首尔测评", "", {"tencent-cloud"}),
    ("下载 8-10TB 数据有什么机推荐", "之前用过搬瓦工和腾讯云", {"bandwagonhost", "tencent-cloud"}),  # 三个汉字的名字正文也认
    ("Which provider?", "AWS or OVH?", set()),  # 三个字符以内的英文名只看标题
    ("AWS vs OVH", "", {"aws", "ovhcloud"}),
    ("dmit这两款是清退了吗？", "", {"dmit"}),
    ("I admit it", "", set()),
    ("美西三网各自精品二选一：VMISS & VMR", "", {"vmiss", "vmrack"}),
    ("Which provider?", "I moved from RackNerd to Hetzner last year", {"racknerd", "hetzner"}),
    ("cc 年付 2G", "", {"cloudcone"}),
    ("Which provider?", "please cc me on the ticket", set()),  # 短别名只看标题
    ("Liquidweb just raised my rate", "", {"liquid-web"}),
    ("A high Caliber build", "", set()),  # 不在别名表里的商家名不参与匹配
])
def test_intel_matching_rules(conn, title, text, expected):
    sync_vendors(conn)
    add_vendor(conn, "caliber", "Caliber")
    slugs = {slug_of(conn, vid) for vid, _ in match_vendors(build_index(conn), title, text)}
    assert slugs == expected


class OneRound:
    id = "nodeseek"

    def __init__(self, settings, posts):
        self.settings, self.posts = settings, posts

    def fetch(self, state=None):
        return FetchResult(self.posts, {"count": len(self.posts)})


def test_pipeline_links_intel_to_vendors(tmp_path, settings):
    from app.db import Database

    db = Database(tmp_path / "radar.db")
    db.migrate()
    with db.conn() as conn:
        sync_vendors(conn)
    post = RawPost("nodeseek", "https://www.nodeseek.com/post-1-1", "出搬瓦工 CN2 GIA VPS 年付", "<p>DC6 机房</p>",
                   categories=["trade"], published_at=datetime(2026, 11, 2, tzinfo=UTC))
    crawl(db, [OneRound(settings, [post])], reason="test")
    rows = db.conn().execute("SELECT v.slug, iv.matched_alias FROM intel_vendors iv JOIN vendors v ON v.id = iv.vendor_id")
    assert [tuple(r) for r in rows] == [("bandwagonhost", "搬瓦工")]


def test_forty_five_added_in_m4_resolve_their_variants(conn):
    """M4.2 新增的商家：库里出现过的另一种写法并到同一家，短别名只在标题里认。"""
    sync_vendors(conn)
    by_slug = {r["slug"]: r["id"] for r in conn.execute("SELECT id, slug FROM vendors")}
    assert resolve_vendor(conn, "ServerHost September Savings") == by_slug["serverhost"]
    assert resolve_vendor(conn, "INET.WS") == by_slug["inetcloud"]
    assert resolve_vendor(conn, "Ava.hosting") == resolve_vendor(conn, "Ava") == by_slug["ava"]
    assert resolve_vendor(conn, "D4Networks") == by_slug["d4-networks"] == resolve_vendor(conn, "D4 Networks")
