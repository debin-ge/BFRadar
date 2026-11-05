"""线路与网络标签（优化方案 O-16、D71）：词表、否定句、套餐与区块标题与标题的取法、LLM 标签的校验。"""

import pytest

from app.extract.classify import classify
from app.extract.merge import specs_range
from app.extract.rules import extract, net_in
from app.extract.validate import validate_and_merge
from app.models import Plan
from app.textutil import Link


@pytest.mark.parametrize("text, tags", [
    ("Los Angeles CN2 GIA - Network", ["cn2gia"]),
    ("cn2-gia vps", ["cn2gia"]),
    ("100 Mbps CN2,GIA,CU,CM Network*", ["cn2gia"]),
    ("洛杉矶GIA线路", ["cn2gia"]),
    ("CN2 GT VPS", ["cn2gt"]),
    ("AS9929 + AS4837 optimized", ["9929", "4837"]),
    ("联通9929 A网", ["9929"]),
    ("CU VIP route", ["9929"]),
    ("联通169 线路", ["4837"]),
    ("CMIN2 and CMI", ["cmin2", "cmi"]),
    ("移动精品网", ["cmin2"]),
    ("BGP多线 VPS", ["bgp"]),
    ("原生IP 三网回程优化", ["native_ip", "3net"]),
    ("native IPv4 included", ["native_ip"]),
    ("CN2 GIA 不限流量", ["cn2gia"]),  # "不限" 不是否定
])
def test_vocabulary(text, tags):
    assert net_in(text) == tags


@pytest.mark.parametrize("text", [
    "no CN2 routing here", "Not CN2 GIA, plain BGP", "不支持CN2", "非 CN2 线路", "CN2 GIA 不支持", "CN2 not available",
    "BGP session included", "多线程性能", "native IPv6 support", "gia is part of a word", "CMIN2X",
    "IPv6 /64 and DDoS protection included",  # IPv6 与 DDoS 防护几乎每帖都写，不当标签
])
def test_negation_and_look_alikes_are_not_tags(text):
    assert net_in(text) == []


def plans_for(title: str, text: str, links=()) -> list[Plan]:
    c = classify("lowendtalk", title, text, ["Offers"])
    return extract("lowendtalk", title, text, list(links), "acme", c.lang, False).plans


TABLE = ("Plan | RAM | Disk | Network | Price\n"
         "LA-1 | 1 GB | 20 GB | CN2 GIA | $5/mo\n"
         "LA-2 | 2 GB | 40 GB | AS9929, AS4837 | $9/mo\n"
         "LA-3 | 4 GB | 80 GB | BGP | $15/mo")


def test_each_plan_gets_its_own_tags():
    one, two, three = plans_for("Acme VPS deals", TABLE)
    assert (one.net, two.net, three.net) == (["cn2gia"], ["9929", "4837"], [])
    assert specs_range([one, two, three])["net"] == ["cn2gia", "9929", "4837"]


def test_heading_above_the_block_applies_to_the_plans_under_it():
    text = "Los Angeles CN2 GIA plans:\nLA-1 | 1 GB | 20 GB | $5/mo\nLA-2 | 2 GB | 40 GB | $9/mo\n"
    assert [p.net for p in plans_for("Acme VPS deals", text)] == [["cn2gia"], ["cn2gia"]]


def test_title_tags_are_shared_by_plans_that_say_nothing():
    text = "Plan | RAM | Disk | Price\nA | 1 GB | 20 GB | $5/mo\nB | 2 GB | 40 GB | $9/mo\n"
    assert [p.net for p in plans_for("DMIT 9929 VPS Black Friday", text)] == [["9929"], ["9929"]]
    assert [p.net for p in plans_for("Acme VPS Black Friday", text)] == [[], []]


def test_a_far_away_heading_is_not_applied():
    """HostDare 那种一帖里好几个地区、线路只写在最上面一个标题里的：不往 12 行以外找，宁可不标。"""
    filler = "\n".join(f"Get 10% off on region {i} plans" for i in range(14))
    text = f"Explore Our Los Angeles VPS Plans — CN2 GIA Network!\n{filler}\nA | 1 GB | 20 GB | $5/mo\n"
    assert plans_for("Acme sale", text)[0].net == []


def test_all_plans_line_gives_a_default():
    text = "All plans include: CN2 GIA network, 1 IPv4\nPlan | RAM | Disk | Price\nA | 1 GB | 20 GB | $5/mo\n"
    assert plans_for("Acme sale", text)[0].net == ["cn2gia"]


# ---------------------------------------------------------------- LLM 给的标签


TITLE = "Acme VPS"
TEXT = "Plan | RAM | Disk | Network | Price\nLA-1 | 1 GB | 20 GB | CN2 GIA | $5/mo\nLA-2 | 2 GB | 40 GB | - | $9/mo"
LINKS = [Link(1, "Order", "https://acme.example/cart")]


def merged(llm_plans):
    c = classify("lowendtalk", TITLE, TEXT, ["Offers"])
    rules = extract("lowendtalk", TITLE, TEXT, LINKS, "acme", c.lang, False)
    rules.completeness = 0.5  # 让 LLM 的套餐胜出
    rules.plan_format = rules.plan_format.__class__("freetext")
    data = {"plans": llm_plans}
    return validate_and_merge(data, title=TITLE, text=TEXT, links=LINKS, author="acme", lang="en", rules=rules,
                              is_black_friday=c.is_black_friday, event_type=c.event_type, product_types=c.product_types)


def llm_plan(name, price, **extra):
    return {"name": name, "price": price, "currency": "USD", "period": "month", "locations": [], **extra}


def test_llm_tags_must_be_written_in_the_post():
    m = merged([llm_plan("LA-1", 5, net=["cn2gia"]), llm_plan("LA-2", 9, net=["9929", "cn2gia"])])
    assert m.plans_from == "llm"
    assert [p.net for p in m.plans] == [["cn2gia"], ["cn2gia"]]  # 9929 原文没写：丢掉，并记一次拒绝
    assert any("线路标签" in r for r in m.rejections)
    clean = merged([llm_plan("LA-1", 5, net=["cn2gia"])])
    assert clean.llm_rejected == 0 and clean.plans[0].net == ["cn2gia"]
