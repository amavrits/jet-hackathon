"""Synthetic data generator (deterministic, seeded). Writes data/jet.duckdb.

Run:  python -m growth.data.seed [--db data/jet.duckdb] [--weeks 8]

Every restaurant gets a realistic baseline (menu, 8 weeks of item-level orders,
reviews, nearby competitors, listing). On top of that, `PLANTED` lists the
problems the demo must be able to find. Tests import `PLANTED` as the oracle.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from growth.data.queries import DB_PATH, connect, create_schema

SEED = 42
END_DATE = date(2026, 10, 5)  # last full day of data (a Sunday)

COMMISSION = {"marketplace": 0.15, "jet_delivery": 0.30}

# ------------------------------------------------------------------ menus

# name, category, base price. Each cuisine has ~12 items.
MENUS: dict[str, list[tuple[str, str, float]]] = {
    "Pizza": [
        ("Garlic Bread", "Starters", 4.5),
        ("Bruschetta", "Starters", 5.5),
        ("Margherita", "Mains", 9.5),
        ("Pepperoni", "Mains", 11.5),
        ("Quattro Formaggi", "Mains", 12.5),
        ("Calzone", "Mains", 12.0),
        ("Diavola", "Mains", 12.0),
        ("Veggie Supreme", "Mains", 11.0),
        ("Fries", "Sides", 3.5),
        ("Side Salad", "Sides", 4.0),
        ("Tiramisu", "Desserts", 5.5),
        ("Cola 33cl", "Drinks", 2.5),
    ],
    "Indian": [
        ("Samosa (2)", "Starters", 4.5),
        ("Onion Bhaji", "Starters", 4.0),
        ("Chicken Tikka Masala", "Mains", 13.5),
        ("Lamb Rogan Josh", "Mains", 14.5),
        ("Butter Chicken", "Mains", 13.5),
        ("Chana Masala", "Mains", 11.0),
        ("Paneer Butter Masala", "Mains", 12.5),
        ("Chicken Biryani", "Mains", 13.0),
        ("Garlic Naan", "Sides", 3.0),
        ("Pilau Rice", "Sides", 3.0),
        ("Gulab Jamun", "Desserts", 4.5),
        ("Mango Lassi", "Drinks", 3.5),
    ],
    "Chinese": [
        ("Spring Rolls (4)", "Starters", 4.5),
        ("Prawn Crackers", "Starters", 2.5),
        ("Sweet & Sour Chicken", "Mains", 11.0),
        ("Beef Black Bean", "Mains", 12.0),
        ("Kung Pao Chicken", "Mains", 11.5),
        ("Crispy Duck", "Mains", 15.0),
        ("Mapo Tofu", "Mains", 10.0),
        ("Chow Mein", "Mains", 10.5),
        ("Egg Fried Rice", "Sides", 3.5),
        ("Steamed Rice", "Sides", 2.5),
        ("Banana Fritters", "Desserts", 4.5),
        ("Jasmine Tea", "Drinks", 2.0),
    ],
    "Burgers": [
        ("Onion Rings", "Starters", 4.0),
        ("Chicken Wings (6)", "Starters", 6.5),
        ("Classic Cheeseburger", "Mains", 10.5),
        ("Bacon BBQ Burger", "Mains", 12.5),
        ("Double Smash", "Mains", 13.5),
        ("Crispy Chicken Burger", "Mains", 11.0),
        ("Veggie Burger", "Mains", 10.5),
        ("Fries", "Sides", 3.5),
        ("Sweet Potato Fries", "Sides", 4.5),
        ("Coleslaw", "Sides", 2.5),
        ("Brownie", "Desserts", 4.5),
        ("Milkshake", "Drinks", 4.5),
    ],
    "Sushi": [
        ("Edamame", "Starters", 4.0),
        ("Miso Soup", "Starters", 3.0),
        ("Salmon Nigiri (6)", "Mains", 12.0),
        ("California Roll (8)", "Mains", 10.5),
        ("Spicy Tuna Roll (8)", "Mains", 12.5),
        ("Dragon Roll (8)", "Mains", 14.0),
        ("Sashimi Mix", "Mains", 16.0),
        ("Chicken Katsu Curry", "Mains", 13.0),
        ("Gyoza (5)", "Sides", 5.5),
        ("Seaweed Salad", "Sides", 4.0),
        ("Mochi (3)", "Desserts", 4.5),
        ("Ramune", "Drinks", 3.0),
    ],
    "Greek": [
        ("Tzatziki & Pita", "Starters", 4.5),
        ("Halloumi Fries", "Starters", 6.0),
        ("Chicken Gyros Wrap", "Mains", 9.5),
        ("Pork Souvlaki Plate", "Mains", 13.5),
        ("Lamb Kleftiko", "Mains", 16.0),
        ("Moussaka", "Mains", 12.5),
        ("Falafel Wrap", "Mains", 9.0),
        ("Grilled Octopus", "Mains", 18.0),
        ("Greek Salad", "Sides", 5.5),
        ("Lemon Potatoes", "Sides", 4.0),
        ("Baklava", "Desserts", 4.5),
        ("Sparkling Water", "Drinks", 2.0),
    ],
    "Vietnamese": [
        ("Summer Rolls (3)", "Starters", 5.5),
        ("Crispy Spring Rolls", "Starters", 5.0),
        ("Beef Pho", "Mains", 12.5),
        ("Chicken Pho", "Mains", 11.5),
        ("Bun Cha", "Mains", 12.0),
        ("Banh Mi Pork", "Mains", 8.5),
        ("Lemongrass Tofu Rice", "Mains", 10.5),
        ("Com Tam", "Mains", 12.0),
        ("Jasmine Rice", "Sides", 2.5),
        ("Pickled Veg", "Sides", 3.0),
        ("Che Ba Mau", "Desserts", 4.5),
        ("Vietnamese Iced Coffee", "Drinks", 3.5),
    ],
    "French": [
        ("Onion Soup", "Starters", 6.5),
        ("Pâté & Toast", "Starters", 7.0),
        ("Steak Frites", "Mains", 18.5),
        ("Coq au Vin", "Mains", 16.0),
        ("Ratatouille", "Mains", 12.5),
        ("Croque Monsieur", "Mains", 9.5),
        ("Duck Confit", "Mains", 19.0),
        ("Frites", "Sides", 4.0),
        ("Green Beans", "Sides", 4.0),
        ("Crème Brûlée", "Desserts", 6.0),
        ("Tarte Tatin", "Desserts", 6.0),
        ("Lemonade", "Drinks", 3.0),
    ],
}

DESCRIPTIONS = [
    "Made fresh to order with our house recipe.",
    "A customer favourite, generously portioned.",
    "Classic preparation, locally sourced ingredients.",
    "Served hot with a side of our signature sauce.",
]

# --------------------------------------------------------------- restaurants

# Each entry: baseline config + planted problems. Keep this list in sync with PLANTED below.
RESTAURANTS: list[dict[str, Any]] = [
    {
        "restaurant_id": "r_bella_napoli",
        "name": "Bella Napoli",
        "cuisine": "Pizza",
        "city": "Amsterdam",
        "postcode": "1017",
        "delivery_model": "marketplace",
        "rating": 4.3,
        "daily_orders": 38,
        "soggy_item": "Calzone",
        "missing_photos": True,
    },
    {
        "restaurant_id": "r_spice_route",
        "name": "Spice Route",
        "cuisine": "Indian",
        "city": "Amsterdam",
        "postcode": "1053",
        "delivery_model": "jet_delivery",
        "rating": 4.1,
        "daily_orders": 30,
        "dead_slot": True,
        "overpriced_category": "Mains",
    },
    {
        "restaurant_id": "r_golden_wok",
        "name": "Golden Wok",
        "cuisine": "Chinese",
        "city": "Rotterdam",
        "postcode": "3011",
        "delivery_model": "marketplace",
        "rating": 4.0,
        "daily_orders": 34,
        "missing_photos": True,
        "missing_descriptions": True,
        "cold_item": "Chow Mein",
    },
    {
        "restaurant_id": "r_burger_barn",
        "name": "Burger Barn",
        "cuisine": "Burgers",
        "city": "Utrecht",
        "postcode": "3511",
        "delivery_model": "jet_delivery",
        "rating": 4.2,
        "daily_orders": 45,
        "soggy_item": "Fries",
        "overpriced_category": "Sides",
    },
    {
        "restaurant_id": "r_sushi_zen",
        "name": "Sushi Zen",
        "cuisine": "Sushi",
        "city": "Amsterdam",
        "postcode": "1071",
        "delivery_model": "marketplace",
        "rating": 4.5,
        "daily_orders": 26,
        "dead_slot": True,
        "wasted_ads": True,
    },
    {
        "restaurant_id": "r_taverna",
        "name": "Taverna Mykonos",
        "cuisine": "Greek",
        "city": "The Hague",
        "postcode": "2511",
        "delivery_model": "marketplace",
        "rating": 4.4,
        "daily_orders": 22,
        "missing_photos": True,
        "dead_item": "Grilled Octopus",
    },
    {
        "restaurant_id": "r_pho_house",
        "name": "Pho House",
        "cuisine": "Vietnamese",
        "city": "Rotterdam",
        "postcode": "3014",
        "delivery_model": "jet_delivery",
        "rating": 4.6,
        "daily_orders": 28,
        "overpriced_category": "Starters",
        "missing_descriptions": True,
    },
    {
        "restaurant_id": "r_petit_bistro",
        "name": "Le Petit Bistro",
        "cuisine": "French",
        "city": "Utrecht",
        "postcode": "3512",
        "delivery_model": "marketplace",
        "rating": 4.7,
        "daily_orders": 18,
        # control restaurant: no planted problems
    },
]

# Oracle for tests and the demo narrator. Derived from RESTAURANTS so it can't drift.
PLANTED: dict[str, dict[str, Any]] = {
    r["restaurant_id"]: {
        k: v
        for k, v in r.items()
        if k
        in {
            "soggy_item",
            "cold_item",
            "dead_slot",
            "overpriced_category",
            "missing_photos",
            "missing_descriptions",
            "dead_item",
            "wasted_ads",
        }
    }
    for r in RESTAURANTS
}

DEAD_SLOT = {"weekday": 1, "hours": range(14, 18)}  # Tuesday 14:00-17:59
OVERPRICE_FACTOR = 1.25

# ------------------------------------------------------------------ reviews

GOOD = [
    "Great food, arrived hot and on time.",
    "Really tasty, would order again.",
    "Generous portions and fast delivery.",
    "One of the best {cuisine} places around.",
    "Perfect as always. {item} was delicious.",
    "Fresh and well packed. {item} highly recommended.",
]
MEH = [
    "Decent but a bit pricey for what you get.",
    "Food was fine, delivery took a while.",
    "{item} was ok, nothing special.",
    "Average. Packaging could be better.",
]
BAD = [
    "Order arrived late and lukewarm.",
    "Missing an item from my order.",
    "{item} was not as described.",
    "Portion was smaller than last time.",
]
SOGGY = [
    "{item} was completely soggy by the time it arrived.",
    "Soggy {item}, really disappointing.",
    "The {item} turned to mush in the box. Soggy and greasy.",
    "Second time the {item} came soggy. Won't order it again.",
    "{item} soggy and cold. Everything else was fine.",
]
COLD = [
    "{item} arrived cold.",
    "Cold {item}, had to microwave it.",
    "The {item} was stone cold on arrival.",
    "{item} cold again, third time now.",
]

# --------------------------------------------------------------------- helpers


def _hourly_weight(weekday: int, hour: int) -> float:
    """Relative demand for a (weekday, hour). Lunch and dinner peaks, weekends busier."""
    if hour < 11 or hour > 22:
        return 0.0
    base = {11: 0.4, 12: 1.0, 13: 1.0, 14: 0.5, 15: 0.3, 16: 0.3, 17: 0.8, 18: 1.6, 19: 2.0, 20: 1.8, 21: 1.0, 22: 0.4}[
        hour
    ]
    if weekday >= 4:  # Fri, Sat, Sun
        base *= 1.35
    return base


def _item_weights(rng: random.Random, items: list[dict[str, Any]], cfg: dict[str, Any]) -> list[float]:
    """Popularity weights. Mains dominate, a couple of bestsellers, one optional dead item."""
    weights = []
    for it in items:
        w = {"Starters": 0.5, "Mains": 1.0, "Sides": 0.7, "Desserts": 0.3, "Drinks": 0.6}[it["category"]]
        w *= rng.uniform(0.6, 1.4)
        if it["name"] == cfg.get("dead_item"):
            w = 0.01
        weights.append(w)
    # mark the top 3 mains as bestsellers (used for missing photos)
    mains = sorted(
        (i for i, it in enumerate(items) if it["category"] == "Mains"),
        key=lambda i: weights[i],
        reverse=True,
    )[:3]
    for i in mains:
        weights[i] *= 2.0
        items[i]["_bestseller"] = True
    return weights


def _make_menu(rng: random.Random, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    rid = cfg["restaurant_id"]
    items: list[dict[str, Any]] = []
    for n, (name, category, price) in enumerate(MENUS[cfg["cuisine"]]):
        price = round(price * rng.uniform(0.97, 1.03), 1)
        if category == cfg.get("overpriced_category"):
            price = round(price * OVERPRICE_FACTOR, 1)
        items.append(
            {
                "menu_item_id": f"{rid}_m{n:02d}",
                "restaurant_id": rid,
                "name": name,
                "category": category,
                "description": None
                if cfg.get("missing_descriptions") and rng.random() < 0.6
                else rng.choice(DESCRIPTIONS),
                "price_eur": price,
                "photo_url": f"https://img.jet.example/{rid}/{n:02d}.jpg",
                "is_available": True,
            }
        )
    return items


def _make_competitors(rng: random.Random, cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """4-6 nearby competitors with menus priced around the *baseline* (un-inflated) prices."""
    rid = cfg["restaurant_id"]
    comps, comp_items = [], []
    adjectives = ["Royal", "Little", "Golden", "Urban", "Mama's", "Express", "House of", "The Real"]
    for c in range(rng.randint(4, 6)):
        cid = f"{rid}_c{c}"
        comps.append(
            {
                "competitor_id": cid,
                "restaurant_id": rid,
                "name": f"{rng.choice(adjectives)} {cfg['cuisine']} {c + 1}",
                "cuisine": cfg["cuisine"],
                "distance_km": round(rng.uniform(0.3, 2.5), 1),
                "rating": round(rng.uniform(3.8, 4.7), 1),
                "is_sponsored": rng.random() < 0.35,
            }
        )
        for n, (name, category, price) in enumerate(MENUS[cfg["cuisine"]]):
            if rng.random() < 0.15:
                continue  # not every competitor sells every item
            comp_items.append(
                {
                    "competitor_item_id": f"{cid}_i{n:02d}",
                    "competitor_id": cid,
                    "name": name,
                    "category": category,
                    "price_eur": round(price * rng.uniform(0.94, 1.06), 1),
                }
            )
    return comps, comp_items


def _make_orders(
    rng: random.Random,
    cfg: dict[str, Any],
    items: list[dict[str, Any]],
    weights: list[float],
    weeks: int,
) -> list[dict[str, Any]]:
    rid = cfg["restaurant_id"]
    start = END_DATE - timedelta(days=weeks * 7 - 1)
    hourly = {(wd, h): _hourly_weight(wd, h) for wd in range(7) for h in range(24)}
    per_week_weight = sum(hourly.values())
    scale = cfg["daily_orders"] * 7 / per_week_weight

    sponsored_share = 0.25 if cfg.get("wasted_ads") else 0.0
    rows: list[dict[str, Any]] = []
    n_order = 0
    for d in range(weeks * 7):
        day = start + timedelta(days=d)
        wd = day.weekday()
        for h in range(24):
            w = hourly[(wd, h)] * scale
            if cfg.get("dead_slot") and wd == DEAD_SLOT["weekday"] and h in DEAD_SLOT["hours"]:
                w *= 0.08
            n_orders = max(0, round(rng.gauss(w, max(0.5, w * 0.25)))) if w > 0 else 0
            for _ in range(n_orders):
                n_order += 1
                oid = f"{rid}_o{n_order:06d}"
                placed = datetime(day.year, day.month, day.day, h, rng.randint(0, 59), rng.randint(0, 59))
                channel = "sponsored" if rng.random() < sponsored_share else "organic"
                n_lines = rng.choices([1, 2, 3, 4], weights=[0.25, 0.4, 0.25, 0.1])[0]
                chosen = {it["menu_item_id"]: it for it in rng.choices(items, weights=weights, k=n_lines)}
                for it in chosen.values():
                    rows.append(
                        {
                            "order_id": oid,
                            "restaurant_id": rid,
                            "menu_item_id": it["menu_item_id"],
                            "qty": rng.choices([1, 2, 3], weights=[0.75, 0.2, 0.05])[0],
                            "unit_price_eur": it["price_eur"],
                            "placed_at": placed,
                            "channel": channel,
                        }
                    )
    return rows


def _make_reviews(
    rng: random.Random,
    cfg: dict[str, Any],
    items: list[dict[str, Any]],
    orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """~6% of orders get a review. Planted complaints are attached to the target item."""
    rid = cfg["restaurant_id"]
    by_name = {it["name"]: it for it in items}
    order_ids = sorted({o["order_id"] for o in orders})
    lines_by_order: dict[str, list[dict[str, Any]]] = {}
    for o in orders:
        lines_by_order.setdefault(o["order_id"], []).append(o)
    item_by_id = {it["menu_item_id"]: it for it in items}

    reviews: list[dict[str, Any]] = []
    n = 0

    def add(oid: str, item: dict[str, Any] | None, rating: int, template: str) -> None:
        nonlocal n
        n += 1
        placed = lines_by_order[oid][0]["placed_at"]
        text = template.format(cuisine=cfg["cuisine"].lower(), item=item["name"] if item else "the food")
        reviews.append(
            {
                "review_id": f"{rid}_rv{n:05d}",
                "restaurant_id": rid,
                "order_id": oid,
                "menu_item_id": item["menu_item_id"] if item else None,
                "rating": rating,
                "text": text,
                "created_at": placed + timedelta(hours=rng.randint(1, 30)),
            }
        )

    for oid in rng.sample(order_ids, k=int(len(order_ids) * 0.06)):
        item = item_by_id[rng.choice(lines_by_order[oid])["menu_item_id"]]
        roll = rng.random()
        if roll < 0.68:
            add(oid, item if rng.random() < 0.5 else None, rng.choice([4, 5, 5]), rng.choice(GOOD))
        elif roll < 0.88:
            add(oid, item if rng.random() < 0.5 else None, 3, rng.choice(MEH))
        else:
            add(oid, item if rng.random() < 0.5 else None, rng.choice([1, 2, 2]), rng.choice(BAD))

    for key, templates in (("soggy_item", SOGGY), ("cold_item", COLD)):
        if key not in cfg:
            continue
        item = by_name[cfg[key]]
        with_item = [
            oid
            for oid, ls in lines_by_order.items()
            if any(line["menu_item_id"] == item["menu_item_id"] for line in ls)
        ]
        for oid in rng.sample(with_item, k=min(14, len(with_item))):
            add(oid, item, rng.choice([1, 2, 2]), rng.choice(templates))

    reviews.sort(key=lambda r: r["created_at"])
    return reviews


def _make_ads(
    rng: random.Random, cfg: dict[str, Any], orders: list[dict[str, Any]], weeks: int
) -> list[dict[str, Any]]:
    rid = cfg["restaurant_id"]
    if not cfg.get("wasted_ads"):
        return []
    # Sponsored listing running all week; most sponsored orders land in slots that were busy anyway.
    sponsored_orders = len({o["order_id"] for o in orders if o["channel"] == "sponsored"})
    budget = 120.0
    return [
        {
            "campaign_id": f"{rid}_ad1",
            "restaurant_id": rid,
            "campaign_type": "sponsored_listing",
            "status": "active",
            "weekly_budget_eur": budget,
            "start_date": END_DATE - timedelta(days=weeks * 7 - 1),
            "end_date": None,
            "impressions": int(sponsored_orders * rng.uniform(60, 90)),
            "clicks": int(sponsored_orders * rng.uniform(4, 6)),
            "attributed_orders": sponsored_orders,
            "spend_eur": round(budget * weeks, 2),
        }
    ]


def _make_listing(cfg: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "restaurant_id": cfg["restaurant_id"],
        "name": cfg["name"],
        "cuisine": cfg["cuisine"],
        "description": f"{cfg['cuisine']} food delivered in {cfg['city']}.",
        "tags": [cfg["cuisine"].lower()],
        "opening_hours": {d: "11:00-23:00" for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        "menu": [
            {
                "menu_item_id": it["menu_item_id"],
                "name": it["name"],
                "category": it["category"],
                "description": it["description"],
                "price_eur": it["price_eur"],
                "photo_url": it["photo_url"],
                "is_available": it["is_available"],
            }
            for it in items
        ],
        "promotions": [],
        "sponsored_listing": {
            "active": bool(cfg.get("wasted_ads")),
            "weekly_budget_eur": 120.0 if cfg.get("wasted_ads") else 0.0,
        },
    }


# ------------------------------------------------------------------- insert


def _insert(con: Any, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in cols)
    con.executemany(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
        [[r[c] for c in cols] for r in rows],
    )


def seed(db_path: Path | str = DB_PATH, weeks: int = 8, verbose: bool = True) -> dict[str, int]:
    """Rebuild the database from scratch. Returns row counts per table."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    con = connect(db_path)
    create_schema(con)
    counts: dict[str, int] = {}

    for cfg in RESTAURANTS:
        rng = random.Random(f"{SEED}:{cfg['restaurant_id']}")
        items = _make_menu(rng, cfg)
        weights = _item_weights(rng, items, cfg)
        if cfg.get("missing_photos"):
            for it in items:
                if it.get("_bestseller"):
                    it["photo_url"] = None
        orders = _make_orders(rng, cfg, items, weights, weeks)
        reviews = _make_reviews(rng, cfg, items, orders)
        comps, comp_items = _make_competitors(rng, cfg)
        ads = _make_ads(rng, cfg, orders, weeks)
        listing = _make_listing(cfg, items)

        _insert(
            con,
            "restaurants",
            [
                {
                    "restaurant_id": cfg["restaurant_id"],
                    "name": cfg["name"],
                    "cuisine": cfg["cuisine"],
                    "city": cfg["city"],
                    "postcode": cfg["postcode"],
                    "delivery_model": cfg["delivery_model"],
                    "commission_rate": COMMISSION[cfg["delivery_model"]],
                    "rating": cfg["rating"],
                    "joined_at": date(2024, rng.randint(1, 12), rng.randint(1, 28)),
                }
            ],
        )
        _insert(con, "menu_items", [{k: v for k, v in it.items() if not k.startswith("_")} for it in items])
        _insert(con, "orders", orders)
        _insert(con, "reviews", reviews)
        _insert(con, "competitors", comps)
        _insert(con, "competitor_menu_items", comp_items)
        _insert(con, "ad_campaigns", ads)
        con.execute(
            "INSERT INTO listings VALUES (?, ?, ?)",
            [cfg["restaurant_id"], json.dumps(listing), datetime.combine(END_DATE, datetime.min.time())],
        )

    for table in [
        "restaurants",
        "menu_items",
        "orders",
        "reviews",
        "competitors",
        "competitor_menu_items",
        "ad_campaigns",
        "listings",
        "change_log",
    ]:
        counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    con.close()

    if verbose:
        print(f"Seeded {db_path}")
        for t, n in counts.items():
            print(f"  {t:<22} {n:>7}")
    return counts


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--weeks", type=int, default=8)
    args = p.parse_args()
    seed(args.db, args.weeks)


if __name__ == "__main__":
    main()
