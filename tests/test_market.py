"""The market and weekly panel are internally consistent. Effect recovery is tested with the estimator."""

from growth.data.seed import RESTAURANTS
from runners import true_model as tm


def test_partners_and_market(con):
    n_partner, n_market = con.execute(
        "SELECT COUNT(*) FILTER (WHERE is_partner), COUNT(*) FILTER (WHERE NOT is_partner) FROM restaurants"
    ).fetchone()
    assert n_partner == len(RESTAURANTS)
    assert n_market >= 200
    assert (
        con.execute("SELECT COUNT(*) FROM restaurants WHERE lat IS NULL OR price_level NOT IN (1,2,3)").fetchone()[0]
        == 0
    )


def test_competitors_are_nearby_same_cuisine_market_restaurants(con):
    rows = con.execute(
        """
        SELECT c.restaurant_id, c.distance_km, p.cuisine = m.cuisine, m.is_partner
        FROM competitors c
        JOIN restaurants p ON p.restaurant_id = c.restaurant_id
        JOIN restaurants m ON m.restaurant_id = c.competitor_id
        """
    ).fetchall()
    assert len(rows) == 5 * len(RESTAURANTS)
    assert all(same and not partner and d <= 2.6 for _, d, same, partner in rows)


def test_panel_covers_every_restaurant_every_week(con):
    n_rest, n_weeks, rows = con.execute(
        "SELECT COUNT(DISTINCT restaurant_id), COUNT(DISTINCT week_start), COUNT(*) FROM restaurant_weeks"
    ).fetchone()
    assert n_weeks == tm.N_WEEKS
    assert rows == n_rest * n_weeks
    assert n_rest == con.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0]


def test_partner_panel_matches_item_level_orders(con):
    for (rid,) in con.execute("SELECT restaurant_id FROM restaurants WHERE is_partner").fetchall():
        panel = con.execute(
            "SELECT SUM(orders) FROM restaurant_weeks WHERE restaurant_id = ? AND week_start >= ?",
            [rid, tm.WEEKS[-8]],
        ).fetchone()[0]
        actual = con.execute("SELECT COUNT(DISTINCT order_id) FROM orders WHERE restaurant_id = ?", [rid]).fetchone()[0]
        assert panel == actual, rid


def test_change_events_match_panel(con):
    # every photo event is a jump in photo_share between consecutive weeks of that restaurant
    bad = con.execute(
        """
        SELECT COUNT(*) FROM change_events e
        JOIN restaurant_weeks a ON a.restaurant_id = e.restaurant_id AND a.week_start = e.week_start
        JOIN restaurant_weeks b ON b.restaurant_id = e.restaurant_id AND b.week_start = e.week_start - INTERVAL 7 DAY
        WHERE e.change_type = 'photos' AND abs(a.photo_share - b.photo_share) <= 0.2
        """
    ).fetchone()[0]
    assert bad == 0
    assert con.execute("SELECT COUNT(DISTINCT change_type) FROM change_events").fetchone()[0] >= 6
