"""lever_changes and variants: the contract between the agents and growth/ml + the optimiser."""

import pytest

from growth import llm
from growth.agents import menu, pricing, promo_ads, reviews
from growth.data import queries as q
from growth.data.seed import PLANTED
from growth.state import Recommendation

MODEL_KINDS = {
    "add_photos": {"photo_share"},
    "add_descriptions": {"description_share"},
    "price_cut": {"price_index"},
    "slot_promo": {"promo_active"},
    "ad_daypart": {"ad_offpeak_share", "ad_budget_eur"},
}
SHARE_LEVERS = {"photo_share", "description_share", "promo_active", "ad_offpeak_share"}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(llm, "call_structured", lambda *a, **k: None)
    monkeypatch.setattr(reviews, "ask_many", lambda items: [None] * len(items))


def _all(con, rid) -> list[Recommendation]:
    return [r for m in (menu, pricing, promo_ads, reviews) for r in m.run(con, rid)]


@pytest.mark.parametrize("rid", list(PLANTED))
def test_lever_changes_are_consistent_with_history(con, rid):
    current = q.current_levers(con, rid)
    for r in _all(con, rid):
        levers = {lc.lever for lc in r.lever_changes}
        if r.kind in MODEL_KINDS:
            assert levers and levers <= MODEL_KINDS[r.kind], (r.id, levers)
        else:
            assert not levers, r.id  # no lever in the model: impact stays heuristic
        for lc in r.lever_changes:
            assert lc.before == current[lc.lever], (r.id, lc)
            assert lc.after != lc.before, (r.id, lc)
            if lc.lever in SHARE_LEVERS:
                assert 0.0 <= lc.after <= 1.0
            if lc.lever == "price_index":
                assert 0.5 < lc.after < lc.before  # these are cuts
            if lc.lever == "ad_budget_eur":
                assert lc.after > 0


@pytest.mark.parametrize("rid", list(PLANTED))
def test_variants_are_real_alternatives(con, rid):
    recs = _all(con, rid)
    assert len({r.id for r in recs}) == len(recs)
    groups: dict[str, list[Recommendation]] = {}
    for r in recs:
        if r.variant_group:
            groups.setdefault(r.variant_group, []).append(r)
    for group, variants in groups.items():
        assert len(variants) >= 2, group
        assert len({r.kind for r in variants}) == 1
        assert all(r.variant_label for r in variants)
        assert len({str(r.patch_ops()) for r in variants}) == len(variants), "duplicate variant"


def test_deeper_price_cut_means_lower_price_index(con):
    variants = [r for r in pricing.run(con, "r_spice_route")]
    by_target = sorted(variants, key=lambda r: -r.facts["target_premium_pct"])  # 15% -> 5%
    afters = [r.lever_changes[0].after for r in by_target]
    assert afters == sorted(afters, reverse=True) and len(afters) == 3
