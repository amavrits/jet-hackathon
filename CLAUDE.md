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
- LiteLLM for text generation (`growth/llm.py` is the only module that imports it)
- TypeSafe Jev for classification (`growth/classify.py` is the only module that imports
  `typesafe_sdk`). Jev returns typed answers with calibrated confidence, never text. Use it
  for per-item labelling (review tags, yes/no checks). Keep each state short, one item per
  call, and do counting and arithmetic in Python, never in the model.
- Pydantic v2 for all state and LLM outputs (structured output, no free-text parsing)
- DuckDB for data (single file `data/jet.duckdb`)
- Streamlit for the demo UI
- Env, loaded with python-dotenv. Never read or print the env file. `MODEL_MAIN` /
  `MODEL_FAST` for LiteLLM must carry the provider prefix, or set `LLM_API_BASE` /
  `LLM_API_KEY` for a proxy. `JEV_API_KEY` and optional `JEV_MODEL` (default `jev-latest`).
  Jev answers are cached in `data/cache/jev.json` (gitignored); delete it to force fresh calls.

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
  llm.py              # LiteLLM wrapper: structured() -> Pydantic, retry once, else None
  classify.py         # Jev wrapper: ask_many([(state, questions)]) -> answers, disk-cached
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
```
load_context -> [reviews, menu, pricing, promo_ads] (parallel)
             -> impact -> rank -> human_approval (interrupt) -> apply -> summary
```
- Specialist agents only read context and emit `Recommendation`s. They never apply changes.
- `human_approval` uses LangGraph `interrupt`. Nothing is written without approval.
- `apply` writes JSON patches to `listings` and logs to `change_log`. It is reversible.

## Recommendation contract
Every recommendation is a Pydantic model with:
`id, agent, title, rationale, evidence (list of data refs: review ids, order stats),
patch (JSON patch on the listing, or null for advice-only),
impact {orders_per_week, gmv_eur_per_week, jet_revenue_eur_per_week, ad_spend_eur},
confidence (low/med/high)`.
Evidence must reference real rows. No evidence means no recommendation.
Impact numbers come from `impact.py` using simple, explainable heuristics,
never from the LLM inventing figures. Show the formula in the UI.

## Conventions
- Prompts live next to their agent as module-level constants. Keep them short.
- Use `llm.structured(Schema, system, user)` to get Pydantic models back. It retries once
  on validation error, then returns None; the caller skips that recommendation and logs it.
- No network calls besides the LiteLLM endpoint and TypeSafe. No scraping during the demo.
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
