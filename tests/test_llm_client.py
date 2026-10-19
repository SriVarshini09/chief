"""The model client must work with hosts that stream by default (Cline's API does) and must never hang."""

from __future__ import annotations

from types import SimpleNamespace

from chief.config import Config
from chief.llm import LLM, REQUEST_TIMEOUT


class FakeCompletions:
    def __init__(self, message):
        self.message, self.calls = message, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=self.message)])


def llm_with(message):
    cfg = Config(mode="live", llm_base_url="https://api.cline.bot/api/v1", llm_model="deepseek/deepseek-chat", llm_api_key="k")
    llm = LLM(cfg)
    comp = FakeCompletions(message)
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
    return llm, comp


def test_complete_asks_for_a_single_non_streamed_reply_with_a_timeout():
    llm, comp = llm_with(SimpleNamespace(content=" hello ", tool_calls=None))
    assert llm.complete("sys", "user", "fallback") == "hello"
    sent = comp.calls[0]
    assert sent["stream"] is False and sent["timeout"] == REQUEST_TIMEOUT
    assert sent["model"] == "deepseek/deepseek-chat"


def test_tool_calls_are_non_streamed_too_and_parsed():
    call = SimpleNamespace(id="c1", function=SimpleNamespace(name="list_events", arguments="{}"))
    llm, comp = llm_with(SimpleNamespace(content=None, tool_calls=[call]))
    out = llm.chat_with_tools([{"role": "user", "content": "x"}], [{"type": "function", "function": {"name": "list_events"}}])
    assert out["tool_calls"][0]["function"]["name"] == "list_events"
    assert comp.calls[0]["stream"] is False and comp.calls[0]["timeout"] == REQUEST_TIMEOUT


def test_an_empty_reply_falls_back():
    llm, _ = llm_with(SimpleNamespace(content=None, tool_calls=None))  # e.g. a reasoning-only reply
    assert llm.complete("s", "u", "fallback text") == "fallback text"


def test_a_failing_host_falls_back_instead_of_raising():
    llm, comp = llm_with(SimpleNamespace(content="x", tool_calls=None))
    comp.create = lambda **_: (_ for _ in ()).throw(TimeoutError("slow"))
    assert llm.complete("s", "u", "fallback text") == "fallback text"
    assert llm.chat_with_tools([], []) is None


def test_mock_mode_never_calls_the_model():
    cfg = Config(mode="mock", llm_model="m", llm_api_key="k")
    assert LLM(cfg).complete("s", "u", "fb") == "fb"
