"""界面字典（《需求与方案说明》第 7 节）：后端的页面数据、RSS、分享图与前端都按键取词条，参数按名字填，枚举标签也在字典里。

中文 zh-CN.json、英文 en.json 键与占位符一一对应（机房译名 loc.* 只有中文，英文页照原文）。"""

import json
import logging
from collections.abc import Callable
from functools import cache
from pathlib import Path

log = logging.getLogger(__name__)

FILES = {"zh": "zh-CN.json", "en": "en.json"}


@cache
def strings(lang: str) -> dict[str, str]:
    return json.loads((Path(__file__).with_name(FILES[lang])).read_text(encoding="utf-8"))


def translator(lang: str) -> Callable[..., str]:
    table = strings(lang)

    def t(key: str, **params) -> str:
        text = table.get(key)
        if text is None:  # 缺词条不让整页报错：页面上露出键名，日志里提醒
            log.warning("字典 %s 缺少 %s", lang, key)
            return key
        return text.format(**params) if params else text

    return t
