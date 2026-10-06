# CLAUDE.md — JET Restaurant Growth Agent

## What this is
Hackathon project for Just Eat Takeaway (JET). An agentic system that acts as a
"growth manager" for small restaurant partners. It analyses a restaurant's listing,
menu, reviews, sales and local competitors, proposes concrete changes, and, after
owner approval, applies them to the listing.

**Why JET cares:** JET earns commission on order value (~15% marketplace, ~30% when
JET delivers) plus advertising revenue (sponsored listings). Every recommendation
must tie back to one of: more orders, higher basket value, better conversion, or
justified ad spend. If a recommendation can't state its expected impact, drop it.

**Time budget:** hackathon. Favour a working end-to-end demo over completeness.
Do not build anything the demo doesn't show.

## Stack
- Python 3.11+, `pip` + `venv` for env and deps (`requirements.txt`)
- LangGraph for orchestration
- LiteLLM for text generation (`growth/llm.py` and `growth/chat.py` are the only modules that
  import it). The model name picks the provider.
- TypeSafe Jev for classification (`growth/classify.py` is the only module that imports
  `typesafe_sdk`). Jev returns typed answers with calibrated confidence, never text. Use it
  for per-item labelling (review tags, yes/no checks). Keep each state short, one item per
  call, and do counting and arithmetic in Python, never in the model.
- Pydantic v2 for all state and LLM outputs (structured output, no free-text parsing)
- DuckDB for data (single file `data/jet.duckdb`)
- Streamlit for the demo UI
- Env, loaded with python-dotenv. Never read or print the env file. `OPENAI_API_KEY` and/or
  `ANTHROPIC_API_KEY`. `MODEL_MAIN` / `MODEL_FAST` in LiteLLM form; the default follows the key
  present (`openai/gpt-6-luna`, else `anthropic/claude-sonnet-5-5`). `JEV_API_KEY`, optional
  `JEV_MODEL` (default `jev-latest`). Jev answers are cached in `data/cache/jev.json`
  (gitignored); delete it to force fresh calls.

## Commands
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt         # install
python -m runners.generate_data         # build data/jet.duckdb from the true model (~5 s)
python -m growth.cli <rest_id>          # run graph headless, print recommendations
streamlit run app/main.py               # demo UI
pytest -q                               # tests; live API tests auto-skip without keys
pytest -q -m live                       # only the live API tests
ruff check . && ruff format .
```

## Layout
```
growth/
  data/seed.py        # synthetic data generator (deterministic, seeded)
  data/queries.py     # all SQL lives here; agents never write SQL inline
  state.py            # GraphState + Recommendation models
  graph.py            # LangGraph wiring
  agents/
    reviews.py        # Jev-tagged complaints per dish, plus a delivery-fault card
    menu.py           # bestseller photos, missing descriptions (patch writes them), dead dishes
    pricing.py        # like-for-like price premium per category vs nearby competitors
    promo_ads.py      # dead-slot promos + moving sponsored budget out of the dinner peak
    impact.py         # one formula per rec kind; all assumptions are constants at the top
    common.py         # shared facts (basket, volume, commission) and helpers
  llm.py              # call_structured() -> Pydantic via LiteLLM JSON mode, retry once, else None
  classify.py         # Jev wrapper: ask_many([(state, questions)]) -> answers, disk-cached
  chat.py             # owner Q&A about the cards: streamed, read-only tools, number check
  tools/listing.py    # read/apply patches to the mock listing store
app/main.py           # Streamlit UI
runners/
  true_model.py       # TRUE demand model + market simulation. Ground truth, see below
  generate_data.py    # builds data/jet.duckdb and data/true_model.json
docs/ml-design.md     # true model, impact estimation, MCP server, optimisation
tests/
```

## Data model (synthetic)
Market: ~260 restaurants in `restaurants` (lat/lon, cuisine, price_level 1-3); `is_partner`
marks the 8 demo partners, which alone have item-level orders, reviews, menus and listings.
`restaurant_weeks` is a 26-week panel for every restaurant (orders, GMV, levers such as
photo_share, price_index, promo, ad budget); `change_events` logs past changes. Competitors of
a partner are its 5 nearest same-cuisine market restaurants.
**Ground truth rule:** `runners/true_model.py` and `data/true_model.json` hold the true
effects. Nothing under `growth/` may import or read them; estimators must recover effects from
the observable tables, and only tests compare against the truth.
Tables: `restaurants`, `menu_items`, `orders` (item-level, timestamped),
`reviews` (rating, text, item refs), `competitors` (nearby restaurants + menus),
`ad_campaigns`, `listings` (current public listing as JSON).
The listing's `menu` is an object keyed by `menu_item_id`, so patch paths are stable:
`/menu/<menu_item_id>/price_eur`. Promotions append to `/promotions/-`.
Seed 5–10 restaurants with planted, discoverable problems, for example: a dish with
repeated "soggy" reviews, Tuesday afternoon dead slot, prices 25% above the
competitor median on one category, missing photos on bestsellers.
The demo depends on these being findable, so keep them in the seed.

## Graph
Run it via `growth.graph`: `build_graph()`, stream to the approval interrupt, read
`pending_approval()`, resume with `Command(resume={"approve": [ids], "reject": {id: reason}})`.
The graph holds a read-write DuckDB connection; other code in the same process must open the
file read-write too (DuckDB forbids mixed configurations).
```
load_context -> [reviews, menu, pricing, promo_ads] (parallel)
             -> impact -> rank -> human_approval (interrupt) -> apply -> summary
```
- Specialist agents only read context and emit `Recommendation`s. They never apply changes.
- `human_approval` uses LangGraph `interrupt`. Nothing is written without approval.
- `apply` writes JSON patches to `listings` and logs to `change_log`. It is reversible.

## Chat
`growth/chat.py` explains the cards while the graph is paused. The app calls
`answer_for_thread(graph, config, question, history, focus_id=card_id)` and consumes events:
`text` (stream it), `tool` (show "looking up…"), `done` (store `history` unchanged, flag
`unverified_numbers`), `error`. The model only gets read-only tools that wrap `queries.py`
functions; it never writes SQL and cannot change the listing. Every number in an answer is
checked against the cards and tool results.

## Recommendation contract
Every recommendation is a Pydantic model with:
`id, agent, title, rationale, evidence (list of data refs: review ids, order stats),
patch (JSON patch on the listing, or null for advice-only),
impact {orders_per_week, gmv_eur_per_week, jet_revenue_eur_per_week, ad_spend_eur},
confidence (low/med/high)`.
Evidence must reference real rows. No evidence means no recommendation.
Agents also set `kind`, `facts`, and the ML contract fields:
- `lever_changes`: list of `{lever, before, after, note}` using the `restaurant_weeks` column
  names. `before` is the latest panel row (`queries.current_levers`). Empty when no model lever
  applies (dish complaints, delivery, hiding a dish); impact then stays heuristic.
- `variant_group` / `variant_label`: alternatives of one action (price cut depth, ad budget).
  Choose at most one per group. `rank` shows the owner the best per group; all stay in state
  for the optimiser.
Impact numbers come from `impact.py` using simple, explainable heuristics,
never from the LLM inventing figures. Show the formula in the UI.

## Conventions
- Prompts live next to their agent as module-level constants. Keep them short.
- Use `llm.call_structured(Schema, system, user)` to get Pydantic models back. It retries
  once on validation error and returns None on any failure (including a missing key); the
  caller falls back or skips and logs.
- No network calls besides the LLM provider (via LiteLLM) and TypeSafe. No scraping during the demo.
- Type hints everywhere. Small functions. No classes where a function will do.
- Tests: one per agent on a fixture restaurant, asserting the planted problem is found.

## Demo script (what must work)
1. Pick a restaurant in the UI and see the current listing and key stats.
2. Click "Analyse" and stream agent progress.
3. Show ranked recommendations with evidence and projected weekly JET revenue uplift.
4. Approve 2–3, reject 1, then show the updated listing diff.
5. Show the summary: total projected uplift for the restaurant, and for JET if scaled
   across N restaurants.

## Don't
- Don't add auth, a database server, Docker, or a React frontend.
- Don't let agents edit prices or launch ads without approval.
- Don't present LLM-generated numbers as data.
