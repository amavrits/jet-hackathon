"""Stored analyses: run the agents for a restaurant and record which cards to propose.

Two independent selections are stored side by side for every card:
  optimizer_selected  growth.ml.optimize: an integer program over learned-model values
                      (owner never loses, at most N actions, one option per group).
  jev_selected        TypeSafe Jev: a judgment call on the same numbers, given the restaurant's
                      profile. Code does the arithmetic and hands Jev the results as facts
                      (Jev doesn't compare numbers reliably); Jev weighs fit, e.g. whether a premium
                      restaurant should compete on price, or an owner would accept the trade-off.

For each variant group Jev picks one option or "none" (Choice); for each standalone card it
answers "propose this?" (Noul, selected at p >= 0.5).

    analysis_id = analyse(con, graph, "r_spice_route")   # runs the graph to the approval pause
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

import duckdb

from growth import llm
from growth.classify import JEV_MODEL, Choice, Noul, ask_many
from growth.data import queries as q
from growth.graph import new_thread
from growth.ml import optimize as opt
from growth.state import Recommendation

log = logging.getLogger(__name__)

NONE = "none"
PRICE_LEVEL = {1: "budget", 2: "mid-range", 3: "premium"}

CHOICE_INSTRUCTIONS = (
    "A growth assistant can propose one of these options to this restaurant owner, or none. "
    "Pick the option the owner is most likely to accept and benefit from: it should leave the owner "
    "better off even in the worst case, fit how the restaurant positions itself, and still help JET. "
    "Pick 'none' if no option is a good deal for the owner."
)
NOUL_INSTRUCTIONS = (
    "Should a growth assistant propose this change to this restaurant owner? Yes if the owner is likely "
    "to accept it and benefit from it, given how the restaurant positions itself and the expected gain."
)


# ----------------------------------------------------------------------- facts for Jev


def _profile(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "cuisine": r["cuisine"],
        "positioning": PRICE_LEVEL.get(r["price_level"], "mid-range"),
        "rating": r["rating"],
        "delivered_by": "JET couriers" if r["delivery_model"] == "jet_delivery" else "the restaurant itself",
    }


def _facts(rec: Recommendation, o: opt.Option | None) -> dict[str, Any]:
    """What Jev sees about one card: the action plus pre-computed money facts in words and numbers."""
    facts: dict[str, Any] = {"change": rec.action or rec.title, "why": rec.rationale}
    if o is None:
        return facts
    facts["owner_profit_per_week_eur"] = round(o.owner_profit_eur_per_week)
    facts["jet_revenue_per_week_eur"] = round(o.jet_revenue_eur_per_week)
    facts["owner_better_off"] = o.owner_profit_eur_per_week > 0
    if o.source == "model":
        facts["owner_better_off_even_in_worst_case"] = o.owner_profit_lo > 0
    else:
        facts["estimate"] = "rule of thumb, no uncertainty range"
    if rec.impact and rec.impact.partner_cost_eur_per_week:
        facts["owner_gives_up_per_week_eur"] = round(rec.impact.partner_cost_eur_per_week)
    return facts


def _questions(cards: list[Recommendation], options: dict[str, opt.Option], profile: dict[str, Any]):
    """[(key, state, questions)] for ask_many: one Choice per variant group, one Noul per standalone card."""
    groups: dict[str, list[Recommendation]] = {}
    singles: list[Recommendation] = []
    for c in cards:
        (groups.setdefault(c.variant_group, []) if c.variant_group else singles).append(c)
    items = []
    for group, variants in groups.items():
        state = {
            "restaurant": profile,
            "options": {v.variant_label or v.id: _facts(v, options.get(v.id)) for v in variants},
        }
        criteria = {v.variant_label or v.id: json.dumps(_facts(v, options.get(v.id))) for v in variants}
        criteria[NONE] = "Propose none of these options"
        items.append((("choice", group), state, {"pick": Choice(instructions=CHOICE_INSTRUCTIONS, criteria=criteria)}))
    for c in singles:
        state = {"restaurant": profile, "proposal": _facts(c, options.get(c.id))}
        items.append((("noul", c.id), state, {"propose": Noul(instructions=NOUL_INSTRUCTIONS)}))
    return items, groups


def jev_select(
    cards: list[Recommendation], options: dict[str, opt.Option], restaurant: dict[str, Any]
) -> dict[str, tuple[bool, float | None, str]]:
    """recommendation id -> (selected, probability, question type). Unanswered cards are not selected."""
    items, groups = _questions(cards, options, _profile(restaurant))
    answers = ask_many([(state, qs) for _, state, qs in items])
    out: dict[str, tuple[bool, float | None, str]] = {}
    for (key, _, _), a in zip(items, answers, strict=True):
        kind, ref = key
        if kind == "choice":
            pick = a["pick"] if a else None
            for v in groups[ref]:
                label = v.variant_label or v.id
                prob = pick["probabilities"].get(label) if pick else None
                out[v.id] = (bool(pick) and pick["choice"] == label, prob, "choice")
        else:
            p = a["propose"]["noul"] if a else None
            out[ref] = (p is not None and p >= 0.5, p, "noul")
    return out


# ------------------------------------------------------------------------------ run


def analyse(con: duckdb.DuckDBPyConnection, graph: Any, restaurant_id: str) -> str:
    """Run the agents up to the approval pause, select with the optimiser and Jev, store. Returns the id."""
    config = new_thread()
    for _ in graph.stream({"restaurant_id": restaurant_id}, config, stream_mode="updates"):
        pass
    values = graph.get_state(config).values
    cards: list[Recommendation] = [r for recs in values["recommendations"].values() for r in recs]
    shown = {r.id: i for i, r in enumerate(values["ranked"], 1)}
    restaurant = q.get_restaurant(con, restaurant_id)
    errors = list(values.get("errors", []))

    options_list = opt.build_options(con, restaurant_id, cards)
    options = {o.recommendation_id: o for o in options_list if o.recommendation_id}
    plan = opt.optimize_plan(con, restaurant_id, options=options_list)
    opt_chosen = {o.recommendation_id for o in plan.chosen if o.recommendation_id}
    jev = jev_select(cards, options, restaurant)
    if cards and not jev:
        errors.append("Jev returned no selections")

    analysis_id = f"an_{uuid.uuid4().hex[:12]}"
    rows = []
    for c in cards:
        o = options.get(c.id)
        selected, prob, question = jev.get(c.id, (False, None, "noul" if not c.variant_group else "choice"))
        rows.append(
            {
                "analysis_id": analysis_id,
                "recommendation_id": c.id,
                "restaurant_id": restaurant_id,
                "agent": c.agent,
                "kind": c.kind,
                "title": c.title,
                "variant_group": c.variant_group,
                "variant_label": c.variant_label or None,
                "confidence": c.confidence,
                "shown_rank": shown.get(c.id),
                "value_source": o.source if o else "heuristic",
                "jet_revenue_eur_per_week": o.jet_revenue_eur_per_week if o else None,
                "owner_profit_eur_per_week": o.owner_profit_eur_per_week if o else None,
                "owner_profit_lo": o.owner_profit_lo if o and o.source == "model" else None,
                "partner_cost_eur_per_week": c.impact.partner_cost_eur_per_week if c.impact else None,
                "optimizer_selected": c.id in opt_chosen,
                "jev_selected": selected,
                "jev_probability": None if prob is None else round(prob, 4),
                "jev_question": question,
                "recommendation": c.model_dump_json(),
            }
        )

    con.begin()
    try:
        con.execute(
            """
            INSERT INTO analyses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                analysis_id,
                restaurant_id,
                datetime.now(),
                llm.model_name(),
                JEV_MODEL,
                len(rows),
                sum(r["jev_selected"] for r in rows),
                sum(r["optimizer_selected"] for r in rows),
                sum(r["jev_selected"] == r["optimizer_selected"] for r in rows),
                plan.jet_revenue_eur_per_week,
                plan.model_dump_json(
                    include={
                        "objective",
                        "chosen",
                        "binding_constraints",
                        "jet_revenue_eur_per_week",
                        "owner_profit_eur_per_week",
                    }
                ),
                json.dumps(errors),
            ],
        )
        if rows:
            cols = list(rows[0])
            con.executemany(
                f"INSERT INTO analysis_recommendations ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                [[r[k] for k in cols] for r in rows],
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    log.info("analysis %s for %s: %d cards", analysis_id, restaurant_id, len(rows))
    return analysis_id


def latest(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> dict[str, Any] | None:
    """The newest stored analysis for a restaurant, with its recommendation rows."""
    rows = q._rows(
        con, "SELECT * FROM analyses WHERE restaurant_id = ? ORDER BY created_at DESC LIMIT 1", [restaurant_id]
    )
    if not rows:
        return None
    a = rows[0]
    a["recommendations"] = q._rows(
        con,
        """
        SELECT * EXCLUDE (recommendation) FROM analysis_recommendations
        WHERE analysis_id = ? ORDER BY shown_rank NULLS LAST, recommendation_id
        """,
        [a["analysis_id"]],
    )
    return a
