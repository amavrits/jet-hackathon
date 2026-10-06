"""The planted problems in the seed must be discoverable through queries.py."""

from growth.data import queries as q
from growth.data.seed import PLANTED, RESTAURANTS


def test_all_restaurants_have_data(con):
    assert len(q.list_restaurants(con)) == len(RESTAURANTS)
    for r in RESTAURANTS:
        rid = r["restaurant_id"]
        assert q.weekly_summary(con, rid)["orders_per_week"] > 50
        assert len(q.get_reviews(con, rid)) > 20
        assert len(q.get_competitors(con, rid)) >= 4
        assert q.get_listing(con, rid)["menu"]


def test_soggy_item_is_worst_rated(con):
    for rid, planted in PLANTED.items():
        if "soggy_item" in planted:
            worst = q.rating_by_item(con, rid)[0]
            assert worst["name"] == planted["soggy_item"]
            assert worst["avg_rating"] < 2.5
            texts = [r["text"].lower() for r in q.get_reviews(con, rid) if r["item_name"] == planted["soggy_item"]]
            assert sum("soggy" in t for t in texts) < len(texts) / 2  # not keyword-findable


def test_dead_slot_is_visible(con):
    def tue_vs_rest(rid):
        slots = q.orders_by_slot(con, rid)
        tue = sum(s["orders"] for s in slots if s["weekday"] == 1 and 14 <= s["hour"] <= 17)
        rest = [sum(s["orders"] for s in slots if s["weekday"] == d and 14 <= s["hour"] <= 17) for d in (0, 2, 3)]
        return tue, min(rest)

    for rid, planted in PLANTED.items():
        tue, rest = tue_vs_rest(rid)
        if planted.get("dead_slot"):
            assert tue < 0.5 * rest, rid
        else:
            assert tue > 0.6 * rest, rid


def test_overpriced_category_is_top_premium(con):
    for rid, planted in PLANTED.items():
        top = q.price_position_by_category(con, rid)[0]
        if "overpriced_category" in planted:
            assert top["category"] == planted["overpriced_category"]
            assert top["premium_pct"] > 0.18
        else:
            assert top["premium_pct"] < 0.18, (rid, top)


def test_missing_photos_on_bestsellers(con):
    for rid, planted in PLANTED.items():
        top3 = q.item_sales(con, rid)[:3]
        missing = [t["photo_url"] is None for t in top3]
        assert all(missing) if planted.get("missing_photos") else not any(missing), rid


def test_dead_item_and_wasted_ads(con):
    for rid, planted in PLANTED.items():
        if "dead_item" in planted:
            assert q.item_sales(con, rid)[-1]["name"] == planted["dead_item"]
        assert bool(q.get_ad_campaigns(con, rid)) == bool(planted.get("wasted_ads"))
