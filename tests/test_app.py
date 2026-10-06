"""Smoke test of the demo flow through the Streamlit UI, on the real backend. No network."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from growth import chat, llm
from growth.agents import reviews
from runners.generate_data import build

APP = str(Path(__file__).resolve().parents[1] / "app" / "main.py")


@pytest.fixture(autouse=True)
def own_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The UI test applies changes, so it gets a database of its own. No LLM, Jev or chat model calls."""
    path = tmp_path / "jet.duckdb"
    monkeypatch.setenv("JET_DB_PATH", str(path))
    build(path, tmp_path / "true_model.json", verbose=False)
    monkeypatch.setattr(llm, "call_structured", lambda *a, **k: None)
    monkeypatch.setattr(reviews, "ask_many", lambda items: [None] * len(items))

    def fake_turn(model, messages, tools):
        text = "Your Mains cost more than the same dishes nearby."
        yield text
        yield chat.TurnResult(content=text, tool_calls=[], finish_reason="stop")

    monkeypatch.setattr(chat, "stream_turn", fake_turn)


def _button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def _analyse(at: AppTest, rid: str) -> AppTest:
    at.sidebar.selectbox[0].set_value(rid).run()
    _button(at, "Analyse").click().run()
    assert not at.exception, at.exception
    return at


def test_demo_flow_analyse_approve_apply():
    at = AppTest.from_file(APP, default_timeout=120).run()
    assert not at.exception, at.exception
    at = _analyse(at, "r_spice_route")
    assert at.title[0].value == "Spice Route"
    assert any(s.value == "Recommendations (2)" for s in at.subheader)

    ranked = at.session_state["run_ranked"]
    price = next(r for r in ranked if r.kind == "price_cut")
    promo = next(r for r in ranked if r.kind == "slot_promo")
    at.session_state[f"decision-{price.id}"] = "Approve"
    at.session_state[f"decision-{promo.id}"] = "Reject"
    at.session_state[f"reason-{promo.id}"] = "Closed Tuesday afternoons"
    _button(at, "Apply decisions").click().run()
    assert not at.exception, at.exception
    assert any(s.value == "Listing changes" for s in at.subheader)
    assert any(s.value == "Summary" for s in at.subheader)
    summary = at.session_state["run_summary"]
    assert (summary.approved, summary.rejected) == (1, 1)
    assert summary.total.partner_cost_eur_per_week > 0  # the price cut costs the owner margin

    _button(at, "Reset demo").click().run()
    assert not at.exception, at.exception


def test_chat_answers_about_a_card():
    at = _analyse(AppTest.from_file(APP, default_timeout=120).run(), "r_spice_route")
    at.chat_input[0].set_value("Why cut my mains?").run()
    assert not at.exception, at.exception
    messages = at.session_state["run_chat"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert "cost more" in messages[1]["content"]

    card = at.session_state["run_ranked"][0]
    _button(at, "💬 Ask why").click().run()  # first card's button
    assert at.session_state["run_chat_focus"] == card.id
    assert len(at.session_state["run_chat"]) == 4
