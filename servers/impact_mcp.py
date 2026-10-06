"""MCP server "jet-impact": peers, learned effects and impact prediction.

    python -m servers.impact_mcp --transport stdio          # Claude Code / desktop clients
    python -m servers.impact_mcp --transport http --port 8765

Read-only. Never touches runners/true_model.py or data/true_model.json.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from growth.ml import effects as fx
from growth.ml import peers as pe
from growth.ml import predict as pr
from servers import common

mcp = MCPServer(
    "jet-impact",
    instructions="Learned effects of listing changes for JET restaurants, with peer groups and impact prediction.",
)


@mcp.tool()
def get_peers(restaurant_id: str, k: int = 10, same_city: bool = True) -> list[dict[str, Any]]:
    """The k most similar restaurants: nearest by distance, cuisine, price level, size and
    rating, with the score breakdown."""
    c = common.con()
    try:
        common.known_restaurant(c, restaurant_id)
        return pe.get_peers(c, restaurant_id, k, same_city=same_city)
    finally:
        c.close()


@mcp.tool()
def get_benchmark(restaurant_id: str, k: int = 10) -> dict[str, Any]:
    """The restaurant's photo share, description share, price index, ad budget, rating and
    volume against its peers' medians."""
    c = common.con()
    try:
        common.known_restaurant(c, restaurant_id)
        return pe.benchmark(c, restaurant_id, k)
    finally:
        c.close()


@mcp.tool()
def get_market_segments(k: int = 6) -> dict[str, Any]:
    """k-means segments of the whole market on location, price level, size, rating and
    listing quality. Returns segment profiles and every restaurant's label."""
    c = common.con()
    try:
        return pe.segment_market(c, k)
    finally:
        c.close()


@mcp.tool()
def get_effects(lever: str | None = None, refit: bool = False) -> dict[str, Any]:
    """Estimated effect of each lever (photo, description, elasticity by price level, promo,
    rating, ads) with 90% intervals, the number of past changes behind it, and the naive
    estimate for comparison."""
    e = common.effects(refit=refit)
    if lever:
        table = {row["lever"]: row for row in fx.summary_table(e)}
        if lever not in table:
            raise ValueError(f"unknown lever {lever!r}; one of {sorted(table)}")
        return {**table[lever], "method": e["method"]}
    return {
        "method": e["method"],
        "fitted_at": e["fitted_at"],
        "n_obs": e["n_obs"],
        "n_restaurants": e["n_restaurants"],
        "levers": fx.summary_table(e),
    }


@mcp.tool()
def get_current_levers(restaurant_id: str) -> dict[str, Any]:
    """Current lever values and weekly volume for a restaurant: photo_share, description_share,
    price_index, promo_active, rating, ad_budget_eur, ad_offpeak_share."""
    c = common.con()
    try:
        return pr.current_levers(c, restaurant_id)
    finally:
        c.close()


@mcp.tool()
def predict_impact(restaurant_id: str, changes: dict[str, float], robust: bool = False) -> dict[str, Any]:
    """Weekly delta in orders, GMV, JET revenue and owner profit for setting levers to new
    values, with 90% intervals and the formula. changes: lever -> new absolute value, e.g.
    {"photo_share": 1.0, "price_index": 0.95}. robust=true reports the pessimistic end."""
    c = common.con()
    try:
        common.known_restaurant(c, restaurant_id)
        return pr.predict_impact(c, restaurant_id, changes, effects=common.effects(), robust=robust).model_dump()
    finally:
        c.close()


@mcp.resource("growth://model-card")
def model_card() -> str:
    """What the effects model is, what it was fitted on, and how good it is."""
    e = common.effects()
    lines = [
        "# JET growth effects model",
        f"Method: {e['method']}",
        f"Fitted: {e['fitted_at']} on {e['n_obs']} ad-free restaurant-weeks "
        f"from {e['n_restaurants']} restaurants over {e['n_weeks']} weeks.",
        f"Uncertainty: cluster bootstrap by restaurant, {e['bootstrap_draws']} draws, 90% intervals.",
        "",
        "| lever | estimate | 90% interval | past changes | naive estimate |",
        "|---|---|---|---|---|",
    ]
    for row in fx.summary_table(e):
        naive = "" if row["naive"] is None else f"{row['naive']:+.3f}"
        interval = f"{row['lo']:+.3f} to {row['hi']:+.3f}"
        lines.append(f"| {row['lever']} | {row['coef']:+.3f} | {interval} | {row['n_events'] or ''} | {naive} |")
    lines += [
        "",
        "Levers not in the model (dish quality, delivery faults, pruning) keep the heuristic in "
        "growth/agents/impact.py and are marked as such.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    common.serve(mcp, default_port=8765)
