import uuid

import pytest
from langgraph.types import Command

from growth.data import queries
from growth.graph import build_graph
from growth.state import GraphState
from growth.tools import listing


def _start(rid: str):
    graph = build_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    list(graph.stream(GraphState(restaurant_id=rid), config, stream_mode="updates"))
    return graph, config


def test_graph_halts_at_approval_and_writes_nothing():
    with queries.connect() as con:
        before = queries.get_listing(con, "r2")
    graph, config = _start("r2")
    assert graph.get_state(config).next == ("human_approval",)
    with queries.connect() as con:
        assert queries.get_listing(con, "r2") == before
        assert queries.change_log(con, "r2") == []


def test_only_approved_known_recommendations_are_applied():
    graph, config = _start("r3")
    ranked = graph.get_state(config).values["ranked"]
    ids = {r.id for r in ranked}
    assert ids == {"menu-photos", "menu-descriptions"}
    decisions = {"menu-descriptions": True, "menu-photos": False, "never-proposed": True}
    list(graph.stream(Command(resume={"decisions": decisions, "approver": "owner"}), config))
    state = graph.get_state(config).values
    assert state["decisions"] == {"menu-descriptions": True, "menu-photos": False}
    assert state["summary"].approved == 1 and state["summary"].rejected == 1
    with queries.connect() as con:
        log = queries.change_log(con, "r3")
        assert [c["recommendation_id"] for c in log] == ["menu-descriptions"]
        assert log[0]["approver"] == "owner"
        restored = listing.revert(con, log[0]["id"])
        assert restored["menu"][0]["description"] == "Burger."


def test_apply_requires_an_approver():
    with queries.connect() as con, pytest.raises(ValueError):
        listing.apply(con, "r1", "x", [{"op": "replace", "path": "/name", "value": "y"}], " ")
