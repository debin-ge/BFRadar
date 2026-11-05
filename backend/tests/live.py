"""端到端测试共用的现场：main.py serve 起真的 uvicorn → 调度器启动后自动采集六个源 → LLM 队列跑完（M2.13）；
需要时再起已构建的前端（node frontend/server.js，生产模式），接到这个后端上（D60）。

网站走真实 HTTP（随机端口）；采集与 LLM 的请求走 MockTransport：六个源与汇率用 tests/fixtures 的真实快照，
LLM 是假的 OpenAI 兼容接口，只回中文标题与摘要（其余留空，合并时沿用规则结果，D45）。为了几秒内跑完，
把调度器的启动延迟与 LLM 轮询间隔调短；其余全按 serve 的默认配置。
"""

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from queue import Queue

import httpx
import pytest
import uvicorn

import main
from app import scheduler as sched_mod
from app.config import Settings, load_settings
from app.extract.llm import LlmClient
from app.http import HttpClient

from conftest import FIXTURES, FixtureServer, noop_sleep

TOKEN = "e2e-token"
SITE = "https://bfradar.test"
CONTACT = "ops@bfradar.test"
TODAY = "2026-11-03"  # 固定"今天"，免得夹具里优惠的截止日期过去后测试结果变化（O-15）
SOURCES = ("lowendtalk", "lowendspirit", "lowendbox", "nodeseek", "reddit")
LLM_ENV = {"LLM_BASE_URL": "https://llm.e2e.test/v1", "LLM_API_KEY": "sk-e2e", "LLM_MODEL": "e2e-model"}
SUMMARY = "端到端测试摘要"
PAGE_BUDGET = 150 * 1024  # 第 9 节：页面体积 ≤ 150 KB
SLOW = 2.0  # 秒；本机单页一般 5–50 ms，这里只拦明显的退化
ECB_JSON = (FIXTURES / "fx_frankfurter_ecb_2026-11-02.json").read_bytes()
LINK = re.compile(r'(?:href|src)="(/[^"#]*)')
TITLE_SUFFIX = {"zh": " · 黑五雷达", "en": " · BFRadar · BlackFridayRadar"}
HTML_LANG = {"zh": "zh-CN", "en": "en"}
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


class FakeLlm:
    """OpenAI 兼容的 /chat/completions：标题前加"【译】"、摘要固定，其余字段留空。在 LLM 工作线程里被调用。
    社区标题的翻译请求（O-14，正文是 {"titles": [...]}）逐条加"【译】"，另记在 translations，不计入 calls。"""

    def __init__(self) -> None:
        self.calls = 0
        self.translations = 0
        self._lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        user = json.loads(request.content)["messages"][1]["content"]
        if user.startswith('{"titles"'):
            with self._lock:
                self.translations += 1
            reply = {"titles": [f"【译】{t}" for t in json.loads(user)["titles"]]}
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": json.dumps(reply, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40}})
        title = re.search(r"^标题：(.*)$", user, re.M).group(1)
        with self._lock:
            self.calls += 1
        reply = {"vendor": None, "plans": [], "title_zh": f"【译】{title}", "summary_zh": SUMMARY, "confidence": 0.5}
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": json.dumps(reply, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})


class Upstream(FixtureServer):
    """六个源的快照，外加汇率（frankfurter 的 ECB 快照）。"""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.frankfurter.dev":
            self.requests.append(str(request.url))
            return httpx.Response(200, content=ECB_JSON)
        return super().handler(request)


class ErrorLog(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def lines(self) -> list[str]:
        fmt = logging.Formatter()
        return [f"{r.name}: {r.getMessage()}\n{fmt.formatException(r.exc_info) if r.exc_info else ''}"
                for r in self.records]


@dataclass
class Live:
    http: httpx.Client
    settings: Settings
    upstream: Upstream
    llm: FakeLlm
    serve_kwargs: dict
    errors: ErrorLog
    pages: dict[str, httpx.Response] = field(default_factory=dict)  # 走站时取到的页面

    @property
    def origin(self) -> str:
        return str(self.http.base_url).rstrip("/")

    def get(self, path: str, **kw) -> httpx.Response:
        return self.http.get(path, **kw)

    def json(self, path: str):
        r = self.http.get(path)
        assert r.status_code == 200, (path, r.text[:200])
        return r.json()


def wait_for(check, what: str, timeout: float = 90.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := check():
            return value
        time.sleep(0.2)
    raise AssertionError(f"等待超时：{what}")


@contextmanager
def live_site(tmp_path_factory) -> Iterator[Live]:
    settings = load_settings({"DB_PATH": str(tmp_path_factory.mktemp("e2e") / "radar.db"), "SITE_URL": SITE,
                              "REFRESH_TOKEN": TOKEN, "CONTACT_EMAIL": CONTACT, "TODAY": TODAY, **LLM_ENV},
                             env_file=None)
    upstream, llm, errors, captured = Upstream(), FakeLlm(), ErrorLog(), {}
    mp = pytest.MonkeyPatch()
    mp.setattr(main, "load_settings", lambda: settings)
    mp.setattr(sched_mod, "HttpClient", lambda s: HttpClient(s, transport=httpx.MockTransport(upstream.handler),
                                                             sleep=noop_sleep))
    mp.setattr(sched_mod, "LlmClient", lambda s: LlmClient(s, transport=httpx.MockTransport(llm), sleep=noop_sleep))
    mp.setattr(sched_mod, "STARTUP_DELAY", sched_mod.timedelta(seconds=0.2))
    mp.setattr(sched_mod, "LLM_EVERY_MINUTES", 0.05)  # 3 秒
    mp.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app, **kw))
    logging.getLogger().addHandler(errors)

    assert main.main([]) == 0  # 不带命令即 serve：建调度器与应用，交给 uvicorn.run
    app = captured.pop("app")
    server = uvicorn.Server(uvicorn.Config(app, **{**captured, "host": "127.0.0.1", "port": 0}))
    thread = threading.Thread(target=server.run, name="uvicorn-e2e", daemon=True)
    thread.start()
    try:
        wait_for(lambda: server.started, "uvicorn 启动")
        port = server.servers[0].sockets[0].getsockname()[1]
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as http:
            env = Live(http, settings, upstream, llm, captured, errors)
            # 启动 → 采集：调度器启动后自动跑一轮全部源（不手动触发）
            def crawled() -> bool:
                sources = http.get("/healthz").json()["sources"]
                return len(sources) == len(SOURCES) and all(s["last_ok_at"] for s in sources.values())

            wait_for(crawled, "六个源各跑完一轮")
            # → LLM：采集有新优惠时唤醒队列，等全部处理完
            wait_for(lambda: set(env.json("/api/stats")["llm"]) == {"done"}, "LLM 队列清空")
            yield env
    finally:
        server.should_exit = True
        thread.join(60)
        logging.getLogger().removeHandler(errors)
        mp.undo()
    assert not thread.is_alive()  # 停止时等进行中的任务结束，不挂住


def walk(site) -> dict[str, httpx.Response]:
    """从首页出发，沿页面里的站内链接（href / src）广度优先走完；结果记在 site.pages 里，同一模块只走一次。"""
    if site.pages:
        return site.pages
    queue, seen = ["/"], {"/"}
    while queue:
        path = queue.pop(0)
        r = site.get(path)
        site.pages[path] = r
        if r.headers.get("content-type", "").startswith("text/html"):
            for link in (unescape(x) for x in LINK.findall(r.text)):
                if link not in seen:
                    seen.add(link)
                    queue.append(link)
    return site.pages


def split_lang(path: str) -> tuple[str, str]:
    """（语言, 去掉语言前缀的路径）：/en/plans → ("en", "/plans")。"""
    if path == "/en" or path.startswith("/en/"):
        return "en", path[3:] or "/"
    return "zh", path


def cache_rule(path: str) -> str:
    path = split_lang(path)[1]
    if path.startswith("/status"):
        return "no-store"
    if path.startswith(("/deal/", "/vendor/", "/about")):
        return "public, max-age=600"
    return "public, max-age=300"


def all_deals(live: Live) -> tuple[set[int], set[str]]:
    """接口里的全部优惠编号与有优惠的商家（按页取完）。"""
    ids, slugs, page = set(), set(), 1
    while True:
        data = live.json(f"/api/deals?scope=all&page_size=100&page={page}")
        ids |= {d["id"] for d in data["items"]}
        slugs |= {d["vendor_slug"] for d in data["items"] if d["vendor_slug"]}
        if page >= data["pages"]:
            return ids, slugs
        page += 1


# ---------------------------------------------------------------- 前端（D60）


def require(reason: str) -> None:
    """缺前端构建、node、playwright 或 Chrome 时跳过；设 REQUIRE_WEB_E2E=1 时改为失败（检查点与发布前必须跑）。"""
    if os.environ.get("REQUIRE_WEB_E2E") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


@dataclass
class Web:
    http: httpx.Client
    archive: Path
    stderr: list[str]
    pages: dict[str, httpx.Response] = field(default_factory=dict)

    def get(self, path: str, **kw) -> httpx.Response:
        return self.http.get(path, **kw)


DAY_MS = 86_400_000


def seed_archive(archive: Path) -> Path:
    """旧版本静态文件的归档（frontend/assets-archive.js）预先放两个版本：一天前被替换的，文件应还能取到；
    八天前被替换的，启动时应被删掉。"""
    now = int(time.time() * 1000)
    builds = {"manifest-old.js": {"files": ["manifest-old.js", "old-chunk.js"], "replaced": now - DAY_MS},
              "manifest-gone.js": {"files": ["manifest-gone.js", "gone-chunk.js"], "replaced": now - 8 * DAY_MS}}
    archive.mkdir(parents=True)
    for build in builds.values():
        for name in build["files"]:
            (archive / name).write_text(f"// {name}\n")
    (archive / ".builds.json").write_text(json.dumps(builds))
    return archive


@contextmanager
def web_site(api_origin: str, tmp: Path, *, quiet: bool = True) -> Iterator[Web]:
    """已构建的前端：node frontend/server.js（生产模式，PORT=0 随机端口），API_ORIGIN 指向后端（一般是 live.origin）。
    stderr 全部收集；退出（SIGTERM）时断言退出码为 0，quiet 时还断言 stderr 为空。"""
    node = shutil.which("node")
    if node is None:
        require("找不到 node")
    if not (FRONTEND / "build" / "server" / "index.js").exists():
        require("前端未构建（cd frontend && pnpm build）")
    env = {**os.environ, "NODE_ENV": "production", "PORT": "0", "API_ORIGIN": api_origin,
           "ASSETS_ARCHIVE": str(seed_archive(tmp / "assets-archive"))}
    proc = subprocess.Popen([node, "server.js"], cwd=FRONTEND, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    lines: Queue[str | None] = Queue()
    stderr: list[str] = []

    def pump_stdout() -> None:
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threads = [threading.Thread(target=pump_stdout, daemon=True),
               threading.Thread(target=lambda: stderr.extend(proc.stderr), daemon=True)]
    for t in threads:
        t.start()
    try:
        first = lines.get(timeout=30) or ""
        match = re.search(r"^listening on (http://127\.0\.0\.1:\d+)$", first.strip())
        assert match, f"前端没起来：{first}{''.join(stderr)}"
        with httpx.Client(base_url=match[1], timeout=30) as http:
            yield Web(http, tmp / "assets-archive", stderr)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            code = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        for t in threads:
            t.join(5)
    assert code == 0 and (stderr == [] or not quiet), (code, "".join(stderr))
