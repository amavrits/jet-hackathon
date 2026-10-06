"""Pricing agent: categories priced well above the same dishes at nearby competitors.

Like-for-like: each dish is compared with the median price of the same dish name at the
restaurant's nearby competitors. A category whose median premium exceeds PREMIUM_THRESHOLD gets
one recommendation per *variant*: move each over-priced dish to competitor median + target premium,
for each target in TARGET_PREMIUMS. Variants share a variant_group; the optimiser picks at most one.
Price changes always go through owner approval (the graph enforces it).
"""

from __future__ import annotations

import duckdb

from growth.agents.common import WEEKS, base_facts, eur, per_week, slug
from growth.data import queries as q
from growth.state import EvidenceRef, LeverChange, Recommendation

PREMIUM_THRESHOLD = 0.15  # category median premium that triggers a recommendation
# Variants: new price = competitor median * (1 + target). Shallow to deep cut.
TARGET_PREMIUMS = (0.15, 0.10, 0.05)


def _round_price(x: float) -> float:
    """Menu-style price: nearest 0.50 below, ending in .00 or .50."""
    return max(0.5, int(x * 2) / 2)


def _changes(items: list[dict], target: float) -> list[dict]:
    out = []
    for i in items:
        new = _round_price(i["competitor_median"] * (1 + target))
        if new < i["price_eur"]:
            out.append({**i, "new_price": new})
    return out


def run(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> list[Recommendation]:
    if q.get_restaurant(con, restaurant_id) is None:
        return []
    base = base_facts(con, restaurant_id)
    levers = q.current_levers(con, restaurant_id)
    total_revenue = q.total_item_revenue(con, restaurant_id, weeks=WEEKS)
    by_item = q.price_position_by_item(con, restaurant_id)
    sales = {s["menu_item_id"]: s for s in q.item_sales(con, restaurant_id, weeks=WEEKS)}
    recs: list[Recommendation] = []

    for cat in q.price_position_by_category(con, restaurant_id):
        if cat["premium_pct"] < PREMIUM_THRESHOLD or cat["my_items"] < 1:
            continue
        name = cat["category"]
        items = [i for i in by_item if i["category"] == name]
        cat_items = [s for s in sales.values() if s["category"] == name]
        cat_orders_pw = per_week(q.category_orders(con, restaurant_id, name, weeks=WEEKS))
        cat_revenue_pw = round(sum(float(s["revenue_eur"]) for s in cat_items) / WEEKS, 2)
        group = f"pricing-{restaurant_id}-{slug(name)}"
        seen: set[tuple] = set()

        for target in TARGET_PREMIUMS:
            changes = _changes(items, target)
            key = tuple((c["menu_item_id"], c["new_price"]) for c in changes)
            if not changes or key in seen:
                continue  # nothing to cut at this depth, or same prices as a shallower variant
            seen.add(key)
            changed_revenue_pw = round(sum(float(sales[c["menu_item_id"]]["revenue_eur"]) for c in changes) / WEEKS, 2)
            avg_cut = sum(1 - c["new_price"] / c["price_eur"] for c in changes) / len(changes)
            # Revenue-weighted change in the restaurant's overall price level vs its market.
            weighted_cut = (
                sum(
                    float(sales[c["menu_item_id"]]["revenue_eur"]) * (1 - c["new_price"] / c["price_eur"])
                    for c in changes
                )
                / total_revenue
                if total_revenue
                else 0.0
            )
            label = f"within {target:.0%} of local median"
            recs.append(
                Recommendation(
                    id=f"{group}-w{round(target * 100):02d}",
                    agent="pricing",
                    kind="price_cut",
                    title=f"{name} are {cat['premium_pct']:.0%} above nearby competitors",
                    rationale=(
                        f"Your {name.lower()} cost a median {cat['premium_pct']:.0%} more than the same dishes "
                        f"at nearby restaurants. Customers comparing listings see this, and the category sells "
                        f"{cat_orders_pw:.0f} orders a week."
                    ),
                    action=f"Bring {len(changes)} {name.lower()} to {label} (average cut {avg_cut:.0%}).",
                    evidence=[
                        EvidenceRef(
                            kind="competitor",
                            ref=c["menu_item_id"],
                            detail=(
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
                        "target_premium_pct": target,
                        "avg_price_cut_pct": round(avg_cut, 3),
                        "category_orders_per_week": cat_orders_pw,
                        "category_revenue_eur_per_week": cat_revenue_pw,
                        "changed_revenue_eur_per_week": changed_revenue_pw,
                    },
                    lever_changes=[
                        LeverChange(
                            lever="price_index",
                            before=levers["price_index"],
                            after=round(levers["price_index"] * (1 - weighted_cut), 4),
                            note=f"{weighted_cut:.1%} revenue-weighted cut across the menu",
                        )
                    ],
                    variant_group=group,
                    variant_label=label,
                    patch=[
                        {"op": "replace", "path": f"/menu/{c['menu_item_id']}/price_eur", "value": c["new_price"]}
                        for c in changes
                    ],
                    confidence="high" if cat["premium_pct"] >= 0.2 else "med",
                )
            )

    return recs
