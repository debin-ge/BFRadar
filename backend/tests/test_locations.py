"""机房规范化（优化方案 O-12，D67）：五花八门的写法统一成规范名，城市优先，认不出城市时退到国家；地区筛选用。"""

import pytest

from app.locations import COUNTRIES, REGION, known, normalize, normalize_all, region
from app.extract import keywords as kw
from app.i18n import strings


@pytest.mark.parametrize("raw, expected", [
    ("Frankfurt", "Frankfurt"),
    ("Frankfurt, Germany", "Frankfurt"),
    ("Frankfurt am Main, Germany", "Frankfurt"),
    ("Germany (Frankfurt)", "Frankfurt"),
    ("NYC", "New York"),
    ("New York City Metro", "New York"),
    ("New York, NY", "New York"),
    ("Buffalo, New York, USA", "Buffalo"),
    ("Silicon Valley, California", "San Jose"),
    ("US West (Silicon Valley)", "San Jose"),
    ("Ashburn, VA", "Ashburn"),
    ("Ogden, Utah", "Ogden"),
    ("Utah, USA", "United States"),
    ("USA Oregon", "United States"),
    ("Durham, NC, USA", "Durham"),
    ("USA", "United States"),
    ("UK", "United Kingdom"),
    ("West Midlands, UK", "United Kingdom"),
    ("Mazovia, Poland", "Poland"),
    ("SG", "Singapore"),
    ("洛杉矶", "Los Angeles"),
    ("EU", "Europe"),
    ("APAC", "Asia Pacific"),
    ("  Tokyo ", "Tokyo"),
    ("Gotham", "Gotham"),  # 认不出的原样保留
])
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_normalize_all_dedupes_and_keeps_order():
    assert normalize_all(["Frankfurt, Germany", "NYC", "Frankfurt", "New York City", ""]) == ["Frankfurt", "New York"]


def test_regions():
    assert (region("Hong Kong"), region("Dallas, TX"), region("Amsterdam, Netherlands")) == ("asia", "na", "eu")
    assert (region("Sydney"), region("Johannesburg"), region("Sao Paulo")) == ("oc", "af", "sa")
    assert region("Gotham") is None and region(None) is None and not known("Gotham") and known("NYC")


def test_every_canonical_name_has_a_region_and_a_chinese_name():
    """keywords.LOCATIONS 的标准名都在规范表里；每个规范名都有中文名（字典 loc.*）。"""
    assert set(kw.LOCATIONS) <= set(REGION)
    assert COUNTRIES <= set(REGION)
    zh = strings("zh")
    assert [n for n in REGION if f"loc.{n}" not in zh] == []
