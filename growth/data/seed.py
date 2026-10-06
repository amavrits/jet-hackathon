"""Deterministic synthetic data with planted, discoverable problems.

Planted problems (the demo and tests depend on these):
- r1 Bella Napoli:  repeated "soggy" reviews on the Calzone
- r2 Sakura Sushi:  Rolls priced ~25% above the competitor median; high rating, low volume
- r3 Burger Barn:   bestsellers have no photo and a one-word description
- r4 Curry House:   Tuesday afternoon dead slot
- r5 Taco Loco:     sponsored listing campaign that does not pay back for the partner
- r6 Green Bowl:    control restaurant with no planted problem
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from growth.data import queries

SEED = 42
START = datetime(2026, 8, 3)  # a Monday
WEEKS = 8
HOUR_WEIGHTS = {
    11: 0.5, 12: 1.2, 13: 1.0, 14: 0.5, 15: 0.4, 16: 0.5,
    17: 1.0, 18: 1.8, 19: 2.0, 20: 1.5, 21: 0.8, 22: 0.4,
}  # fmt: skip
WEEKDAY_WEIGHTS = [0.8, 0.85, 0.9, 1.0, 1.3, 1.4, 1.2]
BASE_ORDERS_PER_HOUR = 3.0
COMMISSION = {"marketplace": 0.15, "jet_delivery": 0.30}

# (name, category, price, description, has_photo, popularity weight)
RESTAURANTS: list[dict] = [
    {
        "id": "r1",
        "name": "Bella Napoli",
        "cuisine": "Pizza",
        "city": "Amsterdam",
        "delivery_model": "jet_delivery",
        "volume": 1.2,
        "rating_weights": [0.04, 0.06, 0.15, 0.35, 0.40],
        "menu": [
            ("Margherita", "Pizza", 10.50, "Tomato, fior di latte, basil, olive oil.", True, 5),
            ("Pepperoni", "Pizza", 12.50, "Tomato, mozzarella, spicy pepperoni.", True, 4),
            (
                "Quattro Formaggi",
                "Pizza",
                13.50,
                "Mozzarella, gorgonzola, fontina, parmesan.",
                True,
                2,
            ),
            ("Calzone", "Pizza", 13.00, "Folded pizza with ham, ricotta and mozzarella.", True, 3),
            ("Garlic Bread", "Sides", 5.00, "Wood-fired bread with garlic butter.", True, 3),
            (
                "Tiramisu",
                "Desserts",
                6.50,
                "Mascarpone, espresso-soaked savoiardi, cocoa.",
                True,
                2,
            ),
            ("Cola", "Drinks", 2.75, "Ice-cold 330 ml can.", True, 3),
        ],
    },
    {
        "id": "r2",
        "name": "Sakura Sushi",
        "cuisine": "Sushi",
        "city": "Rotterdam",
        "delivery_model": "marketplace",
        "volume": 0.7,
        "rating_weights": [0.0, 0.0, 0.05, 0.25, 0.70],
        "menu": [
            ("Salmon Roll", "Rolls", 11.90, "Eight pieces of salmon and avocado uramaki.", True, 5),
            ("Tuna Roll", "Rolls", 12.90, "Eight pieces of tuna and cucumber uramaki.", True, 3),
            (
                "Dragon Roll",
                "Rolls",
                15.90,
                "Prawn tempura topped with avocado and eel sauce.",
                True,
                3,
            ),
            (
                "California Roll",
                "Rolls",
                10.90,
                "Surimi, avocado and cucumber, sesame outside.",
                True,
                3,
            ),
            ("Gyoza", "Starters", 6.50, "Five pan-fried chicken dumplings with ponzu.", True, 3),
            ("Miso Soup", "Sides", 3.50, "Dashi broth, tofu, wakame and spring onion.", True, 2),
            ("Edamame", "Sides", 4.50, "Steamed soybeans with sea salt.", True, 2),
        ],
    },
    {
        "id": "r3",
        "name": "Burger Barn",
        "cuisine": "Burgers",
        "city": "Utrecht",
        "delivery_model": "marketplace",
        "volume": 1.0,
        "rating_weights": [0.04, 0.06, 0.15, 0.35, 0.40],
        "menu": [
            ("Classic Burger", "Burgers", 11.50, "Burger.", False, 6),
            ("Cheese Burger", "Burgers", 12.50, "Burger.", False, 5),
            ("Fries", "Sides", 3.95, "Fries.", False, 6),
            (
                "Veggie Burger",
                "Burgers",
                12.00,
                "Black bean patty, lettuce, chipotle mayo.",
                True,
                2,
            ),
            ("Onion Rings", "Sides", 4.50, "Beer-battered onion rings with smoky dip.", True, 2),
            ("Milkshake", "Drinks", 4.95, "Vanilla, chocolate or strawberry, 400 ml.", True, 2),
        ],
    },
    {
        "id": "r4",
        "name": "Curry House",
        "cuisine": "Indian",
        "city": "Den Haag",
        "delivery_model": "jet_delivery",
        "volume": 0.9,
        "rating_weights": [0.04, 0.06, 0.15, 0.35, 0.40],
        "dead_slot": (1, range(14, 17)),  # Tuesday afternoon
        "menu": [
            (
                "Butter Chicken",
                "Curries",
                14.50,
                "Tandoori chicken in a creamy tomato sauce.",
                True,
                5,
            ),
            (
                "Lamb Rogan Josh",
                "Curries",
                16.00,
                "Slow-cooked lamb with Kashmiri chilli.",
                True,
                3,
            ),
            (
                "Chana Masala",
                "Curries",
                12.00,
                "Chickpeas in a spiced onion-tomato gravy.",
                True,
                2,
            ),
            ("Garlic Naan", "Breads", 3.50, "Tandoor-baked flatbread with garlic butter.", True, 5),
            ("Pilau Rice", "Sides", 3.50, "Basmati rice with cardamom and saffron.", True, 4),
            (
                "Mango Lassi",
                "Drinks",
                4.00,
                "Yoghurt, Alphonso mango, a pinch of cardamom.",
                True,
                2,
            ),
        ],
    },
    {
        "id": "r5",
        "name": "Taco Loco",
        "cuisine": "Mexican",
        "city": "Eindhoven",
        "delivery_model": "marketplace",
        "volume": 0.8,
        "rating_weights": [0.04, 0.06, 0.15, 0.35, 0.40],
        "campaign": {"weekly_budget_eur": 120.0, "attributed_orders": 14, "active": True},
        "menu": [
            (
                "Tacos al Pastor",
                "Tacos",
                10.50,
                "Three corn tortillas, marinated pork, pineapple.",
                True,
                5,
            ),
            (
                "Chicken Burrito",
                "Burritos",
                11.50,
                "Rice, black beans, chicken tinga, salsa roja.",
                True,
                4,
            ),
            (
                "Veggie Quesadilla",
                "Burritos",
                9.50,
                "Cheese, peppers and corn in a flour tortilla.",
                True,
                2,
            ),
            ("Nachos", "Sides", 7.50, "Tortilla chips, cheese, jalapeño, guacamole.", True, 3),
            ("Churros", "Desserts", 5.50, "Cinnamon sugar churros with chocolate dip.", True, 2),
            ("Horchata", "Drinks", 3.95, "Rice and cinnamon drink, served cold.", True, 2),
        ],
    },
    {
        "id": "r6",
        "name": "Green Bowl",
        "cuisine": "Salads",
        "city": "Amsterdam",
        "delivery_model": "jet_delivery",
        "volume": 1.3,
        "rating_weights": [0.03, 0.05, 0.15, 0.37, 0.40],
        "menu": [
            (
                "Poke Bowl",
                "Bowls",
                13.50,
                "Salmon, sushi rice, edamame, mango, sesame dressing.",
                True,
                5,
            ),
            (
                "Falafel Bowl",
                "Bowls",
                12.00,
                "Falafel, hummus, tabbouleh, pickled red onion.",
                True,
                4,
            ),
            (
                "Chicken Caesar",
                "Salads",
                11.50,
                "Grilled chicken, romaine, parmesan, croutons.",
                True,
                3,
            ),
            (
                "Quinoa Salad",
                "Salads",
                10.50,
                "Quinoa, roasted squash, feta, pomegranate.",
                True,
                2,
            ),
            ("Green Smoothie", "Drinks", 5.00, "Spinach, apple, banana, ginger.", True, 2),
        ],
    },
]

# Item-level complaints planted on purpose: (restaurant id, item name, keyword, texts)
PLANTED_COMPLAINTS = [
    ("r1", "Calzone", "soggy", [
        "Calzone arrived completely soggy, the dough was wet inside.",
        "Soggy calzone again. Pizzas are great but this one travels badly.",
        "The calzone was soggy and lukewarm by the time it got here.",
        "Bottom of the calzone was soggy, could not finish it.",
        "Love the margherita, but the calzone was soggy and steamed in the box.",
        "Soggy dough on the calzone. Please fix the packaging.",
    ]),
]  # fmt: skip

GENERIC_POSITIVE = [
    "Great food, arrived hot and on time.",
    "Really tasty, will order again.",
    "Good portions and friendly driver.",
    "Solid as always.",
    "Lovely flavours, well packed.",
]
GENERIC_MIXED = [
    "Fine, nothing special.",
    "Decent but a bit pricey for the portion.",
    "Took a while to arrive but the food was OK.",
]
GENERIC_NEGATIVE = [
    "Delivery was very late.",
    "Part of my order was missing.",
    "Driver could not find the address, took ages.",
]
ITEM_PRAISE = "The {item} was excellent."


def _menu_frame() -> pd.DataFrame:
    rows, item_id = [], 1
    for r in RESTAURANTS:
        for name, cat, price, desc, photo, _ in r["menu"]:
            slug = name.lower().replace(" ", "-")
            rows.append({
                "id": item_id, "restaurant_id": r["id"], "name": name, "category": cat,
                "price_eur": price, "description": desc,
                "photo_url": f"photos/{r['id']}/{slug}.jpg" if photo else None,
            })  # fmt: skip
            item_id += 1
    return pd.DataFrame(rows)


def _orders_frame(rng: np.random.Generator, menu: pd.DataFrame) -> pd.DataFrame:
    rows, order_id = [], 1
    for r in RESTAURANTS:
        items = menu[menu.restaurant_id == r["id"]]
        weights = np.array([m[5] for m in r["menu"]], dtype=float)
        weights /= weights.sum()
        dead_day, dead_hours = r.get("dead_slot", (None, range(0)))
        for day in range(WEEKS * 7):
            date = START + timedelta(days=day)
            wd = date.weekday()
            for hour, hw in HOUR_WEIGHTS.items():
                lam = BASE_ORDERS_PER_HOUR * hw * WEEKDAY_WEIGHTS[wd] * r["volume"]
                if wd == dead_day and hour in dead_hours:
                    lam *= 0.15
                for _ in range(rng.poisson(lam)):
                    ts = date + timedelta(hours=hour, minutes=int(rng.integers(0, 60)))
                    n_lines = int(rng.choice([1, 2, 3], p=[0.35, 0.45, 0.20]))
                    picks = rng.choice(len(items), size=n_lines, replace=False, p=weights)
                    for line, idx in enumerate(picks, start=1):
                        item = items.iloc[int(idx)]
                        rows.append({
                            "order_id": order_id, "line": line, "restaurant_id": r["id"],
                            "item_id": int(item.id), "qty": int(rng.choice([1, 2], p=[0.85, 0.15])),
                            "unit_price_eur": float(item.price_eur), "ts": ts,
                        })  # fmt: skip
                    order_id += 1
    return pd.DataFrame(rows)


def _reviews_frame(rng: np.random.Generator, menu: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    period_days = WEEKS * 7

    def add(rid: str, rating: int, text: str, item_id: int | None) -> None:
        ts = START + timedelta(days=int(rng.integers(0, period_days)), hours=19)
        rows.append({"id": len(rows) + 1, "restaurant_id": rid, "item_id": item_id,
                     "rating": rating, "text": text, "ts": ts})  # fmt: skip

    for r in RESTAURANTS:
        items = menu[menu.restaurant_id == r["id"]]
        for _ in range(40):
            rating = int(rng.choice([1, 2, 3, 4, 5], p=r["rating_weights"]))
            if rating >= 4 and rng.random() < 0.4:
                item = items.iloc[int(rng.integers(0, len(items)))]
                add(r["id"], rating, ITEM_PRAISE.format(item=item["name"]), int(item.id))
            elif rating >= 4:
                add(r["id"], rating, str(rng.choice(GENERIC_POSITIVE)), None)
            elif rating == 3:
                add(r["id"], rating, str(rng.choice(GENERIC_MIXED)), None)
            else:
                add(r["id"], rating, str(rng.choice(GENERIC_NEGATIVE)), None)

    for rid, item_name, _, texts in PLANTED_COMPLAINTS:
        item_id = int(menu[(menu.restaurant_id == rid) & (menu.name == item_name)].id.iloc[0])
        for text in texts:
            add(rid, int(rng.choice([1, 2])), text, item_id)
    return pd.DataFrame(rows)


def _competitors_frame(rng: np.random.Generator) -> pd.DataFrame:
    rows: list[dict] = []
    for r in RESTAURANTS:
        # Sakura's Rolls are planted ~25% above market; everyone else is priced at market.
        markup = {"Rolls": 1.25} if r["id"] == "r2" else {}
        for c in range(1, 5):
            competitor = f"{r['cuisine']} competitor {c} ({r['city']})"
            for name, cat, price, *_ in r["menu"]:
                market = price / markup.get(cat, 1.0)
                rows.append({
                    "id": len(rows) + 1, "restaurant_id": r["id"], "competitor_name": competitor,
                    "category": cat, "item_name": f"{name} (comp {c})",
                    "price_eur": round(market * float(rng.uniform(0.92, 1.08)), 2),
                })  # fmt: skip
    return pd.DataFrame(rows)


def _campaigns_frame() -> pd.DataFrame:
    end = START + timedelta(days=WEEKS * 7 - 1)
    rows = [
        # Bella Napoli: a campaign that pays back, as a contrast.
        {"restaurant_id": "r1", "weekly_budget_eur": 60.0, "attributed_orders": 48,
         "active": True},
    ]  # fmt: skip
    for r in RESTAURANTS:
        if "campaign" in r:
            rows.append({"restaurant_id": r["id"], **r["campaign"]})
    df = pd.DataFrame(rows)
    df.insert(0, "id", range(1, len(df) + 1))
    df["start_date"] = (end - timedelta(days=27)).date()
    df["end_date"] = end.date()
    df["impressions"] = df.weekly_budget_eur.astype(int) * 75
    return df


def _listing(r: dict, menu: pd.DataFrame, campaigns: pd.DataFrame) -> dict:
    items = menu[menu.restaurant_id == r["id"]]
    camp = campaigns[(campaigns.restaurant_id == r["id"]) & campaigns.active]
    return {
        "restaurant_id": r["id"],
        "name": r["name"],
        "cuisine": r["cuisine"],
        "city": r["city"],
        "menu": [
            {
                "item_id": int(i.id),
                "name": i["name"],
                "category": i.category,
                "price_eur": float(i.price_eur),
                "description": i.description,
                "photo_url": i.photo_url if isinstance(i.photo_url, str) else None,
                "available": True,
            }  # fmt: skip
            for _, i in items.iterrows()
        ],
        "promotions": [],
        "sponsored": {
            "active": not camp.empty,
            "weekly_budget_eur": float(camp.weekly_budget_eur.sum()) if not camp.empty else 0.0,
        },
    }


def seed(path: Path | None = None) -> Path:
    path = path or queries.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    rng = np.random.default_rng(SEED)

    restaurants = pd.DataFrame([
        {"id": r["id"], "name": r["name"], "cuisine": r["cuisine"], "city": r["city"],
         "delivery_model": r["delivery_model"], "commission_rate": COMMISSION[r["delivery_model"]]}
        for r in RESTAURANTS
    ])  # fmt: skip
    menu = _menu_frame()
    orders = _orders_frame(rng, menu)
    reviews = _reviews_frame(rng, menu)
    competitors = _competitors_frame(rng)
    campaigns = _campaigns_frame()
    listings = pd.DataFrame([
        {"restaurant_id": r["id"], "listing": json.dumps(_listing(r, menu, campaigns)),
         "updated_at": datetime.now()}
        for r in RESTAURANTS
    ])  # fmt: skip

    with queries.connect(path) as con:
        queries.create_schema(con)
        for table, df in [
            ("restaurants", restaurants), ("menu_items", menu), ("orders", orders),
            ("reviews", reviews), ("competitors", competitors), ("ad_campaigns", campaigns),
            ("listings", listings),
        ]:  # fmt: skip
            queries.insert_frame(con, table, df)
    return path


if __name__ == "__main__":
    out = seed()
    print(f"Seeded {out}")
