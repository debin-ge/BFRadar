"""机房规范化（《优化方案 v1.0》O-12，D67）。

原帖与 LLM 给出的机房写法五花八门："Frankfurt, Germany"、"NYC"、"New York City Metro"、"Utah, USA"、"US West (Silicon
Valley)"。写库前统一成规范名（城市优先，认不出城市时退到国家），页面按规范名显示中文名（字典 loc.*）、按地区筛选。

规范名沿用金标集的写法（英文城市名或国家名，金标集规范 6.10）；keywords.LOCATIONS 里的城市与国家都在这里。
认不出的写法原样保留，状态页统计"未识别"的个数，照着补这张表。
"""

import re
from functools import lru_cache

from .extract import keywords as kw

REGIONS = ("asia", "na", "eu", "oc", "sa", "af")  # 亚洲（含中东）、北美、欧洲、大洋洲、南美、非洲

# 规范名 → 地区。城市与国家都列；州、省不单列，按别名并到国家。
REGION = {
    # 北美
    "Los Angeles": "na", "San Jose": "na", "Seattle": "na", "Dallas": "na", "Houston": "na", "Phoenix": "na",
    "Tempe": "na", "Chicago": "na", "New York": "na", "New Jersey": "na", "Miami": "na", "Atlanta": "na",
    "Ashburn": "na", "Kansas City": "na", "Buffalo": "na", "Salt Lake City": "na", "Las Vegas": "na",
    "Montreal": "na", "Toronto": "na", "Vancouver": "na", "Beauharnois": "na", "Ogden": "na", "Tampa": "na",
    "Detroit": "na", "Charlotte": "na", "Durham": "na", "Binghamton": "na", "Bend": "na", "Vint Hill": "na",
    "Querétaro": "na", "United States": "na", "Canada": "na", "Mexico": "na",
    # 欧洲
    "London": "eu", "Manchester": "eu", "Coventry": "eu", "Norwich": "eu", "Maidenhead": "eu", "Frankfurt": "eu",
    "Falkenstein": "eu", "Nuremberg": "eu", "Dusseldorf": "eu", "Amsterdam": "eu", "Eygelshoven": "eu",
    "The Hague": "eu", "Paris": "eu", "Roubaix": "eu", "Gravelines": "eu", "Warsaw": "eu", "Vienna": "eu",
    "Helsinki": "eu", "Stockholm": "eu", "Zurich": "eu", "Milan": "eu", "Madrid": "eu", "Lisbon": "eu",
    "Bucharest": "eu", "Constanța": "eu", "Sofia": "eu", "Istanbul": "eu", "United Kingdom": "eu",
    "Germany": "eu", "Netherlands": "eu", "France": "eu", "Sweden": "eu", "Poland": "eu", "Turkey": "eu",
    "Romania": "eu", "Russia": "eu", "Finland": "eu", "Iceland": "eu", "Italy": "eu", "Switzerland": "eu",
    "Spain": "eu", "Denmark": "eu", "Norway": "eu", "Portugal": "eu", "Bulgaria": "eu", "Europe": "eu",
    # 亚洲（含中东）
    "Singapore": "asia", "Hong Kong": "asia", "Tokyo": "asia", "Osaka": "asia", "Seoul": "asia", "Taipei": "asia",
    "Mumbai": "asia", "Dubai": "asia", "Hanoi": "asia", "Lahore": "asia", "Japan": "asia", "India": "asia",
    "Malaysia": "asia", "Vietnam": "asia", "Pakistan": "asia", "Asia Pacific": "asia",
    # 大洋洲、南美、非洲
    "Sydney": "oc", "Melbourne": "oc", "Australia": "oc",
    "Sao Paulo": "sa",
    "Johannesburg": "af", "South Africa": "af",
}

# 其余写法（小写）→ 规范名；keywords.LOCATIONS 的别名自动并入
ALIASES = {
    "nyc": "New York", "new york city": "New York", "new york city metro": "New York", "new york, ny": "New York",
    "silicon valley": "San Jose", "us west": "San Jose", "us east": "Ashburn", "ashburn, va": "Ashburn",
    "frankfurt am main": "Frankfurt", "queretaro": "Querétaro", "constanta": "Constanța", "hague": "The Hague",
    "us": "United States", "u.s.": "United States", "united states of america": "United States", "america": "United States",
    "uk": "United Kingdom", "england": "United Kingdom", "great britain": "United Kingdom", "gb": "United Kingdom",
    "midlands": "United Kingdom", "west midlands": "United Kingdom",
    "nl": "Netherlands", "de": "Germany", "fr": "France", "sg": "Singapore", "hk": "Hong Kong", "jp": "Japan",
    "eu": "Europe", "europe": "Europe", "apac": "Asia Pacific", "asia": "Asia Pacific",
    "vestfold": "Norway", "mazovia": "Poland",
}
# 美国的州（全称与缩写）：只写州时并到"美国"
US_STATES = {
    "alabama", "arizona", "california", "colorado", "florida", "georgia", "illinois", "kansas", "michigan", "missouri",
    "nevada", "new york state", "north carolina", "ohio", "oregon", "pennsylvania", "texas", "utah", "virginia",
    "washington", "az", "ca", "fl", "ga", "il", "ks", "mi", "nc", "nv", "ny", "oh", "or", "tx", "ut", "va", "wa",
}
_PAREN = re.compile(r"\(([^)]*)\)")
_SPLIT = re.compile(r"\s*[,/|]\s*")


@lru_cache(maxsize=1)
def _table() -> dict[str, str]:
    table = {name.lower(): name for name in REGION}
    for name, words in kw.LOCATIONS.items():
        for w in words:
            table.setdefault(w.lower(), name)
    table.update(ALIASES)
    for state in US_STATES:
        table.setdefault(state, "United States")
    return table


def _lookup(text: str) -> str | None:
    key = " ".join(text.lower().replace("-", " ").split()).strip(" .")
    return _table().get(key)


@lru_cache(maxsize=4096)
def normalize(raw: str) -> str:
    """一个机房写法 → 规范名；认不出的原样返回（去掉首尾空白）。

    先整体查；再看括号里（"Germany (Frankfurt)"、"US West (Silicon Valley)"）；再按逗号拆开，从前往后找城市
    （"Ogden, Utah" 取 Ogden），都不是城市时取最具体的国家或州（"Durham, NC, USA" → 美国）。"""
    text = raw.strip()
    if not text:
        return text
    if found := _lookup(text):
        return found
    for inner in _PAREN.findall(text):
        if (found := normalize(inner)) in REGION:
            return found
    parts = [p for p in _SPLIT.split(_PAREN.sub("", text)) if p]
    hits = [found for p in parts if (found := _lookup(p))]
    cities = [h for h in hits if h not in COUNTRIES]
    if cities:
        return cities[0]
    if hits:
        return hits[0]
    words = text.split()  # "USA Oregon"
    if len(words) > 1 and (found := _lookup(words[0]) or _lookup(words[-1])):
        return found
    return text


COUNTRIES = {
    "United States", "Canada", "Mexico", "United Kingdom", "Germany", "Netherlands", "France", "Sweden", "Poland",
    "Turkey", "Romania", "Russia", "Finland", "Iceland", "Italy", "Switzerland", "Spain", "Denmark", "Norway",
    "Portugal", "Bulgaria", "Europe", "Japan", "India", "Malaysia", "Vietnam", "Pakistan", "Asia Pacific",
    "Australia", "South Africa",
}


def normalize_all(names: list[str]) -> list[str]:
    """一个套餐的机房列表：逐个规范化，去重，保持顺序。"""
    out: list[str] = []
    for name in names:
        n = normalize(name)
        if n and n not in out:
            out.append(n)
    return out


def region(name: str | None) -> str | None:
    """规范名所在地区；认不出的写法先规范化再查，仍认不出时为 None。"""
    if not name:
        return None
    return REGION.get(name) or REGION.get(normalize(name))


def known(name: str) -> bool:
    return normalize(name) in REGION
