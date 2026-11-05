"""套餐分段：四种锚点、上下文继承、限定价格与形态判定。"""

import xml.etree.ElementTree as ET

import pytest

from app.extract.segment import has_plan_price, plan_format, segment
from app.models import PlanFormat
from app.textutil import Link, structure_html
from conftest import snapshot


def links(*hrefs):
    return [Link(i, anchor, href) for i, (anchor, href) in enumerate(hrefs, 1)]


def test_table_rows_with_header():
    text = "Plan | vCPU | RAM | Price\nKVM-1 | 1 | 1 GB | $5/mo\nKVM-2 | 2 | 2 GB | $9/mo"
    segs = segment(text, [])
    assert [s.anchor for s in segs] == ["row", "row"]
    assert segs[0].lines == ["KVM-1 | 1 | 1 GB | $5/mo"]
    assert segs[0].header == ["Plan", "vCPU", "RAM", "Price"]
    assert segs[1].context == ["Plan | vCPU | RAM | Price"]


def test_pipe_config_row_feeds_price_only_rows():
    text = ("AMD EPYC || 2vCore || 4GB RAM || 40GB NVMe || 4TB BW/mo||\n"
            "Price: €21/ Annually || Order Now [1] ||\n"
            "Price: €38/ Biennially || Order Now [2] ||")
    segs = segment(text, links(("Order Now", "https://x/cart.php?pid=1"), ("Order Now", "https://x/cart.php?pid=2")))
    assert len(segs) == 2
    assert all(s.context == ["AMD EPYC || 2vCore || 4GB RAM || 40GB NVMe || 4TB BW/mo||"] for s in segs)
    assert segs[1].link_refs == [2]


def test_middle_dot_rows():
    text = "Dual E5-2690 · 16c/32t · 256GB · 1Gbps · 1 IPv4 — $64.99\n>> Order Here << [1]"
    segs = segment(text, links((">> Order Here <<", "https://q/dedicated-servers")))
    assert [s.anchor for s in segs] == ["row"]


def test_order_link_block_starts_at_plan_title():
    text = ("Black Friday: UK EPYC VPS (Norwich)\n55% off yearly with code BFVPS55.\n"
            "UK EPYC E4\n- 2 x AMD EPYC 7642 (2.3GHz - 3.3GHz)\n- 4 GB ECC DDR4 RAM\n- 60GB NVMe SSD\n"
            "- £27 a year with BFVPS55, normally £60\n👉Order UK EPYC E4 [1]")
    segs = segment(text, links(("Order UK EPYC E4", "https://www.vm6.co.uk/enterprise-vps-uk")))
    assert len(segs) == 1 and segs[0].anchor == "order_link"
    assert segs[0].lines[0] == "UK EPYC E4"


def test_order_lines_without_spec_inherit_previous_block():
    text = ("====LXC NAT VPS Deals\nCPU: 1 CPU vCores\nRAM: 256MB DDR3/DDR4\nDisk: 5 Gb SSD Disk Space\n"
            "Order South Africa- $5.50 / Year [1]\nOrder HongKong- $5.50 / Year [2]")
    hrefs = [("Order South Africa- $5.50 / Year", "https://k/store/nat"), ("Order HongKong- $5.50 / Year", "https://k/store/nat")]
    segs = segment(text, links(*hrefs))
    assert len(segs) == 2
    assert "RAM: 256MB DDR3/DDR4" in segs[1].lines[0:0] + segs[1].context


def test_title_block_without_order_link():
    text = ("We have prepared special configurations.\nIncluded 1313 plans:\nRAM: 13GB\nSSD: 113GB NVMe\n"
            "CPU: 6 fair-share EPYC Milan cores\nPrice: $123 triennially\n"
            "Inventory will rotate and update periodically, so keep that page bookmarked! Campaign period: October 26–31, 2026.")
    segs = segment(text, [])
    assert len(segs) == 1 and segs[0].anchor == "title"
    assert segs[0].lines[0] == "Included 1313 plans:" and segs[0].lines[-1] == "Price: $123 triennially"


def test_spec_lines_list_items_and_prose():
    text = "- Starter, 5GB: £1/mo, billed annually. £8.28/year with HALLOWEEN31\nGrab their 100GB plan for just $8.70/mo!"
    segs = segment(text, [])
    assert [s.anchor for s in segs] == ["spec_line", "spec_line"]
    assert plan_format(segs, False, True) is PlanFormat.LIST  # 两块并列时 list 优先


@pytest.mark.parametrize("line, expected", [
    ("Plans from $5/mo", False),
    ("from a $15.95 starter box up to a 128GB RAM machine; none cost more than $100 a month", False),
    ("🎁 LET Black Friday Giveaway: over £800 of EPYC VPS", False),
    ("bringing pricing down to $0.96/GB of RAM", False),
    ("Free egress up to 3× your stored data, then $1/TB", False),
    ("£27 a year (36.13 USD) with BFVPS55, normally £60", True),
    ("Canada-KVM2GB | 2 GB RAM | 5$/Month| Order NOW", True),
    ("Promotional Price: 8.24€/quarterly", True),
])
def test_qualified_prices_are_not_plan_prices(line, expected):
    assert has_plan_price(line) is expected


def test_boilerplate_and_qualified_blocks_are_dropped():
    text = ("So this time we're sharing six dedicated servers, from a $15.95 starter box up to a 128GB RAM machine.\n"
            "The post Cheap VPS Deals! Get a 4GB VPS for Only $3.84/Month! [1] appeared first on LowEndBox [2].")
    assert segment(text, []) == []


@pytest.mark.parametrize("anchors, has_table, expected", [
    (["row"] * 5 + ["order_link"] * 6, False, PlanFormat.PIPE),  # 行占四成以上
    (["row"] * 4 + ["order_link"] * 4, True, PlanFormat.TABLE),
    (["row"] + ["order_link"] * 4, False, PlanFormat.LIST),
    (["spec_line"] * 3, False, PlanFormat.FREETEXT),
    ([], False, PlanFormat.NONE),
])
def test_plan_format(anchors, has_table, expected):
    from app.extract.segment import Segment

    segs = [Segment(a, i, i, ["Paragraph with $5/mo and 2 GB RAM"]) for i, a in enumerate(anchors)]
    assert plan_format(segs, has_table, has_price=bool(anchors)) is expected


def test_real_post_hostdzire_segments_every_price_row():
    root = ET.parse(snapshot("lowendtalk_offers_2026-11-02.xml")).getroot()
    item = next(i for i in root.iter("item") if i.findtext("guid") == "222164@/discussions")
    st = structure_html(item.findtext("description"))
    segs = segment(st.text, st.links)
    assert len(segs) == 41  # 云 VPS 4×3、存储 VPS 6×3、加拿大独服 8、印度独服 3
    assert all(s.anchor == "row" for s in segs)
    assert segs[0].context == ["AMD EPYC || 2vCore || 4GB RAM || 40GB NVMe || 4TB BW/mo|| 1Gbit Port || 1 IPV4/1 IPV6 ||"]
    assert plan_format(segs, False, True) is PlanFormat.PIPE
