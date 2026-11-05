"""FastAPI 应用：生命周期里启停调度器，安全响应头（第 15.4 节），内存缓存（第 15.5 节）。

后端只出数据（D60）：JSON 接口、RSS、sitemap、robots、分享图与给前端的内部页面数据；页面全由前端渲染，
出错也一律给 JSON（{error, message}）。
"""

from contextlib import asynccontextmanager

from anyio import to_thread
from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException

from ..config import Settings
from ..db import Database
from ..scheduler import Scheduler
from ..vendors import prepare
from . import api, feed, internal, og, sitemap
from .cache import PageCache

# 后端不出 HTML，CSP 只作兜底：哪个响应被当成页面打开，也不加载、不执行任何东西
CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def create_app(settings: Settings, *, scheduler: Scheduler | None = None) -> FastAPI:
    """scheduler 为 None 时只提供数据（开发与测试）；serve 默认带调度器。"""
    db = Database(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        prepare(db)  # 迁移并同步商家别名表
        if scheduler:
            scheduler.start()
        yield
        if scheduler:
            await to_thread.run_sync(scheduler.shutdown)  # 等进行中的任务结束，不堵事件循环

    app = FastAPI(lifespan=lifespan, title=f"{settings.site_name_en} API", version="1", docs_url=None,
                  redoc_url=None, openapi_url="/api/openapi.json")
    app.state.settings, app.state.db, app.state.scheduler = settings, db, scheduler
    app.state.cache = PageCache()
    if scheduler:
        scheduler.add_listener(app.state.cache.clear)  # 每轮采集、每批 LLM、汇率更新后失效

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        """未知路径、方法不对等 HTTP 错误都用接口的 {error, message}。"""
        return api.http_error(exc.status_code, str(exc.detail), headers=exc.headers)

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception):
        return api.error(500, "internal", "服务器出错，请稍后再试")

    app.include_router(api.router)
    app.include_router(internal.router)
    app.include_router(feed.router)
    app.include_router(sitemap.router)
    app.include_router(og.router)
    return app
