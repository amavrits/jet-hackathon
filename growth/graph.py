"""LangGraph wiring.

load_context -> [reviews, menu, pricing, promo_ads] -> impact -> rank
             -> human_approval (interrupt) -> apply -> summary
"""

import logging
from statistics import median
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from growth.agents import impact, menu, pricing, promo_ads, reviews
from growth.data import queries
from growth.state import (
    AdCampaign,
    CategoryPrice,
    Context,
    GraphState,
    Kpis,
    MenuItemStats,
    Recommendation,
    Restaurant,
    Review,
    SlotStat,
    Summary,
)
from growth.tools import listing

log = logging.getLogger(__name__)

AGENTS = {
    "reviews": reviews.run,
    "menu": menu.run,
    "pricing": pricing.run,
    "promo_ads": promo_ads.run,
}
EVIDENCE_TABLES = {
    "review": "reviews",
    "menu_item": "menu_items",
    "competitor": "competitors",
    "ad_campaign": "ad_campaigns",
}
CONFIDENCE_ORDER = {"high": 0, "med": 1, "low": 2}
# Our Pydantic state types, allowed explicitly for checkpoint deserialisation.
CHECKPOINT_TYPES = [
    ("growth.state", name)
    for name in (
        "AdCampaign", "CategoryPrice", "Context", "Evidence", "GraphState", "Impact", "Kpis",
        "MenuItemStats", "Recommendation", "Restaurant", "Review", "SlotStat", "Summary",
    )
]  # fmt: skip


def build_context(rid: str) -> Context:
    with queries.connect() as con:
        current = listing.get(con, rid)
        stats = {s["item_id"]: s for s in queries.item_order_stats(con, rid)}
        menu_stats = [
            MenuItemStats(
                item_id=e["item_id"],
                name=e["name"],
                category=e["category"],
                price_eur=e["price_eur"],
                description=e["description"],
                has_photo=bool(e["photo_url"]),
                orders_per_week=stats[e["item_id"]]["orders_per_week"],
                gmv_eur_per_week=stats[e["item_id"]]["gmv_eur_per_week"],
            )
            for e in current["menu"]
            if e.get("available", True)
        ]
        competitors = queries.competitor_prices(con, rid)
        categories = sorted({m.category for m in menu_stats})
        category_prices = []
        for cat in categories:
            comp = [c["price_eur"] for c in competitors if c["category"] == cat]
            if comp:
                category_prices.append(
                    CategoryPrice(
                        category=cat,
                        own_median_eur=median(m.price_eur for m in menu_stats if m.category == cat),
                        competitor_median_eur=median(comp),
                        competitor_count=len(comp),
                    )
                )
        return Context(
            restaurant=Restaurant(**queries.get_restaurant(con, rid)),
            kpis=Kpis(**queries.kpis(con, rid)),
            peer_median_orders_per_week=queries.peer_median_orders_per_week(con),
            menu=menu_stats,
            reviews=[Review(**r) for r in queries.reviews(con, rid)],
            slots=[SlotStat(**s) for s in queries.slot_stats(con, rid)],
            category_prices=category_prices,
            campaigns=[AdCampaign(**c) for c in queries.ad_campaigns(con, rid)],
            listing=current,
        )


def load_context(state: GraphState) -> dict[str, Any]:
    return {"context": build_context(state.restaurant_id)}


def _agent_node(name: str):
    def node(state: GraphState) -> dict[str, Any]:
        return {"recommendations": AGENTS[name](state.context)}

    node.__name__ = name
    return node


def _evidence_exists(rec: Recommendation) -> bool:
    with queries.connect() as con:
        for kind, table in EVIDENCE_TABLES.items():
            ids = [e.row_id for e in rec.evidence if e.kind == kind and e.row_id is not None]
            if ids and queries.existing_ids(con, table, ids) != set(ids):
                return False
    return True


def score(state: GraphState) -> dict[str, Any]:
    scored = []
    for rec in state.recommendations:
        if not _evidence_exists(rec):
            log.warning("dropped %s: evidence references rows that do not exist", rec.id)
            continue
        est = impact.estimate(rec, state.context)
        if est is None:
            log.warning("dropped %s: no impact could be stated", rec.id)
            continue
        scored.append(rec.model_copy(update={"impact": est}))
    return {"scored": scored}


def rank(state: GraphState) -> dict[str, Any]:
    ranked = sorted(
        state.scored,
        key=lambda r: (-r.impact.jet_revenue_eur_per_week, CONFIDENCE_ORDER[r.confidence]),
    )
    return {"ranked": ranked}


def human_approval(state: GraphState) -> dict[str, Any]:
    answer = interrupt({"recommendation_ids": [r.id for r in state.ranked]})
    known = {r.id for r in state.ranked}
    decisions = {k: bool(v) for k, v in answer.get("decisions", {}).items() if k in known}
    return {"decisions": decisions, "approver": str(answer.get("approver", ""))}


def apply(state: GraphState) -> dict[str, Any]:
    change_ids = []
    with queries.connect() as con:
        for rec in state.ranked:
            if state.decisions.get(rec.id) and rec.patch:
                change_ids.append(
                    listing.apply(con, state.restaurant_id, rec.id, rec.patch, state.approver)
                )
    return {"applied_change_ids": change_ids}


def summary(state: GraphState) -> dict[str, Any]:
    approved = [r for r in state.ranked if state.decisions.get(r.id)]
    return {
        "summary": Summary(
            approved=len(approved),
            rejected=sum(1 for v in state.decisions.values() if not v),
            applied_changes=len(state.applied_change_ids),
            orders_per_week=sum(r.impact.orders_per_week for r in approved),
            gmv_eur_per_week=sum(r.impact.gmv_eur_per_week for r in approved),
            jet_revenue_eur_per_week=sum(r.impact.jet_revenue_eur_per_week for r in approved),
            ad_spend_eur=sum(r.impact.ad_spend_eur for r in approved),
            partner_cost_eur_per_week=sum(r.impact.partner_cost_eur_per_week for r in approved),
        )
    }


def build_graph():
    g = StateGraph(GraphState)
    g.add_node("load_context", load_context)
    for name in AGENTS:
        g.add_node(name, _agent_node(name))
        g.add_edge("load_context", name)
    g.add_node("impact", score)
    g.add_node("rank", rank)
    g.add_node("human_approval", human_approval)
    g.add_node("apply", apply)
    g.add_node("summary", summary)
    g.add_edge(START, "load_context")
    g.add_edge(list(AGENTS), "impact")
    g.add_edge("impact", "rank")
    g.add_edge("rank", "human_approval")
    g.add_edge("human_approval", "apply")
    g.add_edge("apply", "summary")
    g.add_edge("summary", END)
    serde = JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)
    return g.compile(checkpointer=InMemorySaver(serde=serde))
