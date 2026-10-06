"""Demo partner restaurants: item-level orders, reviews, menus, listings, planted problems.

Called by runners/generate_data.py, which owns the database and the market around the partners.
Build the database with:  python -m runners.generate_data

Every restaurant gets a realistic baseline (menu, 8 weeks of item-level orders,
reviews, nearby competitors, listing). On top of that, `PLANTED` lists the
problems the demo must be able to find. Tests import `PLANTED` as the oracle.
"""

from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta
from typing import Any

SEED = 42
END_DATE = date(2026, 10, 5)  # last day of data (a Monday)

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
        "lat": 52.3637,
        "lon": 4.892,
        "price_level": 2,
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
        "lat": 52.368,
        "lon": 4.865,
        "price_level": 2,
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
        "lat": 51.92,
        "lon": 4.485,
        "price_level": 1,
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
        "lat": 52.092,
        "lon": 5.118,
        "price_level": 2,
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
        "lat": 52.356,
        "lon": 4.885,
        "price_level": 3,
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
        "lat": 52.079,
        "lon": 4.315,
        "price_level": 2,
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
        "lat": 51.918,
        "lon": 4.47,
        "price_level": 1,
        "name": "Pho House",
        "cuisine": "Vietnamese",
        "city": "Rotterdam",
        "postcode": "3014",
        "delivery_model": "jet_delivery",
        "rating": 4.6,
        "daily_orders": 28,
        "late_delivery": True,
        "overpriced_category": "Starters",
        "missing_descriptions": True,
    },
    {
        "restaurant_id": "r_petit_bistro",
        "lat": 52.087,
        "lon": 5.124,
        "price_level": 3,
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
            "late_delivery",
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
    "Courier took over an hour, food had been sitting somewhere for ages.",
    "Driver went to the wrong address first, everything was cold by the time it got here.",
]
# Planted texture complaint: deliberately varied so a keyword search for "soggy" misses most of them.
SOGGY = [
    "{item} was completely soggy by the time it arrived.",
    "The {item} turned to mush in the box.",
    "Bottom of the {item} was wet and falling apart, steam must get trapped in the packaging.",
    "{item} had zero crunch left, limp and greasy.",
    "Not crispy at all. The {item} was like wet cardboard.",
    "Had to throw half the {item} away, the dough was sodden.",
    "The {item} sweats in that closed container, every time it ends up floppy.",
    "Love the place but the {item} never survives the trip, it arrives a damp mess.",
    "{item} was chewy and wet instead of crisp. Rest of the order was great.",
    "Second time the {item} came limp. Won't order it again.",
]
COLD = [
    "{item} arrived cold even though the rest of the order was hot.",
    "Had to microwave the {item}, it was barely room temperature.",
    "The {item} was stone cold, the other dishes were fine so it was not the driver.",
    "Everything else steaming, but the {item} was lukewarm and the sauce had congealed.",
    "{item} tastes like it was cooked an hour before the order. Fridge cold in the middle.",
    "The {item} was not warm at all, noodles stuck together in a cold lump.",
]

# Planted courier problem (JET-delivered restaurant): about the delivery, never about one dish.
LATE = [
    "Waited 75 minutes, the app kept saying the courier was 5 minutes away.",
    "Food was ready on time according to the tracker but the rider only picked it up 40 min later.",
    "Driver cycled around the block twice, couldn't find us, everything arrived cold.",
    "Order sat at the restaurant waiting for a courier for ages. Not the restaurant's fault.",
    "Delivery estimate jumped from 30 to 70 minutes after I ordered.",
    "Rider had three other orders on him, ours came last and lukewarm.",
    "Courier marked it delivered before he even arrived.",
    "Friday night and it took well over an hour, soup was barely warm.",
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


def _make_competitors(
    rng: random.Random, cfg: dict[str, Any], market: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The 5 nearest same-cuisine market restaurants, with menus priced around the *baseline* prices.

    Competitor menus are only generated for these; the rest of the market exists in the weekly panel.
    """
    rid = cfg["restaurant_id"]
    same = [m for m in market if m["cuisine"] == cfg["cuisine"] and not m.get("is_partner")]
    nearest = sorted(same, key=lambda m: haversine_km(cfg["lat"], cfg["lon"], m["lat"], m["lon"]))[:5]
    comps, comp_items = [], []
    for m in nearest:
        cid = m["restaurant_id"]
        comps.append(
            {
                "competitor_id": cid,
                "restaurant_id": rid,
                "name": m["name"],
                "cuisine": m["cuisine"],
                "distance_km": round(haversine_km(cfg["lat"], cfg["lon"], m["lat"], m["lon"]), 2),
                "rating": m["rating"],
                "is_sponsored": bool(m.get("has_ads")),
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


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


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

    if cfg.get("late_delivery"):
        # Peak-hour orders only: the planted courier problem shows up when the network is busy.
        peak = [oid for oid, ls in lines_by_order.items() if ls[0]["placed_at"].hour in (18, 19, 20)]
        for oid in rng.sample(peak, k=min(30, len(peak))):
            add(oid, None, rng.choice([1, 1, 2]), rng.choice(LATE))

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
        # Keyed by menu_item_id so JSON Patch paths stay stable: /menu/<menu_item_id>/price_eur
        "menu": {
            it["menu_item_id"]: {
                "name": it["name"],
                "category": it["category"],
                "description": it["description"],
                "price_eur": it["price_eur"],
                "photo_url": it["photo_url"],
                "is_available": it["is_available"],
            }
            for it in items
        },
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


def seed_partners(con: Any, market: list[dict[str, Any]], weeks: int = 8) -> None:
    """Insert the demo partners with item-level detail. Competitors are picked from `market`.

    The DB lifecycle (schema, market, weekly panel) is owned by runners/generate_data.py.
    """
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
        comps, comp_items = _make_competitors(rng, cfg, market)
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
                    "lat": cfg["lat"],
                    "lon": cfg["lon"],
                    "price_level": cfg["price_level"],
                    "is_partner": True,
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
