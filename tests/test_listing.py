"""Listing store: apply is atomic and logged, revert is safe with later changes."""

import pytest

from growth.data import queries as q
from growth.tools import listing as L

RID = "r_spice_route"


@pytest.fixture
def wcon(writable_db):
    c = q.connect(writable_db)
    yield c
    c.close()


def _price(c, item):
    return L.get_listing(c, RID)["menu"][item]["price_eur"]


def test_apply_writes_listing_and_logs_before_image(wcon):
    before = L.get_listing(wcon, RID)
    item = next(iter(before["menu"]))
    change = L.apply_patch(wcon, RID, "rec-1", [{"op": "replace", "path": f"/menu/{item}/price_eur", "value": 9.5}])
    assert _price(wcon, item) == 9.5
    (row,) = L.history(wcon, RID)
    assert row["change_id"] == change.change_id
    assert row["recommendation_id"] == "rec-1"
    assert row["listing_before"] == before
    assert L.diff(before, L.get_listing(wcon, RID)) == [
        {"op": "replace", "path": f"/menu/{item}/price_eur", "value": 9.5}
    ]


@pytest.mark.parametrize(
    "ops",
    [
        [{"op": "replace", "path": "/menu/no_such_item/price_eur", "value": 1}],  # bad pointer
        [{"op": "replace", "path": "/name", "value": "Hacked"}],  # identity field
        [],  # empty
    ],
)
def test_invalid_patch_changes_nothing(wcon, ops):
    before = L.get_listing(wcon, RID)
    with pytest.raises(L.PatchError):
        L.apply_patch(wcon, RID, "rec-x", ops)
    assert L.get_listing(wcon, RID) == before
    assert L.history(wcon, RID) == []


def test_partial_failure_is_atomic(wcon):
    before = L.get_listing(wcon, RID)
    item = next(iter(before["menu"]))
    ops = [
        {"op": "replace", "path": f"/menu/{item}/price_eur", "value": 1.0},
        {"op": "replace", "path": "/menu/missing/price_eur", "value": 2.0},
    ]
    with pytest.raises(L.PatchError):
        L.apply_patch(wcon, RID, "rec-x", ops)
    assert L.get_listing(wcon, RID) == before


def test_revert_newest_then_older_reverts_everything_after(wcon):
    original = L.get_listing(wcon, RID)
    a, b = list(original["menu"])[:2]
    c1 = L.apply_patch(wcon, RID, "r1", [{"op": "replace", "path": f"/menu/{a}/price_eur", "value": 1.0}])
    c2 = L.apply_patch(wcon, RID, "r2", [{"op": "replace", "path": f"/menu/{b}/price_eur", "value": 2.0}])
    c3 = L.apply_patch(wcon, RID, "r3", [{"op": "add", "path": "/promotions/-", "value": {"id": "p"}}])

    assert L.revert(wcon, c3.change_id) == [c3.change_id]
    assert L.get_listing(wcon, RID)["promotions"] == original["promotions"]
    assert _price(wcon, b) == 2.0

    # reverting c1 must also undo c2, or c2 would be silently lost and the log would lie
    assert L.revert(wcon, c1.change_id) == [c2.change_id, c1.change_id]
    assert L.get_listing(wcon, RID) == original
    assert L.revert(wcon, c1.change_id) == []  # idempotent
    assert all(h["reverted_at"] is not None for h in L.history(wcon, RID))
