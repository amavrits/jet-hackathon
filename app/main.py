"""Streamlit demo UI for the JET restaurant growth agent.

Layout and flow by the app owner; wired to the real agents, graph and chat backend.
Run:  streamlit run app/main.py
"""

import sys
import uuid
from pathlib import Path
from typing import Any

# `streamlit run app/main.py` puts app/ on sys.path, not the repo root, so `growth` wouldn't import.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import altair as alt
import pandas as pd
import streamlit as st
from langgraph.types import Command

from growth import chat, llm
from growth.data import queries
from growth.graph import build_graph
from growth.state import Recommendation, Summary
from growth.tools import listing as listing_tool

# JET colours (PIE design system): orange, charcoal, cream, green, red.
ORANGE, CHARCOAL, CREAM, GREEN, RED = "#F36805", "#242E30", "#F6F3EF", "#2B7836", "#CC0300"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
NODE_LABELS = {
    "load_context": "Loaded listing, orders, reviews, competitors and campaigns",
    "reviews": "Reviews agent: complaint themes per dish (TypeSafe Jev)",
    "menu": "Menu agent: photos and descriptions on bestsellers",
    "pricing": "Pricing agent: dish prices against nearby competitors",
    "promo_ads": "Promo & ads agent: slow slots and sponsored listing",
    "impact": "Impact model: weekly impact computed",
    "rank": "Ranked by projected JET revenue",
    "__interrupt__": "Waiting for owner approval",
    "human_approval": "Owner decisions received",
    "apply": "Approved changes applied to the listing",
    "summary": "Summary computed",
}
AGENT_LABELS = {"reviews": "Reviews", "menu": "Menu", "pricing": "Pricing", "promo_ads": "Promo & ads"}
TOOL_LABELS = {
    "get_dish_reviews": "reviews",
    "get_competitor_prices": "competitor prices",
    "get_dish_sales": "dish sales",
    "get_orders_by_time": "order times",
    "explain_impact": "the impact formula",
}
SUGGESTIONS = ["Why this recommendation?", "How is the impact calculated?", "What happens if I say no?"]

st.set_page_config(page_title="JET Growth Agent", page_icon="📈", layout="wide")
st.markdown(
    f"""<style>
    [data-testid="stSidebar"] {{ border-right: 4px solid {ORANGE}; }}
    [data-testid="stMetricValue"] {{ color: {CHARCOAL}; }}
    h1 {{ color: {ORANGE}; }}
    </style>""",
    unsafe_allow_html=True,
)


@st.cache_resource
def graph(db: str):
    """One compiled graph per database file, shared across reruns so paused runs survive."""
    return build_graph(db)


def current_graph():
    return graph(str(queries.db_path()))


def ensure_data() -> None:
    if not queries.db_path().exists():
        from runners.generate_data import build

        with st.spinner("Generating synthetic market data..."):
            build(verbose=False)


def reset_run() -> None:
    for key in [k for k in st.session_state if k.startswith(("run_", "decision-", "reason-"))]:
        del st.session_state[key]


def eur(value: float, signed: bool = False) -> str:
    return f"€{value:+,.2f}" if signed else f"€{value:,.2f}"


# --- data loading -----------------------------------------------------------------


def load_overview(rid: str) -> dict[str, Any]:
    with queries.connect() as con:
        return {
            "restaurant": queries.get_restaurant(con, rid),
            "kpis": queries.kpis(con, rid),
            "listing": listing_tool.get_listing(con, rid),
            "weekday": queries.orders_by_weekday(con, rid),
            "items": {s["menu_item_id"]: s for s in queries.item_sales(con, rid)},
        }


def _promo(p: dict) -> str:
    return f"{p['discount_pct']:.0%} off {p['day'].capitalize()} {p['start']}-{p['end']}"


def _sponsored(s: dict) -> str:
    if not s.get("active"):
        return "off"
    sched = ", ".join(f"{x['day'].capitalize()} {x['start']}-{x['end']}" for x in s.get("schedule", []))
    return f"on, €{s['weekly_budget_eur']:.0f}/week" + (f", only {sched}" if sched else ", all day")


def listing_diff(before: dict, after: dict) -> pd.DataFrame:
    rows = []
    for item_id, item in after["menu"].items():
        old = before["menu"].get(item_id, {})
        for field in ("price_eur", "description", "photo_url", "is_available"):
            if old.get(field) != item.get(field):
                rows.append([item["name"], field, old.get(field), item.get(field)])
    for promo in after["promotions"][len(before["promotions"]) :]:
        rows.append(["Promotions", "added", "", _promo(promo)])
    if before["sponsored_listing"] != after["sponsored_listing"]:
        rows.append(
            [
                "Sponsored listing",
                "settings",
                _sponsored(before["sponsored_listing"]),
                _sponsored(after["sponsored_listing"]),
            ]
        )
    return pd.DataFrame(rows, columns=["Where", "Field", "Before", "After"]).astype(str)


# --- rendering ----------------------------------------------------------------------


def render_overview(ov: dict[str, Any]) -> None:
    r, k = ov["restaurant"], ov["kpis"]
    model = "JET delivers" if r["delivery_model"] == "jet_delivery" else "Marketplace"
    st.title(r["name"])
    st.caption(
        f"{r['cuisine']} · {r['city']} · {model} · commission {r['commission_rate']:.0%} · last 8 weeks, synthetic data"
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Orders / week", f"{k['orders_per_week']:.0f}")
    c2.metric("GMV / week", eur(k["gmv_eur_per_week"]))
    c3.metric("Avg basket", eur(k["avg_basket_eur"]))
    c4.metric("Avg rating", f"{k['avg_rating']:.2f} ★", help=f"{k['review_count']} reviews")

    left, right = st.columns([2, 3])
    with left:
        st.subheader("Orders by weekday")
        df = pd.DataFrame(ov["weekday"])
        df["Day"] = df.weekday.map(lambda d: WEEKDAYS[d])
        df["Orders/week"] = df.orders_per_week.round(1)
        chart = (
            alt.Chart(df)
            .mark_bar(color=ORANGE, cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
            .encode(
                x=alt.X("Day:N", sort=WEEKDAYS, title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("Orders/week:Q", title="Orders per week"),
                tooltip=["Day", "Orders/week"],
            )
            .properties(height=260)
        )
        st.altair_chart(chart, width="stretch")
    with right:
        st.subheader("Current listing")
        listing = ov["listing"]
        menu = pd.DataFrame(
            [
                {
                    "Item": m["name"],
                    "Category": m["category"],
                    "Price": m["price_eur"],
                    "Sold/week": round(ov["items"][item_id]["orders"] / 8, 1),
                    "Photo": "✓" if m["photo_url"] else "—",
                    "Description": m["description"] or "—",
                }
                for item_id, m in listing["menu"].items()
                if m.get("is_available", True)
            ]
        )
        st.dataframe(
            menu,
            hide_index=True,
            width="stretch",
            column_config={"Price": st.column_config.NumberColumn(format="€%.2f")},
        )
        promos = ", ".join(_promo(p) for p in listing["promotions"])
        st.caption(f"Sponsored listing: {_sponsored(listing['sponsored_listing'])} · Promotions: {promos or 'none'}")


def ask(rec_id: str | None, question: str) -> None:
    """Button callback: focus the chat on a card and queue a question."""
    st.session_state.run_chat_focus = rec_id
    st.session_state.run_chat_pending = question


def render_recommendation(rec: Recommendation, locked: bool) -> None:
    i = rec.impact
    with st.container(border=True):
        head, badge = st.columns([5, 2])
        head.markdown(f"**{rec.title}**")
        option = f" · option: {rec.variant_label}" if rec.variant_label else ""
        badge.caption(f"{AGENT_LABELS[rec.agent]} agent · confidence {rec.confidence}{option}")
        st.write(rec.rationale)
        if rec.action:
            st.markdown(f"**Action:** {rec.action}")
        cols = st.columns(4)
        cols[0].metric("Δ orders / week", f"{i.orders_per_week:+.1f}")
        cols[1].metric("Δ GMV / week", eur(i.gmv_eur_per_week, signed=True))
        cols[2].metric("JET revenue / week", eur(i.jet_revenue_eur_per_week, signed=True))
        cols[3].metric(
            "Cost to partner / week",
            eur(i.partner_cost_eur_per_week, signed=True),
            help="Price revenue on existing sales, promo discounts or extra ad spend. Negative = saving.",
        )
        with st.expander("How this is calculated"):
            st.code(i.formula, language=None)
            if rec.lever_changes:
                st.caption("What changes for the impact model:")
                for lc in rec.lever_changes:
                    st.caption(f"• {lc.lever}: {lc.before} → {lc.after} ({lc.note})")
            st.caption("Heuristics and assumptions live in growth/agents/impact.py.")
        with st.expander(f"Evidence ({len(rec.evidence)} data references)"):
            st.dataframe(
                pd.DataFrame([{"Type": e.kind, "Source": e.ref, "Detail": e.detail} for e in rec.evidence]),
                hide_index=True,
                width="stretch",
            )
        with st.expander("Listing change"):
            if rec.patch:
                st.json(rec.patch_ops(), expanded=False)
            else:
                st.caption("Advice only: nothing on the listing changes.")
        decision_col, ask_col = st.columns([3, 1])
        with decision_col:
            choice = st.segmented_control(
                "Decision",
                ["Approve", "Reject"],
                key=f"decision-{rec.id}",
                disabled=locked,
                label_visibility="collapsed",
            )
            if choice == "Reject":
                st.text_input(
                    "Why not? (optional, helps the next plan)",
                    key=f"reason-{rec.id}",
                    disabled=locked,
                    placeholder="e.g. We're closed Tuesday afternoons",
                )
        ask_col.button(
            "💬 Ask why",
            key=f"ask-{rec.id}",
            on_click=ask,
            args=(rec.id, "Why are you recommending this?"),
            width="stretch",
        )


def render_summary(summary: Summary, rid: str, before: dict) -> None:
    with queries.connect() as con:
        after = listing_tool.get_listing(con, rid)
        log = listing_tool.history(con, rid)

    st.subheader("Listing changes")
    diff = listing_diff(before, after)
    if diff.empty:
        st.caption("No listing changes: approved items were advice only, or nothing was approved.")
    else:
        st.dataframe(diff, hide_index=True, width="stretch")
    if log:
        with st.expander(f"Change log ({len(log)} entries, reversible)"):
            st.dataframe(pd.DataFrame(log).drop(columns=["patch", "listing_before"]), hide_index=True, width="stretch")

    t = summary.total
    st.subheader("Summary")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Approved / rejected", f"{summary.approved} / {summary.rejected}")
    c2.metric("Δ orders / week", f"{t.orders_per_week:+.1f}")
    c3.metric("Δ GMV / week", eur(t.gmv_eur_per_week, signed=True))
    c4.metric("JET revenue / week", eur(t.jet_revenue_eur_per_week, signed=True))
    st.caption(f"Cost to partner: {eur(t.partner_cost_eur_per_week, signed=True)} / week")

    n = st.slider("Scale across N similar restaurants", 100, 10_000, 1_000, step=100)
    yearly = t.jet_revenue_eur_per_week * 52 * n
    st.metric(f"Projected JET revenue per year across {n:,} restaurants", eur(yearly, signed=True))
    st.caption(
        f"= {eur(t.jet_revenue_eur_per_week)}/week × 52 weeks × {n:,} restaurants. "
        "A projection from synthetic data and heuristic uplift rates, not a measured result. "
        "It assumes every restaurant has the same problems and the same uplift."
    )


# --- chat ---------------------------------------------------------------------------


def _answer_stream(question: str, status) -> Any:
    """Yield answer text for st.write_stream; record tools, history and the grounding check."""
    meta: dict[str, Any] = {"tools": []}
    st.session_state.run_chat_last = meta
    events = chat.answer_for_thread(
        current_graph(),
        st.session_state.run_config,
        question,
        st.session_state.get("run_chat_history"),
        focus_id=st.session_state.get("run_chat_focus"),
    )
    for e in events:
        if e.type == "text":
            yield e.text
        elif e.type == "tool":
            label = TOOL_LABELS.get(e.name, e.name)
            meta["tools"].append(label)
            status.caption(f"Looking up {label}…")
        elif e.type == "done":
            meta["unverified"] = e.unverified_numbers
            st.session_state.run_chat_history = e.history
        elif e.type == "error":
            meta["error"] = True
            yield e.text
    status.empty()


def _render_message(m: dict[str, Any]) -> None:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m.get("tools"):
            st.caption("Looked up: " + ", ".join(dict.fromkeys(m["tools"])))
        if m.get("unverified"):
            st.warning(
                "Couldn't match these numbers to the data: " + ", ".join(m["unverified"]) + ". Treat them with care.",
                icon="⚠️",
            )


def render_chat(ranked: list[Recommendation]) -> None:
    st.subheader("Ask why")
    focus = st.session_state.get("run_chat_focus")
    titles = {r.id: r.title for r in ranked}
    if focus in titles:
        f1, f2 = st.columns([4, 1])
        f1.caption(f"About: **{titles[focus]}**")
        if f2.button("Clear", key="run_chat_clear_focus"):
            st.session_state.run_chat_focus = None
            st.rerun()
    else:
        st.caption("Ask about any recommendation, or press 💬 on a card.")

    messages: list[dict[str, Any]] = st.session_state.setdefault("run_chat", [])
    box = st.container(height=520, border=True)
    typed = st.chat_input("Why do you recommend…?", key="run_chat_input")
    if not messages:
        cols = st.columns(len(SUGGESTIONS))
        for col, s in zip(cols, SUGGESTIONS, strict=True):
            col.button(s, key=f"run_suggest_{s}", on_click=ask, args=(focus, s), width="stretch")

    question = typed or st.session_state.pop("run_chat_pending", None)
    with box:
        for m in messages:
            _render_message(m)
        if question:
            user = {"role": "user", "content": question}
            messages.append(user)
            _render_message(user)
            with st.chat_message("assistant"):
                status = st.empty()
                text = st.write_stream(_answer_stream(question, status))
                meta = st.session_state.get("run_chat_last", {})
                if meta.get("tools"):
                    st.caption("Looked up: " + ", ".join(dict.fromkeys(meta["tools"])))
                if meta.get("unverified"):
                    st.warning(
                        "Couldn't match these numbers to the data: " + ", ".join(meta["unverified"]) + ".",
                        icon="⚠️",
                    )
            messages.append(
                {
                    "role": "assistant",
                    "content": text if isinstance(text, str) else "".join(map(str, text)),
                    "tools": meta.get("tools", []),
                    "unverified": meta.get("unverified", []),
                }
            )


# --- graph runs ---------------------------------------------------------------------


def stream(payload: Any, config: dict) -> None:
    with st.status("Running agents...", expanded=True) as status:
        for update in current_graph().stream(payload, config, stream_mode="updates"):
            for node in update:
                st.session_state.run_log.append(NODE_LABELS.get(node, node))
                st.write(f"✓ {NODE_LABELS.get(node, node)}")
        status.update(label="Done", state="complete", expanded=False)


def analyse(rid: str) -> None:
    reset_run()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    st.session_state.run_config = config
    st.session_state.run_log = []
    with queries.connect() as con:
        st.session_state.run_before = listing_tool.get_listing(con, rid)
    stream({"restaurant_id": rid}, config)
    values = current_graph().get_state(config).values
    st.session_state.run_ranked = values["ranked"]
    st.session_state.run_errors = values.get("errors", [])
    st.session_state.run_phase = "review"


def apply_decisions(ranked: list[Recommendation], approver: str) -> None:
    approve, reject = [], {}
    for rec in ranked:
        choice = st.session_state.get(f"decision-{rec.id}")
        if choice == "Approve":
            approve.append(rec.id)
        elif choice == "Reject":
            reject[rec.id] = st.session_state.get(f"reason-{rec.id}", "").strip()
    config = st.session_state.run_config
    stream(Command(resume={"approve": approve, "reject": reject, "approver": approver}), config)
    values = current_graph().get_state(config).values
    st.session_state.run_summary = values["summary"]
    st.session_state.run_errors = values.get("errors", [])
    st.session_state.run_phase = "done"


# --- page -----------------------------------------------------------------------------

ensure_data()
with queries.connect() as _con:
    restaurants = queries.list_restaurants(_con)

with st.sidebar:
    st.header("JET Growth Agent")
    names = {r["restaurant_id"]: f"{r['name']} ({r['city']})" for r in restaurants}
    rid = st.selectbox("Restaurant", list(names), format_func=names.get, on_change=reset_run)
    approver = st.text_input("Approving as", value="Restaurant owner (demo)")
    st.divider()
    if st.button("Reset demo", help="Revert every applied change for this restaurant."):
        with queries.connect() as _con:
            listing_tool.reset(_con, rid)
        reset_run()
        st.rerun()
    st.caption(
        f"Detection: rules over the data, TypeSafe Jev for reviews. Wording and chat: {llm.model_name()}. "
        "Impact figures come from formulas in impact.py, not from an LLM."
    )

render_overview(load_overview(rid))
st.divider()

phase = st.session_state.get("run_phase", "idle")
if st.button("Analyse", type="primary"):
    analyse(rid)
    st.rerun()

if phase != "idle":
    with st.expander("Agent progress", expanded=False):
        for line in st.session_state.run_log:
            st.write(f"✓ {line}")
    for err in st.session_state.get("run_errors", []):
        st.warning(err)

    ranked: list[Recommendation] = st.session_state.run_ranked
    cards_col, chat_col = st.columns([3, 2], gap="large")
    with cards_col:
        st.subheader(f"Recommendations ({len(ranked)})")
        if not ranked:
            st.info("No recommendations: the agents found nothing backed by evidence for this restaurant.")
        for rec in ranked:
            render_recommendation(rec, locked=phase == "done")

        if phase == "review":
            decided = sum(1 for r in ranked if st.session_state.get(f"decision-{r.id}"))
            st.caption(f"{decided} of {len(ranked)} decided. Undecided items are not applied.")
            if st.button("Apply decisions", type="primary", disabled=not approver.strip()):
                apply_decisions(ranked, approver)
                st.rerun()
    with chat_col:
        render_chat(ranked)

    if phase == "done":
        render_summary(st.session_state.run_summary, rid, st.session_state.run_before)
