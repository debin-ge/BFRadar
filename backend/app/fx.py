"""汇率与人民币估算（《需求与方案说明》第 8 节，D10、D47）。

汇率来源按 FX_PROVIDER 顺序尝试：ecb（frankfurter.dev v2 限定 ECB 来源的参考汇率；v2 默认混合上百家央行，v1 已
标为弃用）→ erapi（open.er-api.com）。拉到的写入 fx_rates，一天一行，同一天以优先级高的来源为准；全部失败时沿用
库里最近一次；库里没有时用内置静态表（FX_PROVIDER 含 static 时）。汇率一律存"1 单位外币折合多少人民币"，并与
静态表核对，相差一倍以上视为来源出错（例如报价方向反了）。

人民币估算：price_cny_year = 金额 × 汇率 × 周期系数（月 12、季 4、半年 2、年 1、两年 1/2、三年 1/3、小时 8760）；
一次性与周期不明的价格不折年价，不参与起价排序。起价（第 11 节）：套餐中人民币年价最低者；没有能折年价的套餐时
取折合人民币金额最低者（此时没有年价）；没有套餐时按同样规则用帖子级价格列表。汇率更新后重算库里全部估算值，
不改 updated_at：汇率变动不是优惠的变化。
"""

import json
import logging
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from .config import Settings
from .db import transaction
from .http import HttpClient
from .models import CURRENCIES, Deal, Period

log = logging.getLogger(__name__)

QUOTES = ",".join(c for c in CURRENCIES if c != "CNY")
URLS = {"ecb": f"https://api.frankfurter.dev/v2/rates?base=CNY&quotes={QUOTES}&providers=ecb",
        "erapi": "https://open.er-api.com/v6/latest/CNY"}
PRIORITY = ("ecb", "erapi")  # 同一天的汇率以靠前的来源为准
PERIOD_FACTOR = {Period.MONTH: 12, Period.QUARTER: 4, Period.SEMI: 2, Period.YEAR: 1, Period.BIENNIAL: 1 / 2,
                 Period.TRIENNIAL: 1 / 3, Period.HOUR: 8760}
# 内置静态表：2026-11-02 欧洲央行参考汇率（frankfurter.dev），库里没有汇率且在线来源都不可用时使用
STATIC_RATES = {"CNY": 1.0, "USD": 6.648052, "EUR": 7.456566, "GBP": 8.921402, "HKD": 0.847314, "CAD": 4.7576,
                "AUD": 4.659181, "SGD": 5.198586, "JPY": 0.042105}
MAX_DRIFT = 2.0


class FxError(ValueError):
    pass


@dataclass(frozen=True)
class FxRates:
    date: str  # 来源给出的牌价日期
    provider: str  # ecb / erapi / static
    rates: dict[str, float]  # 币种 → 1 单位折合人民币


STATIC = FxRates("2026-11-02", "static", STATIC_RATES)


# ---------------------------------------------------------------- 来源


def _cny_per_unit(per_cny: Mapping) -> dict[str, float]:
    """来源给的是"1 元折合多少外币"，取倒数；白名单币种须齐全，且与静态表相差不超过一倍。"""
    rates = {"CNY": 1.0}
    for cur in CURRENCIES:
        if cur == "CNY":
            continue
        value = per_cny.get(cur)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise FxError(f"缺少 {cur} 汇率")
        rates[cur] = round(1 / value, 6)
        if not 1 / MAX_DRIFT <= rates[cur] / STATIC_RATES[cur] <= MAX_DRIFT:
            raise FxError(f"{cur} 折合 {rates[cur]} 元，与参考值 {STATIC_RATES[cur]} 相差过大")
    return rates


def parse_ecb(data) -> FxRates:
    """frankfurter.dev v2：[{"date": "2026-11-02", "base": "CNY", "quote": "USD", "rate": 0.15042}, …]；
    出错时为 {"status": 422, "message": …}。"""
    if not isinstance(data, list) or not data:
        raise FxError(f"不是汇率列表：{str(data)[:100]}")
    if bases := {row.get("base") for row in data} - {"CNY"}:
        raise FxError(f"基准币种不是 CNY：{sorted(bases)}")
    day = max(date.fromisoformat(row["date"]) for row in data).isoformat()
    return FxRates(day, "ecb", _cny_per_unit({row.get("quote"): row.get("rate") for row in data}))


def parse_erapi(data: Mapping) -> FxRates:
    """open.er-api.com：{"result": "success", "base_code": "CNY", "time_last_update_unix": …, "rates": {…}}"""
    if data.get("result") != "success" or data.get("base_code") != "CNY":
        raise FxError(f"结果 {data.get('result')!r}，基准币种 {data.get('base_code')!r}")
    day = datetime.fromtimestamp(data["time_last_update_unix"], UTC).date().isoformat()
    return FxRates(day, "erapi", _cny_per_unit(data.get("rates") or {}))


PARSERS = {"ecb": parse_ecb, "erapi": parse_erapi}


def fetch_rates(http: HttpClient, providers: Iterable[str], *, timeout: float) -> tuple[FxRates | None, dict[str, str]]:
    """按顺序尝试在线来源，返回第一个成功的结果与失败来源的错误。static 不是在线来源，见 latest_rates。"""
    errors: dict[str, str] = {}
    for name in providers:
        if name not in PARSERS:
            continue
        try:
            return PARSERS[name](http.get(URLS[name], timeout=timeout).json()), errors
        except Exception as e:  # 一个来源失败就换下一个
            log.warning("汇率来源 %s 失败：%s", name, e)
            errors[name] = f"{type(e).__name__}: {e}"
    return None, errors


# ---------------------------------------------------------------- 入库


def _rank(provider: str) -> int:
    return PRIORITY.index(provider) if provider in PRIORITY else len(PRIORITY)


def save_rates(conn: sqlite3.Connection, fx: FxRates, *, fetched_at: str) -> bool:
    """写入一天的汇率；同一天已有优先级更高来源的不覆盖。调用方负责事务。"""
    row = conn.execute("SELECT provider FROM fx_rates WHERE date = ?", (fx.date,)).fetchone()
    if row and _rank(row["provider"]) < _rank(fx.provider):
        return False
    conn.execute("INSERT OR REPLACE INTO fx_rates (date, rates_json, provider, fetched_at) VALUES (?, ?, ?, ?)",
                 (fx.date, json.dumps(fx.rates, sort_keys=True), fx.provider, fetched_at))
    return True


def latest_rates(conn: sqlite3.Connection, *, static: bool = True) -> FxRates | None:
    """库里最近一天的汇率；库里没有时用内置静态表（static=False 时为 None）。"""
    row = conn.execute("SELECT date, provider, rates_json FROM fx_rates ORDER BY date DESC LIMIT 1").fetchone()
    if row:
        return FxRates(row["date"], row["provider"], json.loads(row["rates_json"]))
    return STATIC if static else None


# ---------------------------------------------------------------- 折算与起价


def cny_year(amount: float | None, currency: str | None, period: str | None, fx: FxRates | None) -> float | None:
    """人民币估算年价；一次性、周期不明或没有汇率时为 None。免费（0 价）不论币种周期都是 0（D55）。"""
    if amount == 0:
        return 0.0
    factor = PERIOD_FACTOR.get(period) if period else None
    if amount is None or factor is None or fx is None or currency not in fx.rates:
        return None
    return round(amount * fx.rates[currency] * factor, 2)


@dataclass(frozen=True)
class StartPrice:
    amount: float
    currency: str
    period: str | None
    cny_year: float | None  # 折不成年价时为 None，不参与起价排序


def start_price(plans: Iterable[tuple], price_list: Iterable[Mapping], fx: FxRates | None) -> StartPrice | None:
    """plans 为（金额, 币种, 周期）序列；price_list 为帖子级价格列表，只在没有套餐时使用。"""
    if fx is None:
        return None
    for items in (list(plans), [(p.get("amount"), p.get("currency"), p.get("period")) for p in price_list]):
        priced = [(a, c, p) for a, c, p in items if a is not None and (c in fx.rates or a == 0)]  # 免费不看币种
        if not priced:
            continue
        yearly = [(y, a, c, p) for a, c, p in priced if (y := cny_year(a, c, p, fx)) is not None]
        if yearly:
            y, a, c, p = min(yearly, key=lambda t: t[0])
            return StartPrice(a, c, p and str(p), y)
        a, c, p = min(priced, key=lambda t: t[0] * fx.rates.get(t[1], 1.0))
        return StartPrice(a, c, p and str(p), None)
    return None


def apply_fx(deal: Deal, fx: FxRates | None) -> None:
    """保存前填写套餐的人民币估算年价与优惠的起价。"""
    for p in deal.plans:
        p.price_cny_year = cny_year(p.price, p.currency, p.period, fx)
    best = start_price([(p.price, p.currency, p.period) for p in deal.plans], deal.price_list, fx)
    deal.min_price = best.amount if best else None
    deal.min_price_currency = best.currency if best else None
    deal.min_price_period = Period(best.period) if best and best.period else None
    deal.min_price_cny_year = best.cny_year if best else None


def refresh_cny(conn: sqlite3.Connection, fx: FxRates | None) -> int:
    """用 fx 重算库里全部套餐与优惠的估算值，不改 updated_at；返回起价有变化的优惠数。调用方负责事务。"""
    by_deal: dict[int, list[tuple]] = defaultdict(list)
    plan_updates = []
    for r in conn.execute("SELECT id, deal_id, price, currency, period, price_cny_year FROM plans"
                          " ORDER BY deal_id, position"):
        value = cny_year(r["price"], r["currency"], r["period"], fx)
        if value != r["price_cny_year"]:
            plan_updates.append((value, r["id"]))
        by_deal[r["deal_id"]].append((r["price"], r["currency"], r["period"]))
    deal_updates = []
    for r in conn.execute("SELECT id, price_list_json, min_price, min_price_currency, min_price_period,"
                          " min_price_cny_year FROM deals"):
        best = start_price(by_deal.get(r["id"], []), json.loads(r["price_list_json"]), fx)
        new = (best.amount, best.currency, best.period, best.cny_year) if best else (None, None, None, None)
        if new != (r["min_price"], r["min_price_currency"], r["min_price_period"], r["min_price_cny_year"]):
            deal_updates.append((*new, r["id"]))
    conn.executemany("UPDATE plans SET price_cny_year = ? WHERE id = ?", plan_updates)
    conn.executemany("UPDATE deals SET min_price = ?, min_price_currency = ?, min_price_period = ?,"
                     " min_price_cny_year = ? WHERE id = ?", deal_updates)
    return len(deal_updates)


# ---------------------------------------------------------------- 每日任务


@dataclass
class FxUpdate:
    rates: FxRates | None  # 当前生效的汇率
    fetched: FxRates | None  # 本次拉到的汇率；全部来源失败时为 None
    errors: dict[str, str]
    refreshed: int  # 起价有变化的优惠数


def update_rates(conn: sqlite3.Connection, http: HttpClient, settings: Settings, *, now: str) -> FxUpdate:
    """汇率任务（第 13 节，每日北京时间 09:00）：拉取 → 入库 → 用当前生效的汇率重算库里全部估算值。"""
    fetched, errors = fetch_rates(http, settings.fx_providers, timeout=settings.fx_http_timeout)
    with transaction(conn):
        if fetched:
            save_rates(conn, fetched, fetched_at=now)
        current = latest_rates(conn, static="static" in settings.fx_providers)
        refreshed = refresh_cny(conn, current)
    if fetched:
        log.info("汇率 %s %s，起价变化 %d 条", fetched.provider, fetched.date, refreshed)
    else:
        log.warning("汇率来源全部失败，沿用 %s", f"{current.provider} {current.date}" if current else "（无）")
    return FxUpdate(current, fetched, errors, refreshed)


# ---------------------------------------------------------------- 显示（第 8 节）

SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "CNY": "¥", "JPY": "JP¥", "HKD": "HK$", "CAD": "C$", "AUD": "A$",
           "SGD": "S$"}
PERIOD_LABELS = {
    "zh": {"month": "月", "quarter": "季", "semi": "半年", "year": "年", "biennial": "两年", "triennial": "三年",
           "hour": "小时", "onetime": "一次性"},
    "en": {"month": "mo", "quarter": "qtr", "semi": "6mo", "year": "yr", "biennial": "2yr", "triennial": "3yr",
           "hour": "hr", "onetime": "one-time"},
}
HOURLY_NOTE = {"zh": "（按全年 8760 小时）", "en": " (8,760 h)"}
FREE = {"zh": "免费", "en": "Free"}


def _amount(value: float) -> str:
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"


def format_price(amount: float, currency: str, period: str | None, lang: str, cny: float | None = None) -> str:
    """"€21/年 ≈ ¥157/年"、"€21/yr ≈ ¥157/yr"；人民币原价不附估算；一次性写在金额后；小时价的年估算注明按全年。
    0 价写"免费"，不附周期与估算（D55）。"""
    if amount == 0:
        return FREE[lang]
    labels = PERIOD_LABELS[lang]
    text = f"{SYMBOLS.get(currency, f'{currency} ')}{_amount(amount)}"
    if period == Period.ONETIME:
        text += f" {labels['onetime']}"
    elif period:
        text += f"/{labels[period]}"
    if cny is not None and currency != "CNY":
        text += f" ≈ ¥{cny:,.0f}/{labels['year']}"
        if period == Period.HOUR:
            text += HOURLY_NOTE[lang]
    return text
