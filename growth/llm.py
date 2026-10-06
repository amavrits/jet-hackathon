"""Single entry point for LLM calls. Agents never call a provider SDK directly.

Everything goes through LiteLLM, so the provider is picked by the model name in env:
  MODEL_MAIN  - reasoning work (default: gpt-luna)
  MODEL_FAST  - cheap per-item work like review tagging (default: MODEL_MAIN)
  LLM_API_BASE / LLM_API_KEY - optional, for a LiteLLM proxy or OpenAI-compatible endpoint

`structured()` returns a validated Pydantic model or None. It retries once with the
validation error fed back, then gives up and logs - never raises on bad model output.
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

import litellm
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

load_dotenv()

log = logging.getLogger(__name__)

MODEL_MAIN: str = os.environ.get("MODEL_MAIN", "gpt-luna")
MODEL_FAST: str = os.environ.get("MODEL_FAST", MODEL_MAIN)

_EXTRA: dict[str, str] = {
    k: v
    for k, v in {
        "api_base": os.environ.get("LLM_API_BASE"),
        "api_key": os.environ.get("LLM_API_KEY"),
    }.items()
    if v
}

litellm.suppress_debug_info = True
litellm.drop_params = True  # silently drop params a provider doesn't support (e.g. temperature)

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(text: str) -> str:
    """Strip code fences and anything before the first '{' / after the last '}'."""
    text = _FENCE.sub("", text.strip())
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def _complete(model: str, messages: list[dict[str, str]], schema: type[BaseModel] | None) -> str:
    kwargs: dict = {"model": model, "messages": messages, "temperature": 0, **_EXTRA}
    if schema is not None:
        kwargs["response_format"] = schema  # LiteLLM converts Pydantic -> provider json_schema
    resp = litellm.completion(**kwargs)
    return resp.choices[0].message.content or ""


def structured(
    schema: type[T],
    system: str,
    user: str,
    *,
    model: str = MODEL_MAIN,
) -> T | None:
    """Ask the model for a `schema` instance. One retry on invalid output, then None."""
    schema_json = json.dumps(schema.model_json_schema())
    messages = [
        {"role": "system", "content": f"{system}\n\nRespond with JSON matching this schema:\n{schema_json}"},
        {"role": "user", "content": user},
    ]

    last_error = ""
    for attempt in (1, 2):
        try:
            raw = _complete(model, messages, schema if attempt == 1 else None)
            return schema.model_validate_json(_extract_json(raw))
        except (ValidationError, json.JSONDecodeError) as e:
            last_error = str(e)
            log.warning("llm: invalid %s on attempt %d: %s", schema.__name__, attempt, last_error[:300])
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": f"That was not valid. Error:\n{last_error}\n\nReturn only JSON matching the schema.",
                }
            )
        except Exception as e:  # provider/network error: don't retry, the SDK already did
            log.error("llm: %s call failed for %s: %s", model, schema.__name__, e)
            return None

    log.error("llm: giving up on %s after 2 attempts: %s", schema.__name__, last_error[:300])
    return None


def structured_many(
    schema: type[T],
    system: str,
    users: list[str],
    *,
    model: str = MODEL_FAST,
    workers: int = 8,
) -> list[T | None]:
    """`structured()` over many inputs in parallel. Order is preserved; failures are None."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda u: structured(schema, system, u, model=model), users))
