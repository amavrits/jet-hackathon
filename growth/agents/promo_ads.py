"""Promo and ads agent: promos for dead slots, sponsored listing budget.

Rule-based stub. Ads are never launched or paused without owner approval.
"""

from statistics import mean

from growth.state import Context, Evidence, Recommendation

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DEAD_SLOT_RATIO = 0.4  # slot sells under 40% of the same slot on other days
MIN_BASELINE_ORDERS = 2.0
PROMO_DISCOUNT = 0.10
MIN_PARTNER_ROAS = 2.0  # attributed GMV per euro of ad spend
NEW_CAMPAIGN_BUDGET_EUR = 50.0
MIN_RATING_FOR_ADS = 4.4

SLOT_HOURS = {
    "lunch 11-14": "11:00-14:00",
    "afternoon 14-17": "14:00-17:00",
    "dinner 17-21": "17:00-21:00",
    "late 21-23": "21:00-23:00",
}


def _dead_slots(ctx: Context) -> list[Recommendation]:
    recs = []
    for s in ctx.slots:
        peers = [
            p.orders_per_week for p in ctx.slots if p.slot == s.slot and p.weekday != s.weekday
        ]
        if not peers:
            continue
        baseline = mean(peers)
        if baseline < MIN_BASELINE_ORDERS or s.orders_per_week >= DEAD_SLOT_RATIO * baseline:
            continue
        day = WEEKDAYS[s.weekday]
        promo = {
            "type": "discount",
            "percent": int(PROMO_DISCOUNT * 100),
            "weekday": day,
            "hours": SLOT_HOURS[s.slot],
            "funded_by": "restaurant",
        }
        recs.append(
            Recommendation(
                id=f"promo-{day.lower()}-{s.slot.split()[0]}",
                agent="promo_ads",
                title=f"{promo['percent']}% off on {day} {SLOT_HOURS[s.slot]}",
                rationale=(
                    f"{day} {s.slot.split()[0]} averages {s.orders_per_week:.1f} orders a week, "
                    f"against {baseline:.1f} for the same hours on other days. A small discount "
                    "in that slot only; the restaurant funds the discount."
                ),
                evidence=[
                    Evidence(
                        kind="order_stat",
                        ref=f"orders: restaurant_id={ctx.restaurant.id}, weekday={day}, "
                        f"slot={s.slot}, last 8 weeks",
                        detail=f"{s.orders_per_week:.1f} orders/week",
                    ),
                    Evidence(
                        kind="order_stat",
                        ref=f"orders: restaurant_id={ctx.restaurant.id}, other weekdays, "
                        f"slot={s.slot}, last 8 weeks",
                        detail=f"{baseline:.1f} orders/week on average",
                    ),
                ],
                patch=[{"op": "add", "path": "/promotions/-", "value": promo}],
                impact_basis={
                    "kind": "slot_promo",
                    "slot_orders": s.orders_per_week,
                    "baseline_orders": baseline,
                    "discount": PROMO_DISCOUNT,
                },
                confidence="high" if s.orders_per_week < 0.25 * baseline else "med",
            )
        )
    return recs


def _ads(ctx: Context) -> list[Recommendation]:
    basket = ctx.kpis.avg_basket_eur
    active = [c for c in ctx.campaigns if c.active]
    for c in active:
        orders_pw = c.attributed_orders / c.weeks
        roas = orders_pw * basket / c.weekly_budget_eur
        if roas < MIN_PARTNER_ROAS:
            return [
                Recommendation(
                    id=f"ads-pause-{c.id}",
                    agent="promo_ads",
                    title=f"Pause the €{c.weekly_budget_eur:.0f}/week sponsored listing",
                    rationale=(
                        f"The campaign brings about {orders_pw:.1f} orders a week, worth "
                        f"€{orders_pw * basket:.0f} in sales for €{c.weekly_budget_eur:.0f} of "
                        f"spend (return {roas:.1f}x, below {MIN_PARTNER_ROAS:.0f}x). It costs the "
                        "partner more than it earns. This lowers JET ad revenue."
                    ),
                    evidence=[
                        Evidence(
                            kind="ad_campaign",
                            ref=f"ad_campaigns.id={c.id}",
                            detail=f"{c.attributed_orders} attributed orders over {c.weeks} weeks",
                            row_id=c.id,
                        )
                    ],
                    patch=[
                        {
                            "op": "replace",
                            "path": "/sponsored",
                            "value": {"active": False, "weekly_budget_eur": 0.0},
                        }
                    ],
                    impact_basis={
                        "kind": "ads_pause",
                        "budget": c.weekly_budget_eur,
                        "attributed_orders_per_week": orders_pw,
                    },
                    confidence="high",
                )
            ]
    if active:
        return []
    k = ctx.kpis
    if k.avg_rating >= MIN_RATING_FOR_ADS and k.orders_per_week < ctx.peer_median_orders_per_week:
        return [
            Recommendation(
                id="ads-start",
                agent="promo_ads",
                title=f"Trial a €{NEW_CAMPAIGN_BUDGET_EUR:.0f}/week sponsored listing",
                rationale=(
                    f"Customers rate you {k.avg_rating:.2f}★, but you get "
                    f"{k.orders_per_week:.0f} orders a week against a peer median of "
                    f"{ctx.peer_median_orders_per_week:.0f}. That points to visibility, not "
                    "quality. Trial for 4 weeks, then keep it only if it pays back."
                ),
                evidence=[
                    Evidence(
                        kind="review",
                        ref=f"reviews: restaurant_id={ctx.restaurant.id}",
                        detail=f"average {k.avg_rating:.2f}★ over {k.review_count} reviews",
                    ),
                    Evidence(
                        kind="order_stat",
                        ref=f"orders: restaurant_id={ctx.restaurant.id}, last 8 weeks",
                        detail=f"{k.orders_per_week:.0f} orders/week "
                        f"(peer median {ctx.peer_median_orders_per_week:.0f})",
                    ),
                ],
                patch=[
                    {
                        "op": "replace",
                        "path": "/sponsored",
                        "value": {"active": True, "weekly_budget_eur": NEW_CAMPAIGN_BUDGET_EUR},
                    }
                ],
                impact_basis={"kind": "ads_start", "budget": NEW_CAMPAIGN_BUDGET_EUR},
                confidence="low",
            )
        ]
    return []


def run(ctx: Context) -> list[Recommendation]:
    return _dead_slots(ctx) + _ads(ctx)
