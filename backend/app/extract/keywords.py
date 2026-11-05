"""关键词表（《需求与方案说明》附录 B），规则层集中在此维护。

英文词按词边界匹配、不区分大小写；中文词直接子串匹配。相对附录 B 的补充写在对应条目的注释里。
"""

import re


# 只以 ASCII 字母数字为词边界：Python 的 \w 含汉字，"出vps" 这类中英粘连会被挡住；连字符也不算边界（"Pre-Black Friday"）
_B, _A = r"(?<![A-Za-z0-9_])", r"(?![A-Za-z0-9_])"


def _words(*words: str) -> str:
    """英文词加 ASCII 词边界（允许复数 s），中文与带符号的词原样。"""
    parts = []
    for w in words:
        if re.fullmatch(r"[\w\s\-./+]+", w) and re.search(r"[a-z]", w, re.I):
            parts.append(rf"{_B}{w.replace(' ', r'[\s-]?')}s?{_A}")
        else:
            parts.append(re.escape(w) if not w.startswith("(?") else w)
    return "|".join(parts)


def rx(*words: str) -> re.Pattern:
    return re.compile(_words(*words), re.I)


# ---- 活动
BLACK_FRIDAY = rx("black friday", "blackfriday", "BFCM", "bf2026", "黑五", "黑色星期五", "黑5")
BF_SHORT = re.compile(rf"{_B}BF{_A}")  # 大写缩写，区分大小写，避免误伤 bf 开头的码
CYBER_MONDAY = rx("cyber monday", "cybermonday", "网一", "网络星期一")
HOLIDAY = rx("halloween", "thanksgiving", "christmas", "xmas", "new year", "万圣", "感恩节", "圣诞", "元旦", "新年")
ANNIVERSARY = rx("anniversary", "birthday", "周年", "生日", r"(?:turning|turns) \d+")  # 补充 birthday：GreenCloud 13 周岁
RESTOCK = rx("restock", "restocked", "back in stock", "补货")
LAUNCH = rx("launch", "launched", "launching", "new location", "now available in", "now live in", "grand opening",
            "上线", "新机房")
GIVEAWAY = rx("giveaway", "抽奖", "赠送")

# ---- 否定与猜测：这类句子里的黑五词不算本次活动（金标集规范 5.2）
NEGATION = re.compile(r"(?<![\w'’])(?:no|not|never|without|n['’]t)(?![\w'’])|不|没有|无|再见", re.I)
NOT_NEGATION = re.compile(r"don['’]t miss|do not miss|not only|no coupon needed|no code needed", re.I)
SPECULATIVE = rx("maybe", "perhaps", "might", "wait and see", "stay tuned", "coming soon", "upcoming", "surprise",
                 "也许", "敬请期待")
COMPARATIVE_ZH = re.compile(r"比黑五|黑五还")  # "比黑五还便宜"
OFFER_WORDS = rx("offer", "deal", "sale", "discount", "% off", "coupon", "promo", "优惠", "促销", "折扣")

# ---- 价格与折扣（判断"有可购买套餐或折扣"）
PRICE = re.compile(
    r"[$€£¥]\s?\d|\d(?:[.,]\d+)?\s?(?:USD|EUR|GBP|CNY|HKD|CAD|AUD|SGD|JPY|刀|美刀|元|欧|镑)(?![a-z])"
    r"|\d\s?/\s?(?:mo|month|yr|year|annually|quarter)\b|\d+(?:\.\d+)?\s*[€£$](?![\w])|年付|月付|季付",  # 含 "5$/Month"
    re.I,
)
# 价格前后的限定语：这类价格不是某个套餐的价格（"from $5/mo"、"over £800"、"$0.96/GB"），金标集规范 6.1
PRICE_QUALIFIER_BEFORE = re.compile(r"(?:from|starting (?:at|from)|starts at|as low as|over|under|below|up to|"
                                    r"more than|less than|worth|save|saving|down to|低至|起)\s*(?:a|an|only|just)?\s*$", re.I)
PRICE_PER_UNIT_AFTER = re.compile(r"^\s*(?:USD|EUR|GBP)?\s*(?:/|per\s+)(?:GB|TB|IP|hour|hr)\b", re.I)
# 整套免费的产品（"Free NVMe Shared Hosting"、"Free VPS Plan"、"免费主机"），记为 0 价套餐（D55）；
# 赠品（free SSL / IP / migration / setup / domain / backup）、试用（free trial）不算
FREE_PLAN = re.compile(
    r"\bfree\b(?:\s+(?!(?:to|for|with|on|in|of|and|or|your|our|a|an|the|trial|migration|upgrade|setup|domains?|"
    r"ssl|backups?|ips?|ipv4|ipv6|bandwidth|traffic|ddos|support|month|months)\b)[\w®-]+){0,3}"
    r"\s+(?:hosting|vps|vds|servers?|plans?|tiers?|accounts?)\b"
    r"|\bzero[\s-]cost\b|免费(?:的)?[\w\s]{0,6}?(?:主机|空间|VPS|服务器|套餐|小鸡)",
    re.I,
)

DISCOUNT = re.compile(r"\d+\s?%\s?(?:off|discount|recurring|cashback)|coupon|promo ?code|\bcode\b\s*[:：]?\s*[A-Z0-9]{4,}"
                      r"|优惠码|折扣码|\d+(?:\.\d)?\s?折", re.I)

# ---- 混合源：促销词与产品词（附录 B）
PROMO = rx("offer", "deal", "sale", "special", "discount", "% off", "coupon", "promo", "code", "cheap",
           "优惠", "促销", "折扣", "优惠码", "特价", "打折", "年付", "便宜", "白嫖", "翻倍", "限量", "秒杀")
PRODUCT = rx("vps", "vds", "kvm", "lxc", "server", "dedicated", "dedi", "bare metal", "hosting", "cloud", "nvme",
             "storage", "nat", "独服", "服务器", "小鸡", "鸡", "主机", "机器", "存储",
             "云服务器", "云主机", "家里云",  # 附录 B 的单字"云"误伤"云南"等，收窄
             # 补充：中文圈对线路、配置与交易的说法，NodeSeek 情报大多只写这些（11-02 快照）
             "cn2", "gia", "cmin2", "9929", "4837", "三网", "回程", "线路", "机房", "原生ip", "ipv4", "ipv6", "bgp",
             "流量", "带宽", "搬瓦工", "bwg", "dmit", "留档",
             r"(?:正价|线路|性能|翻倍|绝版|大盘|传家宝|年付|月付|特价|黑五)机",
             rf"{_B}\d+\s?[hcHC核]\s?\d+\s?[gG]")  # 2h4g、2c4g60g、2核4G
QUESTION = re.compile(r"[?？]\s*$|^(?:求|问|请教|有没有|哪家|推荐)|^best\b|(?<![A-Za-z])(?:looking for|recommend|which|how|"
                      r"help|advice|any(?:one)? (?:know|suggest))(?![A-Za-z])|求推荐|求助", re.I)

# ---- 产品类型（金标集规范 5.4）。storage 只指对象 / 备份存储，大盘 VPS 归 vps
PRODUCT_TYPES = {
    "vps": rx("vps", "vds", "kvm", "lxc", "nat vps", "cloud server", "cloud instance", "virtual server",
              "virtual machine", "云服务器", "小鸡"),
    "dedicated": rx("dedicated", "dedi", "bare metal", "bare-metal", "独服", "独立服务器", "物理机"),
    "hosting": rx("shared hosting", "web hosting", "webhosting", "cpanel", "directadmin", "reseller hosting",
                  "wordpress hosting", "plesk", "虚拟主机"),
    "storage": rx("object storage", "s3", "s3-compatible", "backup storage", "对象存储"),
}
# 不收的产品（D62）：不会出现在数据里，只用来认出"这帖卖的不是上面四类"——参与打分，帖子卖的只是这些时整条丢弃
OUT_OF_SCOPE_PRODUCTS = {
    "mail": rx("email hosting", "mail hosting", "email", "mailbox", "邮箱"),
    "domain": rx("domain registration", "domain transfer", "域名注册"),
    "colo": rx("colocation", "colo", "托管机柜"),
    "other": rx("ip transit", "ipv4 lease", "ipv4 rental", "ipv6 rental", "ip lease", "license", "licence", "cdn"),
}
# 出售硬件（整机、GPU、配件，一次性卖断）不是租用的主机产品：标题命中就整条不收（优化方案 O-04）
HARDWARE_SALE = rx("hardware sale", "hardware clearance", "hardware liquidation", "selling hardware",
                   "selling our hardware", "surplus hardware", "used hardware", "refurbished servers",
                   "servers for sale", "出售硬件", "硬件出售")
# NAT 套餐（共用一个地址的端口）：套餐名或备注里单独出现的 NAT（alternative、international 不算）
NAT = rx("nat")

# ---- 线路与网络标签（优化方案 O-16）：标签 → 原文写法，大小写、全半角、空格与连字符的差别都兼容。
# IPv6 与 DDoS 防护几乎每帖都写，贴上去没有区分度，不当标签；"BGP" 单独出现多半是 BGP 会话服务，只认 "BGP 多线"。
NET_TAGS = {
    "cn2gia": re.compile(r"CN2[\s_-]*GIA|(?-i:(?<![A-Za-z0-9])GIA(?![A-Za-z0-9]))", re.I),
    "cn2gt": re.compile(r"CN2[\s_-]*GT(?![A-Za-z])", re.I),
    "9929": re.compile(r"(?<![A-Za-z0-9])AS[\s-]?9929(?!\d)|(?<![\d.])9929(?!\d)|(?<![A-Za-z])CU[\s-]?VIP(?![A-Za-z])"
                       r"|联通\s*(?:精品|VIP)|A\s?网(?![络站])", re.I),
    "4837": re.compile(r"(?<![A-Za-z0-9])AS[\s-]?4837(?!\d)|(?<![\d.])4837(?!\d)|联通\s*169", re.I),
    "cmin2": re.compile(r"(?<![A-Za-z])CMIN2(?![A-Za-z])|移动\s*精品\s*网", re.I),
    "cmi": re.compile(r"(?<![A-Za-z0-9])CMI(?![A-Za-z0-9])", re.I),
    "bgp": re.compile(r"BGP\s*多线|多线\s*BGP|BGP[\s-]*multi-?line|多线(?!程)", re.I),
    "native_ip": re.compile(r"原生\s*IP(?!v6)|(?<![A-Za-z])native\s+(?:IPs?|IPv4)(?![A-Za-z0-9])", re.I),
    "3net": re.compile(r"三网|电信[、/\s]*联通[、/\s]*移动|CT/CU/CM", re.I),
}
NET_NEGATION_BEFORE = re.compile(r"(?<![A-Za-z])(?:no|not|non|without)(?![A-Za-z])|不|无|非|没", re.I)
NET_NEGATION_AFTER = re.compile(r"^[\s:,(（]{0,3}(?:不支持|不含|不提供|没有|无(?![线限])|not\s+(?:supported|available|included)|n/a)", re.I)

# ---- 语言
CJK = re.compile(r"[一-鿿]")
LATIN = re.compile(r"[A-Za-z]")

# ---- 机房（附录 B，扩充）：原文写法 → 标准名。按出现位置取，城市优先于国家
LOCATIONS = {
    "Los Angeles": ["los angeles", "lax", "洛杉矶"], "San Jose": ["san jose", "圣何塞"], "Seattle": ["seattle", "西雅图"],
    "Dallas": ["dallas", "达拉斯"], "Houston": ["houston"], "Phoenix": ["phoenix"], "Tempe": ["tempe"],
    "Chicago": ["chicago", "芝加哥"], "New York": ["new york", "nyc", "纽约"], "New Jersey": ["new jersey"],
    "Miami": ["miami"], "Atlanta": ["atlanta"], "Ashburn": ["ashburn"], "Kansas City": ["kansas city"],
    "Buffalo": ["buffalo"], "Salt Lake City": ["salt lake city"], "Las Vegas": ["las vegas"], "Montreal": ["montreal"],
    "Toronto": ["toronto"], "Vancouver": ["vancouver"], "London": ["london", "伦敦"], "Manchester": ["manchester"],
    "Coventry": ["coventry"], "Norwich": ["norwich"], "Maidenhead": ["maidenhead"], "Frankfurt": ["frankfurt", "法兰克福"],
    "Falkenstein": ["falkenstein"], "Nuremberg": ["nuremberg"], "Dusseldorf": ["dusseldorf", "düsseldorf"],
    "Amsterdam": ["amsterdam", "阿姆斯特丹"], "Eygelshoven": ["eygelshoven"], "Paris": ["paris", "巴黎"],
    "Roubaix": ["roubaix"], "Warsaw": ["warsaw"], "Vienna": ["vienna"], "Helsinki": ["helsinki"],
    "Stockholm": ["stockholm"], "Zurich": ["zurich"], "Milan": ["milan"], "Madrid": ["madrid"], "Bucharest": ["bucharest"],
    "Istanbul": ["istanbul"], "Singapore": ["singapore", "新加坡"], "Hong Kong": ["hong kong", "hongkong", "香港"],
    "Tokyo": ["tokyo", "东京"], "Osaka": ["osaka", "大阪"], "Seoul": ["seoul", "首尔"], "Taipei": ["taipei", "台北"],
    "Sydney": ["sydney", "悉尼"], "Melbourne": ["melbourne"], "Mumbai": ["mumbai"], "Johannesburg": ["johannesburg"],
    "Dubai": ["dubai"], "Sao Paulo": ["sao paulo", "são paulo"],
    # 国家（原文只写国家时填国家名，金标集规范 6.10）
    "United States": ["united states", "usa"], "Canada": ["canada"], "United Kingdom": ["united kingdom"],
    "Germany": ["germany"], "Netherlands": ["netherlands"], "France": ["france"], "Japan": ["japan"], "India": ["india"],
    "Australia": ["australia"], "South Africa": ["south africa"], "Sweden": ["sweden"], "Poland": ["poland"],
    "Turkey": ["turkey"], "Romania": ["romania"],
}
COUNTRIES = {"United States", "Canada", "United Kingdom", "Germany", "Netherlands", "France", "Japan", "India",
             "Australia", "South Africa", "Sweden", "Poland", "Turkey", "Romania"}
LOCATION_RX = re.compile("|".join(
    rf"(?P<L{i}>{_B}{re.escape(alias)}{_A})" if alias.isascii() else rf"(?P<L{i}>{re.escape(alias)})"
    for i, alias in enumerate(a for aliases in LOCATIONS.values() for a in aliases)), re.I)
LOCATION_NAMES = [name for name, aliases in LOCATIONS.items() for _ in aliases]
