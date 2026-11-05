"""反幻觉校验与合并：编造的码、价格、链接、库存依据会被拒并回退规则；金标作为 LLM 输出时不应被误拒。"""

import json
import re
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.config import load_settings
from app.extract.classify import classify
from app.extract.llm import LlmClient, build_messages, rules_hint
from app.extract.rules import extract
from app.extract.validate import clip, same_brand, validate_and_merge, zh_len
from app.models import EventType, Period, PlanFormat, ProductType, Recurring
from app.textutil import Link

sys.path.insert(0, str(Path(__file__).parent / "tools"))
import pool  # noqa: E402

TITLE = "KVM Deals | Halloween"
TEXT = ("Use code SPOOKY50 for 50% off recurring.\n"
        "Plan | RAM | Disk | Price\nKVM-1 | 2 GB | 20 GB NVMe | $5/mo | Order [1]\nKVM-2 | 4 GB | 40 GB NVMe | $9/mo | Order [2]\n"
        "Limited stock available.")
LINKS = [Link(1, "Order", "https://host.example/cart.php?pid=1"), Link(2, "Order", "https://host.example/cart.php?pid=2")]


def rules_and_class():
    c = classify("lowendtalk", TITLE, TEXT, ["Offers"])
    return extract("lowendtalk", TITLE, TEXT, LINKS, "hostexample", c.lang, False), c


def llm_plan(name, ram, disk, price, link):
    return {"name": name, "ram_gb": ram, "disk_gb": disk, "disk_type": "NVMe", "price": price, "currency": "USD",
            "period": "month", "order_link_index": link, "locations": []}


GOOD = {
    "vendor": "host", "is_black_friday": False, "event_type": "holiday", "product_types": ["vps"],
    "discount": {"pct": 50, "recurring": "recurring", "text": "50% off recurring"},
    "coupons": [{"code": "SPOOKY50", "applies_to": None}],
    "plans": [llm_plan("KVM-1", 2, 20, 5, 1), llm_plan("KVM-2", 4, 40, 9, 2)],
    "purchase_link_index": 1, "summary_zh": "Host 万圣节 KVM 五折循环，$5/月起", "summary_en": "Host: 50% off KVM from $5/mo",
    "title_zh": "KVM 优惠 | 万圣节", "title_en": "should be ignored for an English post", "confidence": 0.9,
}


def merge(data, **overrides):
    rules, c = rules_and_class()
    rules = overrides.pop("rules", rules)
    return validate_and_merge(data, title=TITLE, text=TEXT, links=LINKS, author="hostexample", lang="en", rules=rules,
                              is_black_friday=c.is_black_friday, event_type=c.event_type,
                              product_types=c.product_types)


def low_completeness(rules):
    rules.completeness = 0.5  # 让 LLM 套餐有机会胜出
    return rules


def test_clean_output_is_accepted():
    rules, _ = rules_and_class()
    m = merge(GOOD, rules=low_completeness(rules))
    assert m.llm_rejected == 0 and m.rejections == []
    assert (m.vendor, m.event_type, m.recurring, m.coupon_codes) == ("host", EventType.HOLIDAY, Recurring.RECURRING, ["SPOOKY50"])
    assert m.purchase_url == LINKS[0].href
    assert m.plans_from == "llm" and [(p.name, p.price, p.period, p.order_url) for p in m.plans] == [
        ("KVM-1", 5, Period.MONTH, LINKS[0].href), ("KVM-2", 9, Period.MONTH, LINKS[1].href)]
    assert m.summary_zh and m.title_zh == "KVM 优惠 | 万圣节"
    assert m.title_en is None  # 英文帖不需要英文标题翻译
    assert m.extracted_by == "llm" and m.confidence == 0.9


def test_fabricated_values_fall_back_to_rules():
    bad = json.loads(json.dumps(GOOD))
    bad["coupons"] = [{"code": "SPOOKY50"}, {"code": "FAKE99"}, {"code": "spooky50"}]  # 大小写不同也算编造
    bad["vendor"] = "Totally Different Corp"
    bad["is_black_friday"] = True
    bad["event_type"] = "black_friday"
    bad["purchase_link_index"] = 7
    bad["discount"] = {"pct": 70, "recurring": "recurring", "text": "70% off"}
    bad["summary_en"] = "Host: KVM from $3/mo"
    m = merge(bad)
    rules, c = rules_and_class()
    assert m.coupon_codes == ["SPOOKY50"]
    assert m.vendor == rules.vendor and m.purchase_url == rules.purchase_url
    assert (m.is_black_friday, m.event_type) == (c.is_black_friday, c.event_type)
    assert m.discount_pct == rules.discount_pct
    assert m.summary_en is None
    assert m.llm_rejected == 8 and len(m.rejections) == 8


def test_plans_with_invented_prices_are_rejected():
    rules, _ = rules_and_class()
    data = dict(GOOD, plans=[llm_plan("KVM-1", 2, 20, 5, 1), llm_plan("KVM-3", 8, 80, 15, 9)])
    m = merge(data, rules=low_completeness(rules))
    assert [p.name for p in m.plans] == ["KVM-1"] and m.plans_from == "llm"
    data = dict(GOOD, plans=[llm_plan("A", 2, 20, 5, 1), llm_plan("B", 8, 80, 15, 1), llm_plan("C", 8, 80, 16, 1)])
    m = merge(data, rules=low_completeness(rules_and_class()[0]))
    assert m.plans_from == "rules"  # 通过的不足一半，整体回退


@pytest.mark.parametrize("summary, ok", [
    ("Host KVM 5 折循环，$5/月起", True),  # 5 折 = 50% off
    ("Host: 50% off recurring, from $5/mo", True),
    ("Host KVM 7 折，$5/月起", False),  # 原文没有 30% off
    ("Host: 70% off, from $5/mo", False),
    ("Host: 50% off, from $4.99/mo", False),
])
def test_summary_figures_must_be_grounded(summary, ok):
    m = merge(dict(GOOD, summary_en=summary))
    assert (m.summary_en == summary) is ok and (not m.rejections) is ok


def test_rules_keep_plans_for_clean_tables():
    rules, _ = rules_and_class()
    assert rules.plan_format is PlanFormat.PIPE and rules.completeness >= 0.8
    m = merge(dict(GOOD, plans=[llm_plan("Only", 2, 20, 5, 1)]))
    assert m.plans_from == "rules" and [p.name for p in m.plans] == ["KVM-1", "KVM-2"]
    assert m.summary_zh  # 摘要仍来自 LLM（D22）


def test_summary_is_trimmed_and_unknown_enums_rejected():
    data = dict(GOOD, summary_zh="很长" * 50, product_types=["vps", "gpu-cloud"], event_type="flash")
    m = merge(data)
    _, c = rules_and_class()
    assert len(m.summary_zh) == 60 and m.summary_zh.endswith("…")
    assert m.product_types == c.product_types and m.event_type == c.event_type  # 整项回退为分类结果
    assert any("product_types" in r for r in m.rejections) and any("event_type" in r for r in m.rejections)


def test_product_types_outside_the_four_are_dropped_and_only_them_means_not_collected():
    """D62：LLM 说的邮箱、域名、托管、其他是合法说法但不收；只剩它们时 product_types 为空，流水线据此整条不收。"""
    m = merge(dict(GOOD, product_types=["vps", "other"]))
    assert m.product_types == [ProductType.VPS] and m.rejections == []
    m = merge(dict(GOOD, product_types=["mail", "domain"]))
    assert m.product_types == [] and m.rejections == []


VM6_TITLE = "VM6 | KVM Deals"
VM6_TEXT = ("VM6 Networks here. 50% off, renewal prices unchanged.\n"
            "KVM-1 | 2 GB | 20 GB NVMe | $5/mo | Order [1]")
VM6_LINKS = [Link(1, "Order", "https://vm6.co.uk/cart.php?a=add&pid=1")]


def merge_vm6(data):
    c = classify("lowendtalk", VM6_TITLE, VM6_TEXT, ["Offers"])
    rules = extract("lowendtalk", VM6_TITLE, VM6_TEXT, VM6_LINKS, "VM6", c.lang, False)
    assert (rules.vendor, rules.recurring) == ("VM6", Recurring.RECURRING)
    return validate_and_merge(data, title=VM6_TITLE, text=VM6_TEXT, links=VM6_LINKS, author="VM6", lang="en",
                              rules=rules, is_black_friday=False, event_type=c.event_type,
                              product_types=c.product_types)


def test_weaker_llm_claims_keep_rules_results():
    m = merge_vm6({"vendor": "VM6 Networks", "discount": {"pct": 50, "recurring": "unknown", "text": "50% off"}})
    assert m.rejections == []  # 都通过了校验，只是不覆盖规则
    assert (m.vendor, m.recurring) == ("VM6", Recurring.RECURRING)


def test_purchase_link_on_same_site_keeps_rules_choice():
    title, text = "Acme | KVM", "Acme VPS from $5/mo, order here [1]. Visit our site [2] or our partner [3]."
    links = [Link(1, "order here", "https://billing.acme.example/cart.php?a=add&pid=1"),
             Link(2, "our site", "https://acme.example/"), Link(3, "partner", "https://other.example/")]
    c = classify("lowendtalk", title, text, ["Offers"])
    rules = extract("lowendtalk", title, text, links, "Acme", c.lang, False)
    assert rules.purchase_url == links[0].href

    def merged(index):
        return validate_and_merge({"purchase_link_index": index}, title=title, text=text, links=links, author="Acme",
                                  lang="en", rules=rules, is_black_friday=False, event_type=c.event_type,
                                  product_types=c.product_types).purchase_url

    assert merged(2) == links[0].href  # 同一商家站点：保留规则按固定顺序选的下单链接
    assert merged(3) == links[2].href  # 换了站点：以 LLM 为准


def test_contradicting_llm_claims_still_win():
    m = merge_vm6({"vendor": "Networks", "discount": {"pct": 50, "recurring": "first_term", "text": "50% off"}})
    assert (m.vendor, m.recurring) == ("Networks", Recurring.FIRST_TERM)


def test_same_brand():
    assert same_brand("VM6", "VM6 Networks") and same_brand("VM6 Networks", "VM6")
    assert same_brand("DeluxHost", "DeluxHost.net") and same_brand("hostdzire", "HostDZire")
    assert not same_brand("VM6", "VM") and not same_brand("Host", "HostDZire") and not same_brand(None, "VM6")


ZH_LONG = "EnderHost 首登 LEB，多伦多 Ryzen VPS 用码 LETSPECIAL96 低至 $3.84/月，续费同价；其他机房用码 LETSPECIAL50 享五折，库存有限，先到先得。"


def test_clip_measures_zh_in_characters_and_cuts_at_clauses():
    fits = "Mango Mail 万圣节邮件托管促销，最高 40% 折扣，100GB 套餐 $8.70/月、30GB 套餐 $3.50/月，限时。"
    assert zh_len(fits) == 46.5 and clip(fits, 60, zh_len) == fits  # 英文字母算半字，不必截
    out = clip(ZH_LONG, 60, zh_len)
    assert out == "EnderHost 首登 LEB，多伦多 Ryzen VPS 用码 LETSPECIAL96 低至 $3.84/月，续费同价；其他机房用码 LETSPECIAL50 享五折…"
    assert zh_len(out) <= 60


@pytest.mark.parametrize("text, measure", [(ZH_LONG, zh_len), (
    "Mango Mail Halloween sale: up to 40% off email hosting, 100GB plan $8.70/mo and 30GB plan $3.50/mo, limited time.",
    len)])
def test_clip_never_cuts_inside_a_token(text, measure):
    for limit in range(4, 80):
        out = clip(text, limit, measure)
        if out is None or out == text:
            continue
        head = out[:-1]
        assert measure(out) <= limit and text.startswith(head)
        token = re.compile(r"[A-Za-z0-9.$/%]")  # 汉字之间可以切，英文词、数字与价格中间不行
        assert not (token.match(head[-1]) and token.match(text[len(head)])), (limit, out)
    assert clip("A" * 200, 140) is None  # 无处可切时宁可不要


# ---------------------------------------------------------------- 金标作为 LLM 输出：不应误拒


def gold_payload(labels: dict, links: list[Link]) -> dict:
    index = {lk.href: lk.n for lk in links}
    return {
        "vendor": labels["vendor"], "is_black_friday": labels["is_black_friday"], "event_type": labels["event_type"],
        "product_types": labels["product_types"], "discount": labels["discount"],
        "coupons": [{"code": c} for c in labels["coupon_codes"]],
        "plans": [dict({k: v for k, v in p.items() if k != "order_url"}, order_link_index=index.get(p["order_url"]))
                  for p in labels["plans"]],
        "purchase_link_index": index.get(labels["purchase_url"]),
        "summary_zh": f"{labels['vendor']} 优惠", "summary_en": f"{labels['vendor']} deals", "confidence": 1,
    }


DEV_OFFERS = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((Path(__file__).parent / "gold").glob("*.json"))]
DEV_OFFERS = [s for s in DEV_OFFERS if s["split"] == "dev" and s["labels"]["record_kind"] == "offer"]


@pytest.mark.parametrize("sample", DEV_OFFERS, ids=[s["id"] for s in DEV_OFFERS])
def test_gold_labels_pass_validation(sample):
    item = pool.load_item(sample["fixture"], sample["fixture_item_guid"], sample["source"])
    st = pool.render(item.html)
    c = classify(sample["source"], item.title, st.text, item.categories)
    rules = extract(sample["source"], item.title, st.text, st.links, item.author, c.lang, "<td" in item.html.lower())
    rules.completeness = 0.0  # 强制走 LLM 套餐，检查套餐转换
    labels = sample["labels"]
    m = validate_and_merge(gold_payload(labels, st.links), title=item.title, text=st.text, links=st.links,
                           author=item.author, lang=c.lang, rules=rules, is_black_friday=c.is_black_friday,
                           event_type=c.event_type, product_types=c.product_types)
    assert m.rejections == [], m.rejections
    assert m.plans_from == ("llm" if labels["plans"] else "rules")
    if labels["plans"]:
        assert [(p.price, p.currency, str(p.period) if p.period else None, p.ram_gb, p.disk_gb, p.order_url)
                for p in m.plans] == [(p["price"], p["currency"], p["period"], p["ram_gb"], p["disk_gb"], p["order_url"])
                                      for p in labels["plans"]]
    assert (m.vendor, m.coupon_codes, m.purchase_url) == (labels["vendor"], labels["coupon_codes"], labels["purchase_url"])


# ---------------------------------------------------------------- 端到端（mock 服务端，真实帖）


def test_end_to_end_with_mock_server():
    sample = next(s for s in DEV_OFFERS if s["id"] == "let-222765")
    item = pool.load_item(sample["fixture"], sample["fixture_item_guid"], sample["source"])
    st = pool.render(item.html)
    c = classify("lowendtalk", item.title, st.text, item.categories)
    rules = extract("lowendtalk", item.title, st.text, st.links, item.author, c.lang, False)
    reply = gold_payload(sample["labels"], st.links) | {
        "summary_zh": "VM6 英国 EPYC VPS 黑五：年付 £27 起（码 BFVPS55），首期优惠。", "title_zh": "黑五 | 英国 VPS"}
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply, ensure_ascii=False)}}]})

    env = {"LLM_BASE_URL": "https://llm.example.com/v1", "LLM_API_KEY": "k", "LLM_MODEL": "m"}
    with LlmClient(load_settings(env, env_file=None), transport=httpx.MockTransport(handler)) as llm:
        resp = llm.complete(build_messages("lowendtalk", sample["published_at"], item.title, st.text, st.links,
                                           rules_hint(rules)))
    m = validate_and_merge(resp.data, title=item.title, text=st.text, links=st.links, author=item.author, lang=c.lang,
                           rules=rules, is_black_friday=c.is_black_friday, event_type=c.event_type,
                           product_types=c.product_types)
    assert "UK EPYC E4" in seen["body"]["messages"][1]["content"]
    assert m.rejections == [] and m.summary_zh.startswith("VM6") and m.title_zh == "黑五 | 英国 VPS"
    assert m.coupon_codes == ["BFVPS55", "BFVPS60"] and m.recurring is Recurring.FIRST_TERM


# ---------------------------------------------------------------- 免费套餐（D55）

FREE_TEXT = ("Free NVMe Shared Hosting - USA\n- 1GB NVMe Storage\n- 50GB bandwidth per month\n- Free SSL Certificate\n"
             "Order Free Hosting [1]")
FREE_LINKS = [Link(1, "Order Free Hosting", "https://acme.example/free-hosting")]
FREE_PLAN = {"name": "Free NVMe Shared Hosting - USA", "price": 0, "currency": "USD", "period": "month", "disk_gb": 1,
             "disk_type": "NVMe", "order_link_index": 1, "locations": ["USA"]}


def free_merge(title):
    c = classify("lowendspirit", title, FREE_TEXT, ["Free Offers and Stuff"])
    rules = extract("lowendspirit", title, FREE_TEXT, FREE_LINKS, "acme", c.lang, False)
    return validate_and_merge({"plans": [FREE_PLAN]}, title=title, text=FREE_TEXT, links=FREE_LINKS, author="acme",
                              lang="en", rules=low_completeness(rules), is_black_friday=c.is_black_friday,
                              event_type=c.event_type, product_types=c.product_types)


def test_free_plan_needs_a_free_title():
    m = free_merge("Acme - Free Web Hosting, No Ads")
    assert m.plans_from == "llm" and [(p.price, p.currency, p.period) for p in m.plans] == [(0.0, None, None)]
    assert m.plans[0].order_url == "https://acme.example/free-hosting" and not m.rejections
    m = free_merge("Acme shared hosting")  # 标题没写送免费主机：0 价不在原文，拒
    assert m.plans_from == "rules" and m.plans == [] and any("价格不在原文" in r for r in m.rejections)


# ---------------------------------------------------------------- 截止日期（O-15、D70）


def dated(data, text_extra="", published="2026-11-03T10:00:00Z"):
    text = TEXT + text_extra
    c = classify("lowendtalk", TITLE, text, ["Offers"])
    rules = extract("lowendtalk", TITLE, text, LINKS, "hostexample", c.lang, False, published)
    merged = validate_and_merge(data, title=TITLE, text=text, links=LINKS, author="hostexample", lang="en", rules=rules,
                                is_black_friday=c.is_black_friday, event_type=c.event_type,
                                product_types=c.product_types, published=published)
    return rules, merged


def test_rule_end_date_is_used_when_the_llm_gives_none():
    rules, m = dated({}, "\nOffer valid until December 2.")
    assert rules.ends_at == m.ends_at == date(2026, 12, 2) and m.ends_source == "rule"


def test_llm_end_date_must_appear_in_the_text():
    # 原文写了 12 月 2 日与 12 月 9 日：LLM 取其中一个，通过校验、来源记为 llm
    _, ok = dated({"ends_at": "2026-12-09"}, "\nValid until December 2. Coupon valid until December 9.")
    assert ok.ends_at == date(2026, 12, 9) and ok.ends_source == "llm" and ok.llm_rejected == 0
    # 原文没有的日期：拒绝并回退规则层（规则层认出的 12 月 2 日）
    _, bad = dated({"ends_at": "2026-12-15"}, "\nValid until December 2.")
    assert bad.ends_at == date(2026, 12, 2) and bad.ends_source == "rule"
    assert any("ends_at" in r for r in bad.rejections)
    # 原文没写日期，LLM 编了一个：拒绝，没有截止日期
    _, none = dated({"ends_at": "2026-12-15"})
    assert none.ends_at is None and none.ends_source is None and any("ends_at" in r for r in none.rejections)
    # 格式不对：拒绝
    assert dated({"ends_at": "December 2"}, "\nValid until December 2.")[1].ends_source == "rule"


def test_llm_may_resolve_a_relative_deadline():
    _, m = dated({"ends_at": "2026-11-05"}, "\nFlash sale: 48 hours only!")
    assert m.ends_at == date(2026, 11, 5) and m.ends_source == "llm"
