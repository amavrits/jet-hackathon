"""Shared plumbing for the MCP servers: database access, cached effects, CLI flags."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import duckdb
from mcp.server.mcpserver.exceptions import ToolError

from growth.data import queries as q
from growth.ml import effects as fx

_db_path: Path = Path(os.environ.get("JET_DB_PATH", "data/jet.duckdb"))
_effects_path: Path | None = None
_effects: dict[str, Any] | None = None


def configure(db_path: Path | str, effects_path: Path | str | None = None) -> None:
    """Point the server at a database (tests use a temporary one)."""
    global _db_path, _effects_path, _effects
    _db_path, _effects_path, _effects = Path(db_path), Path(effects_path) if effects_path else None, None


def con() -> duckdb.DuckDBPyConnection:
    """A fresh read-only connection per call. The servers never write."""
    return q.connect(_db_path, read_only=True)


def effects(refit: bool = False) -> dict[str, Any]:
    global _effects
    if _effects is None or refit:
        c = con()
        try:
            _effects = fx.load(c, _effects_path, refit=refit)
        finally:
            c.close()
    return _effects


def known_restaurant(c: duckdb.DuckDBPyConnection, rid: str) -> dict[str, Any]:
    """ToolError reaches the client as an error result with this message, not a server crash."""
    r = q.get_restaurant(c, rid)
    if r is None:
        raise ToolError(f"unknown restaurant_id {rid!r}")
    return r


def serve(mcp: Any, default_port: int) -> None:
    ap = argparse.ArgumentParser(description=mcp.name)
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--port", type=int, default=default_port)
    ap.add_argument("--db", default=str(_db_path))
    args = ap.parse_args()
    configure(args.db)
    if args.transport == "http":
        mcp.run(transport="streamable-http", host="127.0.0.1", port=args.port)
    else:
        mcp.run(transport="stdio")
