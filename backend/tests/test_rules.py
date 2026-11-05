"""字段规则：价格角色与周期、配置、帖子级字段，以及真实帖的端到端抽取。"""

import xml.etree.ElementTree as ET

import pytest

from app.extract import keywords as kw
from app.extract import rules
from app.extract.rules import (extract, extract_codes, extract_discount, extract_purchase_url,
                               extract_vendor, price_hits, select_prices)
from app.models import DiskType, Period, Recurring
from app.textutil import Link, structure_html
from conftest import snapshot


def picked(line, lang="en", hint=None):
    return [(h.amount, h.currency, h.period) for h in select_prices(price_hits(line, lang, hint))]


# ---------------------------------------------------------------- 价格与周期


@pytest.mark.parametrize("line, expected", [
    ("- £27 a year (36.13 USD) with BFVPS55, normally £60", [(27, "GBP", Period.YEAR)]),
    ("- £48 for 2 years (64.22 USD) with BFVPS60, works out at £2/mo", [(48, "GBP", Period.BIENNIAL)]),
    ("Price: €21/ Annually || Order Now [2] ||", [(21, "EUR", Period.YEAR)]),
    ("Houston KVM | $5/Month or $60/Year | Order", [(5, "USD", Period.MONTH), (60, "USD", Period.YEAR)]),
    ("Nitro 1 | 1 vCPU | first payment $2.00 | then $4.00/mo", [(2, "USD", None)]),
    ("First Month Price: $14.36\nRecurring Price: $15.95/mo", [(14.36, "USD", Period.MONTH)]),
    ("- Starter, 5GB: £1/mo, billed annually. £8.28/year with HALLOWEEN31", [(8.28, "GBP", Period.YEAR)]),
    ("€5.49/mo → €3.84/mo with coupon", [(3.84, "EUR", Period.MONTH)]),
    ("Canada-KVM2GB | 2 GB RAM | 5$/Month| Order NOW", [(5, "USD", Period.MONTH)]),
    ("Promotional Price: 8.24€/quarterly", [(8.24, "EUR", Period.QUARTER)]),
    ("Price: $123 triennially", [(123, "USD", Period.TRIENNIAL)]),
    ("4 GB | 2 | 150 GB | 20 TB | $27/yr [6] - $2.70/m [7]", [(27, "USD", Period.YEAR), (2.70, "USD", Period.MONTH)]),
    ("💰£7.50 GBP / $10 *per month", [(7.50, "GBP", Period.MONTH)]),
    ("EPYC $1 Server", []),
    ("Plans from $5/mo, up to $20/mo", []),
    ("PROMO PRICE: €1,200/month (excl. VAT)", [(1200, "EUR", Period.MONTH)]),
    ("年付 99 元，月付 10 元", [(99, "CNY", Period.YEAR), (10, "CNY", Period.MONTH)]),
])
def test_price_roles_and_periods(line, expected):
    got = [x for ln in line.splitlines() for x in picked(ln)] if "\n" in line else picked(line)
    if "\n" in line:  # 多行时与套餐块一样整体挑选
        got = [(h.amount, h.currency, h.period) for h in select_prices(
            [h for ln in line.splitlines() for h in price_hits(ln, "en")])]
    assert got == expected


def test_yen_depends_on_language_and_hint_fills_period():
    assert picked("¥199", "zh", Period.YEAR) == [(199, "CNY", Period.YEAR)]
    assert picked("¥199", "en") == [(199, "JPY", None)]


@pytest.mark.parametrize("line, expected", [  # 前五条取自 11-02 快照池里没有套餐的优惠帖
    ("Pricing: Starting from $340.66", [(340.66, "USD", None)]),
    ("Check our Web Hosting (starts only from 1,99€ per month)", [(1.99, "EUR", "month")]),
    ("build your own VPS plan - from $0.10 p/month or $0.000135 per hour", [(0.1, "USD", "month")]),
    ("Official ISPmanager Licenses starting at $7.04/mo", [(7.04, "USD", "month")]),
    ("Plesk Hosting in Germany – Starting at just $2.35/mo!", [(2.35, "USD", "month")]),
    ("香港 CN2 年付 99 元起", [(99, "CNY", "year")]),
    ("Minimum account top-up is only $3", []),
    ("Save $50 on your first order", []),
    ("Object storage from $6/TB per month", []),
])
def test_start_prices_are_post_level_only(line, expected):
    got = rules.start_prices("", line, "zh" if "元" in line else "en")
    assert [(p["amount"], p["currency"], p["period"]) for p in got] == expected
    assert picked(line) == [] or expected == []  # 起价不进套餐价格


@pytest.mark.parametrize("text, expected", [
    ("All prices USD · billed monthly · bare-metal", Period.MONTH),
    ("Monthly prices below, in USD.", Period.MONTH),
    ("Billing Cycle: Annual Billing", Period.YEAR),
    ("Nothing about billing here.", None),
])
def test_default_period(text, expected):
    assert rules.default_period(text) is expected


# ---------------------------------------------------------------- 配置


def plans_of(text, links=(), title="", storage_like=False):
    from app.extract.segment import segment

    links = list(links)
    return rules.extract_plans(text, links, segment(text, links), "en", extract_codes(title, text), storage_like)


def test_table_header_maps_columns():
    text = ("Plan | vCPU | RAM | HDD Storage | NVMe SSD | Port | Price/month | Order\n"
            "STORAGE-1 | 1 | 2 GB | 500 GB | 5 GB | 1 Gbps | €1.49 | ORDER STORAGE-1 [1]")
    (p,) = plans_of(text, [Link(1, "ORDER STORAGE-1", "https://d/cart?add=storage-1")])
    assert (p.name, p.cpu_cores, p.ram_gb, p.disk_gb, p.disk_type, p.port_gbps) == ("STORAGE-1", 1, 2, 500, DiskType.HDD, 1)
    assert (p.price, p.currency, p.period, p.order_url) == (1.49, "EUR", Period.MONTH, "https://d/cart?add=storage-1")


def test_pipe_config_row_and_ipv4_column_is_not_recorded():
    text = ("AMD EPYC || 2vCore || 4GB RAM || 40GB NVMe || 4TB BW/mo|| 1Gbit Port || 1 IPV4/1 IPV6 ||\n"
            "Price: €21/ Annually || Order Now [1] ||")
    (p,) = plans_of(text, [Link(1, "Order Now", "https://h/cart.php?pid=1")])
    assert (p.cpu_cores, p.ram_gb, p.disk_gb, p.disk_type, p.bandwidth_tb, p.port_gbps) == (
        2, 4, 40, DiskType.NVME, 4, 1)
    assert not hasattr(p, "ipv4")  # 商家默认都给 IPv4，不记（D69）
    z = plans_of("Plan | vCore | RAM | SSD | Port | IPv4 | Price/year\nZ-0 | 1 | 512 MB | 8 GB | 1 Gbps | ❌ | €6")[0]
    assert (z.ram_gb, z.disk_gb, z.disk_type, z.period) == (0.5, 8, DiskType.SSD, Period.YEAR)  # IPv4 列里的 ❌ 不影响别的字段


def test_middle_dot_row_positional_ram_and_disk():
    text = "All prices USD · billed monthly\nDual E5-2690 · 16c/32t · 256GB · 4x 300GB SAS · 1Gbps · 1 IPv4 — $64.99"
    (p,) = plans_of(text)
    assert (p.cpu_cores, p.ram_gb, p.disk_gb, p.disk_type, p.period) == (16, 256, 1200, None, Period.MONTH)


def test_list_block_with_heading_location_and_codes():
    text = ("🖤 Black Friday: UK EPYC VPS (Norwich)\nUK EPYC E4\n- 2 x AMD EPYC 7642 (2.3GHz - 3.3GHz)\n"
            "- 4 GB ECC DDR4 RAM\n- 60GB NVMe SSD\n- 5TB Traffic @ 10Gbps\n- 1 IPv4 Included\n"
            "- £27 a year (36.13 USD) with BFVPS55, normally £60\n- £48 for 2 years (64.22 USD) with BFVPS60\n"
            "👉Order UK EPYC E4 [1]")
    a, b = plans_of(text, [Link(1, "Order UK EPYC E4", "https://www.vm6.co.uk/enterprise-vps-uk")])
    assert (a.name, a.cpu_cores, a.ram_gb, a.disk_gb, a.bandwidth_tb, a.port_gbps, a.locations) == (
        "UK EPYC E4", 2, 4, 60, 5, 10, ["Norwich"])
    assert (a.price, a.period, a.coupon_code, b.price, b.period, b.coupon_code) == (
        27, Period.YEAR, "BFVPS55", 48, Period.BIENNIAL, "BFVPS60")


def test_post_defaults_fill_missing_fields():
    text = ("CPU | Memory | Storage | Port | Monthly Price | Order\n"
            "1× Intel Xeon E5-2670v3 | 64GB DDR4 | 1TB SSD | 1Gbps | $60/mo | Order Now [1]\n"
            "Every plan includes:\n- 100TB monthly bandwidth\n- IPv4 /31 allocation — 1 customer-usable IP\n- NYC location")
    (p,) = plans_of(text, [Link(1, "Order Now", "https://p/?cmd=cart&id=1")])
    assert (p.name, p.ram_gb, p.disk_gb, p.bandwidth_tb, p.locations) == (
        "1× Intel Xeon E5-2670v3", 64, 1000, 100, ["New York"])


def test_storage_like_sizes_and_sentence_clauses():
    text = "Grab their 100GB plan for just $8.70/mo or their 30GB olabn for $3.50/mo!"
    plans = plans_of(text, storage_like=True)
    assert [(p.disk_gb, p.price) for p in plans] == [(100, 8.70), (30, 3.50)]
    assert all(p.ram_gb is None for p in plans)


def test_global_code_is_not_written_to_plans():
    text = ("- HN-FR1: 4GB DDR3 / 1x1TB HDD SATA\nFirst Month Additional 10% Off with Promocode: LET10 - First Month Price: $14.36\n"
            "→ Order HN-FR1 [1]\n- HN-NL3: 32GB DDR4 / 2x500GB SSD SATA\nFirst Month Price with Promocode: LET10 $52.16\n"
            "→ Order HN-NL3 [2]")
    links = [Link(1, "Order HN-FR1", "https://m/store/fr1"), Link(2, "Order HN-NL3", "https://m/store/nl3")]
    plans = plans_of(text, links)
    assert [p.coupon_code for p in plans] == [None, None]


# ---------------------------------------------------------------- 帖子级字段


def test_vendor_prefers_self_reference_of_link_domain():
    links = [Link(1, "Order", "https://godlike.host/letoffer")]
    assert extract_vendor("KVM VPS from $4/mo", "Godlike.host here — VPS hosting.", links, "ThrowRaPest", "lowendtalk") == "Godlike.host"
    links = [Link(1, "Order", "https://deluxhost.net/en/cart?add=z-0")]
    assert extract_vendor("DeluxHost.net | VPS Restock", "our existing DeluxHost offers. DeluxHost is great.",
                          links, "x", "lowendtalk") == "DeluxHost"
    links = [Link(1, "Mango Mail!", "https://mymangomail.com")]
    assert extract_vendor("Mango Mail Halloween Sale: Up to 40%", "email contact@mymangomail.com", links, "raindog308",
                          "lowendbox") == "Mango Mail"
    assert extract_vendor("[PureVoltage] Pre-Black Friday", "Hello", [], "pv", "lowendtalk") == "PureVoltage"
    assert extract_vendor("[Early Black Friday Sale] VPS Deals", "Hi", [], "HostDZire", "lowendtalk") == "HostDZire"
    assert extract_vendor("Some news", "text", [], "raindog308", "lowendbox") is None


@pytest.mark.parametrize("title, text, pct, recurring", [
    ("BLACK FRIDAY | UK VPS | up to 60% OFF", "The discount covers the first term, then it renews at the normal price.",
     60, Recurring.FIRST_TERM),
    ("TNA – 50% OFF Recurring", "Discounts are recurring for the lifetime of the service.", 50, Recurring.RECURRING),
    ("Mango Mail: Up to 40% Perfect Off", "up to 40% percent OFF!", 40, Recurring.UNKNOWN),
    ("Deals", "The recurring price under each server is what you keep paying. 10% Off First Month.", 10, Recurring.FIRST_TERM),
    ("GreenCloud is Turning 13!", "13% cashback as account credit. we are committed to keeping our renewal prices unchanged.",
     None, Recurring.RECURRING),
    ("黑五 85折", "续费同价", 15, Recurring.RECURRING),
    ("Mixed", "30% off recurring on VPS; 50% off your first month on dedicated.", 50, Recurring.MIXED),
])
def test_discount(title, text, pct, recurring):
    (got_pct, _), got_rec = extract_discount(title, text)
    assert (got_pct, got_rec) == (pct, recurring)


@pytest.mark.parametrize("text, expected", [
    ("55% off yearly with code BFVPS55 or 60% off 2 years with code BFVPS60.", ["BFVPS55", "BFVPS60"]),
    ("Halloween Coupon: HALLOWEEN50", ["HALLOWEEN50"]),
    ("Promo Code: Earlybird1U", ["Earlybird1U"]),
    ("USE LETB25 - RECURRING 25% off", ["LETB25"]),
    ("Get 30% OFF for life with coupon : LET_NL10_2026", ["LET_NL10_2026"]),
    ("优惠码 BF2026 立减", ["BF2026"]),
    ("Built with DDR5 RAM and NVMe. We pick a winner using RANDOM.ORG. No coupon needed.", []),
    ("Order now with VirtFusion panel, IPv4 and OFF-site backups", []),
])
def test_coupon_codes(text, expected):
    assert extract_codes("", text) == expected


def test_purchase_url_prefers_landing_then_first_order_link():
    links = [Link(1, "Order UK EPYC E4", "https://www.vm6.co.uk/enterprise-vps-uk"),
             Link(2, "All Black Friday VPS deals", "https://www.vm6.co.uk/promotions"),
             Link(3, "Facebook", "https://www.facebook.com/x")]
    assert extract_purchase_url(links) == "https://www.vm6.co.uk/promotions"
    links = [Link(1, "Looking Glass", "https://lg.example.com"), Link(2, "Order Now", "https://example.com/cart.php?pid=1")]
    assert extract_purchase_url(links) == "https://example.com/cart.php?pid=1"
    assert extract_purchase_url([Link(1, "Mango Mail!", "https://mymangomail.com")]) == "https://mymangomail.com"


# ---------------------------------------------------------------- 真实帖


def test_real_vm6_post_end_to_end():
    root = ET.parse(snapshot("lowendtalk_offers_2026-11-02.xml")).getroot()
    item = next(i for i in root.iter("item") if i.findtext("guid") == "222765@/discussions")
    st = structure_html(item.findtext("description"))
    ex = extract("lowendtalk", item.findtext("title"), st.text, st.links, "VM6", "en", False)
    assert ex.vendor == "VM6" and ex.coupon_codes == ["BFVPS55", "BFVPS60"]
    assert (ex.discount_pct, ex.recurring) == (60, Recurring.FIRST_TERM)
    assert not hasattr(ex, "stock_status") and not any(hasattr(p, "stock_status") for p in ex.plans)  # D63
    assert ex.purchase_url == "https://www.vm6.co.uk/promotions"
    assert [(p.name, p.price, p.period) for p in ex.plans] == [
        ("UK EPYC E4", 27, Period.YEAR), ("UK EPYC E4", 48, Period.BIENNIAL), ("UK EPYC E8", 48.6, Period.YEAR),
        ("UK EPYC E8", 86.4, Period.BIENNIAL), ("UK EPYC E16", 163.2, Period.BIENNIAL), ("UK Xeon S", 1.75, Period.MONTH),
        ("UK Xeon M", 3.5, Period.MONTH), ("UK Xeon L", 7, Period.MONTH), ("UK AMD G2", 6, Period.MONTH)]
    assert ex.plans[-1].locations == ["Coventry"] and ex.plans[0].locations == ["Norwich"]
    assert ex.completeness == 1.0


# ---------------------------------------------------------------- 免费套餐（D55）


@pytest.mark.parametrize("text", ["Free NVMe Shared Hosting - USA", "Order Free Hosting [1]", "Free VPS Plan",
                                  "Dewlance® - Free Web Hosting That Never Wipes", "our free tier", "Zero Cost",
                                  "免费主机 1G", "免费的 VPS"])
def test_free_product_words(text):
    assert kw.FREE_PLAN.search(text)


@pytest.mark.parametrize("text", ["Free SSL Certificate", "Free LetsEncrypt SSL", "free migration to our VPS",
                                  "7-day free trial on all VPS plans", "Free setup on all servers",
                                  "free IPv6 for every server", "Get a free domain with hosting",
                                  "free upgrade to NVMe plan", "1 month free on yearly plans", "免费送 IPv6"])
def test_freebies_are_not_free_products(text):
    assert not kw.FREE_PLAN.search(text)  # 赠品与试用不是免费套餐（金标集规范 6.x）


def test_real_free_hosting_post_becomes_a_free_plan():
    """LES 免费主机帖：标题写明送免费主机，带规格的免费产品块记为 0 价套餐，币种与周期留空。"""
    root = ET.parse(snapshot("lowendspirit_free-offers-and-stuff_2026-11-02.xml")).getroot()
    item = next(i for i in root.iter("item") if "Free Web Hosting That Never Wipes" in i.findtext("title"))
    st = structure_html(item.findtext("description"))
    ex = extract("lowendspirit", item.findtext("title"), st.text, st.links, "Dewlance", "en", False)
    [p] = ex.plans
    assert (p.name, p.price, p.currency, p.period) == ("Free NVMe Shared Hosting - USA", 0.0, None, None)
    assert (p.disk_gb, p.disk_type, p.locations) == (1.0, DiskType.NVME, ["United States"])
    assert p.order_url == "https://www.dewlance.com/free-hosting"
    paid_title = extract("lowendspirit", "Dewlance shared hosting", st.text, st.links, "Dewlance", "en", False)
    assert paid_title.plans == []  # 标题没说送免费主机时不出 0 价套餐


# ---------------------------------------------------------------- 表头与多项配置单元格（D56）

ORDER_LINKS = [Link(1, "Order", "https://host.test/cart.php?pid=1"), Link(2, "Order", "https://host.test/cart.php?pid=2")]


def test_feature_line_above_the_table_is_not_its_header():
    """表头上方紧挨着一行用 · 分隔的特性，同样"像表头"：取更像字段名的那行，否则各列错位（10 GB 被当流量）。"""
    from app.extract.segment import segment

    text = ("KVM virtualization · PletX DDoS protection · BGP sessions available · Unlimited traffic (FUP)\n"
            "Plan | vCPU | RAM | SSD | Port | Price/year | Order\n"
            "X-0 | 1 | 512 MB | 10 GB | 1 Gbps | €6 | Order [1]\n"
            "X-3 | 2 | 4 GB | 60 GB | 10 Gbps | €14 | Order [2]")
    assert segment(text, ORDER_LINKS)[0].header == ["Plan", "vCPU", "RAM", "SSD", "Port", "Price/year", "Order"]
    assert [(p.name, p.cpu_cores, p.ram_gb, p.disk_gb, p.disk_type, p.port_gbps, p.bandwidth_tb, p.price, p.period)
            for p in plans_of(text, ORDER_LINKS)] == [
        ("X-0", 1, 0.5, 10, DiskType.SSD, 1, None, 6, Period.YEAR),
        ("X-3", 2, 4, 60, DiskType.SSD, 10, None, 14, Period.YEAR)]


def test_slash_separated_specs_in_one_cell():
    """"Specs" 一格里用 / 分隔多项：按项拆开再认，"40GB NVMe" 后面的 "3TB BW" 不再把它带成流量。"""
    text = ("Plan | Specs | Regular | Order\n"
            "Core VPS | 1 vCPU / 2GB RAM / 40GB NVMe / 3TB BW | €5.50/mo | Order [1]\n"
            "Win 4G | 2 vCPU / 4GB RAM / 60GB NVMe / RDP | €15.50/mo | Order [2]")
    assert [(p.name, p.cpu_cores, p.ram_gb, p.disk_gb, p.disk_type, p.bandwidth_tb) for p in plans_of(text, ORDER_LINKS)] \
        == [("Core VPS", 1, 2, 40, DiskType.NVME, 3), ("Win 4G", 2, 4, 60, DiskType.NVME, None)]


def test_bandwidth_skips_out_of_range_neighbours():
    cfg = rules.Config()
    rules._apply_bandwidth(cfg, "40GB NVMe 3TB BW")  # 没有分隔符时，40GB 的上下文里也有 BW
    assert cfg.bandwidth_tb == 3


# ---------------------------------------------------------------- 价格列的角色、周期与码列（D57）


def table_plans(header: str, *rows: str):
    text = "\n".join([header, *rows])
    links = [Link(n, "Order", f"https://host.test/cart.php?pid={n}") for n in range(1, len(rows) + 1)]
    ex = extract("lowendtalk", "Host Black Friday", text, links, None, "en", False)
    return ex, [(p.name, p.price, p.period, p.price_note, p.coupon_code) for p in ex.plans]


def test_regular_and_with_code_columns_keep_one_promo_plan():
    _, plans = table_plans("Plan | Specs | Regular | With SNOW50 | Order",
                           "Core VPS | 1 vCPU / 2GB RAM / 40GB NVMe | €5.50/mo | €2.75/mo | Order [1]",
                           "Plus VPS | 2 vCPU / 4GB RAM / 65GB NVMe | €9.50/mo | €4.75/mo | Order [2]",
                           "Use coupon SNOW50 on monthly plans. Other code: XMAS2026")
    assert plans == [("Core VPS", 2.75, Period.MONTH, "promo", "SNOW50"), ("Plus VPS", 4.75, Period.MONTH, "promo", "SNOW50")]


def test_sale_column_beats_regular_and_approx_monthly_and_coupon_column_is_a_code():
    ex, plans = table_plans("Plan | Specs | Regular | Sale Price | ~Monthly | Coupon | Order",
                            "Starter VPS | 1 vCPU / 1GB RAM | €29.50/yr | €12.39/yr | €1.03/mo | SNOWSTARTER1445 | Order [1]",
                            "Pro VPS | 4 vCPU / 8GB RAM | €195.00/yr | €39.00/yr | €3.25/mo | SNOWPRO4950 | Order [2]")
    assert plans == [("Starter VPS", 12.39, Period.YEAR, "promo", "SNOWSTARTER1445"),
                     ("Pro VPS", 39.0, Period.YEAR, "promo", "SNOWPRO4950")]
    assert ex.coupon_codes == ["SNOWSTARTER1445", "SNOWPRO4950"]  # 码列里的码也是帖子级的码


def test_period_comes_from_the_column():
    _, plans = table_plans("Storage | Snapshots | Monthly | Annual* | Order",
                           "500 GB | 2 | Annual only | €7.99 | Order [1]",
                           "1 TB | 5 | €1.99 | €23.88 | Order [2]",
                           "*Annual billing is prepaid.")
    assert [(price, period) for _, price, period, _, _ in plans] == [
        (7.99, Period.YEAR), (1.99, Period.MONTH), (23.88, Period.YEAR)]


def test_first_year_and_renewal_columns():
    _, plans = table_plans("Plan | RAM | NVMe | 1st Year | Renewal | Order",
                           "M-0 | 512 MB | 8 GB | €5 | €7 | Order [1]",
                           "Billed monthly for other plans.")
    assert plans == [("M-0", 5, Period.YEAR, "promo", None)]  # 首年价为活动价，续费价是原价


def test_price_and_renewal_header_is_not_a_regular_column():
    _, plans = table_plans("Plan | RAM | Monthly price & renewal", "HHS VPS 1 | 2 GB | £1.50 ≈ US$2.01")
    assert plans == [("HHS VPS 1", 1.5, Period.MONTH, None, None)]


def test_tier_columns_are_separate_plans():
    _, plans = table_plans("Plan | Standard | Premium", "KVM-1 | $5/mo | $8/mo")
    assert [(price, note) for _, price, _, note, _ in plans] == [(5, None), (8, None)]  # "Standard" 是档位名，不是原价列


def test_numbered_plan_names_and_link_arrows():
    _, plans = table_plans("Plan | vCPU | RAM | NVMe | Monthly | Semi-annual", "#1 | 2 | 1.5 GB | 25 GB | €9.90 | €53.50")
    assert plans == [("#1", 9.9, Period.MONTH, None, None), ("#1", 53.5, Period.SEMI, None, None)]
    [hit] = price_hits("2 GB RAM — Order for £1.50/month → [3]", "en")
    assert hit.kind == "plain"  # 箭头后面是链接序号，不是 "$20 → $10" 的新价
