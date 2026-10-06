"""Typed classification with TypeSafe Jev. The only module that imports typesafe_sdk.

Jev answers typed questions (Choice / Noul / Score) about a short `state` with calibrated
confidence. It does not write text; wording goes through growth.llm.

Env:
  JEV_API_KEY (or TYPESAFE_API_KEY)  - required for live calls
  JEV_MODEL                          - default "jev-latest"
  JEV_CACHE                          - default "data/cache/jev.json"; set to "" to disable

Results are cached on disk keyed by (model, state, questions), so repeat demo runs are instant
and a flaky network can't break the pitch.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Question, TypeSafeError

load_dotenv()

log = logging.getLogger(__name__)

JEV_MODEL: str = os.environ.get("JEV_MODEL", "jev-latest")
_CACHE_PATH = os.environ.get("JEV_CACHE", "data/cache/jev.json")
_CONCURRENCY = 16

# Answers are stored as plain dicts: {"<question>": {"type": "choice", "choice": ..., "confidence": ...}}
Answers = dict[str, dict[str, Any]]

__all__ = ["Choice", "Noul", "Answers", "ask_many", "JEV_MODEL"]


# --------------------------------------------------------------------- cache

_cache_lock = threading.Lock()
_cache: dict[str, Answers] | None = None


def _load_cache() -> dict[str, Answers]:
    global _cache
    if _cache is None:
        _cache = {}
        if _CACHE_PATH and Path(_CACHE_PATH).exists():
            try:
                _cache = json.loads(Path(_CACHE_PATH).read_text())
            except json.JSONDecodeError:
                log.warning("jev: ignoring corrupt cache %s", _CACHE_PATH)
    return _cache


def _save_cache() -> None:
    if not _CACHE_PATH or _cache is None:
        return
    path = Path(_CACHE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_cache))


def _key(model: str, state: Any, questions: Mapping[str, Question]) -> str:
    qs = {k: (v.model_dump() if hasattr(v, "model_dump") else v) for k, v in questions.items()}
    blob = json.dumps({"m": model, "s": state, "q": qs}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------------- calls


def _api_key() -> str | None:
    return os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")


async def _ask_all(items: Sequence[tuple[Any, Mapping[str, Question]]], model: str) -> list[Answers | None]:
    cache = _load_cache()
    keys = [_key(model, s, q) for s, q in items]
    out: list[Answers | None] = [cache.get(k) for k in keys]
    todo = [i for i, a in enumerate(out) if a is None]
    if not todo:
        return out

    key = _api_key()
    if not key:
        log.error("jev: JEV_API_KEY not set; %d uncached items skipped", len(todo))
        return out

    sem = asyncio.Semaphore(_CONCURRENCY)
    async with AsyncTypeSafeClient(api_key=key, model=model) as client:

        async def one(i: int) -> None:
            state, questions = items[i]
            async with sem:
                try:
                    resp = await client.system_one(state, questions)
                except TypeSafeError as e:
                    log.warning("jev: item %d failed: %s", i, e)
                    return
            out[i] = {name: ans.model_dump() for name, ans in resp.answers.items()}

        await asyncio.gather(*(one(i) for i in todo))

    with _cache_lock:
        for i in todo:
            if out[i] is not None:
                cache[keys[i]] = out[i]
        _save_cache()
    return out


def ask_many(items: Sequence[tuple[Any, Mapping[str, Question]]], *, model: str = JEV_MODEL) -> list[Answers | None]:
    """Ask each (state, questions) pair in parallel. Order preserved; failures are None.

    Safe to call from sync code whether or not an event loop is already running
    (Streamlit, LangGraph): in that case it runs on a worker thread.
    """
    if not items:
        return []
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_ask_all(items, model))
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _ask_all(items, model)).result()
