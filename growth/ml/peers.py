"""Who does a restaurant compete with, and where does it sit in the market?

get_peers      k nearest neighbours by a weighted distance over proximity, cuisine, price level,
               size and rating. "Who competes with me" depends on the restaurant, so nearest
               neighbours fit better than fixed clusters.
benchmark      the restaurant's lever levels against its peers' medians.
segment_market k-means over the whole market on location, price level, size, rating and listing
               quality. Gives JET a map of segments for the portfolio view; cuisine is reported
               per segment rather than used as a feature, so segments cut across cuisines.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Any

import duckdb
import numpy as np

from growth.data import queries as q

WEIGHTS = {"distance": 1.0, "cuisine": 1.5, "price_level": 0.5, "size": 0.5, "rating": 0.3}
DISTANCE_SCALE_KM = 2.5  # one unit of distance penalty
LEVERS = ["photo_share", "description_share", "price_index", "ad_budget_eur", "rating", "orders_per_week"]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _features(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    return q.restaurant_features(con)


def similarity_components(a: dict[str, Any], b: dict[str, Any]) -> dict[str, float]:
    """Each term of the weighted distance, so the UI can show why someone is a peer."""
    km = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
    return {
        "distance_km": round(km, 2),
        "distance": min(km / DISTANCE_SCALE_KM, 3.0),
        "cuisine": 0.0 if a["cuisine"] == b["cuisine"] else 1.0,
        "price_level": abs(a["price_level"] - b["price_level"]),
        "size": abs(math.log(max(a["orders_per_week"], 1)) - math.log(max(b["orders_per_week"], 1))),
        "rating": abs(a["rating"] - b["rating"]),
    }


def score(components: dict[str, float], weights: dict[str, float] = WEIGHTS) -> float:
    return sum(weights[k] * components[k] for k in weights)


def get_peers(
    con: duckdb.DuckDBPyConnection,
    restaurant_id: str,
    k: int = 10,
    *,
    same_city: bool = True,
    weights: dict[str, float] = WEIGHTS,
) -> list[dict[str, Any]]:
    """The k most similar restaurants, closest first, with the distance breakdown."""
    feats = {f["restaurant_id"]: f for f in _features(con)}
    me = feats.get(restaurant_id)
    if me is None:
        raise KeyError(f"unknown restaurant {restaurant_id}")
    out = []
    for rid, f in feats.items():
        if rid == restaurant_id or (same_city and f["city"] != me["city"]):
            continue
        comp = similarity_components(me, f)
        out.append(
            {
                "restaurant_id": rid,
                "name": f["name"],
                "cuisine": f["cuisine"],
                "price_level": f["price_level"],
                "rating": f["rating"],
                "distance_km": comp["distance_km"],
                "orders_per_week": round(float(f["orders_per_week"]), 1),
                "photo_share": round(float(f["photo_share"]), 2),
                "description_share": round(float(f["description_share"]), 2),
                "price_index": round(float(f["price_index"]), 3),
                "advertiser": bool(f["advertiser"]),
                "score": round(score(comp, weights), 3),
                "components": {k: round(v, 3) for k, v in comp.items() if k != "distance_km"},
            }
        )
    return sorted(out, key=lambda p: p["score"])[:k]


def benchmark(con: duckdb.DuckDBPyConnection, restaurant_id: str, k: int = 10) -> dict[str, Any]:
    """Own lever levels vs the peer median. Positive gap = peers are ahead."""
    feats = {f["restaurant_id"]: f for f in _features(con)}
    me = feats[restaurant_id]
    peers = get_peers(con, restaurant_id, k)
    rows = []
    for lever in LEVERS:
        mine = float(me[lever])
        peer = float(median(float(feats[p["restaurant_id"]][lever]) for p in peers))
        rows.append(
            {"lever": lever, "mine": round(mine, 3), "peer_median": round(peer, 3), "gap": round(peer - mine, 3)}
        )
    return {
        "restaurant_id": restaurant_id,
        "k": len(peers),
        "peer_ids": [p["restaurant_id"] for p in peers],
        "levers": rows,
    }


# ------------------------------------------------------------------ segments (k-means)


def _kmeans(X: np.ndarray, k: int, seed: int, iters: int = 50) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    centres = X[rng.choice(len(X), 1)]
    for _ in range(1, k):  # k-means++ seeding
        d2 = ((X[:, None, :] - centres[None, :, :]) ** 2).sum(-1).min(1)
        centres = np.vstack([centres, X[rng.choice(len(X), p=d2 / d2.sum())]])
    labels = np.zeros(len(X), dtype=int)
    for _ in range(iters):
        new = ((X[:, None, :] - centres[None, :, :]) ** 2).sum(-1).argmin(1)
        if (new == labels).all() and _ > 0:
            break
        labels = new
        for j in range(k):
            if (labels == j).any():
                centres[j] = X[labels == j].mean(0)
    return labels, centres


SEGMENT_FEATURES = [
    "lat",
    "lon_scaled",
    "price_level",
    "log_orders",
    "rating",
    "photo_share",
    "description_share",
    "advertiser",
]


def segment_market(con: duckdb.DuckDBPyConnection, k: int = 6, seed: int = 0) -> dict[str, Any]:
    """Cluster the whole market. Returns segments with a profile and every restaurant's label."""
    feats = _features(con)
    lat0 = float(np.mean([f["lat"] for f in feats]))
    X = np.array(
        [
            [
                f["lat"],
                f["lon"] * math.cos(math.radians(lat0)),
                f["price_level"],
                math.log(max(float(f["orders_per_week"]), 1)),
                f["rating"],
                float(f["photo_share"]),
                float(f["description_share"]),
                float(bool(f["advertiser"])),
            ]
            for f in feats
        ]
    )
    Z = (X - X.mean(0)) / np.where(X.std(0) > 0, X.std(0), 1)
    labels, _ = _kmeans(Z, k, seed)
    segments = []
    for j in range(k):
        members = [f for f, lab in zip(feats, labels, strict=True) if lab == j]
        if not members:
            continue
        cuisines: dict[str, int] = {}
        cities: dict[str, int] = {}
        for m in members:
            cuisines[m["cuisine"]] = cuisines.get(m["cuisine"], 0) + 1
            cities[m["city"]] = cities.get(m["city"], 0) + 1
        segments.append(
            {
                "segment": j,
                "n": len(members),
                "partners": [m["restaurant_id"] for m in members if m["is_partner"]],
                "cities": dict(sorted(cities.items(), key=lambda kv: -kv[1])),
                "top_cuisines": dict(sorted(cuisines.items(), key=lambda kv: -kv[1])[:3]),
                "profile": {
                    "price_level": round(float(np.mean([m["price_level"] for m in members])), 2),
                    "orders_per_week": round(float(median(float(m["orders_per_week"]) for m in members)), 1),
                    "rating": round(float(np.mean([m["rating"] for m in members])), 2),
                    "photo_share": round(float(np.mean([m["photo_share"] for m in members])), 2),
                    "description_share": round(float(np.mean([m["description_share"] for m in members])), 2),
                    "advertisers": round(float(np.mean([bool(m["advertiser"]) for m in members])), 2),
                },
            }
        )
    return {
        "k": k,
        "features": SEGMENT_FEATURES,
        "segments": segments,
        "labels": {f["restaurant_id"]: int(lab) for f, lab in zip(feats, labels, strict=True)},
    }
