"""One test per agent on a fixture restaurant, asserting the planted problem is found."""

from growth.agents import impact, menu, pricing, promo_ads, reviews
from growth.graph import build_context


def test_reviews_finds_soggy_calzone():
    recs = reviews.run(build_context("r1"))
    assert [r.title for r in recs] == ["Fix 'soggy' complaints on Calzone"]
    assert len(recs[0].evidence) >= 5


def test_menu_finds_bestsellers_without_photos_and_descriptions():
    recs = {r.id: r for r in menu.run(build_context("r3"))}
    assert set(recs) == {"menu-photos", "menu-descriptions"}
    assert recs["menu-photos"].patch is None  # owner uploads photos


def test_pricing_finds_overpriced_rolls():
    ctx = build_context("r2")
    recs = pricing.run(ctx)
    assert [r.id for r in recs] == ["pricing-rolls"]
    basis = recs[0].impact_basis
    assert all(basis["new_prices"][i] < basis["old_prices"][i] for i in basis["old_prices"])


def test_promo_finds_tuesday_afternoon_dead_slot():
    recs = promo_ads.run(build_context("r4"))
    assert [r.id for r in recs] == ["promo-tuesday-afternoon"]


def test_ads_pauses_campaign_that_does_not_pay_back():
    recs = promo_ads.run(build_context("r5"))
    assert [r.id for r in recs] == ["ads-pause-2"]


def test_control_restaurant_gets_no_recommendations():
    ctx = build_context("r6")
    assert [r for agent in (reviews, menu, pricing, promo_ads) for r in agent.run(ctx)] == []


def test_impact_formula_is_reproducible():
    ctx = build_context("r1")
    rec = reviews.run(ctx)[0]
    est = impact.estimate(rec, ctx)
    calzone = next(m for m in ctx.menu if m.name == "Calzone")
    expected_orders = calzone.orders_per_week * impact.COMPLAINT_FIX_RECOVERY
    assert est.orders_per_week == round(expected_orders, 2)
    assert est.jet_revenue_eur_per_week == round(
        expected_orders * ctx.kpis.avg_basket_eur * ctx.restaurant.commission_rate, 2
    )
