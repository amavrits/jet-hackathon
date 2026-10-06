"""Build data/jet.duckdb from scratch: market + true-model weekly history + partner detail.

Run:  python -m runners.generate_data [--db data/jet.duckdb]

Steps
  1. Market: ~260 restaurants with location, cuisine, price level (runners.true_model.make_market).
  2. Partners: the 8 demo restaurants with item-level orders, reviews, menus, listings and planted
     problems (growth.data.seed). Their competitors are their nearest same-cuisine market neighbours.
  3. Weekly panel (26 weeks) for every restaurant from the TRUE model, plus the change log.
     Partners' last 8 panel weeks are their actual item-level totals, so both views agree.
  4. Ground truth -> data/true_model.json (gitignored; for validating estimators only).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np

from growth.data import queries as q
from growth.data.seed import RESTAURANTS, _insert, seed_partners
from runners import true_model as tm

TRUTH_PATH = Path("data/true_model.json")
PEAK_HOURS = [18, 19, 20, 21]


def _partner_state(con: Any, rid: str) -> dict[str, float]:
    """Current lever values of a partner, measured from its own tables."""
    menu = q.get_menu(con, rid)
    prices = q.price_position_by_item(con, rid)
    ads = [c for c in q.get_ad_campaigns(con, rid) if c["status"] == "active"]
    budget = float(ads[0]["weekly_budget_eur"]) if ads else 0.0
    offpeak = 0.3
    if ads:
        s = q.sponsored_share_in_hours(con, rid, PEAK_HOURS)
        offpeak = 1 - s["sponsored_in_hours"] / s["sponsored"] if s["sponsored"] else 0.3
    return {
        "photo_share": sum(m["photo_url"] is not None for m in menu) / len(menu),
        "description_share": sum(bool(m["description"]) for m in menu) / len(menu),
        "price_index": float(median(1 + p["premium_pct"] for p in prices)) if prices else 1.0,
        "promo_active": 0.0,
        "ad_budget_eur": budget,
        "ad_offpeak_share": offpeak,
        "rating": float(q.get_restaurant(con, rid)["rating"]),
    }


def _partner_actual_weeks(con: Any, rid: str) -> dict[Any, dict[str, float]]:
    rows = con.execute(
        """
        WITH o AS (
            SELECT order_id, MIN(placed_at) AS placed_at, SUM(qty * unit_price_eur) AS basket
            FROM orders WHERE restaurant_id = ? GROUP BY order_id
        )
        SELECT ?::DATE + 7 * CAST(FLOOR(date_diff('day', ?::DATE, placed_at::DATE) / 7) AS INTEGER) AS week_start,
               COUNT(*) AS orders, SUM(basket) AS gmv
        FROM o GROUP BY 1
        """,
        [rid, tm.WEEKS[0], tm.WEEKS[0]],
    ).fetchall()
    return {w: {"orders": int(n), "gmv": float(g)} for w, n, g in rows}


def build(db_path: Path | str = q.DB_PATH, truth_path: Path | str = TRUTH_PATH, verbose: bool = True) -> dict[str, int]:
    db_path, truth_path = Path(db_path), Path(truth_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = q.connect(db_path)
    q.create_schema(con)
    rng = np.random.default_rng(tm.SEED)

    # 1-2. market and partners
    market = tm.make_market(rng)
    alpha_mean = float(np.mean([m["alpha"] for m in market]))
    levers = {m["restaurant_id"]: tm.plan_levers(rng, m, alpha_mean) for m in market}
    for m in market:
        m["has_ads"] = bool(levers[m["restaurant_id"]]["ad_budget_eur"][-1] > 0)
    seed_partners(con, market)
    _insert(
        con,
        "restaurants",
        [
            {
                **{k: m[k] for k in ("restaurant_id", "name", "cuisine", "city", "postcode", "delivery_model")},
                "commission_rate": 0.30 if m["delivery_model"] == "jet_delivery" else 0.15,
                "rating": round(float(levers[m["restaurant_id"]]["rating"][-1]), 1),
                **{k: m[k] for k in ("joined_at", "lat", "lon", "price_level", "is_partner")},
            }
            for m in market
        ],
    )

    # 3. weekly panel + change log
    shocks = tm.city_shocks(rng)
    weeks: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for m in market:
        weeks += tm.simulate_weeks(rng, m, levers[m["restaurant_id"]], shocks)
        events += tm.change_events(m["restaurant_id"], levers[m["restaurant_id"]])

    for p in RESTAURANTS:
        rid = p["restaurant_id"]
        state = _partner_state(con, rid)
        actual = _partner_actual_weeks(con, rid)
        target = sum(a["orders"] for a in actual.values()) / max(1, len(actual))
        r = {**p, "alpha": tm.calibrate_alpha(target, p["price_level"], p["city"], shocks, state)}
        rows = tm.simulate_weeks(rng, r, tm.constant_levers(state), shocks)
        for row in rows:
            a = actual.get(row["week_start"])
            if a:  # last 8 weeks: the actual item-level totals
                row["orders"], row["gmv_eur"] = a["orders"], round(a["gmv"], 2)
                row["avg_basket_eur"] = round(a["gmv"] / a["orders"], 2)
        weeks += rows

    _insert(con, "restaurant_weeks", weeks)
    _insert(con, "change_events", events)

    # 4. ground truth, outside the database
    truth_path.parent.mkdir(parents=True, exist_ok=True)
    truth_path.write_text(json.dumps(tm.truth_json(), indent=2))

    tables = [
        "restaurants",
        "restaurant_weeks",
        "change_events",
        "menu_items",
        "orders",
        "reviews",
        "competitors",
        "competitor_menu_items",
        "ad_campaigns",
        "listings",
    ]
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    con.close()
    if verbose:
        print(f"Built {db_path}  (ground truth: {truth_path})")
        for t, n in counts.items():
            print(f"  {t:<22} {n:>7}")
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(q.DB_PATH))
    ap.add_argument("--truth", default=str(TRUTH_PATH))
    args = ap.parse_args()
    build(args.db, args.truth)


if __name__ == "__main__":
    main()
