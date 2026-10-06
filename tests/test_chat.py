"""Chat backend: tools hit real data, the tool loop round-trips, numbers are checked. No network."""

import json

import pytest

from growth import chat, llm
from growth.agents import menu, pricing, promo_ads
from growth.data import queries as q

RID = "r_pho_house"


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.setattr(llm, "call_structured", lambda *a, **k: None)


@pytest.fixture
def cards(con):
    from growth.agents import impact

    return impact.run([r for m in (menu, pricing, promo_ads) for r in m.run(con, RID)])


def _ctx(con, cards):
    return chat._Ctx(con=con, restaurant_id=RID, recs={c.id: c for c in cards})


# ----------------------------------------------------------------- tools on real data


def test_competitor_prices_tool(con, cards):
    out, err = chat.run_tool(_ctx(con, cards), "get_competitor_prices", {"dish": "crispy spring rolls"})
    data = json.loads(out)
    assert not err
    assert data["dish"] == "Crispy Spring Rolls"
    assert len(data["competitors"]) >= 3
    assert data["your_price_eur"] > max(c["price_eur"] for c in data["competitors"])  # planted overpricing


def test_dish_reviews_tool(con):
    ctx = chat._Ctx(con=con, restaurant_id="r_bella_napoli", recs={})
    data = json.loads(chat.run_tool(ctx, "get_dish_reviews", {"dish": "Calzone", "limit": 5})[0])
    assert len(data["reviews"]) == 5
    assert all(r["rating"] <= 3 for r in data["reviews"])


def test_orders_by_time_shows_the_dead_slot(con):
    ctx = chat._Ctx(con=con, restaurant_id="r_spice_route", recs={})
    days = json.loads(chat.run_tool(ctx, "get_orders_by_time", {})[0])["by_day"]
    assert days["tue"]["afternoon"] < 0.5 * days["wed"]["afternoon"]


def test_explain_impact_includes_formula_and_variants(con, cards):
    price = next(c for c in cards if c.kind == "price_cut")
    data = json.loads(chat.run_tool(_ctx(con, cards), "explain_impact", {"recommendation_id": price.id})[0])
    assert data["formula"] and data["inputs"]["avg_price_cut_pct"] > 0
    assert data["assumptions"]["PRICE_ELASTICITY"] == 1.5
    assert data["other_variants"]


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_competitor_prices", {"dish": "Lasagne"}),
        ("get_dish_sales", {"dish": 3}),
        ("nope", {}),
        ("explain_impact", {"recommendation_id": "x"}),
        ("get_dish_reviews", "not a dict"),
    ],
)
def test_bad_tool_calls_return_errors_not_exceptions(con, cards, name, args):
    out, err = chat.run_tool(_ctx(con, cards), name, args)
    assert err and out


# ------------------------------------------------------------------- grounding check


def test_unverified_numbers():
    grounding = '{"price_eur": 6.2, "competitor_median": 4.7, "premium_pct": 0.319, "orders": 1527}'
    ok = "You charge €6.20 against a median of €4.70, which is 32% more, across 1,527 orders."
    assert chat.unverified_numbers(ok, grounding) == []
    assert chat.unverified_numbers("Sales will rise by 18% to €7,400.", grounding) == ["18", "7,400"]
    assert chat.unverified_numbers("Your 2 starters at 3 competitors.", grounding) == []


# ------------------------------------------------------------------- the tool loop


class FakeTurns:
    """Stands in for chat.stream_turn: replays scripted turns, records what the model was sent."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls: list[list[dict]] = []

    def __call__(self, model, messages, tools):
        self.calls.append(json.loads(json.dumps(messages, default=str)))  # snapshot: the list mutates
        text, tool_calls, finish = self.turns.pop(0)
        if isinstance(text, Exception):
            raise text
        if text:
            yield text
        yield chat.TurnResult(content=text, tool_calls=tool_calls, finish_reason=finish)


def _call(name, args, cid="call_1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_tool_loop_round_trip(con, cards):
    fake = FakeTurns(
        ("", [_call("get_competitor_prices", {"dish": "Crispy Spring Rolls"})], "tool_calls"),
        ("You charge €6.20; nearby the median is €4.70.", [], "stop"),
    )
    events = list(chat.answer(con, RID, cards, "Why are my starters too expensive?", turn=fake))
    assert [e.type for e in events] == ["tool", "text", "done"]
    done = events[-1]
    assert done.text == "You charge €6.20; nearby the median is €4.70."
    assert done.unverified_numbers == []

    second = fake.calls[1]
    assert second[0]["role"] == "system" and "CONTEXT" in second[0]["content"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["id"] == "call_1"
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call_1"
    assert "Express Vietnamese Club" in second[-1]["content"]

    # history excludes the system prompt and round-trips: the next question only appends
    assert all(m["role"] != "system" for m in done.history)
    fake2 = FakeTurns(("Yes.", [], "stop"))
    list(chat.answer(con, RID, cards, "Sure?", done.history, turn=fake2))
    assert fake2.calls[0][1 : 1 + len(done.history)] == json.loads(json.dumps(done.history, default=str))


def test_bad_tool_arguments_become_error_results(con, cards):
    bad = {"id": "call_9", "type": "function", "function": {"name": "get_dish_sales", "arguments": "{not json"}}
    fake = FakeTurns(("", [bad], "tool_calls"), ("Sorry.", [], "stop"))
    list(chat.answer(con, RID, cards, "x", turn=fake))
    assert fake.calls[1][-1]["content"].startswith("ERROR:")


def test_invented_number_is_flagged(con, cards):
    fake = FakeTurns(("This will bring 37 extra orders and €912 a week.", [], "stop"))
    done = list(chat.answer(con, RID, cards, "How much?", turn=fake))[-1]
    assert set(done.unverified_numbers) == {"37", "912"}


def test_content_filter_and_failures(con, cards):
    refused = list(chat.answer(con, RID, cards, "x", turn=FakeTurns(("", [], "content_filter"))))
    assert refused[-1].type == "done" and "can't help" in refused[-1].text

    events = list(chat.answer(con, RID, cards, "x", turn=FakeTurns((RuntimeError("no key"), [], ""))))
    assert [e.type for e in events] == ["error"]


def test_answer_for_thread_uses_all_variants(writable_db, monkeypatch):
    from growth import graph as G
    from growth.agents import reviews

    monkeypatch.setattr(reviews, "ask_many", lambda items: [None] * len(items))
    g = G.build_graph(writable_db)
    cfg = G.new_thread()
    list(g.stream({"restaurant_id": RID}, cfg, stream_mode="updates"))
    fake = FakeTurns(("ok", [], "stop"))
    list(chat.answer_for_thread(g, cfg, "hi", db_path=writable_db, turn=fake))
    ctx = json.loads(fake.calls[0][0]["content"].split("CONTEXT\n", 1)[1])
    labels = {r["variant_label"] for r in ctx["recommendations"] if r["kind"] == "price_cut"}
    assert len(labels) == 2  # both Pho House price variants, not just the one shown
    assert q.get_restaurant  # noqa: B018  (keeps the import meaningful for readers)


@pytest.mark.live
@pytest.mark.skipif(
    not any(__import__("os").environ.get(k) for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")), reason="no LLM key"
)
def test_live_chat_uses_tools_and_stays_grounded(con, cards):
    price = next(c for c in cards if c.kind == "price_cut")
    events = list(chat.answer(con, RID, cards, "Which competitors charge less for spring rolls?", focus_id=price.id))
    assert events[-1].type == "done", events[-1].text
    assert any(e.type == "tool" for e in events)
    assert events[-1].unverified_numbers == []
