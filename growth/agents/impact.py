"""Impact estimates: simple, explainable heuristics. Never LLM output.

Each function returns an Impact with the formula filled in with real numbers, so the
UI can show exactly how the figure was reached. The uplift rates below are assumptions,
not measurements; they are listed here so they can be challenged in one place.
"""

from collections.abc import Callable
from typing import Any

from growth.state import Context, Impact, Recommendation

COMPLAINT_FIX_RECOVERY = 0.15  # share of the dish's weekly orders recovered by fixing a complaint
PHOTO_UPLIFT = 0.10  # uplift on a bestseller's orders from adding a photo
DESCRIPTION_UPLIFT = 0.05  # uplift on a bestseller's orders from a useful description
PRICE_ELASTICITY = 1.5  # % more orders per 1% price cut, within the category
SLOT_PROMO_RECOVERY = 0.30  # share of the gap to the slot baseline that a promo recovers
COST_PER_INCREMENTAL_AD_ORDER_EUR = 6.0  # sponsored listing spend per incremental order


def _impact(ctx: Context, d_orders: float, d_gmv: float, formula: str, **extra: float) -> Impact:
    rate = ctx.restaurant.commission_rate
    ad_revenue = extra.pop("ad_revenue", 0.0)
    jet = d_gmv * rate + ad_revenue
    jet_formula = f"JET revenue = ΔGMV €{d_gmv:.2f} × commission {rate:.0%}"
    if ad_revenue:
        jet_formula += f" {'+' if ad_revenue > 0 else '-'} ad spend €{abs(ad_revenue):.2f}"
    return Impact(
        orders_per_week=round(d_orders, 2),
        gmv_eur_per_week=round(d_gmv, 2),
        jet_revenue_eur_per_week=round(jet, 2),
        formula=f"{formula}\n{jet_formula} = €{jet:.2f}/week",
        **{k: round(v, 2) for k, v in extra.items()},
    )


def _item_orders(ctx: Context, item_ids: list[int]) -> float:
    return sum(m.orders_per_week for m in ctx.menu if m.item_id in item_ids)


def complaint_fix(ctx: Context, basis: dict[str, Any]) -> Impact:
    orders = _item_orders(ctx, [basis["item_id"]])
    basket = ctx.kpis.avg_basket_eur
    d = orders * COMPLAINT_FIX_RECOVERY
    return _impact(
        ctx, d, d * basket,
        f"Δorders = dish orders {orders:.1f}/wk × recovery {COMPLAINT_FIX_RECOVERY:.0%} = {d:.1f}\n"
        f"ΔGMV = Δorders × avg basket €{basket:.2f} = €{d * basket:.2f}",
    )  # fmt: skip


def _uplift(ctx: Context, basis: dict[str, Any], rate: float, label: str) -> Impact:
    orders = _item_orders(ctx, basis["item_ids"])
    basket = ctx.kpis.avg_basket_eur
    d = orders * rate
    return _impact(
        ctx, d, d * basket,
        f"Δorders = bestseller orders {orders:.1f}/wk × {label} uplift {rate:.0%} = {d:.1f}\n"
        f"ΔGMV = Δorders × avg basket €{basket:.2f} = €{d * basket:.2f}",
    )  # fmt: skip


def photos(ctx: Context, basis: dict[str, Any]) -> Impact:
    return _uplift(ctx, basis, PHOTO_UPLIFT, "photo")


def descriptions(ctx: Context, basis: dict[str, Any]) -> Impact:
    return _uplift(ctx, basis, DESCRIPTION_UPLIFT, "description")


def price_change(ctx: Context, basis: dict[str, Any]) -> Impact:
    old: dict[str, float] = basis["old_prices"]
    new: dict[str, float] = basis["new_prices"]
    stats = {str(m.item_id): m for m in ctx.menu}
    cut = 1 - sum(new.values()) / sum(old.values())
    lift = PRICE_ELASTICITY * cut
    old_gmv = sum(stats[i].orders_per_week * old[i] for i in old)
    new_gmv = sum(stats[i].orders_per_week * (1 + lift) * new[i] for i in old)
    orders = sum(stats[i].orders_per_week for i in old)
    given_up = sum(stats[i].orders_per_week * (old[i] - new[i]) for i in old)
    return _impact(
        ctx, orders * lift, new_gmv - old_gmv,
        f"avg price cut {cut:.1%}; Δorders = category orders {orders:.1f}/wk × elasticity "
        f"{PRICE_ELASTICITY} × {cut:.1%} = {orders * lift:.1f}\n"
        f"ΔGMV = new GMV €{new_gmv:.2f} - current GMV €{old_gmv:.2f} = €{new_gmv - old_gmv:.2f}\n"
        f"Partner cost = revenue given up on current orders €{given_up:.2f}/wk",
        partner_cost_eur_per_week=given_up,
    )  # fmt: skip


def slot_promo(ctx: Context, basis: dict[str, Any]) -> Impact:
    slot, base, disc = basis["slot_orders"], basis["baseline_orders"], basis["discount"]
    basket = ctx.kpis.avg_basket_eur
    d = (base - slot) * SLOT_PROMO_RECOVERY
    d_gmv = d * basket * (1 - disc)
    cost = disc * basket * (slot + d)
    return _impact(
        ctx, d, d_gmv,
        f"Δorders = (baseline {base:.1f} - slot {slot:.1f}) × recovery {SLOT_PROMO_RECOVERY:.0%} "
        f"= {d:.1f}\n"
        f"ΔGMV = Δorders × avg basket €{basket:.2f} × (1 - {disc:.0%}) = €{d_gmv:.2f}\n"
        f"Partner cost = {disc:.0%} × basket × all slot orders {slot + d:.1f} = €{cost:.2f}/wk",
        partner_cost_eur_per_week=cost,
    )  # fmt: skip


def ads_start(ctx: Context, basis: dict[str, Any]) -> Impact:
    budget = basis["budget"]
    basket = ctx.kpis.avg_basket_eur
    d = budget / COST_PER_INCREMENTAL_AD_ORDER_EUR
    return _impact(
        ctx, d, d * basket,
        f"Δorders = budget €{budget:.2f} ÷ €{COST_PER_INCREMENTAL_AD_ORDER_EUR:.2f} per "
        f"incremental order = {d:.1f}\n"
        f"ΔGMV = Δorders × avg basket €{basket:.2f} = €{d * basket:.2f}\n"
        f"Partner cost = ad spend €{budget:.2f}/wk",
        ad_revenue=budget, ad_spend_eur=budget, partner_cost_eur_per_week=budget,
    )  # fmt: skip


def ads_pause(ctx: Context, basis: dict[str, Any]) -> Impact:
    budget, attributed = basis["budget"], basis["attributed_orders_per_week"]
    basket = ctx.kpis.avg_basket_eur
    return _impact(
        ctx, -attributed, -attributed * basket,
        f"Δorders = - attributed orders {attributed:.1f}/wk\n"
        f"ΔGMV = Δorders × avg basket €{basket:.2f} = €{-attributed * basket:.2f}\n"
        f"Partner cost = - ad spend saved €{budget:.2f}/wk",
        ad_revenue=-budget, ad_spend_eur=-budget, partner_cost_eur_per_week=-budget,
    )  # fmt: skip


HEURISTICS: dict[str, Callable[[Context, dict[str, Any]], Impact]] = {
    "complaint_fix": complaint_fix,
    "photos": photos,
    "descriptions": descriptions,
    "price_change": price_change,
    "slot_promo": slot_promo,
    "ads_start": ads_start,
    "ads_pause": ads_pause,
}


def estimate(rec: Recommendation, ctx: Context) -> Impact | None:
    """Return the impact, or None if the recommendation cannot state one (and must be dropped)."""
    heuristic = HEURISTICS.get(rec.impact_basis.get("kind", ""))
    if heuristic is None:
        return None
    impact = heuristic(ctx, rec.impact_basis)
    if abs(impact.orders_per_week) < 0.01 and abs(impact.jet_revenue_eur_per_week) < 0.01:
        return None
    return impact
