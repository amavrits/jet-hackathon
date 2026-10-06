"""Pricing agent: category price positioning against nearby competitors.

Rule-based stub. Recommendations are advisory; prices change only after owner approval.
"""

from growth.state import Context, Evidence, Recommendation

OVERPRICED_RATIO = 1.15  # flag categories priced more than 15% above competitor median
TARGET_RATIO = 1.05  # propose landing 5% above competitor median
MIN_COMPETITORS = 3


def _round_price(value: float) -> float:
    """Round to the nearest .49 / .99 price point."""
    return round(round(value * 2) / 2 - 0.01, 2)


def run(ctx: Context) -> list[Recommendation]:
    recs = []
    for cp in ctx.category_prices:
        ratio = cp.own_median_eur / cp.competitor_median_eur
        if cp.competitor_count < MIN_COMPETITORS or ratio <= OVERPRICED_RATIO:
            continue
        factor = TARGET_RATIO * cp.competitor_median_eur / cp.own_median_eur
        items = [m for m in ctx.menu if m.category == cp.category]
        new_prices = {str(m.item_id): _round_price(m.price_eur * factor) for m in items}
        patch = []
        for idx, entry in enumerate(ctx.listing["menu"]):
            if str(entry["item_id"]) in new_prices:
                patch += [
                    {"op": "test", "path": f"/menu/{idx}/item_id", "value": entry["item_id"]},
                    {
                        "op": "replace",
                        "path": f"/menu/{idx}/price_eur",
                        "value": new_prices[str(entry["item_id"])],
                    },
                ]
        recs.append(
            Recommendation(
                id=f"pricing-{cp.category.lower()}",
                agent="pricing",
                title=f"Bring {cp.category} prices closer to the local market",
                rationale=(
                    f"Your median {cp.category} price is €{cp.own_median_eur:.2f}, "
                    f"{(ratio - 1):.0%} above the median of {cp.competitor_count} nearby items "
                    f"(€{cp.competitor_median_eur:.2f}). Proposed: about {TARGET_RATIO - 1:.0%} "
                    "above market. The price decision stays with the owner."
                ),
                evidence=[
                    Evidence(
                        kind="competitor",
                        ref=f"competitors: restaurant_id={ctx.restaurant.id}, "
                        f"category={cp.category}",
                        detail=f"median €{cp.competitor_median_eur:.2f} "
                        f"across {cp.competitor_count} items",
                    ),
                    *[
                        Evidence(
                            kind="menu_item",
                            ref=f"menu_items.id={m.item_id}",
                            detail=f"{m.name}: €{m.price_eur:.2f} -> "
                            f"€{new_prices[str(m.item_id)]:.2f}, {m.orders_per_week:.0f} sold/week",
                            row_id=m.item_id,
                        )
                        for m in items
                    ],
                ],
                patch=patch,
                impact_basis={
                    "kind": "price_change",
                    "old_prices": {str(m.item_id): m.price_eur for m in items},
                    "new_prices": new_prices,
                },
                confidence="high" if cp.competitor_count >= 8 else "med",
            )
        )
    return recs
