"""Small helpers shared by the specialist agents."""

from __future__ import annotations

import re
from typing import Any

import duckdb

from growth.data import queries as q

WEEKS = 8  # analysis window for sales, matches the seed
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def base_facts(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> dict[str, float]:
    """Facts every impact formula needs: basket size, volume, commission."""
    restaurant = q.get_restaurant(con, restaurant_id) or {}
    summary = q.weekly_summary(con, restaurant_id, weeks=WEEKS)
    return {
        "avg_basket_eur": round(summary["avg_basket_eur"] or 0.0, 2),
        "orders_per_week": round(summary["orders_per_week"] or 0.0, 1),
        "commission_rate": float(restaurant.get("commission_rate", 0.15)),
    }


def eur(x: float) -> str:
    return f"€{x:,.2f}"


def per_week(units: Any) -> float:
    return round(float(units or 0) / WEEKS, 1)
