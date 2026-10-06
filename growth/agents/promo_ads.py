"""Promo & ads agent: fill slow slots, and point ad spend at them instead of the peak.

slot_promo   A (weekday, daypart) with fewer than DEAD_SLOT_RATIO of the median orders that the
             same daypart gets on the other days. The patch adds a timed promotion.
ad_daypart   An always-on sponsored listing whose orders land mostly in the dinner peak, where
             the restaurant is busy anyway. The patch schedules the budget into the slow slots
             (or the off-peak hours, if there are none). Budget stays the same.
"""

from __future__ import annotations

from statistics import median

import duckdb

from growth.agents.common import WEEKDAYS, WEEKS, base_facts, eur
from growth.data import queries as q
from growth.state import Evidence, Recommendation

DAYPARTS: dict[str, range] = {"lunch": range(11, 14), "afternoon": range(14, 18), "dinner": range(18, 23)}
PEAK_HOURS = [18, 19, 20, 21]
DEAD_SLOT_RATIO = 0.5
PROMO_DISCOUNT = 0.15
PROMO_MIN_BASKET_EUR = 15.0
PEAK_SHARE_THRESHOLD = 0.5  # share of sponsored orders in peak hours that counts as "peak-heavy"


def find_slow_slots(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> list[dict]:
    """Slots well below what the same daypart does on other days. Counts are over WEEKS weeks."""
    slots = q.orders_by_slot(con, restaurant_id, weeks=WEEKS)
    found = []
    for part, hours in DAYPARTS.items():
        per_day = [sum(s["orders"] for s in slots if s["weekday"] == d and s["hour"] in hours) for d in range(7)]
        for d, n in enumerate(per_day):
            others = median(per_day[:d] + per_day[d + 1 :])
            if others and n < DEAD_SLOT_RATIO * others:
                found.append(
                    {
                        "weekday": d,
                        "daypart": part,
                        "start": f"{hours.start:02d}:00",
                        "end": f"{hours.stop:02d}:00",
                        "orders": n,
                        "expected": others,
                        "ratio": n / others,
                    }
                )
    return found


def run(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> list[Recommendation]:
    if q.get_restaurant(con, restaurant_id) is None:
        return []
    base = base_facts(con, restaurant_id)
    slow = find_slow_slots(con, restaurant_id)
    recs: list[Recommendation] = []

    for s in slow:
        day = WEEKDAYS[s["weekday"]]
        label = f"{day.capitalize()} {s['daypart']} ({s['start']}-{s['end']})"
        recs.append(
            Recommendation(
                id=f"promo-{restaurant_id}-{day}-{s['daypart']}",
                agent="promo_ads",
                kind="slot_promo",
                title=f"{label} is a dead slot: run a {PROMO_DISCOUNT:.0%} promo",
                rationale=(
                    f"{label} averaged {s['orders'] / WEEKS:.1f} orders a week, against {s['expected'] / WEEKS:.1f} "
                    f"for the same hours on other days ({s['ratio']:.0%}). The kitchen is open and idle."
                ),
                action=(
                    f"Run {PROMO_DISCOUNT:.0%} off orders over {eur(PROMO_MIN_BASKET_EUR)} "
                    f"in this slot only, for four weeks."
                ),
                evidence=[
                    Evidence(kind="order_stat", ref=f"{day}_{s['daypart']}_orders_{WEEKS}w", value=float(s["orders"])),
                    Evidence(
                        kind="order_stat",
                        ref=f"other_days_{s['daypart']}_median_{WEEKS}w",
                        value=float(s["expected"]),
                    ),
                ],
                facts={
                    **base,
                    "slot_orders_per_week": round(s["orders"] / WEEKS, 2),
                    "expected_orders_per_week": round(s["expected"] / WEEKS, 2),
                    "discount_pct": PROMO_DISCOUNT,
                },
                patch=[
                    {
                        "op": "add",
                        "path": "/promotions/-",
                        "value": {
                            "id": f"slot-{day}-{s['daypart']}",
                            "day": day,
                            "start": s["start"],
                            "end": s["end"],
                            "discount_pct": PROMO_DISCOUNT,
                            "min_basket_eur": PROMO_MIN_BASKET_EUR,
                        },
                    }
                ],
                confidence="high" if s["ratio"] < 0.4 else "med",
            )
        )

    for c in q.get_ad_campaigns(con, restaurant_id):
        if c["status"] != "active":
            continue
        share = q.sponsored_share_in_hours(con, restaurant_id, PEAK_HOURS)
        if not share["sponsored"]:
            continue
        peak_share = share["sponsored_in_hours"] / share["sponsored"]
        if peak_share < PEAK_SHARE_THRESHOLD:
            continue
        weekly_budget = float(c["weekly_budget_eur"])
        sponsored_pw = share["sponsored"] / WEEKS
        cpo = weekly_budget / sponsored_pw if sponsored_pw else 0.0
        schedule = (
            [{"day": WEEKDAYS[s["weekday"]], "start": s["start"], "end": s["end"]} for s in slow]
            if slow
            else [{"day": d, "start": "14:00", "end": "18:00"} for d in WEEKDAYS[:5]]
        )
        target = ", ".join(f"{x['day'].capitalize()} {x['start']}-{x['end']}" for x in schedule)
        recs.append(
            Recommendation(
                id=f"ads-{restaurant_id}-{c['campaign_id']}-daypart",
                agent="promo_ads",
                kind="ad_daypart",
                title="Move sponsored listing budget out of the dinner peak",
                rationale=(
                    f"{peak_share:.0%} of sponsored orders arrive between 18:00 and 22:00, when the restaurant is "
                    f"already at its busiest and most of those customers would likely have ordered anyway. "
                    f"Each sponsored order costs about {eur(cpo)}."
                ),
                action=f"Keep the {eur(weekly_budget)}/week budget but only run the sponsored listing in: {target}.",
                evidence=[
                    Evidence(
                        kind="campaign",
                        ref=c["campaign_id"],
                        note=f"{eur(weekly_budget)}/week, {c['attributed_orders']} attributed orders",
                        value=weekly_budget,
                    ),
                    Evidence(kind="order_stat", ref="sponsored_peak_share", value=round(peak_share, 3)),
                ],
                facts={
                    **base,
                    "weekly_budget_eur": weekly_budget,
                    "sponsored_orders_per_week": round(sponsored_pw, 1),
                    "cost_per_sponsored_order_eur": round(cpo, 2),
                    "peak_share": round(peak_share, 3),
                },
                patch=[{"op": "add", "path": "/sponsored_listing/schedule", "value": schedule}],
                confidence="med",
            )
        )

    return recs
