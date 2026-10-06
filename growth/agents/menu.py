"""Menu agent: photos and descriptions on bestsellers.

Rule-based stub. Description copy is a template; an LLM version can write better copy.
"""

from growth.state import Context, Evidence, MenuItemStats, Recommendation

BESTSELLER_COUNT = 3
MIN_DESCRIPTION_CHARS = 25


def _bestsellers(ctx: Context) -> list[MenuItemStats]:
    return sorted(ctx.menu, key=lambda m: m.orders_per_week, reverse=True)[:BESTSELLER_COUNT]


def _evidence(item: MenuItemStats, problem: str) -> list[Evidence]:
    return [
        Evidence(
            kind="menu_item",
            ref=f"menu_items.id={item.item_id}",
            detail=f"{item.name}: {problem}",
            row_id=item.item_id,
        ),
        Evidence(
            kind="order_stat",
            ref=f"orders: item_id={item.item_id}, last 8 weeks",
            detail=f"{item.orders_per_week:.0f} sold per week (top {BESTSELLER_COUNT})",
        ),
    ]


def _listing_index(ctx: Context, item_id: int) -> int:
    for i, entry in enumerate(ctx.listing["menu"]):
        if entry["item_id"] == item_id:
            return i
    raise KeyError(item_id)


def _draft_description(item: MenuItemStats) -> str:
    return f"{item.name}: one of our most-ordered {item.category.lower()}, made fresh to order."


def run(ctx: Context) -> list[Recommendation]:
    top = _bestsellers(ctx)
    recs = []

    no_photo = [m for m in top if not m.has_photo]
    if no_photo:
        names = ", ".join(m.name for m in no_photo)
        recs.append(
            Recommendation(
                id="menu-photos",
                agent="menu",
                title=f"Add photos to {len(no_photo)} bestseller(s): {names}",
                rationale=(
                    "Your most-ordered dishes show no photo on the listing. The owner needs to "
                    "upload the photos, so this is advice only and changes nothing automatically."
                ),
                evidence=[e for m in no_photo for e in _evidence(m, "no photo")],
                patch=None,
                impact_basis={"kind": "photos", "item_ids": [m.item_id for m in no_photo]},
                confidence="high",
            )
        )

    short = [m for m in top if len(m.description) < MIN_DESCRIPTION_CHARS]
    if short:
        patch = []
        for m in short:
            idx = _listing_index(ctx, m.item_id)
            patch += [
                {"op": "test", "path": f"/menu/{idx}/item_id", "value": m.item_id},
                {
                    "op": "replace",
                    "path": f"/menu/{idx}/description",
                    "value": _draft_description(m),
                },
            ]
        recs.append(
            Recommendation(
                id="menu-descriptions",
                agent="menu",
                title=f"Rewrite {len(short)} bestseller description(s)",
                rationale=(
                    f"Descriptions under {MIN_DESCRIPTION_CHARS} characters "
                    f"({', '.join(repr(m.description) for m in short)}) tell customers nothing. "
                    "The draft copy is a template; the owner should check it before approving."
                ),
                evidence=[
                    e for m in short for e in _evidence(m, f"description is '{m.description}'")
                ],
                patch=patch,
                impact_basis={"kind": "descriptions", "item_ids": [m.item_id for m in short]},
                confidence="med",
            )
        )
    return recs
