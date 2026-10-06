"""All SQL lives here. Agents and UI call these functions; they never write SQL inline."""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "jet.duckdb"

SLOT_SQL = """
    CASE
        WHEN hour(ts) BETWEEN 11 AND 13 THEN 'lunch 11-14'
        WHEN hour(ts) BETWEEN 14 AND 16 THEN 'afternoon 14-17'
        WHEN hour(ts) BETWEEN 17 AND 20 THEN 'dinner 17-21'
        ELSE 'late 21-23'
    END
"""

SCHEMA = """
CREATE TABLE restaurants (
    id VARCHAR PRIMARY KEY, name VARCHAR, cuisine VARCHAR, city VARCHAR,
    delivery_model VARCHAR, commission_rate DOUBLE
);
CREATE TABLE menu_items (
    id INTEGER PRIMARY KEY, restaurant_id VARCHAR, name VARCHAR, category VARCHAR,
    price_eur DOUBLE, description VARCHAR, photo_url VARCHAR
);
CREATE TABLE orders (
    order_id INTEGER, line INTEGER, restaurant_id VARCHAR, item_id INTEGER,
    qty INTEGER, unit_price_eur DOUBLE, ts TIMESTAMP
);
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY, restaurant_id VARCHAR, item_id INTEGER, rating INTEGER,
    text VARCHAR, ts TIMESTAMP
);
CREATE TABLE competitors (
    id INTEGER PRIMARY KEY, restaurant_id VARCHAR, competitor_name VARCHAR,
    category VARCHAR, item_name VARCHAR, price_eur DOUBLE
);
CREATE TABLE ad_campaigns (
    id INTEGER PRIMARY KEY, restaurant_id VARCHAR, start_date DATE, end_date DATE,
    weekly_budget_eur DOUBLE, impressions INTEGER, attributed_orders INTEGER, active BOOLEAN
);
CREATE TABLE listings (
    restaurant_id VARCHAR PRIMARY KEY, listing VARCHAR, updated_at TIMESTAMP
);
CREATE SEQUENCE change_log_seq START 1;
CREATE TABLE change_log (
    id INTEGER PRIMARY KEY DEFAULT nextval('change_log_seq'), restaurant_id VARCHAR,
    recommendation_id VARCHAR, patch VARCHAR, before VARCHAR, after VARCHAR,
    approver VARCHAR, applied_at TIMESTAMP DEFAULT current_timestamp, reverted_at TIMESTAMP
);
"""


def db_path() -> Path:
    return Path(os.environ.get("JET_DB_PATH", DEFAULT_DB_PATH))


@contextmanager
def connect(
    path: Path | None = None, read_only: bool = False
) -> Iterator[duckdb.DuckDBPyConnection]:
    con = duckdb.connect(str(path or db_path()), read_only=read_only)
    try:
        yield con
    finally:
        con.close()


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


# --- schema and seeding -------------------------------------------------------


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA)


def insert_frame(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> None:
    con.register("_frame", df)
    cols = ", ".join(df.columns)
    con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM _frame")
    con.unregister("_frame")


# --- reads ---------------------------------------------------------------------


def list_restaurants(con: duckdb.DuckDBPyConnection) -> list[dict]:
    return _rows(con, "SELECT * FROM restaurants ORDER BY name")


def get_restaurant(con: duckdb.DuckDBPyConnection, rid: str) -> dict:
    rows = _rows(con, "SELECT * FROM restaurants WHERE id = ?", [rid])
    if not rows:
        raise KeyError(f"unknown restaurant {rid}")
    return rows[0]


def weeks_of_data(con: duckdb.DuckDBPyConnection) -> float:
    row = con.execute(
        "SELECT (date_diff('day', min(ts)::DATE, max(ts)::DATE) + 1) / 7.0 FROM orders"
    ).fetchone()
    return float(row[0] or 1.0)


def kpis(con: duckdb.DuckDBPyConnection, rid: str) -> dict:
    weeks = weeks_of_data(con)
    orders = _rows(
        con,
        """
        SELECT count(DISTINCT order_id) / ? AS orders_per_week,
               sum(qty * unit_price_eur) / ? AS gmv_eur_per_week
        FROM orders WHERE restaurant_id = ?
        """,
        [weeks, weeks, rid],
    )[0]
    reviews = _rows(
        con,
        "SELECT avg(rating) AS avg_rating, count(*) AS review_count "
        "FROM reviews WHERE restaurant_id = ?",
        [rid],
    )[0]
    opw = orders["orders_per_week"] or 0.0
    gmv = orders["gmv_eur_per_week"] or 0.0
    return {
        "orders_per_week": opw,
        "gmv_eur_per_week": gmv,
        "avg_basket_eur": gmv / opw if opw else 0.0,
        "avg_rating": reviews["avg_rating"] or 0.0,
        "review_count": reviews["review_count"],
    }


def peer_median_orders_per_week(con: duckdb.DuckDBPyConnection) -> float:
    weeks = weeks_of_data(con)
    row = con.execute(
        """
        SELECT median(n) / ? FROM (
            SELECT count(DISTINCT order_id) AS n FROM orders GROUP BY restaurant_id
        )
        """,
        [weeks],
    ).fetchone()
    return float(row[0] or 0.0)


def item_order_stats(con: duckdb.DuckDBPyConnection, rid: str) -> list[dict]:
    weeks = weeks_of_data(con)
    return _rows(
        con,
        """
        SELECT m.id AS item_id,
               coalesce(sum(o.qty), 0) / ? AS orders_per_week,
               coalesce(sum(o.qty * o.unit_price_eur), 0) / ? AS gmv_eur_per_week
        FROM menu_items m
        LEFT JOIN orders o ON o.item_id = m.id
        WHERE m.restaurant_id = ?
        GROUP BY m.id
        """,
        [weeks, weeks, rid],
    )


def reviews(con: duckdb.DuckDBPyConnection, rid: str) -> list[dict]:
    return _rows(
        con,
        "SELECT id, rating, text, item_id, strftime(ts, '%Y-%m-%d') AS ts "
        "FROM reviews WHERE restaurant_id = ? ORDER BY ts",
        [rid],
    )


def slot_stats(con: duckdb.DuckDBPyConnection, rid: str) -> list[dict]:
    weeks = weeks_of_data(con)
    return _rows(
        con,
        f"""
        SELECT isodow(ts) - 1 AS weekday, {SLOT_SQL} AS slot,
               count(DISTINCT order_id) / ? AS orders_per_week
        FROM orders WHERE restaurant_id = ?
        GROUP BY ALL ORDER BY weekday, slot
        """,
        [weeks, rid],
    )


def orders_by_weekday(con: duckdb.DuckDBPyConnection, rid: str) -> list[dict]:
    weeks = weeks_of_data(con)
    return _rows(
        con,
        """
        SELECT isodow(ts) - 1 AS weekday, count(DISTINCT order_id) / ? AS orders_per_week
        FROM orders WHERE restaurant_id = ? GROUP BY ALL ORDER BY weekday
        """,
        [weeks, rid],
    )


def competitor_prices(con: duckdb.DuckDBPyConnection, rid: str) -> list[dict]:
    return _rows(
        con,
        "SELECT id, competitor_name, category, item_name, price_eur "
        "FROM competitors WHERE restaurant_id = ?",
        [rid],
    )


def ad_campaigns(con: duckdb.DuckDBPyConnection, rid: str) -> list[dict]:
    return _rows(
        con,
        """
        SELECT id, weekly_budget_eur, attributed_orders, active,
               greatest(1, date_diff('day', start_date, end_date) // 7) AS weeks
        FROM ad_campaigns WHERE restaurant_id = ?
        """,
        [rid],
    )


def existing_ids(con: duckdb.DuckDBPyConnection, table: str, ids: list[int]) -> set[int]:
    if not ids or table not in {"reviews", "menu_items", "competitors", "ad_campaigns"}:
        return set()
    rows = con.execute(f"SELECT id FROM {table} WHERE id IN (SELECT unnest(?))", [ids]).fetchall()
    return {r[0] for r in rows}


# --- listings and change log ----------------------------------------------------


def get_listing(con: duckdb.DuckDBPyConnection, rid: str) -> dict:
    row = con.execute("SELECT listing FROM listings WHERE restaurant_id = ?", [rid]).fetchone()
    if row is None:
        raise KeyError(f"no listing for {rid}")
    return json.loads(row[0])


def save_listing(con: duckdb.DuckDBPyConnection, rid: str, listing: dict) -> None:
    con.execute(
        "UPDATE listings SET listing = ?, updated_at = current_timestamp WHERE restaurant_id = ?",
        [json.dumps(listing), rid],
    )


def insert_change(
    con: duckdb.DuckDBPyConnection,
    rid: str,
    recommendation_id: str,
    patch: list[dict],
    before: dict,
    after: dict,
    approver: str,
) -> int:
    row = con.execute(
        """
        INSERT INTO change_log (restaurant_id, recommendation_id, patch, before, after, approver)
        VALUES (?, ?, ?, ?, ?, ?) RETURNING id
        """,
        [
            rid,
            recommendation_id,
            json.dumps(patch),
            json.dumps(before),
            json.dumps(after),
            approver,
        ],
    ).fetchone()
    return int(row[0])


def get_change(con: duckdb.DuckDBPyConnection, change_id: int) -> dict:
    rows = _rows(con, "SELECT * FROM change_log WHERE id = ?", [change_id])
    if not rows:
        raise KeyError(f"no change {change_id}")
    return rows[0]


def mark_reverted(con: duckdb.DuckDBPyConnection, change_id: int) -> None:
    con.execute("UPDATE change_log SET reverted_at = current_timestamp WHERE id = ?", [change_id])


def change_log(con: duckdb.DuckDBPyConnection, rid: str) -> list[dict]:
    return _rows(
        con,
        "SELECT id, recommendation_id, approver, applied_at, reverted_at, patch "
        "FROM change_log WHERE restaurant_id = ? ORDER BY id",
        [rid],
    )
