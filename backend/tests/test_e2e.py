"""后端端到端（M2.13）：main.py serve 起真的 uvicorn → 调度器启动后自动采集各源 → LLM 队列跑完；接口、RSS、
状态页数据、健康检查与手动刷新按 M2 验收清单（《实施计划》第 4 节）逐项检查。页面由前端渲染（D60）：
沿链接走遍全站、首页要素、搜索、详情页、商家页、状态页与关于页在 test_e2e_web.py 里查；现场（真实 HTTP、快照与假 LLM）见 live.py。
"""

from urllib.parse import urlsplit

import feedparser
import pytest

from live import SITE, SOURCES, TOKEN, all_deals, live_site, wait_for

SEASON = "2026-10-01T00:00:00Z"


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    with live_site(tmp_path_factory) as env:
        yield env


# ---------------------------------------------------------------- 启动与采集


def test_serve_wiring(live):
    """serve 的参数：单 worker（D25）、信任转发头（前端与 Cloudflare 后面）、不另配日志。"""
    assert live.serve_kwargs == {"host": "0.0.0.0", "port": 8000, "workers": 1, "log_config": None,
                                 "proxy_headers": True, "forwarded_allow_ips": "*"}
    hosts = {urlsplit(u).hostname for u in live.upstream.requests}
    assert {"lowendtalk.com", "lowendspirit.com", "lowendbox.com", "rss.nodeseek.com", "www.reddit.com", "t.me",
            "www.v2ex.com", "www.nodeloc.com", "hostingdiscussion.com",
            "api.frankfurter.dev"} <= hosts  # 各源与汇率都由调度器自己拉取


def test_startup_crawl_and_llm(live):
    stats = live.json("/api/stats")
    deals = stats["deals"]
    assert deals["total"] > 200
    assert {"lowendtalk", "lowendspirit", "lowendbox"} <= set(deals["by_source"]) <= set(SOURCES)  # 夹具里没有品牌官方帖，Reddit 只出情报
    assert stats["llm"] == {"done": deals["total"]} and live.llm.calls == deals["total"]  # 每条优惠调用一次
    assert stats["fx"]["provider"] == "ecb" and stats["fx"]["date"] == "2026-11-02"
    assert stats["plans"] > 1000 and stats["intel"]["total"] > 0
    runs = live.json("/api/runs?limit=50")["items"]
    assert {r["reason"] for r in runs} <= {"scheduled", "llm"} and all(r["ok"] for r in runs)  # 启动后自己跑的
    llm_runs = [s for r in runs for s in r["sources"] if s["source"] == "llm"]
    assert llm_runs and all(s["fetched"] > 0 for s in llm_runs)  # 队列为空的轮次不记运行（D54）


# ---------------------------------------------------------------- M2 验收清单（接口与数据）


def test_acceptance_scopes(live):
    """验收 2：三档范围逐级包含；前两档不出现 10-01 之前发布的帖子；按页取完的条数与总数一致。"""
    totals = {s: live.json(f"/api/deals?scope={s}")["total"] for s in ("bf", "promo", "all")}
    assert 0 < totals["bf"] < totals["promo"] < totals["all"]
    for scope in ("bf", "promo"):
        page, pages = 1, 1
        while page <= pages:
            data = live.json(f"/api/deals?scope={scope}&page_size=100&page={page}")
            assert all(d["published_at"] >= SEASON for d in data["items"]), scope
            page, pages = page + 1, data["pages"]
    ids, _ = all_deals(live)
    assert len(ids) == totals["all"] == live.json("/api/stats")["deals"]["total"]


def test_acceptance_plan_filters(live):
    """验收 5：套餐筛选的结果全部满足条件，默认按人民币年价升序。"""
    data = live.json("/api/plans?ram_min=2&period=year&page_size=100")
    assert data["total"] and all(p["ram_gb"] >= 2 and p["period"] == "year" for p in data["items"])
    prices = [p["price_cny_year"] for p in data["items"] if p["price_cny_year"] is not None]
    assert prices == sorted(prices)
    cores = live.json("/api/plans?cpu_min=4&page_size=100")
    assert cores["total"] and all(p["cpu_cores"] >= 4 for p in cores["items"])


def test_acceptance_status_feed_and_api(live):
    """验收 8、10、11：状态页数据、RSS、接口；状态页、RSS 与接口不在页面上链接，直接访问地址（D59）。"""
    status = live.json("/internal/v1/zh/status")
    facts = {f["label"]: f["value"] for f in status["facts"]}
    assert facts["调度器"].startswith("运行中") and "ECB 2026-11-02" in facts["汇率"] and "NodeSeek 覆盖率" in facts
    assert [s["key"] for s in status["sources"]] == [*SOURCES, "llm"]
    assert not any(s["failing"] or any(r["bad"] for r in s["rows"]) for s in status["sources"])

    feed = live.get("/feed.xml")
    parsed = feedparser.parse(feed.content)
    assert not parsed.bozo and parsed.version == "rss20" and parsed.entries
    ids, _ = all_deals(live)
    assert all(e.link.startswith(SITE + "/deal/") and int(e.link.rsplit("/", 1)[1]) in ids for e in parsed.entries)

    deals = live.json("/api/deals?page=1")
    plans = live.json("/api/plans?ram_min=2")
    assert deals["total"] and deals["items"] and plans["total"] and plans["items"]
    assert all(p["ram_gb"] >= 2 for p in plans["items"])


def test_health_and_errors(live):
    r = live.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] and r.json()["failing"] == []
    head = live.http.head("/healthz")
    assert head.status_code == 200 and head.content == b""
    for path in ("/api/nope", "/nope", "/status"):  # 后端只出数据，页面地址也是 JSON 404
        r = live.get(path)
        assert r.status_code == 404 and r.json()["error"] == "not_found", path


def test_acceptance_manual_refresh_and_cache(live):
    """验收 12：不带或带错 token 401，带对 200（返回 run_id），重复触发 409；刷新跑完即清缓存。"""
    assert live.http.post("/api/refresh").status_code == 401
    assert live.http.post("/api/refresh", headers={"X-Refresh-Token": "nope"}).status_code == 401
    before = live.json("/api/runs?limit=1")["items"][0]["id"]  # 这一份进了 60 秒的缓存
    r = live.http.post("/api/refresh", headers={"X-Refresh-Token": TOKEN})
    assert r.status_code == 200 and r.json()["run_id"] > before
    run_id = r.json()["run_id"]
    assert live.http.post("/api/refresh", headers={"X-Refresh-Token": TOKEN}).status_code == 409
    assert live.json("/api/runs?limit=1")["items"][0]["id"] == before  # 还是缓存里的
    wait_for(lambda: not live.json("/internal/v1/zh/status")["refreshing"], "手动刷新跑完")
    top = live.json("/api/runs?limit=1")["items"][0]  # 跑完即清缓存，不用等 60 秒
    assert (top["id"], top["reason"], top["ok"]) == (run_id, "manual", True)
    assert {s["source"] for s in top["sources"]} == set(SOURCES)
    assert live.get("/healthz").json()["ok"]


def test_no_errors_logged(live):
    assert live.errors.lines() == []
