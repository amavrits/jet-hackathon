"""Read and apply JSON patches to the mock listing store. Every change is logged and reversible.

Only the graph's `apply` node calls `apply_recommendation`, and only for owner-approved
recommendations. Agents never write.

- apply: validate -> patch a copy -> write listing + change_log row (with the before-image)
  in one transaction. A bad patch changes nothing.
- revert: restores the before-image of a change. Because a before-image also predates every
  later change, reverting change X first reverts all newer active changes (newest first), so
  nothing is silently lost. Returns every change id it reverted.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime
from typing import Any

import duckdb
import jsonpatch
import jsonpointer

from growth.data import queries as q
from growth.state import AppliedChange, PatchOp, Recommendation

# Top-level listing keys a patch may touch. Identity fields (restaurant_id, name) are off limits.
EDITABLE = {"menu", "promotions", "sponsored_listing", "description", "tags", "opening_hours"}


class PatchError(ValueError):
    """The patch is invalid for this listing. Nothing was written."""


def get_listing(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> dict[str, Any]:
    listing = q.get_listing(con, restaurant_id)
    if listing is None:
        raise KeyError(f"no listing for {restaurant_id}")
    return listing


def preview(listing: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the patched listing without writing anything. Raises PatchError."""
    for op in ops:
        top = op.get("path", "").lstrip("/").split("/", 1)[0]
        if top not in EDITABLE:
            raise PatchError(f"path {op.get('path')!r} is not editable")
    try:
        return jsonpatch.apply_patch(copy.deepcopy(listing), ops)
    except (jsonpatch.JsonPatchException, jsonpointer.JsonPointerException) as e:
        raise PatchError(str(e)) from e


def diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Minimal RFC 6902 diff, for showing what changed in the UI."""
    return jsonpatch.make_patch(before, after).patch


def apply_patch(
    con: duckdb.DuckDBPyConnection, restaurant_id: str, recommendation_id: str, ops: list[dict[str, Any]]
) -> AppliedChange:
    if not ops:
        raise PatchError("empty patch")
    con.begin()
    try:
        before = get_listing(con, restaurant_id)
        after = preview(before, ops)
        change_id = f"chg_{uuid.uuid4().hex[:12]}"
        now = datetime.now()
        q.save_listing(con, restaurant_id, after, now)
        q.log_change(con, change_id, restaurant_id, recommendation_id, ops, before, now)
        con.commit()
    except Exception:
        con.rollback()
        raise
    return AppliedChange(
        change_id=change_id,
        recommendation_id=recommendation_id,
        patch=[PatchOp(**op) for op in ops],
        applied_at=now,
    )


def apply_recommendation(con: duckdb.DuckDBPyConnection, restaurant_id: str, rec: Recommendation) -> AppliedChange:
    """Apply an approved recommendation's patch. Advice-only recommendations raise PatchError."""
    return apply_patch(con, restaurant_id, rec.id, rec.patch_ops())


def revert(con: duckdb.DuckDBPyConnection, change_id: str) -> list[str]:
    """Undo a change, and every newer active change for the same listing. Returns reverted ids."""
    change = q.get_change(con, change_id)
    if change is None:
        raise KeyError(f"unknown change {change_id}")
    if change["reverted_at"] is not None:
        return []
    rid = change["restaurant_id"]
    con.begin()
    try:
        stack = q.active_changes_since(con, rid, change["applied_at"])  # newest first, includes change_id
        now = datetime.now()
        for c in stack:
            q.mark_reverted(con, c["change_id"], now)
        q.save_listing(con, rid, json.loads(change["listing_before"]), now)
        con.commit()
    except Exception:
        con.rollback()
        raise
    return [c["change_id"] for c in stack]


def history(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> list[dict[str, Any]]:
    """Change log for a listing, newest first, with patches decoded."""
    rows = q.get_change_log(con, restaurant_id)
    return [{**r, "patch": json.loads(r["patch"]), "listing_before": json.loads(r["listing_before"])} for r in rows]


def reset(con: duckdb.DuckDBPyConnection, restaurant_id: str) -> list[str]:
    """Revert every active change to a listing (back to the original). Returns reverted ids."""
    active = [h for h in history(con, restaurant_id) if h["reverted_at"] is None]
    return revert(con, active[-1]["change_id"]) if active else []  # oldest active: reverts all after it
