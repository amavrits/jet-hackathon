"""Choose the best combination of changes, not just the best single card.

optimize_plan      one restaurant. Options are the agents' recommendations plus grids for price
                   cuts and ad budgets. Every option is valued with predict_impact, then an
                   integer program picks the set that maximises the objective subject to:
                   the owner must not lose money, at most N actions, one option per group,
                   an ad budget cap, and any options the owner has rejected. The chosen set is
                   re-scored jointly, because log effects multiply rather than add.
optimize_portfolio JET's question: which restaurants get account-manager hours and ad credits.
                   A multiple-choice knapsack over each restaurant's candidate plans.

Both are small enough to solve exactly with scipy's MILP (HiGHS) in milliseconds.
"""

from __future__ import annotations

import math
from typing import Any, Literal

import duckdb
import numpy as np
from pydantic import BaseModel, Field
from scipy.optimize import Bounds, LinearConstraint, milp

from growth.ml import effects as fx
from growth.ml import predict as pr
from growth.state import Recommendation

Objective = Literal["jet_revenue", "owner_profit", "balanced"]
AD_GRID = (0.0, 50.0, 100.0, 150.0, 200.0, 300.0)  # the agents propose no fresh ad budgets, so the grid does
HOURS_PER_ACTION = 0.5  # account-manager time to get one change live
AD_LEVERS = {"ad_budget_eur", "ad_offpeak_share"}  # anything touching these shares the "ads" group


class Option(BaseModel):
    id: str
    group: str  # at most one option per group can be chosen
    label: str
    changes: dict[str, float]  # lever -> new value; empty for heuristic-only options
    recommendation_id: str | None = None
    source: Literal["model", "heuristic"] = "model"
    orders_per_week: float
    gmv_eur_per_week: float
    jet_revenue_eur_per_week: float
    jet_revenue_lo: float
    owner_profit_eur_per_week: float
    owner_profit_lo: float
    ad_spend_eur_per_week: float
    hours: float = HOURS_PER_ACTION


class Plan(BaseModel):
    restaurant_id: str
    objective: Objective
    robust: bool
    max_actions: int
    chosen: list[Option]
    additive_jet_revenue_eur_per_week: float
    joint: pr.Prediction | None  # the chosen model-backed changes re-scored together
    jet_revenue_eur_per_week: float  # joint model value + heuristic options
    owner_profit_eur_per_week: float
    ad_spend_eur_per_week: float
    binding_constraints: list[str]
    considered: int
    excluded: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------------- options


def _option_from_changes(
    con: duckdb.DuckDBPyConnection,
    rid: str,
    id_: str,
    group: str,
    label: str,
    changes: dict[str, float],
    effects: dict[str, Any],
    rec_id: str | None = None,
) -> Option:
    p = pr.predict_impact(con, rid, changes, effects=effects)
    return Option(
        id=id_,
        group=group,
        label=label,
        changes=changes,
        recommendation_id=rec_id,
        orders_per_week=p.orders_per_week.point,
        gmv_eur_per_week=p.gmv_eur_per_week.point,
        jet_revenue_eur_per_week=p.jet_revenue_eur_per_week.point,
        jet_revenue_lo=p.jet_revenue_eur_per_week.lo,
        owner_profit_eur_per_week=p.owner_profit_eur_per_week.point,
        owner_profit_lo=p.owner_profit_eur_per_week.lo,
        ad_spend_eur_per_week=p.ad_spend_eur_per_week,
    )


def _heuristic_option(rec: Recommendation, commission: float) -> Option | None:
    if rec.impact is None:
        return None
    gmv = rec.impact.gmv_eur_per_week
    owner = (1 - commission - pr.FOOD_COST) * gmv - rec.impact.ad_spend_eur
    return Option(
        id=rec.id,
        group=rec.id,
        label=rec.title,
        changes={},
        recommendation_id=rec.id,
        source="heuristic",
        orders_per_week=rec.impact.orders_per_week,
        gmv_eur_per_week=gmv,
        jet_revenue_eur_per_week=rec.impact.jet_revenue_eur_per_week,
        jet_revenue_lo=0.0,  # no interval: the heuristic has none, so the robust solve ignores it
        owner_profit_eur_per_week=round(owner, 2),
        owner_profit_lo=min(0.0, round(owner, 2)),
        ad_spend_eur_per_week=rec.impact.ad_spend_eur,
    )


def default_recommendations(con: duckdb.DuckDBPyConnection, rid: str) -> list[Recommendation]:
    """The deterministic agents (menu, pricing, promo_ads). Reviews need the classifier, so the
    caller passes those in when it has them."""
    from growth.agents import impact, menu, pricing, promo_ads

    recs = [r for m in (menu, pricing, promo_ads) for r in m.run(con, rid)]
    return impact.run(recs)


def build_options(
    con: duckdb.DuckDBPyConnection,
    rid: str,
    recs: list[Recommendation] | None = None,
    *,
    effects: dict[str, Any] | None = None,
    ad_grid: tuple[float, ...] = AD_GRID,
) -> list[Option]:
    """One option per recommendation (its `lever_changes` give the lever values; variants of one
    `variant_group` are mutually exclusive), plus an ad-budget grid. Recommendations without a
    lever in the model become heuristic options valued by agents/impact.py."""
    effects = effects or fx.load(con)
    now = pr.current_levers(con, rid)
    recs = default_recommendations(con, rid) if recs is None else recs
    options: list[Option] = []

    for rec in recs:
        changes = pr.changes_for_recommendation(rec)
        if not changes:
            opt = _heuristic_option(rec, now["commission_rate"])
            if opt:
                options.append(opt)
            continue
        group = "ads" if set(changes) & AD_LEVERS else (rec.variant_group or rec.id)
        label = f"{rec.title} ({rec.variant_label})" if rec.variant_label else rec.title
        options.append(_option_from_changes(con, rid, rec.id, group, label, changes, effects, rec.id))

    for budget in ad_grid:
        if budget == now["ad_budget_eur"]:
            continue
        for sched, off in (("current", now["ad_offpeak_share"]), ("offpeak", pr.MAX_OFFPEAK_SHARE)):
            if budget == 0 and sched == "offpeak":
                continue
            label = (
                "Stop sponsored listing" if budget == 0 else f"Sponsored listing at €{budget:.0f}/wk, {sched} schedule"
            )
            options.append(
                _option_from_changes(
                    con,
                    rid,
                    f"ads:{budget:.0f}:{sched}",
                    "ads",
                    label,
                    {"ad_budget_eur": budget, "ad_offpeak_share": off},
                    effects,
                )
            )
    return options


# ------------------------------------------------------------------------- solve


def _value(o: Option, objective: Objective, robust: bool) -> float:
    jet = o.jet_revenue_lo if robust else o.jet_revenue_eur_per_week
    owner = o.owner_profit_lo if robust else o.owner_profit_eur_per_week
    return {"jet_revenue": jet, "owner_profit": owner, "balanced": 0.5 * (jet + owner)}[objective]


def _merge_changes(now: dict[str, Any], chosen: list[Option]) -> dict[str, float]:
    """Combine the chosen options' lever values into one joint change."""
    new: dict[str, float] = {}
    for o in chosen:
        for lever, v in o.changes.items():
            if lever == "price_index":
                new[lever] = new.get(lever, now[lever]) * (v / now[lever])
            elif lever in ("photo_share", "description_share", "promo_active"):
                new[lever] = min(1.0, new.get(lever, now[lever]) + (v - now[lever]))
            else:
                new[lever] = v
    return new


def optimize_plan(
    con: duckdb.DuckDBPyConnection,
    rid: str,
    *,
    recs: list[Recommendation] | None = None,
    options: list[Option] | None = None,
    objective: Objective = "jet_revenue",
    max_actions: int = 3,
    ad_budget_cap_eur: float = 300.0,
    exclude: list[str] | None = None,
    robust: bool = False,
    effects: dict[str, Any] | None = None,
) -> Plan:
    """Best set of options under the constraints. `exclude` holds option ids or groups the
    owner rejected; they never come back."""
    effects = effects or fx.load(con)
    now = pr.current_levers(con, rid)
    all_options = options if options is not None else build_options(con, rid, recs, effects=effects)
    excluded = set(exclude or [])
    opts = [o for o in all_options if o.id not in excluded and o.group not in excluded]
    if not opts:
        return _plan(rid, [], all_options, objective, robust, max_actions, [], sorted(excluded), con, now, effects)

    n = len(opts)
    value = np.array([_value(o, objective, robust) for o in opts])
    owner = np.array([o.owner_profit_lo if robust else o.owner_profit_eur_per_week for o in opts])
    ads = np.array([o.ad_spend_eur_per_week for o in opts])
    A, lb, ub, names = [], [], [], []
    groups = sorted({o.group for o in opts})
    for g in groups:
        A.append([1.0 if o.group == g else 0.0 for o in opts])
        lb.append(0)
        ub.append(1)
        names.append(f"one option for {g}")
    A.append([1.0] * n)
    lb.append(0)
    ub.append(max_actions)
    names.append(f"at most {max_actions} actions")
    A.append(list(owner))
    lb.append(0.0)
    ub.append(np.inf)
    names.append("owner profit >= 0")
    A.append(list(ads))
    lb.append(-np.inf)
    ub.append(ad_budget_cap_eur - now["ad_budget_eur"])
    names.append(f"ad budget <= €{ad_budget_cap_eur:.0f}")

    res = milp(c=-value, integrality=np.ones(n), bounds=Bounds(0, 1), constraints=LinearConstraint(np.array(A), lb, ub))
    x = np.round(res.x).astype(bool) if res.success and res.x is not None else np.zeros(n, dtype=bool)
    chosen = [o for o, pick in zip(opts, x, strict=True) if pick]

    binding = []
    rows = np.array(A) @ x.astype(float)
    for row, lo_, hi_, name in zip(rows, lb, ub, names, strict=True):
        if (np.isfinite(hi_) and abs(row - hi_) < 1e-6 and name.startswith(("at most", "ad budget"))) or (
            name.startswith("owner") and abs(row - lo_) < 1e-6 and chosen
        ):
            binding.append(name)
    return _plan(rid, chosen, all_options, objective, robust, max_actions, binding, sorted(excluded), con, now, effects)


def _plan(rid, chosen, all_options, objective, robust, max_actions, binding, excluded, con, now, effects) -> Plan:
    model_opts = [o for o in chosen if o.changes]
    joint = (
        pr.predict_impact(con, rid, _merge_changes(now, model_opts), effects=effects, robust=robust)
        if model_opts
        else None
    )
    heur_jet = sum(o.jet_revenue_eur_per_week for o in chosen if not o.changes)
    heur_owner = sum(o.owner_profit_eur_per_week for o in chosen if not o.changes)
    return Plan(
        restaurant_id=rid,
        objective=objective,
        robust=robust,
        max_actions=max_actions,
        chosen=chosen,
        additive_jet_revenue_eur_per_week=round(sum(o.jet_revenue_eur_per_week for o in chosen), 2),
        joint=joint,
        jet_revenue_eur_per_week=round((joint.jet_revenue_eur_per_week.point if joint else 0.0) + heur_jet, 2),
        owner_profit_eur_per_week=round((joint.owner_profit_eur_per_week.point if joint else 0.0) + heur_owner, 2),
        ad_spend_eur_per_week=round(sum(o.ad_spend_eur_per_week for o in chosen), 2),
        binding_constraints=binding,
        considered=len(all_options),
        excluded=excluded,
    )


# --------------------------------------------------------------------- portfolio


class Allocation(BaseModel):
    restaurant_id: str
    plan: Plan
    hours: float


class Portfolio(BaseModel):
    restaurant_ids: list[str]
    ad_budget_eur_per_week: float
    hours: float
    allocations: list[Allocation]
    jet_revenue_eur_per_week: float
    ad_spend_eur_per_week: float
    hours_used: float
    skipped: list[str]


def candidate_plans(
    con: duckdb.DuckDBPyConnection, rid: str, *, recs=None, effects=None, robust: bool = False
) -> list[Plan]:
    """A few distinct plans per restaurant: 1 to 3 actions, with and without any ad spend."""
    options = build_options(con, rid, recs, effects=effects)
    plans: dict[tuple[str, ...], Plan] = {}
    for k in (1, 2, 3):
        for cap in (0.0, AD_GRID[-1]):
            p = optimize_plan(
                con, rid, options=options, max_actions=k, ad_budget_cap_eur=cap, robust=robust, effects=effects
            )
            key = tuple(sorted(o.id for o in p.chosen))
            if key and key not in plans:
                plans[key] = p
    return list(plans.values())


def optimize_portfolio(
    con: duckdb.DuckDBPyConnection,
    restaurant_ids: list[str],
    *,
    ad_budget_eur_per_week: float = 500.0,
    hours: float = 4.0,
    recs_by_restaurant: dict[str, list[Recommendation]] | None = None,
    robust: bool = False,
    effects: dict[str, Any] | None = None,
) -> Portfolio:
    """Pick at most one plan per restaurant to maximise JET revenue within ad credits and hours."""
    effects = effects or fx.load(con)
    cands: list[tuple[str, Plan]] = []
    for rid in restaurant_ids:
        recs = (recs_by_restaurant or {}).get(rid)
        cands += [(rid, p) for p in candidate_plans(con, rid, recs=recs, effects=effects, robust=robust)]
    if not cands:
        return Portfolio(
            restaurant_ids=restaurant_ids,
            ad_budget_eur_per_week=ad_budget_eur_per_week,
            hours=hours,
            allocations=[],
            jet_revenue_eur_per_week=0,
            ad_spend_eur_per_week=0,
            hours_used=0,
            skipped=restaurant_ids,
        )

    n = len(cands)
    value = np.array([p.jet_revenue_eur_per_week for _, p in cands])
    ads = np.array([max(p.ad_spend_eur_per_week, 0.0) for _, p in cands])
    hrs = np.array([len(p.chosen) * HOURS_PER_ACTION for _, p in cands])
    A, lb, ub = [], [], []
    for rid in restaurant_ids:
        A.append([1.0 if r == rid else 0.0 for r, _ in cands])
        lb.append(0)
        ub.append(1)
    A.append(list(ads))
    lb.append(-np.inf)
    ub.append(ad_budget_eur_per_week)
    A.append(list(hrs))
    lb.append(-np.inf)
    ub.append(hours)
    res = milp(c=-value, integrality=np.ones(n), bounds=Bounds(0, 1), constraints=LinearConstraint(np.array(A), lb, ub))
    x = np.round(res.x).astype(bool) if res.success and res.x is not None else np.zeros(n, dtype=bool)
    allocations = [
        Allocation(restaurant_id=r, plan=p, hours=len(p.chosen) * HOURS_PER_ACTION)
        for (r, p), pick in zip(cands, x, strict=True)
        if pick
    ]
    got = {a.restaurant_id for a in allocations}
    return Portfolio(
        restaurant_ids=restaurant_ids,
        ad_budget_eur_per_week=ad_budget_eur_per_week,
        hours=hours,
        allocations=allocations,
        jet_revenue_eur_per_week=round(float(value[x].sum()), 2),
        ad_spend_eur_per_week=round(float(ads[x].sum()), 2),
        hours_used=round(float(hrs[x].sum()), 2),
        skipped=[r for r in restaurant_ids if r not in got],
    )


def _finite(x: float) -> float:
    return x if math.isfinite(x) else 0.0
