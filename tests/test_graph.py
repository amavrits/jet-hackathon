"""Graph: parallel agents, approval interrupt, apply only what the owner approved, summary."""

import pytest

from growth import graph as G
from growth import llm
from growth.agents import reviews
from growth.data import queries as q
from growth.tools import listing as L


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(llm, "call_structured", lambda *a, **k: None)
    monkeypatch.setattr(reviews, "ask_many", lambda items: [None] * len(items))  # no Jev in graph tests


def _run_to_approval(db, rid):
    g = G.build_graph(db)
    cfg = G.new_thread()
    nodes = [n for upd in g.stream({"restaurant_id": rid}, cfg, stream_mode="updates") for n in upd]
    return g, cfg, nodes


def _listing(db, rid):
    c = q.connect(db)  # same config as the graph's connection; DuckDB forbids mixing in one process
    try:
        return L.get_listing(c, rid), L.history(c, rid)
    finally:
        c.close()


def test_pauses_for_approval_without_writing(writable_db):
    before, _ = _listing(writable_db, "r_spice_route")
    g, cfg, nodes = _run_to_approval(writable_db, "r_spice_route")
    assert set(G.SPECIALISTS) <= set(nodes)
    assert nodes[-1] == "__interrupt__"
    cards = G.pending_approval(g, cfg)
    assert {c.kind for c in cards} == {"price_cut", "slot_promo"}
    assert all(c.impact is not None for c in cards)
    jet = [c.impact.jet_revenue_eur_per_week for c in cards]
    assert jet == sorted(jet, reverse=True)
    after, log = _listing(writable_db, "r_spice_route")
    assert after == before and log == []


def test_applies_only_approved_and_records_reasons(writable_db):
    g, cfg, _ = _run_to_approval(writable_db, "r_spice_route")
    cards = {c.kind: c for c in G.pending_approval(g, cfg)}
    price, promo = cards["price_cut"], cards["slot_promo"]
    final = g.invoke(G.Command(resume={"approve": [price.id], "reject": {promo.id: "Closed Tuesday afternoons"}}), cfg)

    assert final["approvals"] == {price.id: True, promo.id: False}
    assert final["rejection_reasons"] == {promo.id: "Closed Tuesday afternoons"}
    assert [a.recommendation_id for a in final["applied"]] == [price.id]
    assert final["errors"] == []
    s = final["summary"]
    assert (s.approved, s.rejected) == (1, 1)
    assert s.total.jet_revenue_eur_per_week == price.impact.jet_revenue_eur_per_week

    listing, log = _listing(writable_db, "r_spice_route")
    for op in price.patch:
        item = op.path.split("/")[2]
        assert listing["menu"][item]["price_eur"] == op.value
    assert listing["promotions"] == []
    assert len(log) == 1


def test_advice_only_and_unknown_ids(writable_db):
    g, cfg, _ = _run_to_approval(writable_db, "r_bella_napoli")  # photos card is advice-only
    ids = [c.id for c in G.pending_approval(g, cfg)]
    final = g.invoke(G.Command(resume={"approve": [*ids, "nope"], "reject": []}), cfg)
    assert final["applied"] == []
    assert any("nope" in e for e in final["errors"])
    assert final["summary"].approved == len(ids)


def test_failing_agent_does_not_sink_the_run(writable_db, monkeypatch):
    def boom(con, rid):
        raise RuntimeError("kaput")

    monkeypatch.setitem(G.SPECIALISTS, "pricing", boom)
    g, cfg, _ = _run_to_approval(writable_db, "r_spice_route")
    cards = G.pending_approval(g, cfg)
    assert [c.kind for c in cards] == ["slot_promo"]
    assert any("pricing agent failed" in e for e in g.get_state(cfg).values["errors"])


def test_unknown_restaurant_fails_fast(writable_db):
    g = G.build_graph(writable_db)
    with pytest.raises(ValueError, match="unknown partner"):
        g.invoke({"restaurant_id": "m_000"}, G.new_thread())


def test_owner_sees_one_variant_per_group_but_state_keeps_all(writable_db):
    g, cfg, _ = _run_to_approval(writable_db, "r_spice_route")
    shown = G.pending_approval(g, cfg)
    groups = [c.variant_group for c in shown if c.variant_group]
    assert len(groups) == len(set(groups)) == 1
    all_recs = [r for recs in g.get_state(cfg).values["recommendations"].values() for r in recs]
    assert sum(r.variant_group == groups[0] for r in all_recs) == 3


def test_checkpoint_round_trip_has_no_unregistered_types(writable_db, caplog):
    g, cfg, _ = _run_to_approval(writable_db, "r_pho_house")
    with caplog.at_level("WARNING"):
        ids = [c.id for c in G.pending_approval(g, cfg)]
        g.invoke(G.Command(resume={"approve": ids}), cfg)
    assert not [r for r in caplog.records if "unregistered type" in r.getMessage()]
