"""llm.call_structured() contract, with a fake Anthropic client (no network, no key)."""

from types import SimpleNamespace

import anthropic
import httpx2
from pydantic import BaseModel

from growth import llm


class Tag(BaseModel):
    theme: str
    severity: int


def _tool_reply(payload: dict, block_id: str = "tu_1"):
    block = SimpleNamespace(type="tool_use", id=block_id, name=llm.TOOL_NAME, input=payload)
    return SimpleNamespace(content=[block], stop_reason="tool_use")


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_valid_first_try():
    fake = FakeClient([_tool_reply({"theme": "soggy", "severity": 3})])
    assert llm.call_structured(Tag, "sys", "user", llm=fake) == Tag(theme="soggy", severity=3)
    call = fake.calls[0]
    assert call["tool_choice"] == {"type": "auto"}  # forced tool choice is rejected by current models
    assert call["tools"][0]["name"] == llm.TOOL_NAME


def test_retries_once_with_validation_error_fed_back():
    fake = FakeClient([_tool_reply({"theme": "soggy"}), _tool_reply({"theme": "soggy", "severity": 1}, "tu_2")])
    assert llm.call_structured(Tag, "s", "u", llm=fake).severity == 1
    feedback = fake.calls[1]["messages"][-1]["content"][0]
    assert feedback["type"] == "tool_result" and feedback["is_error"]


def test_gives_up_after_two_invalid_attempts():
    fake = FakeClient([_tool_reply({"x": 1}), _tool_reply({"y": 2}, "tu_2")])
    assert llm.call_structured(Tag, "s", "u", llm=fake) is None


def test_api_error_returns_none_instead_of_raising():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.APIConnectionError(request=request)
    assert llm.call_structured(Tag, "s", "u", llm=FakeClient([err])) is None


def test_missing_credentials_returns_none():
    assert llm.call_structured(Tag, "s", "u", llm=FakeClient([TypeError("no auth")])) is None
