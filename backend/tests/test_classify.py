"""分类规则：《金标集标注规范》第 5.1–5.5、7 节的边界案例。"""

import pytest

from app.extract.classify import classify
from app.models import EventType, IntelCategory, ProductType, RecordKind

PRICE = "Plan A | 2 vCPU | 4GB RAM | 40GB NVMe | €21/year | Order Now [1]"


def offer(title, text=PRICE, source="lowendtalk", categories=("Offers",)):
    return classify(source, title, text, categories)


# ---------------------------------------------------------------- 是否黑五与活动类型


@pytest.mark.parametrize("title", [
    "[Early Black Friday Sale] VPS & Dedicated Server Deals",
    "[PureVoltage] Pre-Black Friday ϟ NYC Dedicated from $60/mo",
    "BLACK FRIDAY | UK VPS | EPYC 7642 up to 60% OFF",
    "BlackFriday KVM from $9/yr",
    "BF 2026 | NVMe VPS",
    "黑五特惠 年付 10 刀",
])
def test_black_friday_in_title(title):
    c = offer(title)
    assert c.is_black_friday and c.event_type is EventType.BLACK_FRIDAY


def test_cyber_monday_only():
    c = offer("Cyber Monday Flash Sale - 50% OFF")
    assert c.is_black_friday and c.event_type is EventType.CYBER_MONDAY


def test_body_only_black_friday_offer_counts():
    c = offer("Raff Technologies: KVM VPS from $4.99/mo",
              "Hello LET.\nOur Black Friday offer and current discount: 30% off your first month.\n" + PRICE)
    assert c.is_black_friday and c.event_type is EventType.BLACK_FRIDAY


@pytest.mark.parametrize("text", [
    "Important Notice: No Black Friday Sale This Year\nGreenCloud will NOT be holding a Black Friday sale this year.",
    "Maybe there'll be a surprise on Black Friday? Let's wait and see!",
    "这次比黑五还便宜！",
])
def test_negated_or_speculative_body_mentions_do_not_count(text):
    assert not offer("Hostname Summer Deals", f"{text}\n{PRICE}").is_black_friday


def test_dont_miss_is_not_a_negation():
    assert offer("KVM deals", f"Don't miss our Black Friday sale!\n{PRICE}").is_black_friday


def test_halloween_post_with_black_friday_teaser_is_holiday():
    c = offer("Ava.hosting - Boooh! Halloween Discount 15% OFF!!",
              f"Happy Halloween!\n{PRICE}\nBlack Friday is coming next month.")
    assert not c.is_black_friday and c.event_type is EventType.HOLIDAY


def test_preview_without_price_is_bf_other():
    c = offer("Our Black Friday sale starts November 26 - stay tuned", "Bookmark this thread.")
    assert c.is_black_friday and c.event_type is EventType.OTHER


def test_bf_giveaway_without_price_is_black_friday():
    c = offer("DartNode - Countdown to Black Friday Dedicated Server Giveaway", "Comment below to enter the giveaway.")
    assert c.is_black_friday and c.event_type is EventType.BLACK_FRIDAY


@pytest.mark.parametrize("title, lead, expected", [
    ("HostSailor Halloween Special - 50% OFF Lifetime", "", EventType.HOLIDAY),
    ("Halloween Anniversary Sale", "", EventType.HOLIDAY),  # 节日优先于周年
    ("GreenCloud is Turning 13! | Exclusive Deals", "", EventType.ANNIVERSARY),
    ("BERO HOST 10th Anniversary Deals", "", EventType.ANNIVERSARY),
    ("DeluxHost.net | VPS Restock | NL", "", EventType.RESTOCK),
    ("Early Bird Discounts on Cheap KVM!", "Phoenix-area launch offers.", EventType.LAUNCH),
    ("Frankfurt KVM from €3.25", "", EventType.SALE),
])
def test_event_type_priority(title, lead, expected):
    assert offer(title, f"{lead}\n{PRICE}").event_type is expected


def test_giveaway_vs_sale():
    assert offer("Giveaway: 3x free VPS", "Comment to win.").event_type is EventType.GIVEAWAY
    assert offer("Giveaway + KVM deals", f"Comment to win.\n{PRICE}").event_type is EventType.SALE
    assert offer("Free web hosting for testers", "No price, just feedback.").event_type is EventType.OTHER


# ---------------------------------------------------------------- 记录类型


def test_lowendbox_uses_article_categories():
    news = classify("lowendbox", "MassiveGrid: When the Lifetime Price Guarantee Turns Out to Not Be Quite So “Lifetime”",
                    "MassiveGrid said in their offer: Your discounted rate is locked in forever. $5/mo.",
                    ["Editorial & News", "massivegrid"])
    assert news.record_kind is RecordKind.INTEL and news.intel_category is IntelCategory.NEWS
    deal = classify("lowendbox", "Cheap VPS Deals in Toronto", PRICE, ["Low End Virtual", "toronto"])
    assert deal.record_kind is RecordKind.OFFER
    unknown = classify("lowendbox", "KVM sale", "Use coupon SAVE20 for 20% off. $3/mo", ["Something New"])
    assert unknown.record_kind is RecordKind.OFFER


@pytest.mark.parametrize("categories, title, text, kind, category", [
    (["photo-share"], "gif-1p", "", RecordKind.IGNORE, None),
    (["trade"], "230原油出 HostBilby黑五7刀翻倍机 2h4g60g", "", RecordKind.INTEL, IntelCategory.RESALE),
    (["trade"], "[六折出]搬瓦工正价机美西ECOMMERCE", "", RecordKind.INTEL, IntelCategory.RESALE),
    (["trade"], "明盘出土区 chatGPT plus。500 元。", "", RecordKind.IGNORE, None),
    (["carpool"], "[人找车] 求上车 DMIT EB，月用 300G 双向", "", RecordKind.INTEL, IntelCategory.CARPOOL),
    (["review"], "[NQ] PPflight MINIMO", "", RecordKind.INTEL, IntelCategory.REVIEW),
    (["daily"], "求推荐一个做生图和视频中转站的服务器！", "", RecordKind.INTEL, IntelCategory.QUESTION),
    (["daily"], "雅思自学经验分享帖-写作", "一分钱不花", RecordKind.IGNORE, None),
    (["promotion"], "XX云 黑五特惠 年付 99 元", "优惠码 BF2026", RecordKind.OFFER, None),
])
def test_nodeseek(categories, title, text, kind, category):
    c = classify("nodeseek", title, text, categories)
    assert c.record_kind is kind and c.intel_category == category


def test_intel_black_friday_uses_title_only():
    c = classify("nodeseek", "230原油出 HostBilby黑五7刀翻倍机 2h4g60g", "", ["trade"])
    assert c.is_black_friday and c.event_type is EventType.OTHER  # 金标集规范 10.2
    c = classify("nodeseek", "出一台搬瓦工", "黑五买的，现在出", ["trade"])
    assert not c.is_black_friday


@pytest.mark.parametrize("title, text, kind, category", [
    ("VPS hosting for minecraft server?", "", RecordKind.INTEL, IntelCategory.QUESTION),
    ("Best VPS Provider", "", RecordKind.INTEL, IntelCategory.QUESTION),
    ("Leaseweb VPS 2 KYC Verification – No Update After 9 Days", "", RecordKind.INTEL, IntelCategory.DISCUSSION),
    ("My cat learned to open doors", "Cute.", RecordKind.IGNORE, None),
])
def test_reddit(title, text, kind, category):
    c = classify("reddit", title, text, ["VPS"])
    assert c.record_kind is kind and c.intel_category == category


@pytest.mark.parametrize("source, cats, title, kind, category", [
    ("lowendtalk", ["Offers"], "Any title", RecordKind.OFFER, None),
    ("lowendtalk", ["Shared Hosting Offers"], "cPanel hosting $10/yr", RecordKind.OFFER, None),
    ("lowendtalk", ["Giveaways & Freebies"], "Free VPS giveaway", RecordKind.OFFER, None),
    ("lowendtalk", [], "Old post without category", RecordKind.OFFER, None),
    ("lowendtalk", ["Requests"], "Looking for a cheap VPS in Asia", RecordKind.INTEL, IntelCategory.QUESTION),
    ("lowendtalk", ["General"], "Which VPS did you buy this Black Friday", RecordKind.INTEL, IntelCategory.QUESTION),
    ("lowendtalk", ["General"], "Hetzner raises VPS prices", RecordKind.INTEL, IntelCategory.DISCUSSION),
    ("lowendtalk", ["News"], "Provider X acquired, VPS customers migrated", RecordKind.INTEL, IntelCategory.NEWS),
    ("lowendtalk", ["General"], "Happy birthday to me", RecordKind.IGNORE, None),
    ("lowendspirit", ["Dedicated servers"], "E3 box", RecordKind.OFFER, None),
    ("lowendspirit", ["Technical"], "Debian 13 on my VPS fails to boot", RecordKind.INTEL, IntelCategory.DISCUSSION),
])
def test_forum_discussions_feed_categories(source, cats, title, kind, category):
    """LET / LES 全站讨论 feed（D82）：Offers 各分类是优惠，其他分类只有情报与无关。"""
    c = classify(source, title, "VPS: 1 GB RAM, $10/yr" if kind is RecordKind.OFFER else "", cats)
    assert c.record_kind is kind and c.intel_category == category


@pytest.mark.parametrize("cats, author, title, kind", [
    (["Hostinger"], "HostingerCOM", "Waiting for Hostinger Black Friday deals? Up to 82% off + extra 10% with BFREDDIT",
     RecordKind.OFFER),
    (["CloudwaysbyDO"], "PoojafromCloudways", "Our Black Friday offer is live: 40% off for 4 months", RecordKind.OFFER),
    (["hostney"], "hostney", "Black Friday: 30% off every plan for up to 3 years", RecordKind.OFFER),
    (["Hostinger"], "LukeToronto", "Black Friday hosting 80% off renewal?", RecordKind.INTEL),  # 用户帖
    (["Hostinger"], "HostingerCOM", "From 9 sales to 800+, a website story", RecordKind.INTEL),  # 官方但不是促销
    (["Hostinger"], "HostingerCOM", "Hostinger weekly update #51", RecordKind.INTEL),
    (["Hostinger"], "HostingerCOM", "Do you still plan your holiday sales around Black Friday?", RecordKind.INTEL),
    (["CloudwaysbyDO"], "PoojafromCloudways", "PHP2Shell Breakdown: critical PHP vulnerabilities", RecordKind.INTEL),
    (["VPS"], "vultrfan", "Vultr VPS 50% off this week", RecordKind.INTEL),  # 不在官方版块
])
def test_reddit_official_vendor_posts_are_offers(cats, author, title, kind):
    """D83：品牌官方账号在自家版块发的促销帖算优惠，其余 Reddit 帖照旧只有情报与无关。"""
    text = "web hosting plans, 50% off with code SAVE50"  # 正文都带折扣与码：只看标题能不能区分
    assert classify("reddit", title, text, cats, author).record_kind is kind


TG_TEXT = "CPU: 1 vCPU\nMemory: 1 GB\nDisk: 15 GB NVMe\nCoupon: C0P3A4Q6OKWG\nPrice: $1.5 USD/monthly"


def test_telegram_relay_is_an_offer_and_channel_tag_marks_black_friday():
    """频道打的 #黑五 标签算黑五：模板正文往往不写 Black Friday（D81）。"""
    c = classify("telegram", "Ryzen-KVM-1GB (India)", TG_TEXT, ["let", "黑五2025", "黑五"])
    assert c.record_kind is RecordKind.OFFER and c.is_black_friday and c.event_type is EventType.BLACK_FRIDAY
    assert c.evidence["black_friday"] == "频道标签"
    cm = classify("telegram", "Cyber Monday VPS", TG_TEXT, ["let", "黑五"])
    assert cm.is_black_friday and cm.event_type is EventType.CYBER_MONDAY  # 正文只写网一的仍是网一
    plain = classify("telegram", "Ryzen-KVM-1GB (India)", TG_TEXT, ["let"])
    assert plain.record_kind is RecordKind.OFFER and not plain.is_black_friday
    assert not classify("reddit", "Ryzen VPS", TG_TEXT, ["黑五"]).is_black_friday  # 只认 Telegram 的标签


# ---------------------------------------------------------------- 产品类型与语言


@pytest.mark.parametrize("title, text, expected", [
    ("VPS & Dedicated Server Deals", PRICE, ["vps", "dedicated"]),
    ("DeluxHost.net | VPS Restock | Improved Storage Servers!", PRICE, ["vps"]),  # 存储型 VPS 归 vps
    ("SystemFreaks S3 Object Storage | 100 GB from $7.20/year", "", ["storage"]),
    ("Cheap VPS Deals in Toronto", "We do game hosting, KVM VPS hosting, dedicated servers.\n" + PRICE, ["vps"]),
    ("SmokyHosts | 500GB SSD cPanel hosting", "", ["hosting"]),
    ("Exclusive Deals, Giveaways, Quizzes", "Price: $123 triennially", ["vps"]),  # 无命中时优惠默认 vps
])
def test_product_types(title, text, expected):
    assert [str(p) for p in offer(title, text).product_types] == expected


@pytest.mark.parametrize("title, text", [
    ("OnePoundEmail 31% OFF on email hosting", ""),
    ("$29/Month colocation in Phoenix", ""),
    ("Namecheap domain registration from $5.98/year", "Domain transfer is $9.98."),
    ("Official ISPmanager Licenses starting at $7.04/mo", "license license"),
])
def test_offers_for_other_products_are_not_collected(title, text):
    """D62：只收 VPS、独服、虚拟主机、存储；帖子卖的只是邮箱、域名、托管机柜、授权等时整条不收。"""
    c = offer(title, text)
    assert c.record_kind is RecordKind.IGNORE and c.product_types == []
    assert not c.is_black_friday and c.event_type is EventType.OTHER  # 与其他 ignore 记录一样只看标题的黑五词


def test_other_products_next_to_a_wanted_one_are_dropped_from_the_list():
    c = offer("Black Friday: KVM VPS and email hosting", PRICE)
    assert c.record_kind is RecordKind.OFFER and [str(p) for p in c.product_types] == ["vps"]


def test_weak_evidence_for_other_products_does_not_drop_a_post():
    """只提到一两次邮箱的帖子（云存储订阅、公告），或同时写了虚拟主机与邮箱的，不能被当成"只卖其他产品"。"""
    c = offer("Cloud Storage lifetime subscription", "Folderfort based on Backblaze. Contact by email.")
    assert c.record_kind is RecordKind.OFFER
    c = offer("DragonWebHost - 50% off codes", "Across our web hosting and email hosting plans. email email")
    assert c.record_kind is RecordKind.OFFER and [str(p) for p in c.product_types] == ["hosting"]


def test_intel_without_product_words_has_no_product_type():
    c = classify("lowendbox", "Is This the AI Nuclear Winter?", "AI hype cycles.", ["Editorial & News"])
    assert c.product_types == []


@pytest.mark.parametrize("title, text, lang", [
    ("BLACK FRIDAY | UK VPS", "Hi all! We are VM6.", "en"),
    ("折价总价16出 vpsnat 纳泰云NetLab-LA三网优质用户套餐19.9年付", "注意，ip没被墙，但tcp现在用不了", "zh"),
    ("【NQ】 AZURE NZ 三网测评", "IPv4回程 IPv6回程 教育网回程 国际互联 单线程测速报告", "zh"),
])
def test_lang(title, text, lang):
    assert classify("nodeseek", title, text, ["daily"]).lang == lang
    assert ProductType.VPS  # 枚举可导入


@pytest.mark.parametrize("title", [
    "HARDWARE SALE Supermicro AS-4124GQ-TNMI, 4x AMD Instinct MI250 128GB OAM GPU Server (73x available)",
    "Hardware clearance: used Dell R640 dedicated servers",
    "出售硬件：二手 E5 独立服务器整机",
])
def test_hardware_sales_are_not_collected(title):
    """卖断硬件（整机、GPU）不是租用的主机产品，即使标题里有 server、独立服务器也不收（优化方案 O-04）。"""
    c = classify("lowendtalk", title, "Price: $25,000 one-time, shipped from our warehouse.")
    assert c.record_kind is RecordKind.IGNORE and c.product_types == [] and "硬件" in c.evidence["scope"]


def test_dedicated_server_sales_are_still_offers():
    c = classify("lowendtalk", "Black Friday dedicated server sale: Ryzen 9 from $59/mo", "Dedicated server $59/mo")
    assert c.record_kind is RecordKind.OFFER and ProductType.DEDICATED in c.product_types


@pytest.mark.parametrize("source, cats, title, kind", [  # D88：综合推广 / 羊毛版块从严
    ("v2ex", ["promotions"], "[中转站] 小众中转站推广，进来领鸡蛋 10$", "ignore"),
    ("v2ex", ["promotions"], "腾讯云服务器拼团", "intel"),
    ("v2ex", ["promotions"], "黑五 绿云 年付特价", "ignore"),  # 正文没有产品词
    ("nodeloc", ["羊毛党"], "白嫖4h8g服务器【简单上手】", "intel"),
    ("nodeloc", ["优惠情报"], "【羊毛】购买香港流量esim，可用积分免费兑换机票", "ignore"),
    ("nodeloc", ["优惠情报"], "绿云黑五活动开始", "intel"),  # 标题黑五词 + 正文产品词
    ("nodeloc", ["VPS"], "Skywolf 的线路有惊喜啊", "intel"),  # VPS 分类照通用规则
    ("v2ex", ["vps"], "咸鱼云", "ignore"),
])
def test_small_forum_general_boards(source, cats, title, kind):
    text = "三网 CN2 年付 $30 的小鸡" if "黑五" in title and source == "nodeloc" else ""
    assert classify(source, title, text, cats).record_kind == kind


def test_hostingdiscussion_forum_categories():
    c = classify("hostingdiscussion", "Hosting Industry roundup: new datacenter", "VPS", ["Industry News"])
    assert c.record_kind == "intel" and c.intel_category == "news"
