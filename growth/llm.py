"""Structured Claude calls: one helper that returns a validated Pydantic model."""

import logging
import os
from functools import cache
from typing import TypeVar

from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

load_dotenv()
log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_MAIN = "claude-sonnet-5-5"
DEFAULT_FAST = "claude-haiku-4-5-20251001"
TOOL_NAME = "submit"


def model_name(fast: bool = False) -> str:
    if fast:
        return os.environ.get("MODEL_FAST") or DEFAULT_FAST
    return os.environ.get("MODEL_MAIN") or DEFAULT_MAIN


@cache
def client() -> Anthropic:
    return Anthropic()  # reads ANTHROPIC_API_KEY


def call_structured(
    schema: type[T],
    system: str,
    user: str,
    *,
    fast: bool = False,
    max_tokens: int = 4096,
    llm: Anthropic | None = None,
) -> T | None:
    """Ask Claude to fill `schema` via forced tool use.

    Retries once if the output fails validation (the error is sent back to the
    model). If it fails again, logs and returns None so the caller can skip it.
    """
    llm = llm or client()
    tool = {
        "name": TOOL_NAME,
        "description": f"Submit the result as a {schema.__name__}.",
        "input_schema": schema.model_json_schema(),
    }
    messages: list[dict] = [{"role": "user", "content": user}]

    for attempt in (1, 2):
        response = llm.messages.create(
            model=model_name(fast),
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=[tool],
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )
        block = next((b for b in response.content if b.type == "tool_use"), None)
        try:
            if block is None:
                raise ValueError(f"no tool call in response (stop_reason={response.stop_reason})")
            return schema.model_validate(block.input)
        except (ValidationError, ValueError) as err:
            log.warning("%s: invalid output on attempt %d: %s", schema.__name__, attempt, err)
            if block is None:
                continue
            messages += [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": block.id, "name": TOOL_NAME, "input": block.input}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "is_error": True,
                            "content": f"Validation failed, fix and resubmit:\n{err}",
                        }
                    ],
                },
            ]

    log.error("%s: skipped after 2 invalid attempts", schema.__name__)
    return None
