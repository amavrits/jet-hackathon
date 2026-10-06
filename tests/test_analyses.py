"""Stored analyses: one Jev Choice per variant group, one Noul per standalone card, both selections stored."""

import json

import pytest

from growth import analyses, llm
from growth.agents import reviews
from growth.data import queries as q
from growth.graph import build_graph

SEEN: list[tuple[dict, dict]] = []


def fake_jev(items):
    """Pick the first listed option of every group; propose standalone cards that leave the owner better off."""
    out = []
    for state, questions in items:
        SEEN.append((state, questions))
        if "pick" in questions:
            labels = list(questions["pick"].criteria)
            probs = {label: (0.7 if i == 0 else 0.3 / (len(labels) - 1)) for i, label in enumerate(labels)}
            out.append({"pick": {"type": "choice", "choice": labels[0], "confidence": 0.7, "probabilities": probs}})
        else:
            better = state["proposal"].get("owner_better_off", False)
            out.append({"propose": {"type": "noul", "noul": 0.8 if better else 0.2}})
    return out


@pytest.fixture
def setup(writable_db, monkeypatch):
    SEEN.clear()
    monkeypatch.setattr(llm, "call_structured", lambda *a, **k: None)
    monkeypatch.setattr(reviews, "ask_many", lambda items: [None] * len(items))
    monkeypatch.setattr(analyses, "ask_many", fake_jev)
    con = q.connect(writable_db)
    yield con, build_graph(writable_db)
    con.close()


def test_analysis_stores_both_selections(setup):
    con, graph = setup
    aid = analyses.analyse(con, graph, "r_spice_route")
    a = analyses.latest(con, "r_spice_route")
    assert a["analysis_id"] == aid
    recs = a["recommendations"]
    assert {r["kind"] for r in recs} == {"price_cut", "slot_promo"}
    assert a["n_recommendations"] == len(recs) == 4  # 3 price variants + promo

    price = [r for r in recs if r["kind"] == "price_cut"]
    assert all(r["jev_question"] == "choice" for r in price)
    assert sum(r["jev_selected"] for r in price) == 1  # one pick per group
    assert sum(r["shown_rank"] is not None for r in price) == 1  # owner sees one variant
    assert all(r["value_source"] == "model" and r["owner_profit_lo"] is not None for r in price)

    (promo,) = [r for r in recs if r["kind"] == "slot_promo"]
    assert promo["jev_question"] == "noul" and promo["jev_probability"] in (0.8, 0.2)
    assert a["n_agree"] == sum(r["jev_selected"] == r["optimizer_selected"] for r in recs)
    assert a["n_optimizer_selected"] == sum(r["optimizer_selected"] for r in recs)
    plan = json.loads(a["plan"])
    assert "chosen" in plan and "binding_constraints" in plan


def test_jev_gets_computed_facts_not_raw_formulas(setup):
    con, graph = setup
    analyses.analyse(con, graph, "r_spice_route")
    choice_state = next(s for s, qs in SEEN if "pick" in qs)
    assert choice_state["restaurant"]["positioning"] == "mid-range"
    opt = next(iter(choice_state["options"].values()))
    assert {"owner_profit_per_week_eur", "owner_better_off", "owner_better_off_even_in_worst_case"} <= set(opt)
    assert "formula" not in json.dumps(choice_state)
    criteria = next(qs for _, qs in SEEN if "pick" in qs)["pick"].criteria
    assert analyses.NONE in criteria


def test_no_answers_means_nothing_selected(setup, monkeypatch):
    con, graph = setup
    monkeypatch.setattr(analyses, "ask_many", lambda items: [None] * len(items))
    analyses.analyse(con, graph, "r_sushi_zen")
    a = analyses.latest(con, "r_sushi_zen")
    assert a["n_jev_selected"] == 0
    assert all(r["jev_probability"] is None for r in a["recommendations"])


def test_restaurant_without_cards(setup):
    con, graph = setup
    analyses.analyse(con, graph, "r_petit_bistro")
    a = analyses.latest(con, "r_petit_bistro")
    assert a["n_recommendations"] == 0 and a["recommendations"] == []
