"""活动有效期的识别（优化方案 O-15、D70）：用库里真实出现过的写法逐条验证。"""

from datetime import date

import pytest

from app.extract.dates import find_dates


def ends(text: str, published: str = "2026-11-03T10:00:00Z") -> date | None:
    return find_dates(text, published).ends


@pytest.mark.parametrize("text, published, starts, stops", [
    ("Black Friday is running from November 2 to December 2. TOP-UP BONUS", "2026-11-03T08:00:00Z",
     date(2026, 11, 2), date(2026, 12, 2)),
    ("Offers are valid from April 28 to April 30 or while stocks last", "2026-04-28T08:00:00Z",
     date(2026, 4, 28), date(2026, 4, 30)),
    ("Campaign period: October 26–31, 2026 (UTC)", "2026-10-26T08:00:00Z", date(2026, 10, 26), date(2026, 10, 31)),
    ("Sale: Nov 24 - Dec 2", "2026-11-20T08:00:00Z", date(2026, 11, 24), date(2026, 12, 2)),
    ("Sale: Dec 20 - Jan 5", "2026-12-18T08:00:00Z", date(2026, 12, 20), date(2027, 1, 5)),  # 起点在上一年
    ("活动时间：11月24日-12月1日", "2026-11-03T08:00:00Z", date(2026, 11, 24), date(2026, 12, 1)),
    ("即日起至12月1日，全场五折", "2026-11-03T08:00:00Z", None, date(2026, 12, 1)),
])
def test_ranges(text, published, starts, stops):
    got = find_dates(text, published)
    assert (got.starts, got.ends) == (starts, stops)


@pytest.mark.parametrize("text, published, stop", [
    ("⏳ Only through November 2, 2026, and while supplies last!", "2026-10-31T08:00:00Z", date(2026, 11, 2)),
    ("code: GHOSTHOST15 Valid until November 14. Non-recurring discount code!", "2026-10-28T08:00:00Z", date(2026, 11, 14)),
    ("Ends 01.11.2026. - We will randomly select 2 winners", "2026-10-23T08:00:00Z", date(2026, 11, 1)),
    ("Ends 23:59 GMT, Mon 30 Nov 2026. UK EPYC E4", "2026-10-30T08:00:00Z", date(2026, 11, 30)),
    ("The promotion is scheduled to end on November 8, 2026. - Renewal pricing", "2026-10-28T08:00:00Z", date(2026, 11, 8)),
    ("only $7.97 through December 31, 2023!", "2023-12-28T08:00:00Z", date(2023, 12, 31)),
    ("Offer ends 12 midnight UTC on Monday the 7th of September 2026 - 50% recurring", "2026-09-05T08:00:00Z",
     date(2026, 9, 7)),
    ("Redacts Setup Fees on AX42 Until July 14!", "2024-06-15T08:00:00Z", date(2024, 7, 14)),
    ("✔ Offer valid until 9th June 2026 or while stock lasts", "2026-05-28T08:00:00Z", date(2026, 6, 9)),
    ("Expireson:November26,2026", "2026-11-02T08:00:00Z", date(2026, 11, 26)),
    ("Sale ends 2026-12-01 23:59 UTC", "2026-11-20T08:00:00Z", date(2026, 12, 1)),
    ("Deal expires until 11/30", "2026-11-20T08:00:00Z", date(2026, 11, 30)),
    ("Deal ends 30/11", "2026-11-20T08:00:00Z", date(2026, 11, 30)),  # 前一个大于 12：日在前
    ("This part closes December 2.", "2026-11-03T08:00:00Z", date(2026, 12, 2)),
    ("优惠截止 12 月 1 日 24 点", "2026-11-03T08:00:00Z", date(2026, 12, 1)),
    ("截止2026年12月1日", "2026-11-03T08:00:00Z", date(2026, 12, 1)),
    ("活动截止12.1，先到先得", "2026-11-03T08:00:00Z", date(2026, 12, 1)),
])
def test_end_phrases(text, published, stop):
    assert ends(text, published) == stop


def test_year_is_filled_from_the_publish_date():
    assert ends("valid until January 5", "2026-12-20T08:00:00Z") == date(2027, 1, 5)  # 早于发布日期一个多月：进一年
    assert ends("valid until December 2", "2026-11-03T08:00:00Z") == date(2026, 12, 2)
    assert ends("deadline of 3 November", "2026-11-03T08:00:00Z") == date(2026, 11, 3)
    assert ends("Ends Nov 1", "2026-11-10T08:00:00Z") == date(2026, 11, 1)  # 刚过去的日期不进一年，页面标"已结束"
    assert ends("valid until December 2", None) is None  # 没有发布时间又没写年份：补不出
    assert ends("valid until December 2, 2026", None) == date(2026, 12, 2)


def test_relative_phrases_count_from_the_publish_time():
    assert ends("Limited time offer for the next 48 hours!", "2026-11-03T10:00:00Z") == date(2026, 11, 5)
    assert ends("48 hours only! 50% off", "2026-11-03T22:00:00Z") == date(2026, 11, 5)
    assert ends("SmokyHosts - 3 Days Only | Summer Storage VPS Sale", "2026-09-05T08:00:00Z") == date(2026, 9, 8)
    assert ends("限时 3 天，先到先得", "2026-11-03T08:00:00Z") == date(2026, 11, 6)
    assert ends("for next 24 hours", "2022-04-28T08:00:00Z") is None  # 附近没有"限时、只、活动"之类的词
    # 日期写明了，就以日期为准，不再按相对写法推算
    both = "3 Days Only. Offer ends 7 September 2026"
    assert ends(both, "2026-09-05T08:00:00Z") == date(2026, 9, 7)
    # 推算出的日期也算原文"出现过"，供校验 LLM 给的截止日期
    assert date(2026, 11, 5) in find_dates("48 hours only! Sale ends 2026-11-10", "2026-11-03T10:00:00Z").mentioned


@pytest.mark.parametrize("text", [
    "Free 24 Hour Trial for everyone",
    "Unpaid offers will be cancelled within 48 hours of their first order",
    "- 48 hour refund period.",
    "Trial ends automatically after 24 hours.",
    "It's valid on Windows offers only and renews at the same price",
    "you may 10 GB of storage until needed",
    "service limits suspended until Feb 30",
    "Starting October 31st, existing customers can enable IPv6",  # 起点单独出现的不算
    "price locked until December 2030",  # 离发布时间太远：是价格锁定，不是活动期限
])
def test_not_a_deadline(text):
    assert ends(text) is None


def test_a_standalone_start_is_not_taken():
    assert find_dates("The festival starts on November 8. Offer ends November 9.", "2026-11-03T10:00:00Z").starts is None


def test_latest_deadline_wins():
    text = "Black Friday runs until December 2. RE S series bare metal 40% off, runs through December 31"
    assert ends(text) == date(2026, 12, 31)


def test_mentioned_lists_every_date_in_the_text():
    got = find_dates("Winners announced on November 5, 2026. Entries from Oct 7th to Oct 28th.", "2026-10-07T08:00:00Z")
    assert {date(2026, 11, 5), date(2026, 10, 7), date(2026, 10, 28)} <= got.mentioned
