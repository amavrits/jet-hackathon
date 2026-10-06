"""Estimate lever effects from the market panel with two-way fixed effects.

Organic levers (photo, description, price by price level, promo, rating) are fitted on
restaurant-weeks without ads by OLS on log orders, absorbing restaurant and city-by-week fixed
effects. That removes the planted confounding: big restaurants add photos more often, and
over-priced restaurants cut prices more often, so a pooled regression gets both wrong.

Ad yield (kappa * sqrt(budget) * (1 + lambda * offpeak_share)) is fitted by nonlinear least
squares on advertisers that also have ad-free weeks, using their own ad-free weeks to pin the
organic level. The basket model (price pass-through, promo discount) is a restaurant-FE
regression on log basket.

Uncertainty: cluster bootstrap by restaurant. The draws are kept so predictions can carry
intervals without refitting.

Output is a plain dict (see `fit`), cached as data/models/effects.json.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
from scipy.optimize import curve_fit

from growth.data import queries as q
from growth.ml import MODELS_DIR

ORGANIC_LEVERS = ["photo", "description", "elasticity_1", "elasticity_2", "elasticity_3", "promo", "rating"]
EVENT_FOR_LEVER = {
    "photo": "photos",
    "description": "descriptions",
    "elasticity_1": "price",
    "elasticity_2": "price",
    "elasticity_3": "price",
    "promo": "promo_start",
    "rating": None,
    "kappa": "ad_start",
    "offpeak_boost": "ad_daypart",
}
BOOTSTRAP_DRAWS = 100
DEMEAN_ITERS = 60
AD_OFFPEAK_BOOST_MAX = 3.0  # upper bound for lambda; the fit, not a prior, decides where it lands


# ------------------------------------------------------------------ fixed effects


def _group_mean(x: np.ndarray, g: np.ndarray, n_groups: int) -> np.ndarray:
    sums = np.bincount(g, weights=x, minlength=n_groups)
    counts = np.bincount(g, minlength=n_groups)
    return sums / np.maximum(counts, 1)


def _demean_two_way(x: np.ndarray, g1: np.ndarray, g2: np.ndarray, n1: int, n2: int) -> np.ndarray:
    """Remove additive group effects for two (unbalanced) groupings by alternating projections."""
    x = x.astype(float).copy()
    for _ in range(DEMEAN_ITERS):
        before = x.copy()
        x -= _group_mean(x, g1, n1)[g1]
        x -= _group_mean(x, g2, n2)[g2]
        if np.abs(x - before).max() < 1e-9:
            break
    return x


def _two_way_effects(r: np.ndarray, g1: np.ndarray, g2: np.ndarray, n1: int, n2: int) -> tuple[np.ndarray, np.ndarray]:
    """Recover the group effects a[g1] + b[g2] that best explain residual r."""
    a = np.zeros(n1)
    b = np.zeros(n2)
    for _ in range(DEMEAN_ITERS):
        a = _group_mean(r - b[g2], g1, n1)
        b = _group_mean(r - a[g1], g2, n2)
    return a, b


def _codes(values: list[Any]) -> tuple[np.ndarray, int]:
    uniq = {v: i for i, v in enumerate(dict.fromkeys(values))}
    return np.array([uniq[v] for v in values]), len(uniq)


# ------------------------------------------------------------------------ design


def _design(rows: list[dict[str, Any]]) -> np.ndarray:
    """Columns in ORGANIC_LEVERS order."""
    lvl = np.array([r["price_level"] for r in rows])
    ln_p = np.log([r["price_index"] for r in rows])
    return np.column_stack(
        [
            [r["photo_share"] for r in rows],
            [r["description_share"] for r in rows],
            ln_p * (lvl == 1),
            ln_p * (lvl == 2),
            ln_p * (lvl == 3),
            [float(r["promo_active"]) for r in rows],
            [r["rating"] for r in rows],
        ]
    )


def _fit_organic(rows: list[dict[str, Any]]) -> np.ndarray:
    y = np.log([r["orders"] for r in rows])
    X = _design(rows)
    g1, n1 = _codes([r["restaurant_id"] for r in rows])
    g2, n2 = _codes([(r["city"], r["week_start"]) for r in rows])
    yd = _demean_two_way(y, g1, g2, n1, n2)
    Xd = np.column_stack([_demean_two_way(X[:, j], g1, g2, n1, n2) for j in range(X.shape[1])])
    beta, *_ = np.linalg.lstsq(Xd, yd, rcond=None)
    return beta


def _fit_naive(rows: list[dict[str, Any]]) -> np.ndarray:
    """Pooled OLS with an intercept and no fixed effects. The wrong answer, kept for the pitch."""
    y = np.log([r["orders"] for r in rows])
    X = np.column_stack([np.ones(len(rows)), _design(rows)])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return beta[1:]


def _fit_basket(rows: list[dict[str, Any]]) -> np.ndarray:
    """log basket ~ ln(price_index) + promo, restaurant fixed effects. Returns (pass_through, promo)."""
    y = np.log([r["avg_basket_eur"] for r in rows])
    X = np.column_stack([np.log([r["price_index"] for r in rows]), [float(r["promo_active"]) for r in rows]])
    g, n = _codes([r["restaurant_id"] for r in rows])
    yd = y - _group_mean(y, g, n)[g]
    Xd = X - np.column_stack([_group_mean(X[:, j], g, n)[g] for j in range(2)])
    beta, *_ = np.linalg.lstsq(Xd, yd, rcond=None)
    return beta


def _ad_curve(xy: tuple[np.ndarray, np.ndarray], kappa: float, lam: float) -> np.ndarray:
    budget, offpeak = xy
    return kappa * np.sqrt(budget) * (1 + lam * offpeak)


def _fit_ads(
    rows: list[dict[str, Any]], beta: np.ndarray, organic_rows: list[dict[str, Any]]
) -> tuple[np.ndarray, int, int]:
    """(kappa, lambda), restaurants used, ad-weeks used.

    Organic level of each advertiser comes from its own ad-free weeks; city-week shocks from the
    organic fit. Excess orders in ad weeks are regressed on the ad curve.
    """
    yo = np.log([r["orders"] for r in organic_rows])
    g1, n1 = _codes([r["restaurant_id"] for r in organic_rows])
    g2_keys = [(r["city"], r["week_start"]) for r in organic_rows]
    g2, n2 = _codes(g2_keys)
    _, gamma = _two_way_effects(yo - _design(organic_rows) @ beta, g1, g2, n1, n2)
    shock = {k: gamma[i] for k, i in zip(g2_keys, g2, strict=True)}

    by_rest: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_rest.setdefault(r["restaurant_id"], []).append(r)
    budgets, offpeaks, excess = [], [], []
    n_rest = 0
    for rs in by_rest.values():
        free = [r for r in rs if r["ad_budget_eur"] == 0]
        paid = [r for r in rs if r["ad_budget_eur"] > 0]
        if len(free) < 2 or not paid:
            continue
        n_rest += 1
        base_free = np.exp(np.array([shock[(r["city"], r["week_start"])] for r in free]) + _design(free) @ beta)
        # level (not log) calibration of the restaurant's organic volume, so E[orders] is matched
        # and the ad excess is not inflated by the Jensen gap between mean log and log mean
        scale = np.mean([r["orders"] for r in free]) / base_free.mean()
        base_paid = scale * np.exp(np.array([shock[(r["city"], r["week_start"])] for r in paid]) + _design(paid) @ beta)
        for r, organic in zip(paid, base_paid, strict=True):
            budgets.append(r["ad_budget_eur"])
            offpeaks.append(r["ad_offpeak_share"])
            excess.append(r["orders"] - organic)
    if n_rest < 3:
        return np.array([np.nan, np.nan]), n_rest, len(excess)
    # lambda is weakly identified (few restaurants change their daypart); bounded, not regularised
    (kappa, lam), _ = curve_fit(
        _ad_curve,
        (np.array(budgets), np.array(offpeaks)),
        np.array(excess),
        p0=[1.0, 0.5],
        bounds=([0, 0], [10, AD_OFFPEAK_BOOST_MAX]),
    )
    return np.array([kappa, lam]), n_rest, len(excess)


# --------------------------------------------------------------------------- fit


def _interval(draws: np.ndarray) -> tuple[float, float]:
    return float(np.nanpercentile(draws, 5)), float(np.nanpercentile(draws, 95))


def fit(con: duckdb.DuckDBPyConnection, draws: int = BOOTSTRAP_DRAWS, seed: int = 0) -> dict[str, Any]:
    """Fit every effect on the observable panel. Returns the effects dict (see module docstring)."""
    rows = [r for r in q.panel(con) if r["orders"] > 0]
    organic = [r for r in rows if r["ad_budget_eur"] == 0]
    events = q.change_event_counts(con)

    beta = _fit_organic(organic)
    naive = _fit_naive(organic)
    basket = _fit_basket(rows)
    ads, ad_rest, ad_obs = _fit_ads(rows, beta, organic)

    rng = np.random.default_rng(seed)
    rest_ids = sorted({r["restaurant_id"] for r in rows})
    by_rest: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_rest.setdefault(r["restaurant_id"], []).append(r)
    boot_beta, boot_basket, boot_ads = [], [], []
    for _ in range(draws):
        sample = rng.choice(rest_ids, size=len(rest_ids), replace=True)
        # resampled restaurants get fresh ids so a restaurant drawn twice counts as two clusters
        srows = [{**r, "restaurant_id": f"{rid}#{i}"} for i, rid in enumerate(sample) for r in by_rest[rid]]
        sorg = [r for r in srows if r["ad_budget_eur"] == 0]
        b = _fit_organic(sorg)
        boot_beta.append(b)
        boot_basket.append(_fit_basket(srows))
        boot_ads.append(_fit_ads(srows, b, sorg)[0])
    boot_beta, boot_basket, boot_ads = np.array(boot_beta), np.array(boot_basket), np.array(boot_ads)

    def param(name: str, value: float, draws_: np.ndarray) -> dict[str, Any]:
        lo, hi = _interval(draws_)
        ev = EVENT_FOR_LEVER.get(name)
        return {
            "coef": round(float(value), 4),
            "lo": round(lo, 4),
            "hi": round(hi, 4),
            "n_events": events.get(ev, 0) if ev else None,
        }

    return {
        "method": "two-way fixed effects (restaurant + city x week) on log orders, ad-free weeks; "
        "ad yield by nonlinear least squares on advertisers' excess orders; cluster bootstrap by restaurant",
        "fitted_at": datetime.now().isoformat(timespec="seconds"),
        "n_obs": len(organic),
        "n_restaurants": len(rest_ids),
        "n_weeks": len({r["week_start"] for r in rows}),
        "bootstrap_draws": draws,
        "levers": {name: param(name, beta[j], boot_beta[:, j]) for j, name in enumerate(ORGANIC_LEVERS)},
        "naive": {name: round(float(naive[j]), 4) for j, name in enumerate(ORGANIC_LEVERS)},
        "ads": {
            "kappa": param("kappa", ads[0], boot_ads[:, 0]),
            "offpeak_boost": param("offpeak_boost", ads[1], boot_ads[:, 1]),
            "n_restaurants": ad_rest,
            "n_obs": ad_obs,
        },
        "basket": {
            "price_passthrough": param("price_passthrough", basket[0], boot_basket[:, 0]),
            "promo": param("promo_basket", basket[1], boot_basket[:, 1]),
        },
        "draws": {
            **{name: np.round(boot_beta[:, j], 5).tolist() for j, name in enumerate(ORGANIC_LEVERS)},
            "kappa": np.round(np.nan_to_num(boot_ads[:, 0], nan=float(ads[0])), 5).tolist(),
            "offpeak_boost": np.round(np.nan_to_num(boot_ads[:, 1], nan=float(ads[1])), 5).tolist(),
            "price_passthrough": np.round(boot_basket[:, 0], 5).tolist(),
            "promo_basket": np.round(boot_basket[:, 1], 5).tolist(),
        },
    }


def load(con: duckdb.DuckDBPyConnection, path: Path | str | None = None, refit: bool = False) -> dict[str, Any]:
    """Cached effects, fitting and caching them on first use."""
    path = Path(path) if path else MODELS_DIR / "effects.json"
    if path.exists() and not refit:
        return json.loads(path.read_text())
    effects = fit(con)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(effects, indent=1))
    return effects


def summary_table(effects: dict[str, Any]) -> list[dict[str, Any]]:
    """Flat rows for a UI table: lever, estimate, interval, naive estimate, evidence."""
    out = []
    for name, p in effects["levers"].items():
        out.append(
            {"lever": name, **{k: p[k] for k in ("coef", "lo", "hi", "n_events")}, "naive": effects["naive"][name]}
        )
    for name in ("kappa", "offpeak_boost"):
        out.append(
            {"lever": name, **{k: effects["ads"][name][k] for k in ("coef", "lo", "hi", "n_events")}, "naive": None}
        )
    for name, p in effects["basket"].items():
        out.append({"lever": name, **{k: p[k] for k in ("coef", "lo", "hi", "n_events")}, "naive": None})
    return out
