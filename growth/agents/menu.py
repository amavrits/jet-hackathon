"""Menu agent: listing hygiene that costs the restaurant orders.

Findings (all deterministic from sales data):
  add_photos        bestsellers (top 3 by units) with no photo. Advice-only: we can't take the photo.
  add_descriptions  dishes with no description. The patch fills them in, written by growth.llm
                    from the dish name only; a plain template is used if the LLM is unavailable.
  prune_item        a dish selling < 10% of the menu's median units. The patch hides it.

Bundles are deliberately not built: the seed has no co-ordering signal, so any bundle would be
a guess, and the demo doesn't show one.
"""

from __future__ import annotations

from statistics import median

import duckdb
from pydantic import BaseModel

from growth import llm
from growth.agents.common import WEEKS, base_facts, per_week, slug
from growth.data import queries as q
from growth.state import EvidenceRef, LeverChange, Recommendation

BESTSELLERS = 3
DEAD_ITEM_SHARE = 0.10  # of median item units

DESCRIPTION_PROMPT = """You write menu descriptions for a delivery app.
For each dish, write one appetising sentence of at most 15 words.
Describe only what the dish name and cuisine imply. No prices, no claims about awards, sourcing, or ratings."""


class DishDescription(BaseModel):
    menu_item_id: str
    description: str


class Descriptions(BaseModel):
    items: list[DishDescription]


def _write_descriptions(cuisine: str, dishes: list[dict]) -> dict[str, str]:
    """menu_item_id -> description. LLM first, template fallback per missing dish."""
    user = f"Cuisine: {cuisine}\n" + "\n".join(f"- {d['menu_item_id']}: {d['name']} ({d['category']})" for d in dishes)
    out = llm.call_structured(Descriptions, DESCRIPTION_PROMPT, user)
    written = {i.menu_item_id: i.description.strip() for i in out.items} if out else {}
    return {
        d["menu_item_id"]: written.get(d["menu_item_id"])
        or f"Our {d['name']}, a {cuisine} {d['category'].lower().rstrip('s')} made fresh to order."
        for d in dishes
    }


def run(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> list[Recommendation]:
    restaurant = q.get_restaurant(con, restaurant_id)
    if restaurant is None:
        return []
    sales = q.item_sales(con, restaurant_id, weeks=WEEKS)  # sorted by units desc
    base = base_facts(con, restaurant_id)
    levers = q.current_levers(con, restaurant_id)
    n_menu = len(sales)
    recs: list[Recommendation] = []

    # --- bestsellers without photos
    no_photo = [s for s in sales[:BESTSELLERS] if s["photo_url"] is None]
    if no_photo:
        names = ", ".join(s["name"] for s in no_photo)
        orders_pw = sum(per_week(s["orders"]) for s in no_photo)
        recs.append(
            Recommendation(
                id=f"menu-{restaurant_id}-photos",
                agent="menu",
                kind="add_photos",
                title=f"Add photos to your top sellers: {names}",
                rationale=(
                    f"{len(no_photo)} of your {BESTSELLERS} best-selling dishes have no photo. "
                    f"Together they appear in {orders_pw:.0f} orders a week, so they are what most customers see first."
                ),
                action="Upload one clear, well-lit photo for each of these dishes.",
                evidence=[
                    EvidenceRef(
                        kind="menu_item",
                        ref=s["menu_item_id"],
                        detail=f"{s['name']}: #{sales.index(s) + 1} by units, no photo",
                        value=per_week(s["orders"]),
                    )
                    for s in no_photo
                ],
                facts={**base, "item_orders_per_week": orders_pw, "items": float(len(no_photo))},
                lever_changes=[
                    LeverChange(
                        lever="photo_share",
                        before=levers["photo_share"],
                        after=round(min(1.0, levers["photo_share"] + len(no_photo) / n_menu), 3),
                        note=f"+{len(no_photo)} of {n_menu} dishes with a photo",
                    )
                ],
                confidence="high",
            )
        )

    # --- missing descriptions: the patch writes them
    no_desc = [s for s in sales if not s["description"]]
    if no_desc:
        texts = _write_descriptions(restaurant["cuisine"], no_desc)
        orders_pw = sum(per_week(s["orders"]) for s in no_desc)
        recs.append(
            Recommendation(
                id=f"menu-{restaurant_id}-descriptions",
                agent="menu",
                kind="add_descriptions",
                title=f"Add descriptions to {len(no_desc)} dishes",
                rationale=(
                    f"{len(no_desc)} of {len(sales)} dishes have no description, "
                    f"covering {orders_pw:.0f} orders a week. Customers skip dishes they can't picture."
                ),
                action="Review the suggested descriptions below and approve to publish them.",
                evidence=[
                    EvidenceRef(kind="menu_item", ref=s["menu_item_id"], detail=f"{s['name']}: no description")
                    for s in no_desc
                ],
                facts={**base, "item_orders_per_week": orders_pw, "items": float(len(no_desc))},
                lever_changes=[
                    LeverChange(
                        lever="description_share",
                        before=levers["description_share"],
                        after=round(min(1.0, levers["description_share"] + len(no_desc) / n_menu), 3),
                        note=f"+{len(no_desc)} of {n_menu} dishes with a description",
                    )
                ],
                patch=[
                    {
                        "op": "replace",
                        "path": f"/menu/{s['menu_item_id']}/description",
                        "value": texts[s["menu_item_id"]],
                    }
                    for s in no_desc
                ],
                confidence="med",
            )
        )

    # --- dishes that almost never sell
    med_units = median(s["units"] for s in sales) if sales else 0
    for s in sales:
        if med_units and s["units"] < DEAD_ITEM_SHARE * med_units:
            recs.append(
                Recommendation(
                    id=f"menu-{restaurant_id}-prune-{slug(s['name'])}",
                    agent="menu",
                    kind="prune_item",
                    title=f"Hide {s['name']} from the menu",
                    rationale=(
                        f"{s['name']} sold {s['units']} times in {WEEKS} weeks, against a menu median of "
                        f"{med_units:.0f}. It takes menu space and prep stock without earning it."
                    ),
                    action=f"Hide {s['name']} for now. It can be switched back on at any time.",
                    evidence=[
                        EvidenceRef(
                            kind="order_stat",
                            ref=s["menu_item_id"],
                            detail=f"{s['units']} units vs median {med_units:.0f}",
                            value=float(s["units"]),
                        )
                    ],
                    facts={**base, "units_8w": float(s["units"]), "median_units_8w": float(med_units)},
                    patch=[{"op": "replace", "path": f"/menu/{s['menu_item_id']}/is_available", "value": False}],
                    confidence="high",
                )
            )

    return recs
