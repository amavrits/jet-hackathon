"""Smoke test of the demo flow through the Streamlit UI."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from growth.data import seed

APP = str(Path(__file__).resolve().parents[1] / "app" / "main.py")


@pytest.fixture(autouse=True)
def own_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The UI test applies changes, so it gets a database of its own."""
    path = tmp_path / "jet.duckdb"
    monkeypatch.setenv("JET_DB_PATH", str(path))
    seed.seed(path)


def _button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def test_demo_flow_analyse_approve_apply():
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    at.sidebar.selectbox[0].set_value("r2").run()
    assert at.title[0].value == "Sakura Sushi"

    _button(at, "Analyse").click().run()
    assert not at.exception
    assert any(s.value.startswith("Recommendations (2)") for s in at.subheader)

    at.session_state["decision-pricing-rolls"] = "Approve"
    at.session_state["decision-ads-start"] = "Reject"
    _button(at, "Apply decisions").click().run()
    assert not at.exception
    assert any(s.value == "Listing changes" for s in at.subheader)
    assert any(s.value == "Summary" for s in at.subheader)
