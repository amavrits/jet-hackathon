"""Reviews agent finds the planted problems and nothing else.

Offline tests replace Jev with an oracle built from the seed templates, so they check the
grouping and threshold logic without network. The live test calls Jev for real and runs only
when JEV_API_KEY is set (`pytest -m live`).
"""

import os

import pytest

from growth import llm
from growth.agents import reviews as R
from growth.data import seed
from growth.data.seed import PLANTED


def _oracle_answers(state: str, questions: dict) -> dict:
    """What a perfect classifier would say about a seeded review."""
    dishes = [d for d in questions["dish"].criteria if d != R.GENERAL]

    def ans(dish: str, issue: str, fault: str, p: float) -> dict:
        return {
            "is_complaint": {"type": "noul", "noul": p},
            "dish": {"type": "choice", "choice": dish, "confidence": 0.95},
            "issue": {"type": "choice", "choice": issue, "confidence": 0.9},
            "fault": {"type": "choice", "choice": fault, "confidence": 0.9},
        }

    for templates, issue, fault in ((seed.SOGGY, "texture", "packaging"), (seed.COLD, "temperature", "kitchen")):
        for tpl in templates:
            for d in dishes:
                if state == tpl.format(item=d):
                    return ans(d, issue, fault, 0.98)
    if state in seed.LATE:
        return ans(R.GENERAL, "late", "delivery", 0.98)
    if "late" in state.lower() or "delivery took" in state.lower():
        return ans(R.GENERAL, "late", "delivery", 0.9)
    return ans(R.GENERAL, "other", "unclear", 0.7)


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(R, "ask_many", lambda items: [_oracle_answers(s, q) for s, q in items])
    monkeypatch.setattr(llm, "structured", lambda *a, **k: None)


def _expected(rid: str) -> set[str]:
    p = PLANTED[rid]
    out = set()
    if "soggy_item" in p:
        out.add(f"dish_quality:{p['soggy_item']}")
    if "cold_item" in p:
        out.add(f"dish_quality:{p['cold_item']}")
    if p.get("late_delivery"):
        out.add("delivery_quality")
    return out


def _found(recs) -> set[str]:
    out = set()
    for r in recs:
        if r.kind == "dish_quality":
            dish = next(e.note.split(":")[0] for e in r.evidence if e.kind == "menu_item")
            out.add(f"dish_quality:{dish}")
        else:
            out.add(r.kind)
    return out


@pytest.mark.parametrize("rid", list(PLANTED))
def test_finds_exactly_the_planted_review_problems(con, offline, rid):
    recs = R.run(con, rid)
    assert _found(recs) == _expected(rid)
    for r in recs:
        assert r.agent == "reviews"
        assert r.patch is None
        assert all(e.ref.startswith(rid) for e in r.evidence if e.kind == "review")
        assert r.facts["complaints"] >= R.MIN_COMPLAINTS


def test_delivery_card_names_who_delivers(con, offline):
    (rec,) = R.run(con, "r_pho_house")
    assert rec.facts["jet_delivers"] == 1.0
    assert "JET" in rec.action


@pytest.mark.live
@pytest.mark.skipif(not (os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")), reason="no Jev key")
@pytest.mark.parametrize("rid", ["r_bella_napoli", "r_golden_wok", "r_pho_house", "r_petit_bistro"])
def test_live_jev_finds_planted(con, monkeypatch, rid):
    monkeypatch.setattr(llm, "structured", lambda *a, **k: None)
    assert _found(R.run(con, rid)) == _expected(rid)
