"""Reviews agent: find recurring complaints per dish, and whose fault they are.

Pipeline
  1. SQL: low-rated reviews, each with the dishes in its order.
  2. Jev: one call per review -> is it a complaint, which dish, what issue, whose fault.
  3. Python: keep confident tags, count by (dish, issue). Jev doesn't count; we do.
  4. A group with >= MIN_COMPLAINTS becomes a Recommendation, with review ids as evidence.
     Delivery-caused complaints are pooled into a separate recommendation, because on
     JET-delivered restaurants they are JET's problem, not the kitchen's.
  5. gpt-luna (growth.llm) words the title/rationale/action from the counts and quotes only.
     If it fails, a template is used, since the finding itself is already established.

Advice-only: no listing patch. The fix is in the kitchen or the packaging, not the listing.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from typing import Any

import duckdb
from pydantic import BaseModel

from growth import llm
from growth.classify import Answers, Choice, Noul, ask_many
from growth.data import queries as q
from growth.state import Evidence, Recommendation

log = logging.getLogger(__name__)

MIN_COMPLAINTS = 5  # a group needs this many confident complaints to become a recommendation
COMPLAINT_P = 0.5  # Jev noul threshold for "this is a complaint"
LABEL_CONF = 0.6  # minimum Jev confidence on dish / issue / fault labels
MAX_QUOTES = 8  # evidence rows attached per recommendation
# Delivery complaints per 1,000 orders above which delivery gets its own card. The seeded
# baseline sits at 4-9 for every restaurant; a planted courier problem clears 20.
DELIVERY_PER_1000 = 12.0

GENERAL = "general"

ISSUES: dict[str, str] = {
    "texture": "Food arrived soggy, limp, wet, mushy, or no longer crispy",
    "temperature": "A dish arrived cold or lukewarm",
    "late": "The delivery was slow or late",
    "missing": "An item was missing or the order was wrong",
    "portion": "The portion was too small",
    "quality": "Taste, freshness, or the dish was not as described",
    "price": "Too expensive or poor value for money",
    "other": "Something else",
}

FAULTS: dict[str, str] = {
    "kitchen": "Caused by how the restaurant cooked, prepared, or timed the food",
    "packaging": "Caused by the container or packaging, e.g. trapped steam, leaks, or crushing",
    "delivery": "Caused by the courier or the time spent in transit",
    "unclear": "The review does not say what caused it",
}

SYSTEM_PROMPT = """You write one recommendation card for a restaurant owner on a food delivery app.
Use only the facts and quotes given. Do not invent numbers. Plain words, no hype.
title: max 10 words, names the dish and the problem.
rationale: 2 sentences: what customers report and how often.
action: 1-2 concrete steps the owner (or JET, if the fault is delivery on JET couriers) can take this week."""


class ReviewTag(BaseModel):
    review_id: str
    text: str
    complaint_p: float
    dish: str
    dish_conf: float
    issue: str
    issue_conf: float
    fault: str
    fault_conf: float


class Wording(BaseModel):
    title: str
    rationale: str
    action: str


# ------------------------------------------------------------------- tagging


def _questions(order_items: list[str]) -> dict[str, Any]:
    return {
        "is_complaint": Noul(instructions="Is this customer review complaining about something?"),
        "dish": Choice(
            instructions="Which dish from the order is the complaint about?",
            criteria={**{name: None for name in order_items}, GENERAL: "Not about one specific dish"},
        ),
        "issue": Choice(instructions="What is the main problem described?", criteria=ISSUES),
        "fault": Choice(instructions="What most likely caused the problem?", criteria=FAULTS),
    }


def _to_tag(review: dict[str, Any], a: Answers) -> ReviewTag:
    return ReviewTag(
        review_id=review["review_id"],
        text=review["text"],
        complaint_p=a["is_complaint"]["noul"],
        dish=a["dish"]["choice"],
        dish_conf=a["dish"]["confidence"],
        issue=a["issue"]["choice"],
        issue_conf=a["issue"]["confidence"],
        fault=a["fault"]["choice"],
        fault_conf=a["fault"]["confidence"],
    )


def tag_reviews(reviews: list[dict[str, Any]]) -> list[ReviewTag]:
    """One Jev call per review. Reviews Jev failed on are dropped and logged."""
    answers = ask_many([(r["text"], _questions(r["order_items"])) for r in reviews])
    tags = [_to_tag(r, a) for r, a in zip(reviews, answers, strict=True) if a is not None]
    if len(tags) < len(reviews):
        log.warning("reviews: %d of %d reviews could not be tagged", len(reviews) - len(tags), len(reviews))
    return tags


# ----------------------------------------------------------------- grouping


def _confident_complaints(tags: list[ReviewTag]) -> list[ReviewTag]:
    return [t for t in tags if t.complaint_p >= COMPLAINT_P and t.issue_conf >= LABEL_CONF]


def group_dish_issues(tags: list[ReviewTag]) -> dict[tuple[str, str], list[ReviewTag]]:
    """(dish, issue) -> complaints, for groups big enough to act on.

    All faults count here: a courier problem doesn't single out one dish, so a complaint cluster
    on one dish is the restaurant's to fix even when single reviews blame the delivery.
    """
    groups: dict[tuple[str, str], list[ReviewTag]] = defaultdict(list)
    for t in _confident_complaints(tags):
        if t.dish != GENERAL and t.dish_conf >= LABEL_CONF:
            groups[(t.dish, t.issue)].append(t)
    return {k: v for k, v in groups.items() if len(v) >= MIN_COMPLAINTS}


def delivery_complaints(tags: list[ReviewTag]) -> list[ReviewTag]:
    """Complaints blamed on delivery that are not about one specific dish."""
    return [
        t
        for t in _confident_complaints(tags)
        if t.fault == "delivery" and t.fault_conf >= LABEL_CONF and (t.dish == GENERAL or t.dish_conf < LABEL_CONF)
    ]


# ------------------------------------------------------------------ wording


def _confidence(group: list[ReviewTag]) -> str:
    mean_conf = sum(t.issue_conf for t in group) / len(group)
    if len(group) >= 10 and mean_conf >= 0.8:
        return "high"
    return "med" if len(group) >= MIN_COMPLAINTS else "low"


def _word(facts: dict[str, Any], quotes: list[str], fallback: Wording) -> Wording:
    lines = [f"{k}: {v}" for k, v in facts.items()] + ["Customer quotes:"] + [f'- "{s}"' for s in quotes]
    return llm.structured(Wording, SYSTEM_PROMPT, "\n".join(lines)) or fallback


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _review_evidence(group: list[ReviewTag]) -> list[Evidence]:
    ranked = sorted(group, key=lambda t: t.issue_conf, reverse=True)[:MAX_QUOTES]
    return [Evidence(kind="review", ref=t.review_id, note=t.text, value=round(t.issue_conf, 2)) for t in ranked]


# ---------------------------------------------------------------------- run


def run(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> list[Recommendation]:
    restaurant = q.get_restaurant(con, restaurant_id)
    if restaurant is None:
        return []
    reviews = q.low_rated_reviews_with_order_items(con, restaurant_id)
    tags = tag_reviews(reviews)
    weeks = q.review_window_weeks(con, restaurant_id)
    sales = {s["name"]: s for s in q.item_sales(con, restaurant_id, weeks=8)}
    summary = q.weekly_summary(con, restaurant_id, weeks=8)
    recs: list[Recommendation] = []

    for (dish, issue), group in sorted(group_dish_issues(tags).items(), key=lambda kv: -len(kv[1])):
        item = sales.get(dish, {})
        fault = Counter(t.fault for t in group).most_common(1)[0][0]
        dish_orders_pw = item.get("orders", 0) / 8
        facts = {
            "complaints": float(len(group)),
            "complaints_per_week": round(len(group) / weeks, 2),
            "dish_orders_per_week": round(dish_orders_pw, 1),
            "avg_basket_eur": round(summary["avg_basket_eur"] or 0, 2),
            "commission_rate": restaurant["commission_rate"],
        }
        fallback = Wording(
            title=f"{dish}: repeated {issue} complaints",
            rationale=(
                f"{len(group)} low-rated reviews in {weeks:.0f} weeks say the {dish} has a {issue} problem "
                f"({ISSUES[issue].lower()}). Most point to the {fault} as the cause."
            ),
            action=f"Fix the {fault} issue for the {dish} and watch its reviews for two weeks.",
        )
        wording = _word(
            {"dish": dish, "issue": ISSUES[issue], "likely cause": FAULTS[fault], **facts},
            [t.text for t in group[:5]],
            fallback,
        )
        recs.append(
            Recommendation(
                id=f"rev-{restaurant_id}-{_slug(dish)}-{issue}",
                agent="reviews",
                kind="dish_quality",
                title=wording.title,
                rationale=wording.rationale,
                action=wording.action,
                evidence=[
                    *_review_evidence(group),
                    Evidence(
                        kind="menu_item",
                        ref=item.get("menu_item_id", dish),
                        note=f"{dish}: {dish_orders_pw:.1f} orders/week",
                        value=dish_orders_pw,
                    ),
                ],
                facts=facts,
                confidence=_confidence(group),
            )
        )

    late = delivery_complaints(tags)
    total_orders = (summary["orders_per_week"] or 0) * 8
    late_per_1000 = 1000 * len(late) / total_orders if total_orders else 0.0
    if len(late) >= MIN_COMPLAINTS and late_per_1000 >= DELIVERY_PER_1000:
        jet = restaurant["delivery_model"] == "jet_delivery"
        facts = {
            "complaints": float(len(late)),
            "complaints_per_week": round(len(late) / weeks, 2),
            "complaints_per_1000_orders": round(late_per_1000, 1),
            "orders_per_week": round(summary["orders_per_week"] or 0, 1),
            "avg_basket_eur": round(summary["avg_basket_eur"] or 0, 2),
            "commission_rate": restaurant["commission_rate"],
            "jet_delivers": float(jet),
        }
        who = "JET couriers" if jet else "the restaurant's own couriers"
        fallback = Wording(
            title="Delivery, not the kitchen, causes these complaints",
            rationale=(
                f"{len(late)} low-rated reviews blame slow or cold delivery rather than the food. "
                f"This restaurant is delivered by {who}."
            ),
            action=(
                "Flag to JET logistics: check courier assignment and wait times in peak hours."
                if jet
                else "Review own-courier handover times, or consider switching to JET delivery."
            ),
        )
        wording = _word({"delivered by": who, **facts}, [t.text for t in late[:5]], fallback)
        recs.append(
            Recommendation(
                id=f"rev-{restaurant_id}-delivery",
                agent="reviews",
                kind="delivery_quality",
                title=wording.title,
                rationale=wording.rationale,
                action=wording.action,
                evidence=_review_evidence(late),
                facts=facts,
                confidence=_confidence(late),
            )
        )

    return recs
