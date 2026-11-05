"""LLM 层（mock）：请求形态、参数降级、重试、JSON 截取、正文裁剪与提示词组装。"""

import json

import httpx
import pytest

from app.config import load_settings
from app.extract.llm import (MAX_POST_ATTEMPTS, MAX_TEXT_CHARS, SYSTEM_PROMPT, LlmClient, LlmError, LlmState,
                             build_messages, first_json_object, fit_text, llm_after, llm_due, llm_hash)
from app.models import LlmStatus
from app.textutil import Link

LLM_ENV = {"LLM_BASE_URL": "https://llm.example.com/v1/", "LLM_API_KEY": "sk-test", "LLM_MODEL": "some-model"}


def chat(content: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"choices": [{"message": {"role": "assistant", "content": content}}],
                                        "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


class Server:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.bodies: list[dict] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        self.headers.append(request.headers)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(server, sleeps=None):
    return LlmClient(load_settings(LLM_ENV, env_file=None), transport=httpx.MockTransport(server),
                     sleep=(sleeps.append if sleeps is not None else lambda s: None))


MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def test_disabled_without_full_config():
    with pytest.raises(LlmError, match="未配置"):
        LlmClient(load_settings({"LLM_BASE_URL": "https://x/v1", "LLM_MODEL": "m"}, env_file=None))


def test_request_shape_and_parsing():
    server = Server(chat('{"vendor": "VM6", "plans": []}'))
    resp = client(server).complete(MESSAGES)
    assert resp.data == {"vendor": "VM6", "plans": []}
    body = server.bodies[0]
    assert body["model"] == "some-model" and body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert server.headers[0]["authorization"] == "Bearer sk-test"
    assert resp.usage["prompt_tokens"] == 10


def test_unsupported_response_format_and_temperature_are_dropped_and_remembered():
    server = Server(
        httpx.Response(400, json={"error": {"message": "Unknown parameter: response_format"}}),
        httpx.Response(400, json={"error": {"message": "temperature is not supported for this model"}}),
        chat('Here you go:\n```json\n{"vendor": "VM6"}\n```'),
        chat('{"vendor": "X"}'),
    )
    llm = client(server)
    assert llm.complete(MESSAGES).data == {"vendor": "VM6"}
    assert "response_format" in server.bodies[0] and "temperature" in server.bodies[1]
    assert "response_format" not in server.bodies[2] and "temperature" not in server.bodies[2]
    llm.complete(MESSAGES)  # 后续请求不再发送这两项
    assert "response_format" not in server.bodies[3] and "temperature" not in server.bodies[3]
    assert not llm.json_mode and not llm.send_temperature


def test_transient_failure_retried_once():
    sleeps = []
    server = Server(httpx.Response(503, text="overloaded"), chat('{"ok": true}'))
    assert client(server, sleeps).complete(MESSAGES).data == {"ok": True}
    assert sleeps == [5]


@pytest.mark.parametrize("responses, post_specific", [
    ([httpx.Response(500), httpx.Response(502)], False),
    ([httpx.ConnectError("down"), httpx.ConnectError("down")], False),
    ([httpx.Response(401, text="bad key"), httpx.Response(401, text="bad key")], False),
    ([httpx.Response(429), httpx.Response(429)], False),
    ([chat("sorry, I cannot"), chat("still no json")], True),
    ([httpx.Response(400, text="context length exceeded")] * 2, True),
    ([httpx.Response(200, json={"error": "weird"})] * 2, True),
])
def test_gives_up_after_one_retry(responses, post_specific):
    server = Server(*responses)
    with pytest.raises(LlmError) as info:
        client(server).complete(MESSAGES)
    assert len(server.bodies) == 2 and info.value.post_specific is post_specific


@pytest.mark.parametrize("content, expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": {"b": [1, 2]}}\n```', {"a": {"b": [1, 2]}}),
    ('Sure! {"a": "x { not json"} trailing {"b": 2}', {"a": "x { not json"}),
    ('[1, 2] then {"a": 1}', {"a": 1}),
])
def test_first_json_object(content, expected):
    assert first_json_object(content) == expected


def test_first_json_object_rejects_non_json():
    with pytest.raises(LlmError):
        first_json_object("no braces here")


def test_fit_text_keeps_lead_and_price_lines():
    filler = "\n".join(f"Paragraph {i} about our network and support quality." for i in range(600))
    text = f"Intro line\n{filler}\nKVM-9 | 4 GB RAM | 40 GB NVMe\nPrice: $9.99/mo\n{filler}"
    out, trimmed = fit_text(text)
    assert trimmed and len(out) <= MAX_TEXT_CHARS + 200
    assert out.startswith("Intro line") and "KVM-9 | 4 GB RAM | 40 GB NVMe\nPrice: $9.99/mo" in out
    assert "…" in out
    assert fit_text("short") == ("short", False)


def test_build_messages_includes_text_links_and_hint():
    msgs = build_messages("lowendtalk", "2026-10-30T18:17:23Z", "BLACK FRIDAY | UK VPS",
                          "Order [1]", [Link(1, "Order", "https://vm6.co.uk/x")], {"vendor": "VM6"})
    assert msgs[0] == {"role": "system", "content": SYSTEM_PROMPT}
    user = msgs[1]["content"]
    assert "BLACK FRIDAY | UK VPS" in user and "[1] Order → https://vm6.co.uk/x" in user and '"vendor": "VM6"' in user
    assert '"order_link_index"' in SYSTEM_PROMPT and "mixed" in SYSTEM_PROMPT and "price_note" in SYSTEM_PROMPT


def test_llm_status_state_machine():
    h1, h2 = "a" * 64, "b" * 64
    fresh = LlmState()
    assert llm_due(fresh, h1, enabled=False).status is LlmStatus.SKIPPED
    due = llm_due(fresh, h1, enabled=True)
    assert due.status is LlmStatus.PENDING

    done = llm_after(due, h1)
    assert (done.status, done.attempts, done.hash) == (LlmStatus.DONE, 1, llm_hash(h1))
    assert llm_due(done, h1, enabled=True) == done  # 同一内容不再调用
    assert llm_due(done, h1, enabled=False) == done  # 关掉 LLM 也保留已抽取结果
    assert llm_due(done, h2, enabled=True) == LlmState(LlmStatus.PENDING, 0, None)  # 原帖编辑后重抽

    outage = llm_after(due, h1, LlmError("HTTP 503"))
    assert (outage.status, outage.attempts) == (LlmStatus.FAILED, 0)  # 服务故障不计入帖子的失败次数
    assert llm_due(outage, h1, enabled=True).status is LlmStatus.PENDING

    state = due
    for _ in range(MAX_POST_ATTEMPTS):
        state = llm_after(llm_due(state, h1, enabled=True), h1, LlmError("bad json", post_specific=True))
    assert (state.status, state.attempts) == (LlmStatus.FAILED, MAX_POST_ATTEMPTS)
    assert llm_due(state, h1, enabled=True) == state  # 自动轮次不再调用
    assert llm_due(state, h1, enabled=True, backfill=True).status is LlmStatus.PENDING  # llm-backfill 仍会再试
    assert llm_due(state, h2, enabled=True).status is LlmStatus.PENDING  # 内容变了重新计数


def test_llm_hash_tracks_prompt_version(monkeypatch):
    import app.extract.llm as llm
    before = llm_hash("c")
    monkeypatch.setattr(llm, "PROMPT_VERSION", "999")
    assert llm.llm_hash("c") != before
