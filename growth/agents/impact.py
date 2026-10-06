"""Impact estimates: simple, explainable heuristics. Never an LLM.

Each recommendation kind has one formula over the `facts` its agent attached. Every estimate
returns weekly incremental orders and GMV, and JET revenue = GMV x commission (+ any change in
ad spend, which JET also earns). The formula string is shown in the UI as-is.

The assumption constants below are the knobs to defend in the pitch. They are deliberately
conservative and in one place.
"""

from __future__ import annotations

from collections.abc import Callable

from growth.state import Impact, Recommendation

# --- assumptions -----------------------------------------------------------------------------
LOST_ORDERS_PER_COMPLAINT = 1.5  # future orders an unhappy customer skips, per complaint
PHOTO_UPLIFT = 0.08  # extra orders on a dish after adding a photo
DESCRIPTION_UPLIFT = 0.03  # extra orders on a dish after adding a description
PRICE_ELASTICITY = 1.5  # % more orders with that category per % price cut; competitors sit side by side
PROMO_GAP_CLOSED = 0.5  # share of the gap to a normal slot that the promo recovers
INCREMENTAL_PEAK = 0.2  # share of sponsored orders that are truly incremental at peak
INCREMENTAL_OFF_PEAK = 0.6  # ... and off-peak, where the restaurant isn't already busy


def _impact(
    orders: float, gmv: float, commission: float, formula: str, ad_spend: float = 0.0, partner_cost: float = 0.0
) -> Impact:
    return Impact(
        orders_per_week=round(orders, 1),
        gmv_eur_per_week=round(gmv, 2),
        jet_revenue_eur_per_week=round(gmv * commission + ad_spend, 2),
        ad_spend_eur=round(ad_spend, 2),
        partner_cost_eur_per_week=round(partner_cost, 2),
        formula=formula,
    )


def _complaints(f: dict[str, float]) -> Impact:
    orders = f["complaints_per_week"] * LOST_ORDERS_PER_COMPLAINT
    return _impact(
        orders,
        orders * f["avg_basket_eur"],
        f["commission_rate"],
        f"{f['complaints_per_week']:.2f} complaints/wk x {LOST_ORDERS_PER_COMPLAINT} lost orders each "
        f"x €{f['avg_basket_eur']:.2f} basket x {f['commission_rate']:.0%} commission",
    )


def _uplift(rate: float, label: str) -> Callable[[dict[str, float]], Impact]:
    def est(f: dict[str, float]) -> Impact:
        orders = f["item_orders_per_week"] * rate
        return _impact(
            orders,
            orders * f["avg_basket_eur"],
            f["commission_rate"],
            f"{f['item_orders_per_week']:.0f} orders/wk on affected dishes x {rate:.0%} {label} uplift "
            f"x €{f['avg_basket_eur']:.2f} basket x {f['commission_rate']:.0%} commission",
        )

    return est


def _prune(f: dict[str, float]) -> Impact:
    return _impact(0, 0, f["commission_rate"], "No direct uplift assumed: simpler menu, less waste for the kitchen.")


def _price_cut(f: dict[str, float]) -> Impact:
    cut = f["avg_price_cut_pct"]
    orders = f["category_orders_per_week"] * PRICE_ELASTICITY * cut
    lost_margin = f["changed_revenue_eur_per_week"] * cut
    gmv = orders * f["avg_basket_eur"] - lost_margin
    return _impact(
        orders,
        gmv,
        f["commission_rate"],
        f"{f['category_orders_per_week']:.0f} category orders/wk x {PRICE_ELASTICITY} elasticity x {cut:.0%} cut "
        f"= +{orders:.1f} orders x €{f['avg_basket_eur']:.2f} basket, minus {cut:.0%} on "
        f"€{f['changed_revenue_eur_per_week']:.0f}/wk existing sales, x {f['commission_rate']:.0%} commission",
        partner_cost=lost_margin,
    )


def _slot_promo(f: dict[str, float]) -> Impact:
    gap = max(0.0, f["expected_orders_per_week"] - f["slot_orders_per_week"])
    orders = gap * PROMO_GAP_CLOSED
    d = f["discount_pct"]
    # new orders pay the discounted price; existing slot orders now get the discount too
    gmv = orders * f["avg_basket_eur"] * (1 - d) - f["slot_orders_per_week"] * f["avg_basket_eur"] * d
    return _impact(
        orders,
        gmv,
        f["commission_rate"],
        f"gap {gap:.1f} orders/wk x {PROMO_GAP_CLOSED:.0%} recovered x €{f['avg_basket_eur']:.2f} basket "
        f"x {1 - d:.0%} after discount, minus {d:.0%} on {f['slot_orders_per_week']:.1f} existing slot orders, "
        f"x {f['commission_rate']:.0%} commission",
        partner_cost=(f["slot_orders_per_week"] + orders) * f["avg_basket_eur"] * d,
    )


def _ad_daypart(f: dict[str, float]) -> Impact:
    """Reschedule off-peak, optionally at a new budget. Ad yield is concave in budget (sqrt)."""
    sponsored = f["sponsored_orders_per_week"]
    old, new = f["weekly_budget_eur"], f.get("new_weekly_budget_eur", f["weekly_budget_eur"])
    scale = (new / old) ** 0.5 if old else 0.0
    gain = INCREMENTAL_OFF_PEAK - INCREMENTAL_PEAK
    shift = sponsored * f["peak_share"] * gain * scale  # same customers, now reached when they're incremental
    extra = sponsored * (scale - 1) * INCREMENTAL_OFF_PEAK  # more (or fewer) customers reached
    orders = shift + extra
    spend = new - old  # JET earns ad spend, so a budget change is JET revenue too
    return _impact(
        orders,
        orders * f["avg_basket_eur"],
        f["commission_rate"],
        f"{sponsored:.0f} sponsored orders/wk x sqrt(€{new:.0f}/€{old:.0f}) budget scaling; "
        f"{f['peak_share']:.0%} moved off-peak gain "
        f"({INCREMENTAL_OFF_PEAK:.0%} - {INCREMENTAL_PEAK:.0%}) incrementality, "
        f"new reach at {INCREMENTAL_OFF_PEAK:.0%}; x €{f['avg_basket_eur']:.2f} basket x "
        f"{f['commission_rate']:.0%} commission, plus €{spend:+.0f}/wk ad spend",
        ad_spend=spend,
        partner_cost=spend,
    )


ESTIMATORS: dict[str, Callable[[dict[str, float]], Impact]] = {
    "dish_quality": _complaints,
    "delivery_quality": _complaints,
    "add_photos": _uplift(PHOTO_UPLIFT, "photo"),
    "add_descriptions": _uplift(DESCRIPTION_UPLIFT, "description"),
    "prune_item": _prune,
    "price_cut": _price_cut,
    "slot_promo": _slot_promo,
    "ad_daypart": _ad_daypart,
}


def estimate(rec: Recommendation) -> Recommendation:
    """Return a copy of `rec` with `impact` filled in. Unknown kinds are left without impact."""
    est = ESTIMATORS.get(rec.kind)
    return rec.model_copy(update={"impact": est(rec.facts)}) if est else rec


def run(recs: list[Recommendation]) -> list[Recommendation]:
    return [estimate(r) for r in recs]
