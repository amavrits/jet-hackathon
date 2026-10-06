"""Structured LLM calls through LiteLLM: one helper that returns a validated Pydantic model.

Provider-agnostic: the model name picks the provider (`openai/gpt-6-luna`,
`anthropic/claude-sonnet-5-5`, ...). Env:
  MODEL_MAIN / MODEL_FAST   override the defaults below
  OPENAI_API_KEY / ANTHROPIC_API_KEY   read by LiteLLM directly
Defaults follow whichever key is present (OpenAI first).
"""

import json
import logging
import os
import re
import warnings
from collections.abc import Callable
from typing import Any, TypeVar

import litellm
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

load_dotenv()
log = logging.getLogger(__name__)

litellm.suppress_debug_info = True
litellm.drop_params = True  # drop params a model doesn't support (e.g. temperature on reasoning models)
# LiteLLM's usage object trips a harmless Pydantic serializer warning on every OpenAI call.
warnings.filterwarnings("ignore", message="Pydantic serializer warnings", category=UserWarning)

T = TypeVar("T", bound=BaseModel)

OPENAI_DEFAULT = "openai/gpt-6-luna"
ANTHROPIC_DEFAULT = "anthropic/claude-sonnet-5-5"


def _default_model() -> str:
    if os.environ.get("OPENAI_API_KEY"):
        return OPENAI_DEFAULT
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ANTHROPIC_DEFAULT
    return OPENAI_DEFAULT


def model_name(fast: bool = False) -> str:
    if fast:
        return os.environ.get("MODEL_FAST") or os.environ.get("MODEL_MAIN") or _default_model()
    return os.environ.get("MODEL_MAIN") or _default_model()


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(text: str) -> str:
    text = _FENCE.sub("", (text or "").strip())
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def call_structured(
    schema: type[T],
    system: str,
    user: str,
    *,
    fast: bool = False,
    max_tokens: int = 4096,
    completion: Callable[..., Any] | None = None,
) -> T | None:
    """Ask the model to fill `schema`.

    Attempt 1 uses the provider's JSON-schema mode. If the output fails validation, the error is
    sent back and attempt 2 asks for plain JSON. If that fails too, or the call itself fails
    (no key, network, rate limit after LiteLLM's retries), logs and returns None so the caller
    can fall back or skip.
    """
    completion = completion or litellm.completion
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": f"{system}\n\nRespond with JSON matching this schema:\n{json.dumps(schema.model_json_schema())}",
        },
        {"role": "user", "content": user},
    ]
    for attempt in (1, 2):
        kwargs: dict[str, Any] = {"model": model_name(fast), "messages": messages, "max_tokens": max_tokens}
        if attempt == 1:
            kwargs["response_format"] = schema
        try:
            raw = completion(**kwargs).choices[0].message.content or ""
        except Exception as err:  # LiteLLM maps every provider's errors to its own exception types
            log.error("%s: model call failed: %s", schema.__name__, err)
            return None
        try:
            return schema.model_validate_json(_extract_json(raw))
        except ValidationError as err:
            log.warning("%s: invalid output on attempt %d: %s", schema.__name__, attempt, str(err)[:300])
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"Validation failed, fix and resend only the JSON:\n{err}"},
            ]
    log.error("%s: skipped after 2 invalid attempts", schema.__name__)
    return None
