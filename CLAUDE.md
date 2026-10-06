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
- Python 3.12, `uv` for env and deps
- LangGraph for orchestration, Anthropic SDK for Claude
- Pydantic v2 for all state and LLM outputs (structured output, no free-text parsing)
- DuckDB for data (single file `data/jet.duckdb`)
- Streamlit for the demo UI
- Models: from env `MODEL_MAIN` (default `claude-sonnet-5-5`) and `MODEL_FAST`
  (default `claude-haiku-4-5-20251001`) for cheap per-item work like review tagging

## Commands
```bash
uv sync                                 # install
uv run python -m growth.data.seed       # generate synthetic data -> data/jet.duckdb
uv run python -m growth.cli <rest_id>   # run graph headless, print recommendations
uv run streamlit run app/main.py        # demo UI
uv run pytest -q                        # tests
uv run ruff check . && uv run ruff format .
```

## Layout
```
growth/
  data/seed.py        # synthetic data generator (deterministic, seeded)
  data/queries.py     # all SQL lives here; agents never write SQL inline
  state.py            # GraphState + Recommendation models
  graph.py            # LangGraph wiring
  agents/
    reviews.py        # review themes, complaints per dish
    menu.py           # descriptions, photos, dish pruning, bundles
    pricing.py        # price positioning vs nearby competitors
    promo_ads.py      # promo timing (slow slots) + sponsored listing budget
    impact.py         # estimates impact for each recommendation
  tools/listing.py    # read/apply patches to the mock listing store
app/main.py           # Streamlit UI
tests/
```

## Data model (synthetic)
Tables: `restaurants`, `menu_items`, `orders` (item-level, timestamped),
`reviews` (rating, text, item refs), `competitors` (nearby restaurants + menus),
`ad_campaigns`, `listings` (current public listing as JSON).
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
- Use Claude tool use / structured output to get Pydantic models back. Retry once on
  validation error, then skip that recommendation and log it.
- No network calls besides the Anthropic API. No scraping during the demo.
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
