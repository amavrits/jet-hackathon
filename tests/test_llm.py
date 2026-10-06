"""llm.structured() contract, with litellm mocked so no network or key is needed."""

from types import SimpleNamespace

from pydantic import BaseModel

from growth import llm


class Tag(BaseModel):
    theme: str
    severity: int


def _reply(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _fake(responses):
    """Return a litellm.completion stand-in that replays `responses` in order."""
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return _reply(responses[len(calls) - 1])

    return completion, calls


def test_valid_first_try(monkeypatch):
    fake, calls = _fake(['{"theme": "soggy", "severity": 3}'])
    monkeypatch.setattr(llm.litellm, "completion", fake)
    out = llm.structured(Tag, "sys", "user", model="m")
    assert out == Tag(theme="soggy", severity=3)
    assert len(calls) == 1
    assert calls[0]["response_format"] is Tag
    assert calls[0]["model"] == "m"


def test_fenced_json_is_accepted(monkeypatch):
    fake, _ = _fake(['Sure!\n```json\n{"theme": "cold", "severity": 2}\n```'])
    monkeypatch.setattr(llm.litellm, "completion", fake)
    assert llm.structured(Tag, "s", "u", model="m").theme == "cold"


def test_retries_once_with_error_feedback(monkeypatch):
    fake, calls = _fake(['{"theme": "soggy"}', '{"theme": "soggy", "severity": 1}'])
    monkeypatch.setattr(llm.litellm, "completion", fake)
    out = llm.structured(Tag, "s", "u", model="m")
    assert out.severity == 1
    assert len(calls) == 2
    assert "not valid" in calls[1]["messages"][-1]["content"]
    assert "response_format" not in calls[1]


def test_gives_up_after_two_failures(monkeypatch):
    fake, calls = _fake(["nope", "still nope"])
    monkeypatch.setattr(llm.litellm, "completion", fake)
    assert llm.structured(Tag, "s", "u", model="m") is None
    assert len(calls) == 2


def test_provider_error_returns_none(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(llm.litellm, "completion", boom)
    assert llm.structured(Tag, "s", "u", model="m") is None


def test_structured_many_preserves_order(monkeypatch):
    def completion(**kwargs):
        n = kwargs["messages"][-1]["content"]
        return _reply(f'{{"theme": "t{n}", "severity": {n}}}')

    monkeypatch.setattr(llm.litellm, "completion", completion)
    out = llm.structured_many(Tag, "s", ["1", "2", "3"], model="m", workers=3)
    assert [t.severity for t in out] == [1, 2, 3]
