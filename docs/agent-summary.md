# JET Restaurant Growth Agent: summary

## What it is

A growth manager for small restaurant partners on Just Eat Takeaway.com. It reads a
restaurant's listing, menu, orders, reviews, nearby competitors and ad campaigns, finds what
is costing it orders, and proposes concrete fixes. The owner approves or rejects each one,
can ask why, and approved changes go live on the listing, reversibly.

Every proposal must earn its place in money JET cares about: more orders, a higher basket,
better conversion, or ad spend that actually brings customers. JET earns commission on order
value (15% on marketplace, 30% when JET delivers) plus ad revenue. Every card shows the
projected weekly effect on orders, GMV, JET's revenue, and what it costs the owner.

## How it works

```
load_context ─┬─ reviews ───┐
              ├─ menu ──────┤
              ├─ pricing ───┼─ impact ─ rank ─ owner approval ─ apply ─ summary
              └─ promo_ads ─┘             │      (pauses here)     (reversible)
                                          └─ chat: "why this?" while the owner decides
```

A LangGraph run per restaurant. The four specialist agents run in parallel and only read
data. Nothing changes on the listing until the owner decides.

| Agent | Finds | How |
|---|---|---|
| Reviews | Dishes with recurring complaints (soggy, cold), and delivery problems that are the courier's fault, not the kitchen's | TypeSafe Jev tags each low-rated review (complaint? which dish? what issue? whose fault?); Python counts; the LLM writes the card |
| Menu | Bestsellers without photos, dishes without descriptions, dishes that barely sell | Sales data; the LLM writes the missing descriptions, which the patch publishes |
| Pricing | Categories priced well above the same dishes at the 5 nearest same-cuisine competitors | Like-for-like price comparison; proposes 3 cut depths as alternatives |
| Promo & ads | Dead time slots (e.g. Tuesday afternoon at a third of other weekdays); sponsored budget spent at peak, when customers order anyway | Order timing; proposes a timed promo, and the ad budget moved off-peak at 3 budget levels |

Each card carries its **evidence** (real review ids, menu items, competitor prices), the
**listing patch** it would apply, its **lever changes** (how it moves the demand model's
inputs, e.g. photo share 0.75 → 1.0), and, for alternatives, a **variant group**.

## From finding to decision

**Impact.** Two layers. Hand-set formulas in `agents/impact.py` value every card and show
their formula. A learned model (`growth/ml`) values every card that moves a model lever, with
a 90% range and the owner's profit. It learns from 26 weeks of market history using two-way
fixed effects, so it isn't fooled by the fact that big restaurants are the ones adding photos
(see Data). The LLM never produces a number.

**Choosing.** Alternatives (5/10/15% price cut, ad budget levels) and combinations need a
decision. The optimiser (`growth/ml/optimize.py`) solves it as an integer program: maximise
JET revenue so that the **owner never loses money**, with at most three actions, one option
per group, and an ad budget cap. Options the owner rejected never come back. A second
selection by Jev makes the judgment calls code can't, given the restaurant's positioning,
for example whether a premium restaurant should compete on price. Both picks are stored per
card in the `analyses` tables. In the current run they agree on 17 of 23 cards. Jev is more
cautious about weak deals, such as €1-a-week promos.

**Approval and apply.** The owner approves or rejects each card, with an optional reason
("we're closed Tuesday afternoons"). Approved patches are written in one transaction with a
before-image in a change log, and every change can be reverted.

**Explaining.** An "Ask why" chat sits beside the cards. It answers from the cards and five
read-only tools (dish reviews, competitor prices, dish sales, order times, impact breakdown).
It never writes SQL and can't change anything. Every number in an answer is checked against
the data, and unmatched numbers are flagged. It answers in the owner's language.

## Data

Synthetic, built by `python -m runners.generate_data` in about 5 seconds:

- **A market of 268 restaurants** in four Dutch cities, with location, cuisine and price
  level, plus 26 weeks of orders, GMV and listing changes for each.
- **8 demo partners** with item-level orders, reviews, menus and listings. Each has planted
  problems, and one control restaurant has none.
- **A hidden "true" demand model** generates the history. Adoption is deliberately
  confounded: bigger restaurants add photos and advertise more. A naive comparison doubles
  the photo effect (0.26 against a true 0.12), while the fixed-effects model recovers it
  (0.11). The truth stays outside the app and is used only by tests.

## Guardrails

- **No LLM numbers.** Figures come from formulas or the learned model, and their formula is
  shown.
- **Evidence or nothing.** Every card cites real rows.
- **The owner decides.** Nothing is written before approval. Prices and ads never change on
  their own.
- **The owner never loses** under the optimiser.
- **Everything is reversible.** Each change is logged with a before-image.
- **The chat is read-only and number-checked.**
- **Failures degrade gracefully.** If an agent, the LLM or Jev fails, the run continues
  with templates or without that card.

## Stack and running it

Python 3.11, LangGraph, DuckDB, Pydantic, Streamlit. LLM calls go through LiteLLM
(default `openai/gpt-6-luna`). Classification uses TypeSafe Jev. The ML layer is numpy,
scipy and HiGHS, with two local MCP servers (`jet-impact`, `jet-optimizer`).

```bash
pip install -r requirements.txt
python -m runners.generate_data          # build the database
streamlit run app/main.py                # demo UI
python -m runners.analyse_restaurants    # store analyses with optimiser and Jev picks
pytest -q                                # 144 tests; live API tests skip without keys
```

Keys go in the env file: `OPENAI_API_KEY` and `JEV_API_KEY`. Without them the app still
runs, with template wording and no chat.

## Status and next steps

**Working:** the full flow in the app (analyse, cards, chat, approve/reject, apply, summary,
reset); learned effects, optimiser and MCP servers; stored analyses.

**Not yet connected:**
1. **The graph still ranks with the hand-set formulas.** The learned model and the optimiser
   exist but aren't the graph's default yet. This is the main remaining integration.
2. **Owner rejection reasons are stored but don't yet re-plan.** The intended loop is that Jev
   classifies the reason into a constraint and the optimiser re-solves.
3. **The live demo still makes fresh model calls.** Caching them would make runs fast and
   repeatable.
4. **Limits to state in the pitch.** All data is synthetic. The ad off-peak effect can't be
   identified from this market. Jev's choice between price-cut depths is close to a coin
   flip, so the optimiser should own that call.

On real JET data, the same pipeline would learn from years of actual listing changes across
thousands of restaurants. The synthetic market exists to show the method recovers known
effects.
