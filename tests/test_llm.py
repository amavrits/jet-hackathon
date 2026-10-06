"""llm.call_structured() contract with a fake LiteLLM completion (no network, no key)."""

from types import SimpleNamespace

from pydantic import BaseModel

from growth import llm


class Tag(BaseModel):
    theme: str
    severity: int


def _reply(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _fake(*replies):
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        r = replies[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return _reply(r)

    return completion, calls


def test_valid_first_try_uses_schema_mode():
    fake, calls = _fake('{"theme": "soggy", "severity": 3}')
    assert llm.call_structured(Tag, "sys", "user", completion=fake) == Tag(theme="soggy", severity=3)
    assert calls[0]["response_format"] is Tag
    assert calls[0]["model"] == llm.model_name()


def test_fenced_json_is_accepted():
    fake, _ = _fake('Sure!\n```json\n{"theme": "cold", "severity": 2}\n```')
    assert llm.call_structured(Tag, "s", "u", completion=fake).theme == "cold"


def test_retries_once_with_error_fed_back():
    fake, calls = _fake('{"theme": "soggy"}', '{"theme": "soggy", "severity": 1}')
    assert llm.call_structured(Tag, "s", "u", completion=fake).severity == 1
    assert "Validation failed" in calls[1]["messages"][-1]["content"]
    assert "response_format" not in calls[1]


def test_gives_up_after_two_invalid_attempts():
    fake, calls = _fake("nope", "still nope")
    assert llm.call_structured(Tag, "s", "u", completion=fake) is None
    assert len(calls) == 2


def test_call_failure_returns_none():
    fake, _ = _fake(RuntimeError("AuthenticationError: no key"))
    assert llm.call_structured(Tag, "s", "u", completion=fake) is None


def test_default_model_follows_available_key(monkeypatch):
    monkeypatch.delenv("MODEL_MAIN", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert llm.model_name() == llm.OPENAI_DEFAULT
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    assert llm.model_name() == llm.ANTHROPIC_DEFAULT
    monkeypatch.setenv("MODEL_MAIN", "openai/other")
    assert llm.model_name() == "openai/other"
