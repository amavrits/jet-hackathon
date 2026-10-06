"""Chat that explains why the agents proposed what they did. Read-only and grounded.

The owner asks questions while the graph is paused for approval. The model sees the cards
(evidence, facts, impact formula, lever changes, variants) and can call a few read-only tools,
each a thin wrapper over a function in growth/data/queries.py. The model never writes SQL and
cannot change the listing.

    for event in answer(con, restaurant_id, cards, "Why cut my starters?", history, focus_id=card.id):
        if event.type == "text":   show(event.text)          # streamed answer text
        elif event.type == "tool": show_status(event.name)   # e.g. "looking up competitor prices"
        elif event.type == "done": history = event.history   # keep for the next question
                                   warn(event.unverified_numbers)

`history` is opaque: store it and pass it back unchanged. It holds the conversation in
OpenAI chat format (user, assistant with tool_calls, tool results), without the system prompt.

Grounding check: after each answer, every number in the text must appear in the cards or in a
tool result (allowing rounding and fraction <-> percent). Anything else is returned in
`unverified_numbers` so the app can flag it. CLAUDE.md: never present LLM-made numbers as data.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import duckdb
import litellm
from pydantic import BaseModel, Field, ValidationError

from growth import llm
from growth.agents import impact as impact_mod
from growth.agents.common import WEEKDAYS, WEEKS
from growth.agents.promo_ads import DAYPARTS
from growth.data import queries as q
from growth.state import Recommendation

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5
MAX_TOKENS = 8000
CHAT_EFFORT = os.environ.get("CHAT_EFFORT", "low")  # reasoning effort; answers should come back in seconds

SYSTEM_PROMPT = """You explain a restaurant growth assistant's recommendations to the restaurant owner
on a food delivery app. The owner is deciding whether to approve them.

Rules:
- Use only numbers that appear in CONTEXT or in tool results. If a number isn't available, say so
  and offer what you can look up. Never estimate or invent figures.
- Impact figures are projections from a stated formula, not promises. When asked about them,
  walk through the formula with its inputs (the explain_impact tool has them).
- Be direct and short: two to five sentences unless the owner asks for detail. Plain words.
- Answer in the language the owner writes in.
- You cannot change the listing or approve anything. If the owner objects to a recommendation,
  acknowledge the reason and say they can reject it with that reason in the approval screen.
- Use tools to fetch specifics (reviews, competitor prices, sales, order times) rather than
  guessing. Look up only what the question needs."""


# ------------------------------------------------------------------------- events


@dataclass
class ChatEvent:
    type: str  # "text" | "tool" | "done" | "error"
    text: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    unverified_numbers: list[str] = field(default_factory=list)


# -------------------------------------------------------------------------- tools


class DishArg(BaseModel):
    dish: str = Field(description="Dish name as it appears on the menu, e.g. 'Calzone'.")


class DishReviewsArg(DishArg):
    only_complaints: bool = Field(default=True, description="Only reviews rated 3 or lower.")
    limit: int = Field(default=8, ge=1, le=20)


class OptionalDishArg(BaseModel):
    dish: str | None = Field(default=None, description="A dish name, or omit for the whole menu.")


class WeekdayArg(BaseModel):
    weekday: str | None = Field(default=None, description="mon..sun, or omit for all days.")


class RecArg(BaseModel):
    recommendation_id: str


@dataclass
class _Ctx:
    con: duckdb.DuckDBPyConnection
    restaurant_id: str
    recs: dict[str, Recommendation]


def _find_dish(ctx: _Ctx, name: str) -> dict[str, Any]:
    menu = q.get_menu(ctx.con, ctx.restaurant_id)
    exact = [m for m in menu if m["name"].lower() == name.lower()]
    partial = [m for m in menu if name.lower() in m["name"].lower()]
    match = exact or partial
    if len(match) != 1:
        raise LookupError(f"No unique dish matches {name!r}. Menu: {', '.join(m['name'] for m in menu)}")
    return match[0]


def _tool_dish_reviews(ctx: _Ctx, a: DishReviewsArg) -> Any:
    dish = _find_dish(ctx, a.dish)
    rows = q.dish_reviews(ctx.con, ctx.restaurant_id, dish["menu_item_id"], 3 if a.only_complaints else 5, a.limit)
    return {"dish": dish["name"], "reviews": [{**r, "created_at": str(r["created_at"])[:10]} for r in rows]}


def _tool_competitor_prices(ctx: _Ctx, a: DishArg) -> Any:
    dish = _find_dish(ctx, a.dish)
    return {
        "dish": dish["name"],
        "your_price_eur": dish["price_eur"],
        "competitors": q.competitor_prices_for_dish(ctx.con, ctx.restaurant_id, dish["name"]),
    }


def _tool_dish_sales(ctx: _Ctx, a: OptionalDishArg) -> Any:
    rows = q.item_sales(ctx.con, ctx.restaurant_id, weeks=WEEKS)
    if a.dish:
        name = _find_dish(ctx, a.dish)["name"]
        rows = [r for r in rows if r["name"] == name]
    return [
        {
            "dish": r["name"],
            "category": r["category"],
            "price_eur": r["price_eur"],
            "orders_per_week": round(r["orders"] / WEEKS, 1),
            "units_per_week": round(r["units"] / WEEKS, 1),
            "has_photo": r["photo_url"] is not None,
            "has_description": bool(r["description"]),
        }
        for r in rows
    ]


def _tool_orders_by_time(ctx: _Ctx, a: WeekdayArg) -> Any:
    slots = q.orders_by_slot(ctx.con, ctx.restaurant_id, weeks=WEEKS)
    days = [WEEKDAYS.index(a.weekday.lower()[:3])] if a.weekday else range(7)
    return {
        "note": f"average orders per week over the last {WEEKS} weeks",
        "by_day": {
            WEEKDAYS[d]: {
                part: round(sum(s["orders"] for s in slots if s["weekday"] == d and s["hour"] in hours) / WEEKS, 1)
                for part, hours in DAYPARTS.items()
            }
            for d in days
        },
    }


def _tool_explain_impact(ctx: _Ctx, a: RecArg) -> Any:
    rec = ctx.recs.get(a.recommendation_id)
    if rec is None:
        raise LookupError(f"Unknown recommendation. Known ids: {', '.join(ctx.recs)}")
    assumptions = {
        k: v
        for k, v in vars(impact_mod).items()
        if k.isupper() and isinstance(v, int | float) and not k.startswith("_")
    }
    siblings = [
        {"id": r.id, "label": r.variant_label, "impact": r.impact.model_dump() if r.impact else None}
        for r in ctx.recs.values()
        if rec.variant_group and r.variant_group == rec.variant_group and r.id != rec.id
    ]
    return {
        "formula": rec.impact.formula if rec.impact else None,
        "impact": rec.impact.model_dump() if rec.impact else None,
        "inputs": rec.facts,
        "assumptions": assumptions,
        "lever_changes": [lc.model_dump() for lc in rec.lever_changes],
        "other_variants": siblings,
    }


TOOLS: dict[str, tuple[str, type[BaseModel], Callable[[_Ctx, Any], Any]]] = {
    "get_dish_reviews": ("Customer reviews of orders that included a dish.", DishReviewsArg, _tool_dish_reviews),
    "get_competitor_prices": (
        "Each nearby competitor's price for the same dish, plus the restaurant's own price.",
        DishArg,
        _tool_competitor_prices,
    ),
    "get_dish_sales": (
        "Weekly orders, price, photo and description status per dish.",
        OptionalDishArg,
        _tool_dish_sales,
    ),
    "get_orders_by_time": (
        "Average weekly orders per weekday and daypart (lunch, afternoon, dinner).",
        WeekdayArg,
        _tool_orders_by_time,
    ),
    "explain_impact": (
        "The impact formula, its inputs and assumptions, lever changes, and other variants of a recommendation.",
        RecArg,
        _tool_explain_impact,
    ),
}


def tool_specs() -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": n, "description": d, "parameters": m.model_json_schema()}}
        for n, (d, m, _) in TOOLS.items()
    ]


def run_tool(ctx: _Ctx, name: str, raw: Any) -> tuple[str, bool]:
    """Execute one tool call. Returns (json text, is_error). Never raises."""
    if name not in TOOLS:
        return f"Unknown tool {name!r}", True
    _, model, fn = TOOLS[name]
    if not isinstance(raw, dict):  # malformed or unparseable arguments: never run on a guess
        return f"Arguments must be a JSON object, got {raw!r}", True
    try:
        args = model.model_validate(raw)
        return json.dumps(fn(ctx, args), default=str), False
    except (ValidationError, LookupError, ValueError) as e:  # bad or truncated input, unknown dish
        return str(e), True


# --------------------------------------------------------------------- grounding

_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> list[tuple[str, float]]:
    out = []
    for m in _NUM.finditer(text):
        raw = m.group().rstrip(".,")
        try:
            out.append((raw, float(raw.replace(",", ""))))
        except ValueError:
            continue
    return out


def unverified_numbers(answer: str, grounding: str) -> list[str]:
    """Numbers in `answer` not found in `grounding`, allowing rounding and fraction <-> percent."""
    known = [v for _, v in _numbers(grounding)]
    bad = []
    for raw, v in _numbers(answer):
        if v <= 12 and v == int(v):
            continue  # small counts ("3 competitors", "2 starters") are too noisy to police
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        tol = 0.5 * 10**-decimals + 1e-9
        if not any(abs(v - k) <= tol or abs(v - 100 * k) <= tol for k in known):
            bad.append(raw)
    return bad


# -------------------------------------------------------------------------- answer


def _context(con: duckdb.DuckDBPyConnection, restaurant_id: str, cards: list[Recommendation]) -> str:
    r = q.get_restaurant(con, restaurant_id) or {}
    weekly = q.weekly_summary(con, restaurant_id)
    payload = {
        "restaurant": {k: r.get(k) for k in ("name", "cuisine", "city", "delivery_model", "commission_rate", "rating")},
        "last_8_weeks": {k: round(v or 0, 2) for k, v in weekly.items()},
        "recommendations": [
            c.model_dump(
                mode="json",
                include={
                    "id",
                    "agent",
                    "kind",
                    "title",
                    "rationale",
                    "action",
                    "evidence",
                    "facts",
                    "lever_changes",
                    "variant_label",
                    "impact",
                    "confidence",
                    "patch",
                },
            )
            for c in cards
        ],
    }
    return json.dumps(payload, default=str)


@dataclass
class TurnResult:
    """The assembled assistant turn after streaming."""

    content: str
    tool_calls: list[dict[str, Any]]  # OpenAI format: {"id", "type": "function", "function": {"name", "arguments"}}
    finish_reason: str


def stream_turn(model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Iterator[str | TurnResult]:
    """One model call: yields text deltas as they arrive, then the assembled TurnResult."""
    chunks = []
    for chunk in litellm.completion(
        model=model,
        messages=messages,
        tools=tools,
        stream=True,
        max_tokens=MAX_TOKENS,
        reasoning_effort=CHAT_EFFORT,
    ):
        chunks.append(chunk)
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta is not None and delta.content:
            yield delta.content
    final = litellm.stream_chunk_builder(chunks, messages=messages)
    choice = final.choices[0]
    calls = [
        {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments or ""}}
        for c in (choice.message.tool_calls or [])
    ]
    yield TurnResult(content=choice.message.content or "", tool_calls=calls, finish_reason=choice.finish_reason or "")


def answer(
    con: duckdb.DuckDBPyConnection,
    restaurant_id: str,
    cards: list[Recommendation],
    question: str,
    history: list[dict[str, Any]] | None = None,
    *,
    focus_id: str | None = None,
    turn: Callable[..., Iterator[str | TurnResult]] | None = None,
) -> Iterator[ChatEvent]:
    """Stream an answer. `cards` should include every variant (they can be asked about)."""
    turn = turn or stream_turn
    ctx = _Ctx(con=con, restaurant_id=restaurant_id, recs={c.id: c for c in cards})
    context = _context(con, restaurant_id, cards)
    grounding = [context]
    model = llm.model_name()
    system = {"role": "system", "content": f"{SYSTEM_PROMPT}\n\nCONTEXT\n{context}"}
    messages = list(history or [])
    focus = f"[The owner is looking at recommendation {focus_id}.]\n" if focus_id else ""
    messages.append({"role": "user", "content": focus + question})
    text_parts: list[str] = []

    try:
        rounds = 0
        while True:
            result: TurnResult | None = None
            for item in turn(model, [system, *messages], tool_specs()):
                if isinstance(item, TurnResult):
                    result = item
                else:
                    text_parts.append(item)
                    yield ChatEvent(type="text", text=item)
            if result is None:
                raise RuntimeError("model turn ended without a result")

            assistant: dict[str, Any] = {"role": "assistant", "content": result.content or None}
            if result.tool_calls:
                assistant["tool_calls"] = result.tool_calls
            messages.append(assistant)

            if result.finish_reason == "content_filter":
                msg = "I can't help with that one. Ask me about the recommendations or the data behind them."
                text_parts.append(msg)
                yield ChatEvent(type="text", text=msg)
                break
            if not result.tool_calls or result.finish_reason == "length":
                break
            rounds += 1
            if rounds > MAX_TOOL_ROUNDS:
                yield ChatEvent(type="text", text="\n(I stopped looking things up; ask me to continue.)")
                break
            for call in result.tool_calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = None  # run_tool turns this into an error result
                yield ChatEvent(type="tool", name=name, input=args if isinstance(args, dict) else {})
                out, is_error = run_tool(ctx, name, args)
                if not is_error:
                    grounding.append(out)
                messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": f"ERROR: {out}" if is_error else out}
                )
    except Exception as e:  # no key, network, provider error: LiteLLM raises its own types for all of these
        log.error("chat: model call failed: %s", e)
        yield ChatEvent(type="error", text="The assistant is unavailable right now.", history=list(history or []))
        return

    full = "".join(text_parts)
    yield ChatEvent(
        type="done",
        text=full,
        history=messages,
        unverified_numbers=unverified_numbers(full, "\n".join(grounding)),
    )


def answer_for_thread(
    graph: Any,
    config: dict[str, Any],
    question: str,
    history: list[dict[str, Any]] | None = None,
    *,
    focus_id: str | None = None,
    db_path: Any = None,
    turn: Callable[..., Iterator[str | TurnResult]] | None = None,
) -> Iterator[ChatEvent]:
    """Convenience for the app: pull the restaurant and all cards (incl. variants) from a paused graph run.

    Opens the database read-write, matching the graph's connection (DuckDB forbids mixing configs).
    """
    values = graph.get_state(config).values
    cards = [r for recs in values["recommendations"].values() for r in recs]
    con = q.connect(db_path or q.db_path())
    try:
        yield from answer(con, values["restaurant_id"], cards, question, history, focus_id=focus_id, turn=turn)
    finally:
        con.close()
