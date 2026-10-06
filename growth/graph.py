"""LangGraph wiring.

    load_context -> [reviews, menu, pricing, promo_ads] (parallel) -> impact -> rank
                 -> human_approval (interrupt) -> apply -> summary

Usage (what the CLI and Streamlit app do):

    graph = build_graph()
    config = new_thread()
    for update in graph.stream({"restaurant_id": rid}, config, stream_mode="updates"):
        ...                                  # progress per node; stops at the approval interrupt
    pending = pending_approval(graph, config)  # the ranked cards the owner must decide on
    final = graph.invoke(Command(resume={"approve": [...], "reject": {id: reason}}), config)

The approval decision format is {"approve": [rec ids], "reject": {rec id: reason}}. A reject may
also be a list of ids without reasons. Recommendations in neither are left undecided and are not
applied. Nothing is written to the listing before the owner decides.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel

from growth import state as state_models
from growth.agents import impact, menu, pricing, promo_ads, reviews
from growth.data import queries as q
from growth.state import GraphState, Impact, Recommendation, Summary
from growth.tools import listing as listing_tool

log = logging.getLogger(__name__)

SPECIALISTS: dict[str, Callable[[duckdb.DuckDBPyConnection, str], list[Recommendation]]] = {
    "reviews": reviews.run,
    "menu": menu.run,
    "pricing": pricing.run,
    "promo_ads": promo_ads.run,
}
CONFIDENCE_ORDER = {"high": 0, "med": 1, "low": 2}
SCALE_RESTAURANTS = 1000  # "if N partners saw the same uplift" on the summary screen

__all__ = ["build_graph", "new_thread", "pending_approval", "Command", "SPECIALISTS"]


# ------------------------------------------------------------------------- nodes


def _load_context(con: duckdb.DuckDBPyConnection) -> Callable[[GraphState], dict[str, Any]]:
    def node(state: GraphState) -> dict[str, Any]:
        cur = con.cursor()
        restaurant = q.get_restaurant(cur, state.restaurant_id)
        if restaurant is None or not restaurant.get("is_partner"):
            raise ValueError(f"unknown partner restaurant {state.restaurant_id!r}")
        return {
            "context": {
                "restaurant": restaurant,
                "listing": listing_tool.get_listing(cur, state.restaurant_id),
                "weekly": q.weekly_summary(cur, state.restaurant_id),
            }
        }

    return node


def _specialist(con: duckdb.DuckDBPyConnection, name: str) -> Callable[[GraphState], dict[str, Any]]:
    run = SPECIALISTS[name]

    def node(state: GraphState) -> dict[str, Any]:
        try:
            recs = run(con.cursor(), state.restaurant_id)
        except Exception as e:  # one failing agent must not sink the run
            log.exception("agent %s failed", name)
            return {"recommendations": {name: []}, "errors": [f"{name} agent failed: {e}"]}
        return {"recommendations": {name: recs}}

    return node


def impact_node(state: GraphState) -> dict[str, Any]:
    return {"recommendations": {agent: impact.run(recs) for agent, recs in state.recommendations.items()}}


def _rank_key(r: Recommendation) -> tuple[float, int]:
    jet = r.impact.jet_revenue_eur_per_week if r.impact else 0.0
    return (-jet, CONFIDENCE_ORDER.get(r.confidence, 3))


def rank_node(state: GraphState) -> dict[str, Any]:
    """Best first: projected JET revenue, then confidence. The optimiser can replace this node.

    Variants are alternatives, so the owner sees only the best one per variant_group. All
    variants stay in `state.recommendations` for the optimiser.
    """
    best: dict[str, Recommendation] = {}
    singles = []
    for r in sorted(state.all_recommendations(), key=_rank_key):
        if r.variant_group is None:
            singles.append(r)
        elif r.variant_group not in best:
            best[r.variant_group] = r
    return {"ranked": sorted([*singles, *best.values()], key=_rank_key)}


def _parse_decision(decision: Any, valid: set[str]) -> tuple[dict[str, bool], dict[str, str], list[str]]:
    if not isinstance(decision, dict):
        return {}, {}, [f"approval decision must be a dict, got {type(decision).__name__}"]
    approve = decision.get("approve") or []
    reject = decision.get("reject") or {}
    if isinstance(reject, list):
        reject = {rid: "" for rid in reject}
    approvals: dict[str, bool] = {}
    reasons: dict[str, str] = {}
    errors = [f"unknown recommendation id {rid!r}" for rid in [*approve, *reject] if rid not in valid]
    for rid in approve:
        if rid in valid:
            approvals[rid] = True
    for rid, reason in reject.items():
        if rid in valid:
            if rid in approvals:
                errors.append(f"{rid!r} both approved and rejected; treated as rejected")
            approvals[rid] = False
            if reason:
                reasons[rid] = str(reason)
    return approvals, reasons, errors


def human_approval(state: GraphState) -> dict[str, Any]:
    # Re-executed from the top on resume, so nothing before interrupt() may have side effects.
    decision = interrupt(
        {
            "restaurant_id": state.restaurant_id,
            "recommendations": [r.model_dump(mode="json") for r in state.ranked],
        }
    )
    approvals, reasons, errors = _parse_decision(decision, {r.id for r in state.ranked})
    approver = str(decision.get("approver", "")) if isinstance(decision, dict) else ""
    return {"approvals": approvals, "rejection_reasons": reasons, "approver": approver, "errors": errors}


def _apply(con: duckdb.DuckDBPyConnection) -> Callable[[GraphState], dict[str, Any]]:
    def node(state: GraphState) -> dict[str, Any]:
        cur = con.cursor()
        applied, errors = [], []
        for rec in state.ranked:
            if not state.approvals.get(rec.id) or not rec.patch:
                continue  # rejected, undecided, or advice-only
            try:
                applied.append(listing_tool.apply_recommendation(cur, state.restaurant_id, rec))
            except (listing_tool.PatchError, KeyError) as e:
                errors.append(f"could not apply {rec.id}: {e}")
        return {"applied": applied, "errors": errors}

    return node


def summary_node(state: GraphState) -> dict[str, Any]:
    approved = [r for r in state.ranked if state.approvals.get(r.id)]
    rejected = sum(1 for v in state.approvals.values() if v is False)
    impacts = [r.impact for r in approved if r.impact]
    total = Impact(
        orders_per_week=round(sum(i.orders_per_week for i in impacts), 1),
        gmv_eur_per_week=round(sum(i.gmv_eur_per_week for i in impacts), 2),
        jet_revenue_eur_per_week=round(sum(i.jet_revenue_eur_per_week for i in impacts), 2),
        ad_spend_eur=round(sum(i.ad_spend_eur for i in impacts), 2),
        partner_cost_eur_per_week=round(sum(i.partner_cost_eur_per_week for i in impacts), 2),
        formula="Sum of the approved recommendations' weekly impacts.",
    )
    scaled = round(total.jet_revenue_eur_per_week * SCALE_RESTAURANTS, 2)
    name = state.context.get("restaurant", {}).get("name", state.restaurant_id)
    text = (
        f"{name}: {len(approved)} approved, {rejected} rejected, "
        f"{len(state.applied)} listing change{'' if len(state.applied) == 1 else 's'} applied. "
        f"Projected +{total.orders_per_week:.0f} orders and +€{total.gmv_eur_per_week:,.0f} GMV per week, "
        f"worth €{total.jet_revenue_eur_per_week:,.0f}/week to JET. "
        f"If {SCALE_RESTAURANTS:,} partners saw the same uplift: €{scaled:,.0f}/week."
    )
    return {
        "summary": Summary(
            approved=len(approved),
            rejected=rejected,
            total=total,
            scaled_restaurants=SCALE_RESTAURANTS,
            scaled_jet_revenue_eur_per_week=scaled,
            text=text,
        )
    }


# ------------------------------------------------------------------------- graph


def build_graph(db_path: Path | str | None = None, checkpointer: Any = None):
    """Compile the graph against one DuckDB file. Nodes use their own cursor, so parallel nodes are safe.

    The graph keeps a read-write connection open. DuckDB refuses a second connection to the same
    file with a different configuration in the same process, so anything else in the process
    (e.g. the Streamlit app) must also open it read-write: `queries.connect(path)`.
    """
    con = q.connect(db_path or q.db_path())
    g = StateGraph(GraphState)
    g.add_node("load_context", _load_context(con))
    for name in SPECIALISTS:
        g.add_node(name, _specialist(con, name))
    g.add_node("impact", impact_node)
    g.add_node("rank", rank_node)
    g.add_node("human_approval", human_approval)
    g.add_node("apply", _apply(con))
    g.add_node("summary", summary_node)

    g.add_edge(START, "load_context")
    for name in SPECIALISTS:
        g.add_edge("load_context", name)
    g.add_edge(list(SPECIALISTS), "impact")  # fan-in: waits for all four
    g.add_edge("impact", "rank")
    g.add_edge("rank", "human_approval")
    g.add_edge("human_approval", "apply")
    g.add_edge("apply", "summary")
    g.add_edge("summary", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver(serde=serializer()))


def serializer() -> JsonPlusSerializer:
    """Checkpoint serializer that explicitly allows our Pydantic state types.

    LangGraph warns on (and will soon block) deserializing unregistered types, which would break
    resuming a run after the approval interrupt.
    """
    allowed = [
        (state_models.__name__, name)
        for name, obj in vars(state_models).items()
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj.__module__ == state_models.__name__
    ]
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)


def new_thread() -> dict[str, Any]:
    return {"configurable": {"thread_id": uuid.uuid4().hex}}


def pending_approval(graph: Any, config: dict[str, Any]) -> list[Recommendation]:
    """The ranked recommendations waiting for the owner, or [] if the run isn't paused at approval."""
    snapshot = graph.get_state(config)
    if "human_approval" not in snapshot.next:
        return []
    return [Recommendation.model_validate(r) for r in snapshot.values.get("ranked", [])]
