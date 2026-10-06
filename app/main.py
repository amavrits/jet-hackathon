"""Streamlit demo UI for the JET restaurant growth agent."""

import uuid
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
from langgraph.types import Command

from growth.data import queries, seed
from growth.graph import build_graph
from growth.state import GraphState, Recommendation, Summary

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
BAR_COLOR = "#2a78d6"
NODE_LABELS = {
    "load_context": "Loaded listing, orders, reviews, competitors and campaigns",
    "reviews": "Reviews agent: complaint themes per dish",
    "menu": "Menu agent: photos and descriptions on bestsellers",
    "pricing": "Pricing agent: category prices against nearby competitors",
    "promo_ads": "Promo & ads agent: slow slots and sponsored listing",
    "impact": "Impact model: evidence checked, weekly impact computed",
    "rank": "Ranked by projected JET revenue",
    "__interrupt__": "Waiting for owner approval",
    "human_approval": "Owner decisions received",
    "apply": "Approved changes applied to the listing",
    "summary": "Summary computed",
}
AGENT_LABELS = {
    "reviews": "Reviews",
    "menu": "Menu",
    "pricing": "Pricing",
    "promo_ads": "Promo & ads",
}

st.set_page_config(page_title="JET Growth Agent", page_icon="📈", layout="wide")


@st.cache_resource
def graph():
    return build_graph()


def ensure_data() -> None:
    if not queries.db_path().exists():
        with st.spinner("Generating synthetic data..."):
            seed.seed()


def reset_run() -> None:
    for key in [k for k in st.session_state if k.startswith(("run_", "decision-"))]:
        del st.session_state[key]


def eur(value: float, signed: bool = False) -> str:
    return f"€{value:+,.2f}" if signed else f"€{value:,.2f}"


# --- data loading -----------------------------------------------------------------


def load_overview(rid: str) -> dict[str, Any]:
    with queries.connect() as con:
        return {
            "restaurant": queries.get_restaurant(con, rid),
            "kpis": queries.kpis(con, rid),
            "listing": queries.get_listing(con, rid),
            "weekday": queries.orders_by_weekday(con, rid),
            "items": {s["item_id"]: s for s in queries.item_order_stats(con, rid)},
        }


def listing_diff(before: dict, after: dict) -> pd.DataFrame:
    rows = []
    old_menu = {m["item_id"]: m for m in before["menu"]}
    for item in after["menu"]:
        old = old_menu.get(item["item_id"], {})
        for field in ("price_eur", "description", "photo_url", "available"):
            if old.get(field) != item.get(field):
                rows.append([item["name"], field, old.get(field), item.get(field)])
    for promo in after["promotions"][len(before["promotions"]) :]:
        desc = f"{promo['percent']}% off {promo['weekday']} {promo['hours']}"
        rows.append(["Promotions", "added", "", desc])
    if before["sponsored"] != after["sponsored"]:
        fmt = lambda s: f"{'on' if s['active'] else 'off'}, €{s['weekly_budget_eur']:.0f}/week"  # noqa: E731
        rows.append(
            ["Sponsored listing", "status", fmt(before["sponsored"]), fmt(after["sponsored"])]
        )
    return pd.DataFrame(rows, columns=["Where", "Field", "Before", "After"]).astype(str)


# --- rendering ----------------------------------------------------------------------


def render_overview(ov: dict[str, Any]) -> None:
    r, k = ov["restaurant"], ov["kpis"]
    model = "JET delivers" if r["delivery_model"] == "jet_delivery" else "Marketplace"
    st.title(r["name"])
    st.caption(
        f"{r['cuisine']} · {r['city']} · {model} · commission {r['commission_rate']:.0%} · "
        "last 8 weeks, synthetic data"
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
            .mark_bar(color=BAR_COLOR, cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
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
                    "Sold/week": round(ov["items"][m["item_id"]]["orders_per_week"], 1),
                    "Photo": "✓" if m["photo_url"] else "—",
                    "Description": m["description"],
                }
                for m in listing["menu"]
            ]
        )
        st.dataframe(
            menu,
            hide_index=True,
            width="stretch",
            column_config={"Price": st.column_config.NumberColumn(format="€%.2f")},
        )
        sp = listing["sponsored"]
        promos = ", ".join(
            f"{p['percent']}% off {p['weekday']} {p['hours']}" for p in listing["promotions"]
        )
        sponsored = f"on, €{sp['weekly_budget_eur']:.0f}/week" if sp["active"] else "off"
        st.caption(f"Sponsored listing: {sponsored} · Promotions: {promos or 'none'}")


def render_recommendation(rec: Recommendation, locked: bool) -> None:
    i = rec.impact
    with st.container(border=True):
        head, badge = st.columns([5, 2])
        head.markdown(f"**{rec.title}**")
        badge.caption(f"{AGENT_LABELS[rec.agent]} agent · confidence {rec.confidence}")
        st.write(rec.rationale)
        cols = st.columns(4)
        cols[0].metric("Δ orders / week", f"{i.orders_per_week:+.1f}")
        cols[1].metric("Δ GMV / week", eur(i.gmv_eur_per_week, signed=True))
        cols[2].metric("JET revenue / week", eur(i.jet_revenue_eur_per_week, signed=True))
        cols[3].metric(
            "Cost to partner / week",
            eur(i.partner_cost_eur_per_week, signed=True),
            help="Discounts, ad spend or price revenue the restaurant gives up. Negative = saving.",
        )
        with st.expander("How this is calculated"):
            st.code(i.formula, language=None)
            st.caption("Heuristics and assumptions live in growth/agents/impact.py.")
        with st.expander(f"Evidence ({len(rec.evidence)} data references)"):
            st.dataframe(
                pd.DataFrame([{"Source": e.ref, "Detail": e.detail} for e in rec.evidence]),
                hide_index=True,
                width="stretch",
            )
        with st.expander("Listing change"):
            if rec.patch:
                st.json([op for op in rec.patch if op["op"] != "test"], expanded=False)
            else:
                st.caption("Advice only: nothing on the listing changes.")
        st.segmented_control(
            "Decision",
            ["Approve", "Reject"],
            key=f"decision-{rec.id}",
            disabled=locked,
            label_visibility="collapsed",
        )


def render_summary(summary: Summary, rid: str, before: dict) -> None:
    with queries.connect() as con:
        after = queries.get_listing(con, rid)
        log = queries.change_log(con, rid)

    st.subheader("Listing changes")
    diff = listing_diff(before, after)
    if diff.empty:
        st.caption("No listing changes: approved items were advice only, or nothing was approved.")
    else:
        st.dataframe(diff, hide_index=True, width="stretch")
    if log:
        with st.expander(f"Change log ({len(log)} entries, reversible)"):
            st.dataframe(
                pd.DataFrame(log).drop(columns=["patch"]), hide_index=True, width="stretch"
            )

    st.subheader("Summary")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Approved / rejected", f"{summary.approved} / {summary.rejected}")
    c2.metric("Δ orders / week", f"{summary.orders_per_week:+.1f}")
    c3.metric("Δ GMV / week", eur(summary.gmv_eur_per_week, signed=True))
    c4.metric("JET revenue / week", eur(summary.jet_revenue_eur_per_week, signed=True))
    st.caption(f"Cost to partner: {eur(summary.partner_cost_eur_per_week, signed=True)} / week")

    n = st.slider("Scale across N similar restaurants", 100, 10_000, 1_000, step=100)
    yearly = summary.jet_revenue_eur_per_week * 52 * n
    st.metric(f"Projected JET revenue per year across {n:,} restaurants", eur(yearly, signed=True))
    st.caption(
        f"= {eur(summary.jet_revenue_eur_per_week)}/week × 52 weeks × {n:,} restaurants. "
        "A projection from synthetic data and heuristic uplift rates, not a measured result. "
        "It assumes every restaurant has the same problems and the same uplift."
    )


# --- graph runs ---------------------------------------------------------------------


def stream(payload: Any, config: dict) -> None:
    with st.status("Running agents...", expanded=True) as status:
        for update in graph().stream(payload, config, stream_mode="updates"):
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
        st.session_state.run_before = queries.get_listing(con, rid)
    stream(GraphState(restaurant_id=rid), config)
    st.session_state.run_ranked = graph().get_state(config).values["ranked"]
    st.session_state.run_phase = "review"


def apply_decisions(ranked: list[Recommendation], approver: str) -> None:
    decisions = {}
    for rec in ranked:
        choice = st.session_state.get(f"decision-{rec.id}")
        if choice:
            decisions[rec.id] = choice == "Approve"
    config = st.session_state.run_config
    stream(Command(resume={"decisions": decisions, "approver": approver}), config)
    st.session_state.run_summary = graph().get_state(config).values["summary"]
    st.session_state.run_phase = "done"


# --- page -----------------------------------------------------------------------------

ensure_data()
with queries.connect() as _con:
    restaurants = queries.list_restaurants(_con)

with st.sidebar:
    st.header("JET Growth Agent")
    names = {r["id"]: f"{r['name']} ({r['city']})" for r in restaurants}
    rid = st.selectbox("Restaurant", list(names), format_func=names.get, on_change=reset_run)
    approver = st.text_input("Approving as", value="Restaurant owner (demo)")
    st.divider()
    if st.button("Reset demo data", help="Regenerate the synthetic database; undoes all changes."):
        reset_run()
        seed.seed()
        st.rerun()
    st.caption("Agents are rule-based stubs. Impact figures come from impact.py, not from an LLM.")

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

    ranked: list[Recommendation] = st.session_state.run_ranked
    st.subheader(f"Recommendations ({len(ranked)})")
    if not ranked:
        st.info(
            "No recommendations: the agents found nothing backed by evidence for this restaurant."
        )
    for rec in ranked:
        render_recommendation(rec, locked=phase == "done")

    if phase == "review":
        decided = sum(1 for r in ranked if st.session_state.get(f"decision-{r.id}"))
        st.caption(f"{decided} of {len(ranked)} decided. Undecided items are not applied.")
        if st.button("Apply decisions", type="primary", disabled=not approver.strip()):
            apply_decisions(ranked, approver)
            st.rerun()

    if phase == "done":
        render_summary(st.session_state.run_summary, rid, st.session_state.run_before)
