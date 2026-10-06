"""Menu, pricing and promo_ads agents find exactly the planted problems; impact covers every kind.

No network: the LLM is stubbed (menu falls back to template descriptions).
"""

import jsonpatch
import pytest

from growth import llm
from growth.agents import impact, menu, pricing, promo_ads
from growth.data import queries as q
from growth.data.seed import PLANTED


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.setattr(llm, "structured", lambda *a, **k: None)


RIDS = list(PLANTED)


@pytest.mark.parametrize("rid", RIDS)
def test_menu_finds_planted(con, rid):
    p = PLANTED[rid]
    recs = {r.kind: r for r in menu.run(con, rid)}
    assert ("add_photos" in recs) == bool(p.get("missing_photos"))
    assert ("add_descriptions" in recs) == bool(p.get("missing_descriptions"))
    assert ("prune_item" in recs) == ("dead_item" in p)
    if "dead_item" in p:
        assert p["dead_item"] in recs["prune_item"].title


@pytest.mark.parametrize("rid", RIDS)
def test_pricing_finds_planted(con, rid):
    recs = pricing.run(con, rid)
    expected = PLANTED[rid].get("overpriced_category")
    assert [r.title.split(" ")[0] for r in recs] == ([expected] if expected else [])
    for r in recs:
        for op in r.patch:
            assert op["path"].endswith("/price_eur")
        cut = r.facts["avg_price_cut_pct"]
        assert 0 < cut < 0.3


@pytest.mark.parametrize("rid", RIDS)
def test_promo_ads_finds_planted(con, rid):
    p = PLANTED[rid]
    kinds = sorted(r.kind for r in promo_ads.run(con, rid))
    expected = sorted((["slot_promo"] if p.get("dead_slot") else []) + (["ad_daypart"] if p.get("wasted_ads") else []))
    assert kinds == expected


def test_dead_slot_is_tuesday_afternoon(con):
    (rec,) = [r for r in promo_ads.run(con, "r_spice_route") if r.kind == "slot_promo"]
    promo = rec.patch[0]["value"]
    assert (promo["day"], promo["start"], promo["end"]) == ("tue", "14:00", "18:00")


def test_ad_daypart_targets_the_slow_slot(con):
    (rec,) = [r for r in promo_ads.run(con, "r_sushi_zen") if r.kind == "ad_daypart"]
    assert rec.patch[0]["value"] == [{"day": "tue", "start": "14:00", "end": "18:00"}]


@pytest.mark.parametrize("rid", RIDS)
def test_patches_apply_and_impact_is_filled(con, rid):
    recs = impact.run([r for m in (menu, pricing, promo_ads) for r in m.run(con, rid)])
    listing = q.get_listing(con, rid)
    for r in recs:
        assert r.evidence
        assert r.impact is not None and r.impact.formula
        assert r.impact.jet_revenue_eur_per_week >= 0, (r.kind, r.impact.formula)
        if r.patch:
            listing = jsonpatch.apply_patch(listing, r.patch)


def test_every_kind_has_an_estimator():
    kinds = {
        "dish_quality",
        "delivery_quality",
        "add_photos",
        "add_descriptions",
        "prune_item",
        "price_cut",
        "slot_promo",
        "ad_daypart",
    }
    assert kinds <= set(impact.ESTIMATORS)
