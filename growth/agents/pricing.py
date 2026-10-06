"""Pricing agent: categories priced well above the same dishes at nearby competitors.

Like-for-like: each dish is compared with the median price of the same dish name at the
restaurant's nearby competitors. A category whose median premium exceeds PREMIUM_THRESHOLD gets
one recommendation that moves each over-priced dish to competitor median + TARGET_PREMIUM.
Price changes always go through owner approval (the graph enforces it).
"""

from __future__ import annotations

import duckdb

from growth.agents.common import WEEKS, base_facts, eur, per_week, slug
from growth.data import queries as q
from growth.state import Evidence, Recommendation

PREMIUM_THRESHOLD = 0.15  # category median premium that triggers a recommendation
TARGET_PREMIUM = 0.10  # new price = competitor median * (1 + this); a modest premium is fine


def _round_price(x: float) -> float:
    """Menu-style price: nearest 0.50 below, ending in .00 or .50."""
    return max(0.5, int(x * 2) / 2)


def run(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> list[Recommendation]:
    if q.get_restaurant(con, restaurant_id) is None:
        return []
    base = base_facts(con, restaurant_id)
    by_item = q.price_position_by_item(con, restaurant_id)
    sales = {s["menu_item_id"]: s for s in q.item_sales(con, restaurant_id, weeks=WEEKS)}
    recs: list[Recommendation] = []

    for cat in q.price_position_by_category(con, restaurant_id):
        if cat["premium_pct"] < PREMIUM_THRESHOLD or cat["my_items"] < 1:
            continue
        items = [i for i in by_item if i["category"] == cat["category"]]
        changes = []
        for i in items:
            target = _round_price(i["competitor_median"] * (1 + TARGET_PREMIUM))
            if target < i["price_eur"]:
                changes.append({**i, "new_price": target})
        if not changes:
            continue

        cat_items = [s for s in sales.values() if s["category"] == cat["category"]]
        cat_orders_pw = per_week(q.category_orders(con, restaurant_id, cat["category"], weeks=WEEKS))
        cat_revenue_pw = round(sum(float(s["revenue_eur"]) for s in cat_items) / WEEKS, 2)
        changed_revenue_pw = round(sum(float(sales[c["menu_item_id"]]["revenue_eur"]) for c in changes) / WEEKS, 2)
        avg_cut = sum(1 - c["new_price"] / c["price_eur"] for c in changes) / len(changes)

        recs.append(
            Recommendation(
                id=f"pricing-{restaurant_id}-{slug(cat['category'])}",
                agent="pricing",
                kind="price_cut",
                title=f"{cat['category']} are {cat['premium_pct']:.0%} above nearby competitors",
                rationale=(
                    f"Your {cat['category'].lower()} cost a median {cat['premium_pct']:.0%} more than the same dishes "
                    f"at nearby restaurants. Customers comparing listings see this, and the category sells "
                    f"{cat_orders_pw:.0f} orders a week."
                ),
                action=(
                    f"Bring {len(changes)} {cat['category'].lower()} to within {TARGET_PREMIUM:.0%} "
                    f"of the local median (average cut {avg_cut:.0%})."
                ),
                evidence=[
                    Evidence(
                        kind="competitor_item",
                        ref=c["menu_item_id"],
                        note=(
                            f"{c['name']}: {eur(c['price_eur'])} vs median {eur(c['competitor_median'])} "
                            f"at {c['n_competitors']} competitors -> {eur(c['new_price'])}"
                        ),
                        value=round(c["premium_pct"], 3),
                    )
                    for c in changes
                ],
                facts={
                    **base,
                    "premium_pct": round(cat["premium_pct"], 3),
                    "avg_price_cut_pct": round(avg_cut, 3),
                    "category_orders_per_week": cat_orders_pw,
                    "category_revenue_eur_per_week": cat_revenue_pw,
                    "changed_revenue_eur_per_week": changed_revenue_pw,
                },
                patch=[
                    {"op": "replace", "path": f"/menu/{c['menu_item_id']}/price_eur", "value": c["new_price"]}
                    for c in changes
                ],
                confidence="high" if cat["premium_pct"] >= 0.2 else "med",
            )
        )

    return recs
