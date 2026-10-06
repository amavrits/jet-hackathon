"""Effects recover the truth, peers make sense, predictions are coherent.

These are the only tests allowed to read data/true_model.json.
"""

import math

import pytest

from growth.ml import effects as fx
from growth.ml import peers, predict

TOL = {"photo": 0.03, "description": 0.03, "promo": 0.03, "elasticity_1": 0.4, "elasticity_2": 0.4, "elasticity_3": 0.4}


def test_two_way_fe_recovers_true_effects(effects, truth):
    true = {
        "photo": truth["photo"],
        "description": truth["description"],
        "promo": truth["promo"],
        **{f"elasticity_{k}": v for k, v in truth["elasticity"].items()},
    }
    for lever, tol in TOL.items():
        est = effects["levers"][lever]["coef"]
        assert abs(est - true[lever]) <= tol, (lever, est, true[lever])


def test_naive_estimate_is_wrong_where_adoption_is_confounded(effects, truth):
    """Big restaurants add photos more often, so pooled OLS doubles the photo effect."""
    assert effects["naive"]["photo"] > truth["photo"] + 0.08
    assert abs(effects["levers"]["photo"]["coef"] - truth["photo"]) < abs(effects["naive"]["photo"] - truth["photo"])


def test_intervals_bracket_the_point_and_carry_evidence_counts(effects):
    for lever, p in effects["levers"].items():
        assert p["lo"] <= p["coef"] <= p["hi"], lever
    assert effects["levers"]["photo"]["n_events"] > 20
    assert effects["ads"]["n_restaurants"] >= 10
    assert 0.5 <= effects["ads"]["kappa"]["coef"] <= 3.0


def test_basket_model_recovers_pass_through_and_promo_discount(effects, truth):
    assert abs(effects["basket"]["price_passthrough"]["coef"] - 1.0) < 0.1
    assert abs(effects["basket"]["promo"]["coef"] + truth["promo_discount"] * truth["promo_order_share"]) < 0.02


def test_effects_cache_roundtrip(con, tmp_path, effects):
    path = tmp_path / "effects.json"
    first = fx.load(con, path)
    assert path.exists()
    again = fx.load(con, path)
    assert again["levers"] == first["levers"]
    assert {r["lever"] for r in fx.summary_table(first)} >= set(fx.ORGANIC_LEVERS) | {"kappa", "offpeak_boost"}


def test_peers_are_near_same_cuisine_and_similar(con):
    ps = peers.get_peers(con, "r_bella_napoli", 5)
    assert len(ps) == 5
    assert all(p["cuisine"] == "Pizza" for p in ps)
    assert all(p["distance_km"] < 3.0 for p in ps)
    assert [p["score"] for p in ps] == sorted(p["score"] for p in ps)
    assert "r_bella_napoli" not in {p["restaurant_id"] for p in ps}


def test_peer_weights_change_the_answer(con):
    far = peers.get_peers(
        con, "r_bella_napoli", 5, same_city=False, weights={**peers.WEIGHTS, "cuisine": 0.0, "distance": 0.0}
    )
    assert any(p["cuisine"] != "Pizza" for p in far)


def test_benchmark_and_segments(con):
    b = peers.benchmark(con, "r_bella_napoli")
    levers = {r["lever"]: r for r in b["levers"]}
    assert levers["photo_share"]["mine"] == pytest.approx(0.75, abs=0.01)  # 3 of 12 dishes have no photo
    seg = peers.segment_market(con, k=5)
    assert sum(s["n"] for s in seg["segments"]) == len(seg["labels"])
    assert all(seg["labels"][rid] == s["segment"] for s in seg["segments"] for rid in s["partners"])


def test_predict_no_change_is_zero_and_point_within_band(con, effects):
    p = predict.predict_impact(con, "r_bella_napoli", {}, effects=effects)
    assert p.orders_per_week.point == 0 and p.jet_revenue_eur_per_week.point == 0
    p = predict.predict_impact(con, "r_bella_napoli", {"photo_share": 1.0}, effects=effects)
    assert p.orders_per_week.lo <= p.orders_per_week.point <= p.orders_per_week.hi
    assert p.orders_per_week.point > 0 and p.jet_revenue_eur_per_week.point > 0
    assert "photo_share" in p.formula and p.evidence["photo_share"] > 0


def test_predict_matches_true_model_direction_and_size(con, effects, truth):
    """Full photo coverage from 0.75: true uplift is exp(0.12 * 0.25) - 1 on organic orders."""
    now = predict.current_levers(con, "r_bella_napoli")
    p = predict.predict_impact(con, "r_bella_napoli", {"photo_share": 1.0}, effects=effects)
    true_uplift = (math.exp(truth["photo"] * (1.0 - now["photo_share"])) - 1) * now["orders"]
    assert p.orders_per_week.lo <= true_uplift <= p.orders_per_week.hi


def test_price_cut_raises_orders_and_owner_sees_the_margin(con, effects):
    p = predict.predict_impact(con, "r_spice_route", {"price_index": 0.9}, effects=effects)
    assert p.orders_per_week.point > 0
    assert (
        p.gmv_eur_per_week.point
        < p.orders_per_week.point * predict.current_levers(con, "r_spice_route")["avg_basket_eur"]
    )


def test_new_ads_cost_the_owner_and_pay_jet(con, effects):
    p = predict.predict_impact(con, "r_bella_napoli", {"ad_budget_eur": 100.0}, effects=effects)
    assert p.ad_spend_eur_per_week == 100.0
    assert p.jet_revenue_eur_per_week.point > 100.0
    assert p.owner_profit_eur_per_week.point < p.jet_revenue_eur_per_week.point


def test_robust_uses_the_pessimistic_end(con, effects):
    p = predict.predict_impact(con, "r_bella_napoli", {"photo_share": 1.0}, effects=effects, robust=True)
    assert p.orders_per_week.point == p.orders_per_week.lo


def test_unknown_lever_rejected(con, effects):
    with pytest.raises(ValueError):
        predict.predict_impact(con, "r_bella_napoli", {"photos": 1.0}, effects=effects)
