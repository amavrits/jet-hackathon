# ML design: true model, impact estimation, MCP server, optimisation

Status: all of this is built. `growth/ml/` holds peers, effects, predict and optimize;
`servers/impact_mcp.py` and `servers/optimizer_mcp.py` expose them as two MCP servers (the
design below describes one server; two keep "what will happen" and "what should we do" as
separate tools for the agent). `tests/test_ml.py`, `tests/test_optimize.py` and `tests/test_mcp.py`
cover recovery against the truth, the optimiser's constraints and both servers.

Deviations from the design, found while building:
- The ad off-peak boost (lambda) is not identified from this market: only 22 daypart changes,
  most restaurants sit in a narrow 0.2-0.4 off-peak band, and weekly order noise is about twice
  the ad signal. The fit returns lambda near 0 with a 90% interval reaching the bound of 3, so
  `ad_daypart` cards carry a wide interval and the robust optimiser drops them. This is reported
  as-is rather than regularised. Kappa is recovered within its interval.
- Advertisers' organic level is calibrated on the level of orders in their ad-free weeks, not
  the mean of log orders, which otherwise inflates the excess attributed to ads by a few orders
  a week (Jensen gap).
- Recommendations reach the model through `Recommendation.lever_changes` (the contract in
  `growth/state.py`): the predictor reads the `after` values, and the optimiser treats a
  `variant_group` (the 15/10/5% price-cut variants, the ad-budget variants) as one choice. The
  only grid the optimiser adds itself is ad budget, since no agent proposes a fresh campaign.
- Segments are k-means over the whole market (location, price level, size, rating, listing
  quality, advertiser flag) and sit next to the kNN peers, not instead of them.

## 1. Why

The agents find problems with rules and evidence. The weak spot was impact: uplift numbers such
as "photos add 8% orders" or "price elasticity 1.5" were constants we chose. This design replaces
them with effects **learned from the market's history of past changes**, gives every number an
uncertainty range, and turns the final step from "rank the cards" into "choose the best plan
under constraints".

```
                 observable tables (DuckDB)
 restaurants ─┐  restaurant_weeks  change_events
              ▼          │               │
        peers (kNN)  ────┴──► effects (two-way FE / DiD) ──► predict_impact ──► optimise
              │                       ▲                            │               │
              └──────── MCP server (local) exposes all four ───────┴───────────────┘
                                      │
              runners/true_model.py ──┘ ground truth, used only to validate estimators
```

On real JET data the same pipeline would run on years of actual listing changes across
thousands of restaurants. The synthetic history exists to prove the method recovers known
effects.

## 2. Data

Build with `python -m runners.generate_data` (about 5 seconds, deterministic).

| Table | Rows | What it holds |
|---|---|---|
| `restaurants` | 268 | All market restaurants: lat/lon, cuisine, price level 1-3, rating, delivery model. `is_partner` marks the 8 demo partners. |
| `restaurant_weeks` | 6,968 | 26 weekly rows per restaurant: orders, GMV, basket, and the levers photo_share, description_share, price_index, promo_active, ad_budget_eur, ad_offpeak_share, rating. |
| `change_events` | 343 | Past changes: photos, descriptions, price, promo start/end, ad start/stop/budget/daypart, with before and after values. |
| `competitors` | 40 | Each partner's 5 nearest same-cuisine market restaurants, all within 2.5 km. |

Partners keep their item-level orders, reviews, menus, listings and planted problems. Their last
8 panel weeks equal their actual item-level totals, so both views agree. Earlier weeks come from
the true model, calibrated to their volume. These match within 7%.

`data/true_model.json` holds the true parameters. It is gitignored.

**Rule:** nothing under `growth/` may import `runners/true_model.py` or read
`data/true_model.json`. Only tests compare estimates with the truth.

## 3. The true model

Weekly orders of restaurant *r* in week *t*:

```
organic = exp( α_r + season_t + city_shock_c,t
             + 0.12 · photo_share + 0.04 · description_share
             + ε[price_level] · ln(price_index) + 0.08 · promo + 0.25 · (rating − 4.3) )
ads     = 1.1 · √(ad_budget) · (1 + 0.8 · ad_offpeak_share)
orders  ~ NegativeBinomial(mean = organic + ads, shape = 40)
basket  = {18, 24, 34}[price_level] · price_index · (1 − 0.15 · 0.30 · promo) · noise
```

| Lever | True effect | Meaning |
|---|---|---|
| Photos, full coverage | +12.7% orders | log effect 0.12 on share of items with a photo |
| Descriptions, full coverage | +4.1% orders | log effect 0.04 |
| Price elasticity | −2.0 / −1.5 / −0.9 | by price level: budget, mid, premium |
| Promo running | +8.3% orders | 15% discount, redeemed on 30% of orders |
| Rating | +2.5% per 0.1 star | |
| Ads | 1.1 · √budget orders/week | concave; off-peak scheduling raises the yield by up to 1.8x |

**Confounding is deliberate.** Big restaurants are more likely to add photos and descriptions and
to advertise. Restaurants priced above their market are more likely to cut prices. A naive
"restaurants with photos sell more" comparison therefore overstates the effect, which is
exactly the trap real marketplace data sets.

**Validation run on the generated data** (non-advertisers, log orders):

| Parameter | True | Two-way fixed effects | Naive pooled OLS |
|---|---|---|---|
| Photo | 0.120 | 0.114 | 0.259 |
| Description | 0.040 | 0.032 | 0.056 |
| Elasticity, budget | −2.00 | −1.70 | −1.23 |
| Elasticity, mid | −1.50 | −1.50 | −1.79 |
| Elasticity, premium | −0.90 | −0.92 | −2.27 |
| Promo | 0.080 | 0.062 | 0.068 |

Controlling for restaurant and city-week effects recovers the truth. The naive estimate doubles
the photo effect and gets premium price sensitivity wrong by a factor of 2.5. This table is the
core of the ML pitch.

## 4. Estimation (to build: `growth/ml/`)

### 4.1 Peer groups: `peers.py`

For a restaurant, find its *k* most similar market restaurants. Features: haversine distance,
same cuisine, price level, and size measured as median weekly orders. Score with a weighted
distance and take the nearest neighbours. Nearest neighbours fit better than k-means here,
because "who competes with me" depends on the restaurant, not on fixed clusters.

Peers serve three uses:
- **Pricing:** the price index benchmark. This replaces the fixed 5 competitors once menus
  exist for more restaurants.
- **Benchmarks:** for example "your peers have photos on 90% of dishes".
- **Comparison group:** the controls when estimating an effect for this restaurant's market.

### 4.2 Effects: `effects.py`

1. **Organic levers.** Regress log orders on photo, description, ln(price_index) by price
   level, promo and rating. Absorb restaurant fixed effects and city-by-week fixed effects.
   Fit on market restaurants without ads. This is the check in section 3, made into a module.
2. **Ad yield.** On advertisers, take orders minus the predicted organic orders from step 1.
   Fit the ad term `κ · √budget · (1 + λ · offpeak)` by nonlinear least squares, which recovers
   κ and λ.
3. **Uncertainty.** Run a cluster bootstrap by restaurant, with 200 resamples, to get 90%
   intervals for every parameter.
4. **Caveat.** Two-way fixed effects with staggered adoption is biased when effects differ by
   restaurant or over time. The true model uses constant effects, so plain two-way FE is valid
   here. On real data, switch to an event-study or Callaway–Sant'Anna estimator. The interface
   stays the same.

Fitting takes well under a second. Results are cached as `data/models/effects.json`, holding
coefficients, intervals and the number of change events per lever.

### 4.3 Prediction: `predict.py`

```
predict_impact(restaurant_id, changes) ->
    Δorders/week, ΔGMV/week, ΔJET revenue/week (point, 90% interval), formula string
```

Δlog organic orders = Σ β_lever · Δlever. New orders = current orders · exp(Δ) plus the change
in ad orders. GMV uses the basket model. JET revenue = commission · ΔGMV + Δad spend.

**Implemented on the agent side:** every recommendation carries `lever_changes`
(`growth/state.py: LeverChange`), so `predict_impact(restaurant_id, rec.lever_changes)` needs
nothing else. Pricing emits cut-depth variants (within 15/10/5% of the local median) and the
ad agent emits budget variants (x0.75, x1, x1.5, all off-peak), linked by `variant_group`.

How each recommendation kind maps to a lever:

| Recommendation kind | Lever change |
|---|---|
| add_photos | photo_share to the share after the fix |
| add_descriptions | description_share to 1.0 |
| price_cut | price_index scaled by the category's share of revenue |
| slot_promo | promo for the slot, scaled by the slot's share of weekly orders |
| ad_daypart | ad_offpeak_share to the new schedule |
| dish_quality, delivery_quality | not in the true model, so these keep the heuristic, marked as such |
| prune_item | no uplift |

`agents/impact.py` keeps its role and interface. It calls `predict_impact` instead of using
constants. The formula shown in the UI then reads, for example: "photo_share 0.75 → 1.0 ×
β = 0.114 (90%: 0.09–0.14), from 49 restaurants that added photos".

## 5. MCP server (to build: `servers/mcp_server.py`)

A local server that exposes the ML layer as tools. Claude Code, the LangGraph agents and
external tools can then all query the same models.

**Runtime**
- Python `mcp` SDK, using `FastMCP`.
- Two transports. **stdio** for Claude Code and desktop clients. **Streamable HTTP** on
  `127.0.0.1:8765` for the LangGraph app and Streamlit.
- Opens DuckDB read-only. It loads `data/models/effects.json` at startup, or fits and caches it
  if missing.
- No writes. Applying changes stays in `growth/tools/listing.py`, behind owner approval.
- Never reads `data/true_model.json`.

**Tools**

| Tool | Input | Output |
|---|---|---|
| `get_peers` | restaurant_id, k = 10 | peers with distance, similarity score, weekly orders |
| `get_effects` | optional lever | coefficient, 90% interval, number of events, method |
| `predict_impact` | restaurant_id, changes: list of lever and new value | Δorders, ΔGMV, ΔJET revenue with intervals, formula |
| `optimize_plan` | restaurant_id, objective, constraints | chosen actions, expected uplift, binding constraints |
| `optimize_portfolio` | restaurant_ids, budget, capacity | allocation across restaurants, total uplift |

Each tool validates restaurant ids against the `restaurants` table and returns Pydantic-typed
JSON. There is also one resource, `growth://model-card`, which summarises data window,
estimator, coefficients and validation status.

**Layout**

```
growth/ml/peers.py        growth/ml/effects.py
growth/ml/predict.py      growth/ml/optimize.py
servers/mcp_server.py     # thin wrapper: one tool per public function above
```

The graph imports `growth.ml` directly, for speed and simple tests. The MCP server wraps the
same functions, so the logic exists once.

**Claude Code registration**, in `.mcp.json` at the repo root:

```json
{
  "mcpServers": {
    "jet-growth": {
      "command": ".venv/bin/python",
      "args": ["-m", "servers.mcp_server", "--transport", "stdio"]
    }
  }
}
```

Run over HTTP for the app with:

```bash
python -m servers.mcp_server --transport http --port 8765
```

New dependencies: `mcp`, plus `scipy` for nonlinear least squares and the MILP solver.

## 6. Optimisation (to build: `growth/ml/optimize.py`, exposed via MCP)

The final step stops ranking cards one by one and chooses the best **combination** of actions.

### 6.1 Per-restaurant plan: `optimize_plan`

**Decision variables**
- Binary per discrete action: add photos, add descriptions, hide a dish, run a slot promo.
- Price cut per flagged category, chosen from a grid of 0, 5, 10, 15 or 20%. Encoded as one-hot
  binaries, at most one per category.
- Ad budget from a grid such as 0, 50, 100 … 300 €/week, and a schedule of peak or off-peak.
  Encoded as one-hot binaries.

**Objective** (default): maximise expected JET revenue per week, which is commission · GMV
plus ad spend. Alternatives are the restaurant's profit, or a weighted sum of both.

**Constraints**
- **The owner must be better off.** The change in restaurant profit must be at least zero.
  Restaurant profit = (1 − commission − food cost of 30%) · GMV − ad spend − promo discount
  cost. This keeps JET from recommending ads that only pay JET.
- At most 3 actions per plan, because the owner's attention is limited.
- Per-category price cut of 20% or less. Ad budget no higher than an owner-set cap.
- No promo and price cut on the same dishes.
- Owner rejections become constraints. "We're closed Tuesday afternoons" removes that promo.
  The plan is then re-solved, which is the re-planning loop in the demo.

**Problem class:** the effects are log-linear and the ad term is concave, so the exact problem is
a small mixed-integer nonlinear program (MINLP). For one restaurant it is tiny. Grids make the
continuous parts discrete, so every option combination has a value computed by
`predict_impact`. The problem then becomes an integer program over a few hundred options,
solved exactly with `scipy.optimize.milp`, which uses HiGHS. One caveat: summing option values
ignores interactions, since log effects multiply. Handle it by re-scoring the chosen plan with
`predict_impact` on the joint change. Alternatively, enumerate all combinations, which is also
trivially fast at this size.

**Robust variant:** optimise on the lower end of the 90% intervals, so the plan still pays if
the effects are at the pessimistic end. Expose it as a flag.

### 6.2 JET portfolio: `optimize_portfolio`

JET's own question is which restaurants should get account-manager time and ad credits.

- **Variables:** x_{r,o} ∈ {0,1}, meaning restaurant *r* receives option *o*. The options are
  that restaurant's top plans from `optimize_plan`.
- **Objective:** maximise Σ value_{r,o} · x_{r,o}, the expected JET revenue uplift.
- **Constraints:** at most one option per restaurant. Total ad credits within budget *B*. Total
  account-manager hours within capacity *H*. Optionally, a minimum coverage per city.
- **Problem class:** a multiple-choice knapsack, which is a pure integer program. HiGHS solves
  hundreds of restaurants in milliseconds.

### 6.3 Where it plugs in

The graph's rank node becomes a call to `optimize_plan`. The approval step shows the plan,
meaning a set of cards with a total. A rejection with a reason adds a constraint and re-solves.
The summary screen can show `optimize_portfolio` for "JET scaled across N restaurants".

## 7. Tests

- **Recovery:** each fitted effect falls within tolerance of `data/true_model.json`. For
  example, photo within ±0.03, and each elasticity within ±0.4. The naive estimate does *not*
  fall within tolerance, which proves the test can fail.
- **Prediction:** `predict_impact` with no change returns zero. Its point estimate lies inside
  its own interval.
- **Optimiser:** on a hand-built case with a known best plan, it finds that plan. The
  owner-better-off constraint removes an ads-only plan that loses the restaurant money. A
  rejected option never reappears.
- **MCP:** start the server over stdio inside the test, call each tool, and validate the output
  schema.

## 8. Build order

1. `growth/ml/effects.py` plus the recovery tests. This unlocks everything else.
2. `growth/ml/predict.py`, then switch `agents/impact.py` to it.
3. `growth/ml/peers.py`.
4. `growth/ml/optimize.py`, per-restaurant plan first.
5. `servers/mcp_server.py` and `.mcp.json`.
6. `optimize_portfolio` and the graph's re-planning loop.

## 9. Open questions

- **Optimisation objective:** JET revenue or restaurant profit by default? The "owner better
  off" constraint keeps either choice honest.
- **Food cost:** the profit constraint assumes 30% of GMV. It should be a per-restaurant input.
- **Dish complaints and delivery:** these have no lever in the true model. One option is to add
  a "complaint rate" lever that drives rating, which would let them be estimated too.
