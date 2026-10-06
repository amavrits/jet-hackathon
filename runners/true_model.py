"""The TRUE data-generating model for the market. Ground truth for impact estimation.

Nothing under growth/ may import this module or read data/true_model.json. The estimation code
has to recover these effects from the observable tables (restaurant_weeks, change_events,
restaurants) the way it would on real JET data. Tests compare estimates against TRUTH.

Weekly orders of restaurant r in week t:

    base_rt   = exp( alpha_r                                   restaurant size / quality (latent)
                   + season_t + city_shock_{c(r),t}             common shocks
                   + PHOTO        * photo_share_rt
                   + DESCRIPTION  * description_share_rt
                   + ELASTICITY[price_level_r] * ln(price_index_rt)
                   + PROMO        * promo_active_rt
                   + RATING       * (rating_rt - 4.3) )
    ads_rt    = AD_KAPPA * sqrt(ad_budget_rt) * (1 + AD_OFFPEAK_BOOST * ad_offpeak_share_rt)
    orders_rt ~ Poisson( Gamma(shape=DISPERSION, mean=base_rt + ads_rt) )     # negative binomial

    basket_rt = BASKET[price_level_r] * price_index_rt * (1 - PROMO_DISCOUNT * PROMO_ORDER_SHARE * promo_rt) * noise
    gmv_rt    = orders_rt * basket_rt

Treatments are NOT randomly assigned, on purpose, so naive comparisons are biased:
  - bigger restaurants (high alpha) are more likely to add photos/descriptions and to advertise;
  - restaurants priced above their market are more likely to cut prices.
Two-way fixed effects / difference-in-differences with peer groups recover the truth; a
cross-sectional "restaurants with photos sell more" regression does not.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np

from growth.data.seed import END_DATE, MENUS, RESTAURANTS, haversine_km


@dataclass(frozen=True)
class Truth:
    photo: float = 0.12  # log-orders, full photo coverage vs none  (~ +12.7%)
    description: float = 0.04  # log-orders, full description coverage   (~ +4.1%)
    elasticity: dict[int, float] = field(default_factory=lambda: {1: -2.0, 2: -1.5, 3: -0.9})
    promo: float = 0.08  # log-orders while a promo runs            (~ +8.3%)
    promo_discount: float = 0.15
    promo_order_share: float = 0.30  # share of orders that redeem the promo
    rating: float = 0.25  # log-orders per rating point
    ad_kappa: float = 1.1  # incremental orders/week per sqrt(EUR budget/week)
    ad_offpeak_boost: float = 0.8  # off-peak scheduling multiplies ad yield by up to 1.8
    dispersion: float = 40.0  # NB shape; higher = closer to Poisson
    basket: dict[int, float] = field(default_factory=lambda: {1: 18.0, 2: 24.0, 3: 34.0})


TRUTH = Truth()

SEED = 2026
N_WEEKS = 26
N_MARKET = 220  # market restaurants besides the forced near-partner competitors
FIRST_WEEK = END_DATE - timedelta(days=N_WEEKS * 7 - 1)  # last 8 weeks align with partner orders
WEEKS: list[date] = [FIRST_WEEK + timedelta(days=7 * k) for k in range(N_WEEKS)]

CITIES: dict[str, tuple[float, float, float, str]] = {
    # name: (lat, lon, share of market, postcode prefix)
    "Amsterdam": (52.370, 4.895, 0.40, "10"),
    "Rotterdam": (51.922, 4.479, 0.25, "30"),
    "Utrecht": (52.090, 5.121, 0.18, "35"),
    "The Hague": (52.078, 4.300, 0.17, "25"),
}
CUISINES = list(MENUS)
NAME_A = ["Royal", "Little", "Golden", "Urban", "Mama's", "Express", "House of", "The Real", "Casa", "Lucky"]
NAME_B = ["Kitchen", "Corner", "Garden", "Bar", "Spot", "Table", "Street", "Club"]


def season(t: int) -> float:
    """Common weekly demand shock: mild upward trend, summer dip in July/August."""
    d = WEEKS[t]
    summer = -0.08 if d.month in (7, 8) else 0.0
    return 0.002 * t + summer + 0.03 * math.sin(2 * math.pi * t / 13)


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


# --------------------------------------------------------------------------- market


def _offset(rng: np.random.Generator, lat: float, lon: float, km_lo: float, km_hi: float) -> tuple[float, float]:
    dist, bearing = rng.uniform(km_lo, km_hi), rng.uniform(0, 2 * math.pi)
    dlat = dist / 111.0 * math.cos(bearing)
    dlon = dist / (111.0 * math.cos(math.radians(lat))) * math.sin(bearing)
    return round(lat + dlat, 5), round(lon + dlon, 5)


def _restaurant(rng: np.random.Generator, rid: str, cuisine: str, city: str, lat: float, lon: float, level: int):
    return {
        "restaurant_id": rid,
        "name": f"{rng.choice(NAME_A)} {cuisine} {rng.choice(NAME_B)}",
        "cuisine": cuisine,
        "city": city,
        "postcode": f"{CITIES[city][3]}{rng.integers(10, 99)}",
        "lat": lat,
        "lon": lon,
        "price_level": level,
        "rating": round(float(np.clip(rng.normal(4.3, 0.2), 3.5, 4.9)), 1),
        "delivery_model": "jet_delivery" if rng.random() < 0.4 else "marketplace",
        "joined_at": date(int(rng.integers(2019, 2026)), int(rng.integers(1, 13)), int(rng.integers(1, 29))),
        "is_partner": False,
        "alpha": float(rng.normal(math.log(140), 0.45)),
    }


def make_market(rng: np.random.Generator) -> list[dict[str, Any]]:
    """Market restaurants (not partners). Each partner gets 5 same-cuisine neighbours within 0.3-2.5 km."""
    market: list[dict[str, Any]] = []
    for p in RESTAURANTS:
        for k in range(5):
            lat, lon = _offset(rng, p["lat"], p["lon"], 0.3, 2.5)
            r = _restaurant(rng, f"m_{p['restaurant_id'][2:]}_{k}", p["cuisine"], p["city"], lat, lon, p["price_level"])
            market.append(r)
    names, shares = list(CITIES), [c[2] for c in CITIES.values()]
    for n in range(N_MARKET):
        city = str(rng.choice(names, p=shares))
        clat, clon = CITIES[city][:2]
        lat, lon = _offset(rng, clat, clon, 0.0, 4.0)
        level = int(rng.choice([1, 2, 3], p=[0.35, 0.45, 0.20]))
        market.append(_restaurant(rng, f"m_{n:03d}", str(rng.choice(CUISINES)), city, lat, lon, level))
    return market


# ------------------------------------------------------------------------- treatments


def plan_levers(rng: np.random.Generator, r: dict[str, Any], alpha_mean: float) -> dict[str, np.ndarray]:
    """Week-by-week lever values for one market restaurant, with confounded adoption."""
    T = N_WEEKS
    size = (r["alpha"] - alpha_mean) / 0.45  # standardised size

    def coverage(p_low: float, adopt_base: float) -> np.ndarray:
        if rng.random() >= p_low:
            return np.full(T, rng.uniform(0.75, 1.0))
        x = np.full(T, rng.uniform(0.0, 0.35))
        if rng.random() < adopt_base * _sigmoid(1.2 * size) * 2:  # big restaurants adopt more
            x[int(rng.integers(5, 22)) :] = rng.uniform(0.85, 1.0)
        return x

    photo = coverage(0.35, 0.5)
    desc = coverage(0.30, 0.5)

    price = np.full(T, float(np.exp(rng.normal(0.0, 0.08))))
    p_change = 0.15 + (0.35 if price[0] > 1.05 else 0.0)  # pricey restaurants cut more often
    if rng.random() < p_change:
        step = rng.normal(-0.08, 0.05) if rng.random() < 0.8 else rng.normal(0.06, 0.03)
        price[int(rng.integers(5, 22)) :] = price[0] * math.exp(step)

    promo = np.zeros(T, dtype=bool)
    if rng.random() < 0.25:
        s = int(rng.integers(3, 20))
        promo[s : s + int(rng.integers(3, 9))] = True

    budget, offpeak = np.zeros(T), np.full(T, rng.uniform(0.2, 0.4))
    if rng.random() < 0.6 * _sigmoid(size):  # ~30% advertise, big ones more often
        start = 0 if rng.random() < 0.6 else int(rng.integers(3, 20))
        budget[start:] = round(float(rng.uniform(40, 250)), 0)
        if rng.random() < 0.2:
            budget[int(rng.integers(start + 2, 24)) :] *= rng.uniform(0.5, 1.8)
        if rng.random() < 0.25:
            offpeak[int(rng.integers(start + 2, 24)) :] = rng.uniform(0.7, 0.9)

    rating = np.clip(r["rating"] + np.cumsum(rng.normal(0, 0.01, T)), 3.5, 4.95)
    return {
        "photo_share": photo,
        "description_share": desc,
        "price_index": price,
        "promo_active": promo,
        "ad_budget_eur": np.round(budget, 0),
        "ad_offpeak_share": offpeak,
        "rating": rating,
    }


def constant_levers(state: dict[str, float]) -> dict[str, np.ndarray]:
    """Partners: their levers don't change over the history (the problems are current)."""
    return {k: np.full(N_WEEKS, v) for k, v in state.items()}


# ----------------------------------------------------------------------------- outcome


def expected_orders(level: int, alpha: float, shock: float, lv: dict[str, float]) -> tuple[float, float]:
    """(organic, ad-driven) expected weekly orders for one restaurant-week under TRUTH."""
    tr = TRUTH
    log_base = (
        alpha
        + shock
        + tr.photo * lv["photo_share"]
        + tr.description * lv["description_share"]
        + tr.elasticity[level] * math.log(lv["price_index"])
        + tr.promo * float(lv["promo_active"])
        + tr.rating * (lv["rating"] - 4.3)
    )
    ads = tr.ad_kappa * math.sqrt(lv["ad_budget_eur"]) * (1 + tr.ad_offpeak_boost * lv["ad_offpeak_share"])
    return math.exp(log_base), ads


def city_shocks(rng: np.random.Generator) -> dict[str, np.ndarray]:
    out = {}
    for city in CITIES:
        x = np.zeros(N_WEEKS)
        for t in range(1, N_WEEKS):
            x[t] = 0.6 * x[t - 1] + rng.normal(0, 0.03)
        out[city] = np.array([season(t) for t in range(N_WEEKS)]) + x
    return out


def simulate_weeks(
    rng: np.random.Generator, r: dict[str, Any], levers: dict[str, np.ndarray], shocks: dict[str, np.ndarray]
) -> list[dict[str, Any]]:
    tr = TRUTH
    rows = []
    for t, week in enumerate(WEEKS):
        lv = {k: float(v[t]) for k, v in levers.items()}
        organic, ads = expected_orders(r["price_level"], r["alpha"], shocks[r["city"]][t], lv)
        mu = organic + ads
        orders = int(rng.poisson(rng.gamma(tr.dispersion, mu / tr.dispersion)))
        basket = (
            tr.basket[r["price_level"]]
            * lv["price_index"]
            * (1 - tr.promo_discount * tr.promo_order_share * lv["promo_active"])
            * math.exp(rng.normal(0, 0.03))
        )
        rows.append(
            {
                "restaurant_id": r["restaurant_id"],
                "week_start": week,
                "orders": orders,
                "gmv_eur": round(orders * basket, 2),
                "avg_basket_eur": round(basket, 2),
                "photo_share": round(lv["photo_share"], 3),
                "description_share": round(lv["description_share"], 3),
                "price_index": round(lv["price_index"], 4),
                "promo_active": bool(lv["promo_active"]),
                "ad_budget_eur": lv["ad_budget_eur"],
                "ad_offpeak_share": round(lv["ad_offpeak_share"], 3),
                "rating": round(lv["rating"], 2),
            }
        )
    return rows


def change_events(rid: str, levers: dict[str, np.ndarray]) -> list[dict[str, Any]]:
    """Derive the observable change log from the lever paths."""
    events = []

    def add(t: int, kind: str, before: float | None, after: float | None) -> None:
        events.append(
            {
                "event_id": f"{rid}_e{len(events):02d}",
                "restaurant_id": rid,
                "week_start": WEEKS[t],
                "change_type": kind,
                "before_value": None if before is None else round(float(before), 4),
                "after_value": None if after is None else round(float(after), 4),
            }
        )

    for t in range(1, N_WEEKS):
        prev = {k: v[t - 1] for k, v in levers.items()}
        cur = {k: v[t] for k, v in levers.items()}
        if abs(cur["photo_share"] - prev["photo_share"]) > 0.2:
            add(t, "photos", prev["photo_share"], cur["photo_share"])
        if abs(cur["description_share"] - prev["description_share"]) > 0.2:
            add(t, "descriptions", prev["description_share"], cur["description_share"])
        if abs(cur["price_index"] - prev["price_index"]) > 1e-6:
            add(t, "price", prev["price_index"], cur["price_index"])
        if cur["promo_active"] and not prev["promo_active"]:
            add(t, "promo_start", None, None)
        if prev["promo_active"] and not cur["promo_active"]:
            add(t, "promo_end", None, None)
        if prev["ad_budget_eur"] == 0 and cur["ad_budget_eur"] > 0:
            add(t, "ad_start", 0, cur["ad_budget_eur"])
        elif prev["ad_budget_eur"] > 0 and cur["ad_budget_eur"] == 0:
            add(t, "ad_stop", prev["ad_budget_eur"], 0)
        elif abs(cur["ad_budget_eur"] - prev["ad_budget_eur"]) > 0.5:
            add(t, "ad_budget", prev["ad_budget_eur"], cur["ad_budget_eur"])
        if abs(cur["ad_offpeak_share"] - prev["ad_offpeak_share"]) > 0.2:
            add(t, "ad_daypart", prev["ad_offpeak_share"], cur["ad_offpeak_share"])
    return events


def calibrate_alpha(target_orders_pw: float, level: int, city: str, shocks: dict[str, np.ndarray], lv: dict) -> float:
    """Alpha for a partner so that its expected orders over the last 8 weeks match its item-level volume."""
    organic_mult = np.mean([expected_orders(level, 0.0, shocks[city][t], lv)[0] for t in range(N_WEEKS - 8, N_WEEKS)])
    ads = expected_orders(level, 0.0, 0.0, lv)[1]
    return math.log(max(1.0, target_orders_pw - ads) / organic_mult)


def truth_json() -> dict[str, Any]:
    d = asdict(TRUTH)
    d["elasticity"] = {str(k): v for k, v in d["elasticity"].items()}
    d["basket"] = {str(k): v for k, v in d["basket"].items()}
    d["percent_effects"] = {
        "photo_full_coverage": round(math.exp(TRUTH.photo) - 1, 4),
        "description_full_coverage": round(math.exp(TRUTH.description) - 1, 4),
        "promo": round(math.exp(TRUTH.promo) - 1, 4),
    }
    d["seed"] = SEED
    d["weeks"] = [w.isoformat() for w in (WEEKS[0], WEEKS[-1])]
    return d


__all__ = [
    "TRUTH",
    "WEEKS",
    "make_market",
    "plan_levers",
    "constant_levers",
    "simulate_weeks",
    "change_events",
    "city_shocks",
    "calibrate_alpha",
    "truth_json",
    "haversine_km",
]
