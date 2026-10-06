"""Reviews agent: recurring complaints per dish.

Rule-based stub. An LLM version can replace `run` without changing its contract.
"""

from collections import defaultdict

from growth.state import Context, Evidence, Recommendation

COMPLAINT_KEYWORDS = ["soggy", "cold", "burnt", "bland", "greasy", "raw", "stale"]
MIN_COMPLAINTS = 3


def run(ctx: Context) -> list[Recommendation]:
    items = {m.item_id: m for m in ctx.menu}
    hits: dict[tuple[int, str], list] = defaultdict(list)
    for review in ctx.reviews:
        if review.item_id is None or review.rating > 2:
            continue
        text = review.text.lower()
        for kw in COMPLAINT_KEYWORDS:
            if kw in text:
                hits[(review.item_id, kw)].append(review)

    recs = []
    for (item_id, kw), reviews in hits.items():
        if len(reviews) < MIN_COMPLAINTS or item_id not in items:
            continue
        item = items[item_id]
        mentions = sum(1 for r in ctx.reviews if r.item_id == item_id)
        recs.append(
            Recommendation(
                id=f"reviews-{item_id}-{kw}",
                agent="reviews",
                title=f"Fix '{kw}' complaints on {item.name}",
                rationale=(
                    f"{len(reviews)} of {mentions} reviews that mention {item.name} rate it 1-2 "
                    f"stars and say '{kw}'. This is a kitchen or packaging fix (for example a "
                    "vented box), not a listing change. Fixing it recovers repeat orders."
                ),
                evidence=[
                    Evidence(
                        kind="review",
                        ref=f"reviews.id={r.id}",
                        detail=f"{r.rating}★ {r.ts}: {r.text}",
                        row_id=r.id,
                    )
                    for r in reviews
                ],
                patch=None,
                impact_basis={"kind": "complaint_fix", "item_id": item_id},
                confidence="high" if len(reviews) >= 5 else "med",
            )
        )
    return recs
