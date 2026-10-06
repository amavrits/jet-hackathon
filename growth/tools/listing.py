"""Read and apply patches to the mock listing store. The only writer of `listings`."""

import json

import duckdb
import jsonpatch

from growth.data import queries


def get(con: duckdb.DuckDBPyConnection, rid: str) -> dict:
    return queries.get_listing(con, rid)


def apply(
    con: duckdb.DuckDBPyConnection,
    rid: str,
    recommendation_id: str,
    patch: list[dict],
    approver: str,
) -> int:
    """Apply an approved patch and log it. Raises if a `test` op fails (listing changed)."""
    if not approver.strip():
        raise ValueError("an approver is required to apply a change")
    before = queries.get_listing(con, rid)
    after = jsonpatch.apply_patch(before, patch)
    con.execute("BEGIN")
    try:
        queries.save_listing(con, rid, after)
        change_id = queries.insert_change(
            con, rid, recommendation_id, patch, before, after, approver
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return change_id


def revert(con: duckdb.DuckDBPyConnection, change_id: int) -> dict:
    """Undo one change. Raises if the listing has since changed in the same places."""
    change = queries.get_change(con, change_id)
    if change["reverted_at"] is not None:
        raise ValueError(f"change {change_id} is already reverted")
    before, after = json.loads(change["before"]), json.loads(change["after"])
    current = queries.get_listing(con, change["restaurant_id"])
    undo = jsonpatch.make_patch(after, before).patch
    # Guard every path the undo touches: it must still hold the value this change wrote.
    guards = []
    for op in undo:
        if op["op"] in {"replace", "remove"}:
            ptr = jsonpatch.JsonPointer(op["path"])
            guards.append({"op": "test", "path": op["path"], "value": ptr.resolve(after)})
    restored = jsonpatch.apply_patch(current, guards + undo)
    queries.save_listing(con, change["restaurant_id"], restored)
    queries.mark_reverted(con, change_id)
    return restored
