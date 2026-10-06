"""Predict the weekly delta of a set of lever changes for one restaurant.

    organic_now  = orders_now - ads_now,  ads = kappa * sqrt(budget) * (1 + lambda * offpeak)
    organic_new  = organic_now * exp( sum_l beta_l * (new_l - old_l) )     price enters as ln ratio
    orders_new   = organic_new + ads_new
    basket_new   = basket_now * (price_new / price_old) ^ pass_through * exp(promo_b * d_promo)
    d_JET        = commission * d_GMV + d_ad_spend
    d_owner      = (1 - commission - FOOD_COST) * d_GMV - d_ad_spend

Intervals come from re-evaluating the same arithmetic on every bootstrap draw of the effects.
"""

from __future__ import annotations

import math
from typing import Any

import duckdb
import numpy as np
from pydantic import BaseModel, Field

from growth.data import queries as q
from growth.ml import effects as fx
from growth.state import Recommendation

LEVERS = [
    "photo_share",
    "description_share",
    "price_index",
    "promo_active",
    "rating",
    "ad_budget_eur",
    "ad_offpeak_share",
]
FOOD_COST = 0.30  # share of GMV the restaurant spends on ingredients and packaging
MAX_OFFPEAK_SHARE = 0.9  # highest value seen in the market history; stay inside support


class Band(BaseModel):
    point: float
    lo: float
    hi: float


class Prediction(BaseModel):
    restaurant_id: str
    changes: dict[str, dict[str, float]]  # lever -> {"from": x, "to": y}
    orders_per_week: Band
    gmv_eur_per_week: Band
    jet_revenue_eur_per_week: Band
    owner_profit_eur_per_week: Band
    ad_spend_eur_per_week: float  # delta
    formula: str
    evidence: dict[str, int | None] = Field(default_factory=dict)  # lever -> past change events
    method: str


def current_levers(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> dict[str, Any]:
    """Latest panel row: lever values, volume, basket, commission, price level."""
    row = q.latest_week(con, restaurant_id)
    if row is None:
        raise KeyError(f"unknown restaurant {restaurant_id}")
    return {
        **{k: float(row[k]) for k in LEVERS},
        "orders": float(row["orders"]),
        "avg_basket_eur": float(row["avg_basket_eur"]),
        "commission_rate": float(row["commission_rate"]),
        "price_level": int(row["price_level"]),
        "week_start": str(row["week_start"]),
    }


def _params(effects: dict[str, Any], draw: int | None) -> dict[str, float]:
    """One coherent parameter set: the point estimate, or bootstrap draw number `draw`."""
    if draw is None:
        p = {k: v["coef"] for k, v in effects["levers"].items()}
        p["kappa"] = effects["ads"]["kappa"]["coef"]
        p["offpeak_boost"] = effects["ads"]["offpeak_boost"]["coef"]
        p["price_passthrough"] = effects["basket"]["price_passthrough"]["coef"]
        p["promo_basket"] = effects["basket"]["promo"]["coef"]
        return p
    return {k: v[draw] for k, v in effects["draws"].items()}


def _evaluate(now: dict[str, Any], new: dict[str, float], p: dict[str, float]) -> tuple[float, float, float, float]:
    """(d_orders, d_gmv, d_jet, d_owner) per week under one parameter set."""
    ads_now = p["kappa"] * math.sqrt(now["ad_budget_eur"]) * (1 + p["offpeak_boost"] * now["ad_offpeak_share"])
    ads_new = p["kappa"] * math.sqrt(new["ad_budget_eur"]) * (1 + p["offpeak_boost"] * new["ad_offpeak_share"])
    organic_now = max(now["orders"] - ads_now, 1.0)
    d_log = (
        p["photo"] * (new["photo_share"] - now["photo_share"])
        + p["description"] * (new["description_share"] - now["description_share"])
        + p[f"elasticity_{now['price_level']}"] * math.log(new["price_index"] / now["price_index"])
        + p["promo"] * (new["promo_active"] - now["promo_active"])
        + p["rating"] * (new["rating"] - now["rating"])
    )
    orders_new = organic_now * math.exp(d_log) + ads_new
    basket_new = (
        now["avg_basket_eur"]
        * (new["price_index"] / now["price_index"]) ** p["price_passthrough"]
        * math.exp(p["promo_basket"] * (new["promo_active"] - now["promo_active"]))
    )
    d_orders = orders_new - now["orders"]
    d_gmv = orders_new * basket_new - now["orders"] * now["avg_basket_eur"]
    d_ad = new["ad_budget_eur"] - now["ad_budget_eur"]
    c = now["commission_rate"]
    return d_orders, d_gmv, c * d_gmv + d_ad, (1 - c - FOOD_COST) * d_gmv - d_ad


def _band(point: float, draws: np.ndarray, robust: bool) -> Band:
    lo, hi = float(np.percentile(draws, 5)), float(np.percentile(draws, 95))
    return Band(point=round(lo if robust else point, 2), lo=round(lo, 2), hi=round(hi, 2))


def _formula(now: dict[str, Any], new: dict[str, float], p: dict[str, float], effects: dict[str, Any]) -> str:
    parts = []
    for lever, beta in (
        ("photo_share", "photo"),
        ("description_share", "description"),
        ("promo_active", "promo"),
        ("rating", "rating"),
    ):
        if new[lever] != now[lever]:
            e = effects["levers"][beta]
            parts.append(
                f"{lever} {now[lever]:.2f}->{new[lever]:.2f} x {e['coef']:+.3f} (90%: {e['lo']:+.3f} to {e['hi']:+.3f})"
            )
    if new["price_index"] != now["price_index"]:
        e = effects["levers"][f"elasticity_{now['price_level']}"]
        parts.append(
            f"ln(price {now['price_index']:.3f}->{new['price_index']:.3f}) x elasticity {e['coef']:+.2f} "
            f"(90%: {e['lo']:+.2f} to {e['hi']:+.2f}, price level {now['price_level']})"
        )
    if new["ad_budget_eur"] != now["ad_budget_eur"] or new["ad_offpeak_share"] != now["ad_offpeak_share"]:
        k, lam = effects["ads"]["kappa"], effects["ads"]["offpeak_boost"]
        parts.append(
            f"ads {k['coef']:.2f} x sqrt(budget {now['ad_budget_eur']:.0f}->{new['ad_budget_eur']:.0f}) x "
            f"(1 + {lam['coef']:.2f} x off-peak {now['ad_offpeak_share']:.2f}->{new['ad_offpeak_share']:.2f})"
        )
    return "; ".join(parts) if parts else "no lever changed"


def predict_impact(
    con: duckdb.DuckDBPyConnection,
    restaurant_id: str,
    changes: dict[str, float],
    *,
    effects: dict[str, Any] | None = None,
    robust: bool = False,
) -> Prediction:
    """changes: lever -> new absolute value. Levers not listed stay as they are."""
    bad = set(changes) - set(LEVERS)
    if bad:
        raise ValueError(f"unknown levers {sorted(bad)}; valid: {LEVERS}")
    effects = effects or fx.load(con)
    now = current_levers(con, restaurant_id)
    new = {k: now[k] for k in LEVERS}
    for k, v in changes.items():
        new[k] = float(np.clip(v, 0.0, MAX_OFFPEAK_SHARE)) if k == "ad_offpeak_share" else float(v)
    if new["price_index"] <= 0:
        raise ValueError("price_index must be positive")

    point = _evaluate(now, new, _params(effects, None))
    draws = np.array([_evaluate(now, new, _params(effects, d)) for d in range(effects["bootstrap_draws"])])
    evidence = {}
    for lever, key in (
        ("photo_share", "photo"),
        ("description_share", "description"),
        ("promo_active", "promo"),
        ("price_index", f"elasticity_{now['price_level']}"),
    ):
        if new[lever] != now[lever]:
            evidence[lever] = effects["levers"][key]["n_events"]
    if new["ad_budget_eur"] != now["ad_budget_eur"]:
        evidence["ad_budget_eur"] = effects["ads"]["kappa"]["n_events"]
    if new["ad_offpeak_share"] != now["ad_offpeak_share"]:
        evidence["ad_offpeak_share"] = effects["ads"]["offpeak_boost"]["n_events"]

    return Prediction(
        restaurant_id=restaurant_id,
        changes={k: {"from": round(now[k], 4), "to": round(new[k], 4)} for k in LEVERS if new[k] != now[k]},
        orders_per_week=_band(point[0], draws[:, 0], robust),
        gmv_eur_per_week=_band(point[1], draws[:, 1], robust),
        jet_revenue_eur_per_week=_band(point[2], draws[:, 2], robust),
        owner_profit_eur_per_week=_band(point[3], draws[:, 3], robust),
        ad_spend_eur_per_week=round(new["ad_budget_eur"] - now["ad_budget_eur"], 2),
        formula=_formula(now, new, _params(effects, None), effects),
        evidence=evidence,
        method=effects["method"],
    )


# ----------------------------------------------- recommendations -> lever changes


def changes_for_recommendation(rec: Recommendation) -> dict[str, float] | None:
    """The lever values a recommendation moves to, from its `lever_changes` (the contract the
    agents fill in, see growth.state.LeverChange). None when the recommendation has no lever in
    the demand model (dish and delivery complaints, pruning), so the heuristic stays in charge."""
    if not rec.lever_changes:
        return None
    return {lc.lever: float(lc.after) for lc in rec.lever_changes}
