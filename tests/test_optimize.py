"""The optimiser finds the best plan under constraints and respects rejections."""

import pytest

from growth import llm
from growth.agents import impact, menu, pricing, promo_ads
from growth.ml import optimize as opt


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.setattr(llm, "call_structured", lambda *a, **k: None)


@pytest.fixture(scope="module")
def options(con, effects):
    recs = [r for m in (menu, pricing, promo_ads) for r in m.run(con, "r_spice_route")]
    return opt.build_options(con, "r_spice_route", impact.run(recs), effects=effects)


def test_options_cover_recommendations_and_grids(options):
    groups = {o.group for o in options}
    assert "ads" in groups and "pricing-r_spice_route-mains" in groups
    assert not any(o.recommendation_id and "photos" in o.recommendation_id for o in options)  # spice route has photos
    assert len([o for o in options if o.group == "pricing-r_spice_route-mains"]) == 3  # 15/10/5% target variants
    assert all(o.jet_revenue_lo <= o.jet_revenue_eur_per_week for o in options if o.source == "model")


def test_plan_is_best_feasible_set(con, options, effects):
    plan = opt.optimize_plan(con, "r_spice_route", options=options, max_actions=2, effects=effects)
    assert 1 <= len(plan.chosen) <= 2
    assert len({o.group for o in plan.chosen}) == len(plan.chosen)
    assert plan.owner_profit_eur_per_week >= 0
    # brute force over all feasible pairs/singles gives the same additive value
    best = 0.0
    feas = [o for o in options]
    for i, a in enumerate(feas):
        if a.owner_profit_eur_per_week >= 0:
            best = max(best, a.jet_revenue_eur_per_week)
        for b in feas[i + 1 :]:
            if (
                a.group != b.group
                and a.owner_profit_eur_per_week + b.owner_profit_eur_per_week >= 0
                and a.ad_spend_eur_per_week + b.ad_spend_eur_per_week <= 300
            ):
                best = max(best, a.jet_revenue_eur_per_week + b.jet_revenue_eur_per_week)
    assert plan.additive_jet_revenue_eur_per_week == pytest.approx(best, abs=0.05)
    assert plan.joint is not None


def test_owner_better_off_blocks_ads_that_only_pay_jet(con, options, effects):
    """An ads-only option that loses the owner money is never chosen on its own."""
    losing = [o for o in options if o.group == "ads" and o.owner_profit_eur_per_week < 0]
    plan = opt.optimize_plan(con, "r_spice_route", options=losing, max_actions=1, effects=effects)
    assert plan.chosen == []


def test_rejected_option_never_comes_back(con, options, effects):
    plan = opt.optimize_plan(con, "r_spice_route", options=options, max_actions=3, effects=effects)
    assert plan.chosen
    victim = plan.chosen[0]
    again = opt.optimize_plan(
        con, "r_spice_route", options=options, max_actions=3, exclude=[victim.group], effects=effects
    )
    assert all(o.group != victim.group for o in again.chosen)
    assert victim.group in again.excluded


def test_ad_cap_and_action_cap_bind(con, options, effects):
    plan = opt.optimize_plan(
        con, "r_spice_route", options=options, max_actions=1, ad_budget_cap_eur=0.0, effects=effects
    )
    assert all(o.ad_spend_eur_per_week <= 0 for o in plan.chosen)
    assert len(plan.chosen) <= 1


def test_robust_plan_is_no_better_than_the_point_plan(con, options, effects):
    point = opt.optimize_plan(con, "r_spice_route", options=options, effects=effects)
    robust = opt.optimize_plan(con, "r_spice_route", options=options, robust=True, effects=effects)
    assert sum(o.jet_revenue_lo for o in robust.chosen) <= sum(o.jet_revenue_eur_per_week for o in point.chosen)


def test_portfolio_respects_budget_and_hours(con, effects):
    rids = ["r_bella_napoli", "r_spice_route", "r_sushi_zen"]
    pf = opt.optimize_portfolio(con, rids, ad_budget_eur_per_week=100.0, hours=2.0, effects=effects)
    assert pf.ad_spend_eur_per_week <= 100.0 + 1e-6
    assert pf.hours_used <= 2.0 + 1e-6
    assert len({a.restaurant_id for a in pf.allocations}) == len(pf.allocations)
    assert pf.jet_revenue_eur_per_week == pytest.approx(
        sum(a.plan.jet_revenue_eur_per_week for a in pf.allocations), abs=0.05
    )
    bigger = opt.optimize_portfolio(con, rids, ad_budget_eur_per_week=1000.0, hours=10.0, effects=effects)
    assert bigger.jet_revenue_eur_per_week >= pf.jet_revenue_eur_per_week
