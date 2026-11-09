"""Telegram @VPS_spiders 回放（D81）：2025 年黑五窗口的频道快照走解析与整条流水线。

快照是第三方内容，不随仓库发布（tests/fixtures/README.md）；没有时跳过。数字对应 2026-11-07 抓的快照。
"""

import json
import re

import pytest

from app.config import load_settings
from app.db import Database
from app.pipeline import crawl
from app.sources import telegram
from app.sources.base import FetchResult
from app.vendors import prepare

from conftest import FIXTURES


TG_WINDOW = (26166, 26602)  # 2025 年黑五窗口（11-28 至 12-01）


def tg_window_posts():
    msgs = {}
    for path in sorted((FIXTURES / "telegram").glob("vps_spiders_before_*.html")):
        for m in telegram.parse_page(path.read_bytes()):
            msgs.setdefault(m.id, m)
    if not msgs:
        pytest.skip("缺 Telegram 抓取样本（tests/fixtures/telegram/，不随仓库发布）")
    return telegram.collect([m for m in msgs.values() if TG_WINDOW[0] <= m.id <= TG_WINDOW[1]], None, 0)


def test_telegram_replay_2025_black_friday_window():
    """回放 2025 年黑五窗口（2026-11-07 抓的快照）：可见 277 条，256 条是 LET / LES 评论的转发。"""
    posts, stats = tg_window_posts()
    assert stats["messages"] == 277 and stats["relayed"] == 256
    assert stats["skipped"] == {"thread": 15, "no_origin": 6}
    assert all(re.fullmatch(r"https://(?:lowendtalk|lowendspirit)\.com/discussion/comment/\d+", p.url) for p in posts)
    assert sum("lowendspirit" in p.url for p in posts) == 21  # 23 条转发，同一条评论转了两次的按 URL 去重
    assert all("黑五" in p.categories for p in posts)
    html = "".join(p.content_html for p in posts)
    assert not re.search(r"[?&](?:aff|ref|utm_\w+)=", html) and "t.me/c/" not in html and "&amp;amp;" not in html
    assert not re.search(r"(?m)(?:^|<br/>)Stock:", html)
    assert sum(bool(p.author) for p in posts) >= 190


def test_telegram_replay_through_the_pipeline(tmp_path):
    """规则模式（不配 LLM）：每条转发一条优惠，全部判黑五，九成以上抽出套餐。"""
    posts, stats = tg_window_posts()
    settings = load_settings({"DB_PATH": str(tmp_path / "radar.db")}, env_file=None)
    db = Database(settings.db_path)
    prepare(db)

    class Replay:
        id = "telegram"

        def __init__(self):
            self.settings = settings

        def fetch(self, state=None):
            return FetchResult(posts, stats)

    _, (report,) = crawl(db, [Replay()], "replay")
    assert report.error is None and not report.post_errors
    assert report.kinds == {"offer": 256} and report.deals == {"created": 256}
    conn = db.conn()
    deals = conn.execute("SELECT d.*, (SELECT COUNT(*) FROM plans p WHERE p.deal_id = d.id) AS n_plans "
                         "FROM deals d").fetchall()
    assert len(deals) == 256 and all(d["is_black_friday"] for d in deals)
    assert sum(d["event_type"] == "black_friday" for d in deals) >= 250
    assert sum(d["n_plans"] > 0 for d in deals) >= 230  # 其余多是没写价格或只写 From 价
    assert sum(d["vendor_id"] is not None for d in deals) >= 190
    assert sum(bool(json.loads(d["coupon_codes_json"])) for d in deals) >= 140
    urls = [d["purchase_url"] for d in deals if d["purchase_url"]]
    assert len(urls) >= 210 and not any(re.search(r"[?&](?:aff|ref)=|utm_", u) for u in urls)
