"""All SQL lives here. Agents never write SQL inline.

Every function takes an open DuckDB connection and returns plain Python
structures (list[dict]) so agents and the UI never touch SQL.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import duckdb

DB_PATH = Path(os.environ.get("JET_DB_PATH", "data/jet.duckdb"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS restaurants (
    restaurant_id   VARCHAR PRIMARY KEY,
    name            VARCHAR NOT NULL,
    cuisine         VARCHAR NOT NULL,
    city            VARCHAR NOT NULL,
    postcode        VARCHAR NOT NULL,
    delivery_model  VARCHAR NOT NULL,       -- 'marketplace' | 'jet_delivery'
    commission_rate DOUBLE NOT NULL,        -- 0.15 or 0.30
    rating          DOUBLE NOT NULL,
    joined_at       DATE NOT NULL,
    lat             DOUBLE NOT NULL,
    lon             DOUBLE NOT NULL,
    price_level     INTEGER NOT NULL,       -- 1 budget | 2 mid | 3 premium
    is_partner      BOOLEAN NOT NULL        -- TRUE: the 8 demo partners with item-level detail
);

CREATE TABLE IF NOT EXISTS menu_items (
    menu_item_id    VARCHAR PRIMARY KEY,
    restaurant_id   VARCHAR NOT NULL,
    name            VARCHAR NOT NULL,
    category        VARCHAR NOT NULL,       -- Starters | Mains | Sides | Desserts | Drinks
    description     VARCHAR,                -- NULL = missing description
    price_eur       DOUBLE NOT NULL,
    photo_url       VARCHAR,                -- NULL = missing photo
    is_available    BOOLEAN NOT NULL DEFAULT TRUE
);

-- Item-level order lines. One row per (order, menu item).
CREATE TABLE IF NOT EXISTS orders (
    order_id        VARCHAR NOT NULL,
    restaurant_id   VARCHAR NOT NULL,
    menu_item_id    VARCHAR NOT NULL,
    qty             INTEGER NOT NULL,
    unit_price_eur  DOUBLE NOT NULL,
    placed_at       TIMESTAMP NOT NULL,
    channel         VARCHAR NOT NULL        -- 'organic' | 'sponsored'
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id       VARCHAR PRIMARY KEY,
    restaurant_id   VARCHAR NOT NULL,
    order_id        VARCHAR,
    menu_item_id    VARCHAR,                -- NULL = review about the restaurant overall
    rating          INTEGER NOT NULL,       -- 1..5
    text            VARCHAR NOT NULL,
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS competitors (
    competitor_id   VARCHAR PRIMARY KEY,
    restaurant_id   VARCHAR NOT NULL,       -- the partner this competitor is "near"
    name            VARCHAR NOT NULL,
    cuisine         VARCHAR NOT NULL,
    distance_km     DOUBLE NOT NULL,
    rating          DOUBLE NOT NULL,
    is_sponsored    BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS competitor_menu_items (
    competitor_item_id VARCHAR PRIMARY KEY,
    competitor_id   VARCHAR NOT NULL,
    name            VARCHAR NOT NULL,
    category        VARCHAR NOT NULL,
    price_eur       DOUBLE NOT NULL
);

CREATE TABLE IF NOT EXISTS ad_campaigns (
    campaign_id     VARCHAR PRIMARY KEY,
    restaurant_id   VARCHAR NOT NULL,
    campaign_type   VARCHAR NOT NULL,       -- 'sponsored_listing'
    status          VARCHAR NOT NULL,       -- 'active' | 'ended'
    weekly_budget_eur DOUBLE NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE,
    impressions     INTEGER NOT NULL,
    clicks          INTEGER NOT NULL,
    attributed_orders INTEGER NOT NULL,
    spend_eur       DOUBLE NOT NULL
);

-- Current public listing, as the customer sees it.
CREATE TABLE IF NOT EXISTS listings (
    restaurant_id   VARCHAR PRIMARY KEY,
    listing         JSON NOT NULL,
    updated_at      TIMESTAMP NOT NULL
);

-- Weekly panel for every restaurant in the market (partners + the rest), 26 weeks.
-- This is what impact estimation learns from: levers, outcomes, and controls per week.
CREATE TABLE IF NOT EXISTS restaurant_weeks (
    restaurant_id       VARCHAR NOT NULL,
    week_start          DATE NOT NULL,
    orders              INTEGER NOT NULL,
    gmv_eur             DOUBLE NOT NULL,
    avg_basket_eur      DOUBLE NOT NULL,
    photo_share         DOUBLE NOT NULL,    -- share of menu items with a photo
    description_share   DOUBLE NOT NULL,    -- share of menu items with a description
    price_index         DOUBLE NOT NULL,    -- own prices / nearby same-cuisine median (1.0 = at market)
    promo_active        BOOLEAN NOT NULL,
    ad_budget_eur       DOUBLE NOT NULL,    -- weekly sponsored-listing budget, 0 = none
    ad_offpeak_share    DOUBLE NOT NULL,    -- share of the ad budget scheduled outside 18-22h
    rating              DOUBLE NOT NULL,
    PRIMARY KEY (restaurant_id, week_start)
);

-- Observable log of listing changes restaurants made in the past (JET has this for real).
CREATE TABLE IF NOT EXISTS change_events (
    event_id        VARCHAR PRIMARY KEY,
    restaurant_id   VARCHAR NOT NULL,
    week_start      DATE NOT NULL,          -- first week the change is live
    change_type     VARCHAR NOT NULL,       -- photos | descriptions | price | promo_start | promo_end
                                            -- | ad_start | ad_stop | ad_budget | ad_daypart
    before_value    DOUBLE,
    after_value     DOUBLE
);

-- Every approved patch applied to a listing. Reversible via `reverted_at`.
CREATE TABLE IF NOT EXISTS change_log (
    change_id       VARCHAR PRIMARY KEY,
    restaurant_id   VARCHAR NOT NULL,
    recommendation_id VARCHAR NOT NULL,
    patch           JSON NOT NULL,
    listing_before  JSON NOT NULL,
    applied_at      TIMESTAMP NOT NULL,
    reverted_at     TIMESTAMP
);
"""


def connect(path: Path | str = DB_PATH, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the single project database."""
    return duckdb.connect(str(path), read_only=read_only)


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA)


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


# ---------------------------------------------------------------- restaurants


def list_restaurants(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """The demo partners only. Market restaurants live in the same table with is_partner = FALSE."""
    return _rows(con, "SELECT * FROM restaurants WHERE is_partner ORDER BY name")


def get_restaurant(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> dict[str, Any] | None:
    rows = _rows(con, "SELECT * FROM restaurants WHERE restaurant_id = ?", [restaurant_id])
    return rows[0] if rows else None


def get_menu(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> list[dict[str, Any]]:
    return _rows(
        con,
        "SELECT * FROM menu_items WHERE restaurant_id = ? ORDER BY category, name",
        [restaurant_id],
    )


# --------------------------------------------------------------------- orders


def item_sales(con: duckdb.DuckDBPyConnection, restaurant_id: str, weeks: int = 8) -> list[dict[str, Any]]:
    """Units, revenue and order count per menu item over the last N weeks."""
    return _rows(
        con,
        """
        SELECT m.menu_item_id, m.name, m.category, m.price_eur, m.photo_url, m.description,
               COALESCE(SUM(o.qty), 0)                      AS units,
               COALESCE(SUM(o.qty * o.unit_price_eur), 0)   AS revenue_eur,
               COUNT(DISTINCT o.order_id)                   AS orders
        FROM menu_items m
        LEFT JOIN orders o
          ON o.menu_item_id = m.menu_item_id
         AND o.placed_at >= (SELECT MAX(placed_at) FROM orders WHERE restaurant_id = ?) - INTERVAL (?) WEEK
        WHERE m.restaurant_id = ?
        GROUP BY ALL
        ORDER BY units DESC
        """,
        [restaurant_id, weeks, restaurant_id],
    )


def orders_by_slot(con: duckdb.DuckDBPyConnection, restaurant_id: str, weeks: int = 8) -> list[dict[str, Any]]:
    """Distinct orders per (weekday, hour) over the last N weeks. weekday: 0=Mon..6=Sun."""
    return _rows(
        con,
        """
        SELECT (dayofweek(placed_at) + 6) % 7 AS weekday,
               hour(placed_at)                AS hour,
               COUNT(DISTINCT order_id)       AS orders,
               SUM(qty * unit_price_eur)      AS revenue_eur
        FROM orders
        WHERE restaurant_id = ?
          AND placed_at >= (SELECT MAX(placed_at) FROM orders WHERE restaurant_id = ?) - INTERVAL (?) WEEK
        GROUP BY ALL
        ORDER BY weekday, hour
        """,
        [restaurant_id, restaurant_id, weeks],
    )


def weekly_summary(con: duckdb.DuckDBPyConnection, restaurant_id: str, weeks: int = 8) -> dict[str, Any]:
    """Average orders, GMV and basket size per week."""
    rows = _rows(
        con,
        """
        WITH o AS (
            SELECT order_id, SUM(qty * unit_price_eur) AS basket
            FROM orders
            WHERE restaurant_id = ?
              AND placed_at >= (SELECT MAX(placed_at) FROM orders WHERE restaurant_id = ?) - INTERVAL (?) WEEK
            GROUP BY order_id
        )
        SELECT COUNT(*) / ?            AS orders_per_week,
               SUM(basket) / ?         AS gmv_eur_per_week,
               AVG(basket)             AS avg_basket_eur
        FROM o
        """,
        [restaurant_id, restaurant_id, weeks, weeks, weeks],
    )
    return rows[0]


# -------------------------------------------------------------------- reviews


def get_reviews(con: duckdb.DuckDBPyConnection, restaurant_id: str, limit: int = 500) -> list[dict[str, Any]]:
    return _rows(
        con,
        """
        SELECT r.*, m.name AS item_name
        FROM reviews r LEFT JOIN menu_items m USING (menu_item_id)
        WHERE r.restaurant_id = ?
        ORDER BY r.created_at DESC
        LIMIT ?
        """,
        [restaurant_id, limit],
    )


def rating_by_item(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> list[dict[str, Any]]:
    return _rows(
        con,
        """
        SELECT m.menu_item_id, m.name, COUNT(r.review_id) AS n_reviews, AVG(r.rating) AS avg_rating
        FROM menu_items m JOIN reviews r USING (menu_item_id)
        WHERE m.restaurant_id = ?
        GROUP BY ALL
        HAVING COUNT(r.review_id) >= 5
        ORDER BY avg_rating ASC
        """,
        [restaurant_id],
    )


# ---------------------------------------------------------------- competitors


def get_competitors(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> list[dict[str, Any]]:
    return _rows(
        con,
        "SELECT * FROM competitors WHERE restaurant_id = ? ORDER BY distance_km",
        [restaurant_id],
    )


def price_position_by_category(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> list[dict[str, Any]]:
    """Per category: how far the restaurant's prices sit above the nearby-competitor median
    for the *same dish*. premium_pct = median over items of (my_price / competitor_median - 1)."""
    return _rows(
        con,
        """
        WITH comp AS (
            SELECT ci.name, MEDIAN(ci.price_eur) AS competitor_median, COUNT(*) AS n_competitors
            FROM competitor_menu_items ci JOIN competitors c USING (competitor_id)
            WHERE c.restaurant_id = ?
            GROUP BY ci.name
        ),
        per_item AS (
            SELECT m.category, m.name, m.price_eur, comp.competitor_median, comp.n_competitors,
                   m.price_eur / comp.competitor_median - 1 AS item_premium_pct
            FROM menu_items m JOIN comp USING (name)
            WHERE m.restaurant_id = ?
        )
        SELECT category,
               MEDIAN(item_premium_pct)   AS premium_pct,
               COUNT(*)                   AS my_items,
               SUM(n_competitors)         AS competitor_items,
               MEDIAN(price_eur)          AS my_median,
               MEDIAN(competitor_median)  AS competitor_median
        FROM per_item
        GROUP BY category
        ORDER BY premium_pct DESC
        """,
        [restaurant_id, restaurant_id],
    )


def price_position_by_item(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> list[dict[str, Any]]:
    """Each menu item vs the nearby-competitor median for the same dish name. Evidence for pricing."""
    return _rows(
        con,
        """
        WITH comp AS (
            SELECT ci.name, MEDIAN(ci.price_eur) AS competitor_median, COUNT(*) AS n_competitors
            FROM competitor_menu_items ci JOIN competitors c USING (competitor_id)
            WHERE c.restaurant_id = ?
            GROUP BY ci.name
        )
        SELECT m.menu_item_id, m.name, m.category, m.price_eur, comp.competitor_median, comp.n_competitors,
               m.price_eur / comp.competitor_median - 1 AS premium_pct
        FROM menu_items m JOIN comp USING (name)
        WHERE m.restaurant_id = ?
        ORDER BY premium_pct DESC
        """,
        [restaurant_id, restaurant_id],
    )


# ------------------------------------------------------------------------ ads


def get_ad_campaigns(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> list[dict[str, Any]]:
    return _rows(
        con,
        "SELECT * FROM ad_campaigns WHERE restaurant_id = ? ORDER BY start_date DESC",
        [restaurant_id],
    )


# ------------------------------------------------------------------- listings


def get_listing(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> dict[str, Any] | None:
    rows = _rows(con, "SELECT listing FROM listings WHERE restaurant_id = ?", [restaurant_id])
    if not rows:
        return None
    import json

    return json.loads(rows[0]["listing"])


def save_listing(con: duckdb.DuckDBPyConnection, restaurant_id: str, listing: dict[str, Any], at: Any) -> None:
    import json

    con.execute(
        "UPDATE listings SET listing = ?, updated_at = ? WHERE restaurant_id = ?",
        [json.dumps(listing), at, restaurant_id],
    )


def log_change(
    con: duckdb.DuckDBPyConnection,
    change_id: str,
    restaurant_id: str,
    recommendation_id: str,
    patch: list[dict[str, Any]],
    listing_before: dict[str, Any],
    applied_at: Any,
) -> None:
    import json

    con.execute(
        """
        INSERT INTO change_log (change_id, restaurant_id, recommendation_id, patch, listing_before, applied_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [change_id, restaurant_id, recommendation_id, json.dumps(patch), json.dumps(listing_before), applied_at],
    )


def get_change(con: duckdb.DuckDBPyConnection, change_id: str) -> dict[str, Any] | None:
    rows = _rows(con, "SELECT * FROM change_log WHERE change_id = ?", [change_id])
    return rows[0] if rows else None


def active_changes_since(con: duckdb.DuckDBPyConnection, restaurant_id: str, since: Any) -> list[dict[str, Any]]:
    """Unreverted changes applied at or after `since`, newest first."""
    return _rows(
        con,
        """
        SELECT * FROM change_log
        WHERE restaurant_id = ? AND applied_at >= ? AND reverted_at IS NULL
        ORDER BY applied_at DESC
        """,
        [restaurant_id, since],
    )


def mark_reverted(con: duckdb.DuckDBPyConnection, change_id: str, reverted_at: Any) -> None:
    con.execute("UPDATE change_log SET reverted_at = ? WHERE change_id = ?", [reverted_at, change_id])


def get_change_log(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> list[dict[str, Any]]:
    return _rows(
        con,
        "SELECT * FROM change_log WHERE restaurant_id = ? ORDER BY applied_at DESC",
        [restaurant_id],
    )


def low_rated_reviews_with_order_items(
    con: duckdb.DuckDBPyConnection, restaurant_id: str, max_rating: int = 3
) -> list[dict[str, Any]]:
    """Reviews at or below `max_rating`, each with the names of the dishes in its order.

    Reviews are written per order, so the dish a complaint is about has to be read from the text;
    `order_items` is the candidate list for that.
    """
    return _rows(
        con,
        """
        SELECT r.review_id, r.rating, r.text, r.created_at, r.order_id,
               list(DISTINCT m.name ORDER BY m.name) AS order_items
        FROM reviews r
        JOIN orders o USING (order_id)
        JOIN menu_items m ON m.menu_item_id = o.menu_item_id
        WHERE r.restaurant_id = ? AND r.rating <= ?
        GROUP BY r.review_id, r.rating, r.text, r.created_at, r.order_id
        ORDER BY r.created_at
        """,
        [restaurant_id, max_rating],
    )


def review_window_weeks(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> float:
    """Span of the review history in weeks, to turn counts into weekly rates."""
    row = con.execute(
        "SELECT date_diff('day', MIN(created_at), MAX(created_at)) FROM reviews WHERE restaurant_id = ?",
        [restaurant_id],
    ).fetchone()
    return max(1.0, (row[0] or 7) / 7)


def sponsored_share_in_hours(con: duckdb.DuckDBPyConnection, restaurant_id: str, hours: list[int]) -> dict[str, Any]:
    """How many sponsored vs organic orders fall in the given hours (e.g. the dinner peak)."""
    return _rows(
        con,
        """
        SELECT
            COUNT(DISTINCT order_id) FILTER (WHERE channel = 'sponsored') AS sponsored,
            COUNT(DISTINCT order_id) FILTER (
                WHERE channel = 'sponsored' AND list_contains(?, hour(placed_at))
            ) AS sponsored_in_hours,
            COUNT(DISTINCT order_id) AS total,
            COUNT(DISTINCT order_id) FILTER (WHERE list_contains(?, hour(placed_at))) AS total_in_hours
        FROM orders WHERE restaurant_id = ?
        """,
        [hours, hours, restaurant_id],
    )[0]


# ------------------------------------------------------------- market panel (growth.ml)


def panel(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Every restaurant-week with the restaurant's static attributes. Input to effect estimation."""
    return _rows(
        con,
        """
        SELECT w.*, r.city, r.cuisine, r.price_level, r.commission_rate, r.is_partner
        FROM restaurant_weeks w JOIN restaurants r USING (restaurant_id)
        ORDER BY w.restaurant_id, w.week_start
        """,
    )


def restaurant_features(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """One row per restaurant: location, cuisine, price level, size and lever levels. Input to peers."""
    return _rows(
        con,
        """
        SELECT r.restaurant_id, r.name, r.cuisine, r.city, r.lat, r.lon, r.price_level, r.rating,
               r.delivery_model, r.commission_rate, r.is_partner,
               MEDIAN(w.orders)             AS orders_per_week,
               MEDIAN(w.avg_basket_eur)     AS avg_basket_eur,
               AVG(w.photo_share)           AS photo_share,
               AVG(w.description_share)     AS description_share,
               arg_max(w.price_index, w.week_start) AS price_index,
               AVG(w.ad_budget_eur)         AS ad_budget_eur,
               MAX(w.ad_budget_eur) > 0     AS advertiser
        FROM restaurants r JOIN restaurant_weeks w USING (restaurant_id)
        GROUP BY ALL
        ORDER BY r.restaurant_id
        """,
    )


def latest_week(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> dict[str, Any] | None:
    """The most recent panel row: current lever values and volume. Input to prediction."""
    rows = _rows(
        con,
        """
        SELECT w.*, r.name, r.city, r.cuisine, r.price_level, r.commission_rate, r.is_partner, r.rating AS rating_now
        FROM restaurant_weeks w JOIN restaurants r USING (restaurant_id)
        WHERE w.restaurant_id = ?
        ORDER BY w.week_start DESC LIMIT 1
        """,
        [restaurant_id],
    )
    return rows[0] if rows else None


def menu_size(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> int:
    row = con.execute("SELECT COUNT(*) FROM menu_items WHERE restaurant_id = ?", [restaurant_id]).fetchone()
    return int(row[0] or 0)


def change_event_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """How many past changes of each type the market has. Evidence strength for each effect."""
    return {
        r["change_type"]: int(r["n"])
        for r in _rows(con, "SELECT change_type, COUNT(*) AS n FROM change_events GROUP BY 1")
    }


def category_orders(con: duckdb.DuckDBPyConnection, restaurant_id: str, category: str, weeks: int = 8) -> int:
    """Distinct orders containing at least one dish from `category` in the last N weeks."""
    row = con.execute(
        """
        SELECT COUNT(DISTINCT o.order_id)
        FROM orders o JOIN menu_items m USING (menu_item_id)
        WHERE o.restaurant_id = ? AND m.category = ?
          AND o.placed_at >= (SELECT MAX(placed_at) FROM orders WHERE restaurant_id = ?) - INTERVAL (?) WEEK
        """,
        [restaurant_id, category, restaurant_id, weeks],
    ).fetchone()
    return int(row[0] or 0)


LEVERS = ("photo_share", "description_share", "price_index", "promo_active", "ad_budget_eur", "ad_offpeak_share")


def current_levers(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> dict[str, float]:
    """The restaurant's lever values in its latest `restaurant_weeks` row (the `before` of a LeverChange)."""
    row = con.execute(
        f"""
        SELECT {", ".join(LEVERS)} FROM restaurant_weeks
        WHERE restaurant_id = ? ORDER BY week_start DESC LIMIT 1
        """,
        [restaurant_id],
    ).fetchone()
    if row is None:
        raise KeyError(f"no weekly history for {restaurant_id}")
    return {k: float(v) for k, v in zip(LEVERS, row, strict=True)}


def total_item_revenue(con: duckdb.DuckDBPyConnection, restaurant_id: str, weeks: int = 8) -> float:
    """Item revenue over the last N weeks, the weight base for price-index changes."""
    return float(sum(float(s["revenue_eur"]) for s in item_sales(con, restaurant_id, weeks)))


def dish_reviews(
    con: duckdb.DuckDBPyConnection, restaurant_id: str, menu_item_id: str, max_rating: int = 5, limit: int = 10
) -> list[dict[str, Any]]:
    """Reviews of orders that contained the dish, lowest rating first, then newest."""
    return _rows(
        con,
        """
        SELECT DISTINCT r.review_id, r.rating, r.text, r.created_at
        FROM reviews r JOIN orders o USING (order_id)
        WHERE r.restaurant_id = ? AND o.menu_item_id = ? AND r.rating <= ?
        ORDER BY r.rating, r.created_at DESC
        LIMIT ?
        """,
        [restaurant_id, menu_item_id, max_rating, limit],
    )


def competitor_prices_for_dish(
    con: duckdb.DuckDBPyConnection, restaurant_id: str, dish_name: str
) -> list[dict[str, Any]]:
    """Each nearby competitor's price for the same dish name, nearest first."""
    return _rows(
        con,
        """
        SELECT c.name AS competitor, c.distance_km, c.rating, ci.price_eur
        FROM competitors c JOIN competitor_menu_items ci USING (competitor_id)
        WHERE c.restaurant_id = ? AND ci.name = ?
        ORDER BY c.distance_km
        """,
        [restaurant_id, dish_name],
    )
