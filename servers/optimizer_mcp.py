"""MCP server "jet-optimizer": pick the best plan for a restaurant, or the best portfolio for JET.

    python -m servers.optimizer_mcp --transport stdio
    python -m servers.optimizer_mcp --transport http --port 8766

Read-only. Applying a plan still goes through owner approval in the graph.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from growth.ml import optimize as opt
from servers import common

mcp = MCPServer(
    "jet-optimizer",
    instructions="Revenue optimisation over candidate listing changes for JET restaurants: "
    "per-restaurant plans and JET-wide portfolios.",
)


@mcp.tool()
def list_options(restaurant_id: str) -> list[dict[str, Any]]:
    """Every candidate change for a restaurant with its predicted weekly value:
    the agents' recommendations plus price-cut and ad-budget grids."""
    c = common.con()
    try:
        common.known_restaurant(c, restaurant_id)
        return [o.model_dump() for o in opt.build_options(c, restaurant_id, effects=common.effects())]
    finally:
        c.close()


@mcp.tool()
def optimize_plan(
    restaurant_id: str,
    objective: str = "jet_revenue",
    max_actions: int = 3,
    ad_budget_cap_eur: float = 300.0,
    exclude: list[str] | None = None,
    robust: bool = False,
) -> dict[str, Any]:
    """Best combination of changes for one restaurant.
    objective: jet_revenue | owner_profit | balanced. The owner never loses money, at most
    max_actions changes, one option per group, ad spend under the cap. exclude: option ids or
    groups the owner rejected (e.g. "price:mains", "ads"). robust=true optimises the pessimistic
    end of the intervals."""
    if objective not in ("jet_revenue", "owner_profit", "balanced"):
        raise ValueError("objective must be jet_revenue, owner_profit or balanced")
    c = common.con()
    try:
        common.known_restaurant(c, restaurant_id)
        plan = opt.optimize_plan(
            c,
            restaurant_id,
            objective=objective,
            max_actions=max_actions,
            ad_budget_cap_eur=ad_budget_cap_eur,  # type: ignore[arg-type]
            exclude=exclude,
            robust=robust,
            effects=common.effects(),
        )
        return plan.model_dump()
    finally:
        c.close()


@mcp.tool()
def optimize_portfolio(
    restaurant_ids: list[str], ad_budget_eur_per_week: float = 500.0, hours: float = 4.0, robust: bool = False
) -> dict[str, Any]:
    """Which restaurants should get account-manager hours and ad credits: at most one plan per
    restaurant, total ad spend within budget, total hours within capacity, maximising JET revenue."""
    c = common.con()
    try:
        for rid in restaurant_ids:
            common.known_restaurant(c, rid)
        return opt.optimize_portfolio(
            c,
            restaurant_ids,
            ad_budget_eur_per_week=ad_budget_eur_per_week,
            hours=hours,
            robust=robust,
            effects=common.effects(),
        ).model_dump()
    finally:
        c.close()


if __name__ == "__main__":
    common.serve(mcp, default_port=8766)
