"""Both MCP servers expose their tools in-process and return valid, typed output."""

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from growth import llm
from servers import common, impact_mcp, optimizer_mcp


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.setattr(llm, "call_structured", lambda *a, **k: None)


@pytest.fixture(scope="module", autouse=True)
def point_servers_at_test_db(built_db, tmp_path_factory):
    common.configure(built_db, tmp_path_factory.mktemp("models") / "effects.json")


def call(server, name, **args):
    result = asyncio.run(server.call_tool(name, args))
    assert not getattr(result, "isError", False), result
    sc = getattr(result, "structuredContent", None)
    if sc is not None:
        return sc["result"] if isinstance(sc, dict) and set(sc) == {"result"} else sc
    parsed = [json.loads(c.text) for c in result.content]  # a list result is one text block per item
    return parsed if len(parsed) > 1 else parsed[0]


def test_impact_server_lists_expected_tools():
    names = {t.name for t in asyncio.run(impact_mcp.mcp.list_tools())}
    assert {
        "get_peers",
        "get_benchmark",
        "get_market_segments",
        "get_effects",
        "get_current_levers",
        "predict_impact",
    } <= names


def test_impact_server_tools_roundtrip():
    peers = call(impact_mcp.mcp, "get_peers", restaurant_id="r_bella_napoli", k=3)
    assert len(peers) == 3 and all(p["cuisine"] == "Pizza" for p in peers)

    effects = call(impact_mcp.mcp, "get_effects")
    assert {r["lever"] for r in effects["levers"]} >= {"photo", "elasticity_2", "kappa"}
    one = call(impact_mcp.mcp, "get_effects", lever="photo")
    assert one["lo"] <= one["coef"] <= one["hi"]

    pred = call(impact_mcp.mcp, "predict_impact", restaurant_id="r_bella_napoli", changes={"photo_share": 1.0})
    assert pred["orders_per_week"]["point"] > 0
    assert pred["jet_revenue_eur_per_week"]["lo"] <= pred["jet_revenue_eur_per_week"]["point"]

    card = asyncio.run(impact_mcp.mcp.read_resource("growth://model-card"))
    text = list(card)[0].content
    assert "two-way fixed effects" in text and "| photo |" in text


def test_impact_server_rejects_unknown_restaurant():
    """In-process, mcp 2.x raises ToolError; over a transport the client sees isError=true."""
    with pytest.raises(ToolError, match="unknown restaurant_id"):
        asyncio.run(impact_mcp.mcp.call_tool("get_peers", {"restaurant_id": "nope"}))


def test_optimizer_server_plan_and_portfolio():
    names = {t.name for t in asyncio.run(optimizer_mcp.mcp.list_tools())}
    assert {"list_options", "optimize_plan", "optimize_portfolio"} <= names

    options = call(optimizer_mcp.mcp, "list_options", restaurant_id="r_spice_route")
    assert any(o["group"] == "pricing-r_spice_route-mains" for o in options)

    plan = call(optimizer_mcp.mcp, "optimize_plan", restaurant_id="r_spice_route", max_actions=2)
    assert 1 <= len(plan["chosen"]) <= 2 and plan["owner_profit_eur_per_week"] >= 0

    rejected = plan["chosen"][0]["group"]
    again = call(optimizer_mcp.mcp, "optimize_plan", restaurant_id="r_spice_route", max_actions=2, exclude=[rejected])
    assert all(o["group"] != rejected for o in again["chosen"])

    pf = call(
        optimizer_mcp.mcp,
        "optimize_portfolio",
        restaurant_ids=["r_spice_route", "r_sushi_zen"],
        ad_budget_eur_per_week=50.0,
        hours=1.5,
    )
    assert pf["ad_spend_eur_per_week"] <= 50.0 and pf["hours_used"] <= 1.5
