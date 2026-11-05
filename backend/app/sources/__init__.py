"""采集源注册表。新增源：新建模块、在类上加 @register，并在下面导入。"""

from collections.abc import Iterable

from ..config import Settings
from ..http import HttpClient
from .base import REGISTRY, FetchResult, Source, register
from . import lowendbox, lowendspirit, lowendtalk, nodeseek, reddit  # noqa: F401  导入即注册


def build_sources(settings: Settings, http: HttpClient, only: Iterable[str] | None = None,
                  **kwargs) -> list[Source]:
    """按 SOURCES 配置的顺序实例化启用的源；only 进一步限定（CLI --source）。"""
    wanted = list(only) if only else list(settings.sources)
    if unknown := [s for s in wanted if s not in REGISTRY]:
        raise ValueError(f"未知的采集源：{unknown}")
    return [REGISTRY[s](settings, http, **kwargs) for s in wanted]


__all__ = ["REGISTRY", "FetchResult", "Source", "build_sources", "register"]
