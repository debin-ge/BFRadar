"""汇率与人民币估算（第 8 节，D47）：来源回退、同日优先级、折算系数、起价、重算不改 updated_at、显示格式。"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.db import connect, migrate, transaction
from app.fx import (STATIC, URLS, FxError, FxRates, apply_fx, cny_year, fetch_rates, format_price, latest_rates,
                    parse_ecb, parse_erapi, refresh_cny, save_rates, start_price, update_rates)
from app.models import CURRENCIES, Deal, Period, Plan
from app.repo import load_deal, save_deal

FIXTURES = Path(__file__).parent / "fixtures"
ECB_JSON = (FIXTURES / "fx_frankfurter_ecb_2026-11-02.json").read_bytes()
ERAPI_JSON = (FIXTURES / "fx_erapi_2026-11-03.json").read_bytes()
FX = FxRates("2026-11-02", "ecb", {"CNY": 1.0, "USD": 7.0, "EUR": 8.0, "GBP": 9.0, "JPY": 0.05})


@pytest.fixture
def conn():
    c = connect(":memory:")
    migrate(c)
    return c


# ---------------------------------------------------------------- 来源


def test_parse_real_responses():
    ecb = parse_ecb(json.loads(ECB_JSON))
    assert (ecb.date, ecb.provider) == ("2026-11-02", "ecb")
    assert set(ecb.rates) == set(CURRENCIES) and ecb.rates["CNY"] == 1.0
    assert ecb.rates["USD"] == pytest.approx(6.648, abs=0.001) and ecb.rates["JPY"] == pytest.approx(0.0421, abs=1e-4)
    er = parse_erapi(json.loads(ERAPI_JSON))
    assert (er.date, er.provider) == ("2026-11-03", "erapi")
    assert er.rates["EUR"] == pytest.approx(7.453, abs=0.001)


def rows_without(quote):
    return [r for r in json.loads(ECB_JSON) if r["quote"] != quote]


@pytest.mark.parametrize("data, message", [
    ([{**r, "base": "USD"} for r in json.loads(ECB_JSON)], "基准币种"),
    (rows_without("SGD"), "缺少 SGD"),
    (rows_without("USD") + [{"date": "2026-11-02", "base": "CNY", "quote": "USD", "rate": 6.65}], "相差过大"),
    ({"status": 422, "message": "invalid currency: XXX"}, "不是汇率列表"),
])
def test_bad_responses_are_rejected(data, message):  # 第三条：报价方向反了（1 元 = 6.65 美元）
    with pytest.raises(FxError, match=message):
        parse_ecb(data)


def test_fetch_falls_back_in_order(http, server):
    server.overrides[URLS["ecb"]] = ECB_JSON
    server.overrides[URLS["erapi"]] = ERAPI_JSON
    fx, errors = fetch_rates(http, ("ecb", "erapi", "static"), timeout=5)
    assert (fx.provider, errors) == ("ecb", {})

    server.overrides[URLS["ecb"]] = 503
    fx, errors = fetch_rates(http, ("ecb", "erapi", "static"), timeout=5)
    assert fx.provider == "erapi" and list(errors) == ["ecb"]

    server.overrides[URLS["erapi"]] = b'{"result": "error"}'
    fx, errors = fetch_rates(http, ("ecb", "erapi", "static"), timeout=5)
    assert fx is None and list(errors) == ["ecb", "erapi"]

    server.requests.clear()
    assert fetch_rates(http, ("static",), timeout=5) == (None, {}) and server.requests == []


def test_same_day_keeps_higher_priority_source(conn):
    ecb, er = replace(FX, date="2026-11-03"), FxRates("2026-11-03", "erapi", {**FX.rates, "USD": 7.1})
    assert save_rates(conn, er, fetched_at="t1")
    assert save_rates(conn, ecb, fetched_at="t2")  # ECB 覆盖同日的 erapi
    assert not save_rates(conn, er, fetched_at="t3")  # erapi 不覆盖同日的 ECB
    assert latest_rates(conn) == ecb
    save_rates(conn, replace(FX, date="2026-11-01"), fetched_at="t4")
    assert latest_rates(conn).date == "2026-11-03"  # 取牌价日期最新的一天


def test_latest_falls_back_to_static_table(conn):
    assert latest_rates(conn) is STATIC and set(STATIC.rates) == set(CURRENCIES)
    assert latest_rates(conn, static=False) is None


# ---------------------------------------------------------------- 折算与起价


@pytest.mark.parametrize("amount, currency, period, expected", [
    (5, "USD", "month", 420.0),
    (10, "USD", "quarter", 280.0),
    (20, "USD", "semi", 280.0),
    (21, "EUR", "year", 168.0),
    (48, "GBP", "biennial", 216.0),
    (90, "GBP", "triennial", 270.0),
    (0.1, "USD", "hour", 6132.0),
    (99, "CNY", "year", 99.0),
    (1000, "JPY", "month", 600.0),
    (30, "USD", "onetime", None),
    (30, "USD", None, None),
    (30, "SGD", "year", None),  # 汇率表里没有
])
def test_cny_year(amount, currency, period, expected):
    assert cny_year(amount, currency, period, FX) == expected


def test_start_price_prefers_yearly_estimates():
    plans = [(5, "EUR", Period.MONTH), (40, "USD", Period.YEAR), (30, "USD", Period.ONETIME)]
    assert start_price(plans, [], FX) == start_price(plans, [{"amount": 1, "currency": "USD"}], FX)
    best = start_price(plans, [], FX)
    assert (best.amount, best.currency, best.period, best.cny_year) == (40, "USD", "year", 280.0)

    best = start_price([(30, "USD", Period.ONETIME), (25, "EUR", None)], [], FX)  # 都折不成年价：比折合金额
    assert (best.amount, best.currency, best.period, best.cny_year) == (25, "EUR", None, None)

    best = start_price([], [{"amount": 175, "currency": "USD", "period": None, "from": True},
                            {"amount": 2.35, "currency": "USD", "period": "month", "from": True}], FX)
    assert (best.amount, best.period, best.cny_year) == (2.35, "month", 197.4)
    assert start_price([], [], FX) is None and start_price(plans, [], None) is None


def make_deal(url="https://lowendtalk.com/discussion/1", prices=((5, "USD", Period.MONTH), (21, "EUR", Period.YEAR))):
    plans = [Plan(name=f"P{i}", price=a, currency=c, period=p, position=i) for i, (a, c, p) in enumerate(prices)]
    return Deal(url=url, source="lowendtalk", title="t", first_seen_at="t0", last_seen_at="t0", content_hash="h",
                plans=plans, price_list=[{"amount": a, "currency": c, "period": p and str(p)} for a, c, p in prices])


def test_apply_fx_and_roundtrip(conn):
    deal = make_deal()
    apply_fx(deal, FX)
    assert [p.price_cny_year for p in deal.plans] == [420.0, 168.0]
    assert (deal.min_price, deal.min_price_currency, deal.min_price_period, deal.min_price_cny_year) == (
        21, "EUR", Period.YEAR, 168.0)
    save_deal(conn, deal, now="t1")
    loaded = load_deal(conn, deal.url)
    assert (loaded.min_price, loaded.min_price_currency, loaded.min_price_period, loaded.min_price_cny_year) == (
        21.0, "EUR", Period.YEAR, 168.0)
    assert [p.price_cny_year for p in loaded.plans] == [420.0, 168.0]

    apply_fx(deal, None)  # 没有汇率：估算值全部为空
    assert deal.min_price is None and deal.plans[0].price_cny_year is None


def test_refresh_keeps_updated_at_and_matches_save_path(conn):
    deals = [make_deal(), make_deal("https://lowendtalk.com/discussion/2", prices=((30, "USD", Period.ONETIME),))]
    with transaction(conn):
        for d in deals:
            apply_fx(d, FX)
            save_deal(conn, d, now="t1")
    cheaper_eur = FxRates("2026-11-03", "ecb", {**FX.rates, "EUR": 4.0})
    with transaction(conn):
        assert refresh_cny(conn, cheaper_eur) == 1  # 一次性价格的那条没有年价，起价不变
        assert refresh_cny(conn, cheaper_eur) == 0
    d = load_deal(conn, deals[0].url)
    assert ([p.price_cny_year for p in d.plans], d.min_price_cny_year, d.updated_at) == ([420.0, 84.0], 84.0, "t1")

    fresh = make_deal()  # 采集线程用同一份汇率重新保存：与重算结果一致，不算更新
    apply_fx(fresh, cheaper_eur)
    assert not save_deal(conn, fresh, now="t2").updated


def test_update_rates_stores_and_falls_back(conn, http, server, settings):
    server.overrides[URLS["ecb"]] = ECB_JSON
    deal = make_deal()
    apply_fx(deal, FX)
    save_deal(conn, deal, now="t1")

    result = update_rates(conn, http, settings, now="2026-11-03T01:00:00Z")
    assert result.fetched.provider == "ecb" and result.rates == result.fetched and result.errors == {}
    row = conn.execute("SELECT * FROM fx_rates").fetchone()
    assert (row["date"], row["provider"], row["fetched_at"]) == ("2026-11-02", "ecb", "2026-11-03T01:00:00Z")
    assert result.refreshed == 1
    assert load_deal(conn, deal.url).min_price_cny_year == round(21 * result.rates.rates["EUR"], 2)

    server.overrides[URLS["ecb"]] = 503
    server.overrides[URLS["erapi"]] = 500
    result = update_rates(conn, http, settings, now="2026-11-04T01:00:00Z")
    assert result.fetched is None and set(result.errors) == {"ecb", "erapi"}
    assert (result.rates.provider, result.rates.date, result.refreshed) == ("ecb", "2026-11-02", 0)  # 沿用最近一次


# ---------------------------------------------------------------- 显示


@pytest.mark.parametrize("args, zh, en", [
    ((21, "EUR", "year", 156.6), "€21/年 ≈ ¥157/年", "€21/yr ≈ ¥157/yr"),
    ((3.5, "USD", "month", 279.2), "$3.50/月 ≈ ¥279/年", "$3.50/mo ≈ ¥279/yr"),
    ((99, "CNY", "year", 99.0), "¥99/年", "¥99/yr"),
    ((1000, "JPY", "month", 505.3), "JP¥1,000/月 ≈ ¥505/年", "JP¥1,000/mo ≈ ¥505/yr"),
    ((30, "USD", "onetime", None), "$30 一次性", "$30 one-time"),
    ((340.66, "USD", None, None), "$340.66", "$340.66"),
    ((0.1, "USD", "hour", 5826.0), "$0.10/小时 ≈ ¥5,826/年（按全年 8760 小时）", "$0.10/hr ≈ ¥5,826/yr (8,760 h)"),
])
def test_format_price(args, zh, en):
    assert format_price(*args[:3], "zh", args[3]) == zh
    assert format_price(*args[:3], "en", args[3]) == en


def test_free_is_zero_and_shown_as_free():
    """免费套餐（0 价，币种周期留空）：人民币估算为 0、起价排最前、显示"免费"（D55）。"""
    assert cny_year(0, None, None, FX) == 0.0 and cny_year(0.0, "USD", "month", FX) == 0.0
    best = start_price([(5.0, "USD", Period.MONTH), (0.0, None, None)], [], FX)
    assert (best.amount, best.currency, best.period, best.cny_year) == (0.0, None, None, 0.0)
    assert (format_price(0, None, None, "zh"), format_price(0.0, "USD", "month", "en", cny=0.0)) == ("免费", "Free")
