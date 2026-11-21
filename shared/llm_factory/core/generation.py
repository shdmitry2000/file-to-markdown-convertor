"""The one reader of the generation settings every LLM call uses, on every lane.

Settings (the same names in every module; a module changes a value, never a name):

- ``LLM_TEMPERATURE`` (default 1.0): sent on every call. A space's own value or a
  per-call value wins. Gemini 3+ is raised to 1.0 (below it, Google documents
  loops and degraded output) unless the caller marks its value exact — the
  evaluator's judge sends 0.0 on purpose, for repeatable scores.
- ``LLM_MAX_TOKENS`` (default 8192): max output tokens when a call sets none.
- ``LLM_REQUEST_TIMEOUT_SECONDS`` (default 300): one HTTP request — what the proxy
  lane always used. Single requests do run past 110 s (wiki compiles with thinking
  on, measured with no busy wait before them), so it is not lower. The callers' own
  300 s deadlines still bound a call; one that also waited out 429s is then reported
  as timed out. markdown-api sets 120: a page call may be retried on a timeout, and
  3 x 120 + 3 + 180 s stays under the converter's 600 s stall cut.
- ``LLM_QUERY_TIMEOUT_SECONDS`` (default 120): one LLM call made while a user waits
  (a template's /search or /answer), in all — attempts and busy waits together, and
  never past the time the request has left (core/deadline.py). Background work
  (ingest, compile, judge) keeps LLM_REQUEST_TIMEOUT_SECONDS and the busy wait.

Constants, not settings: thinking is OFF unless a call, or a client
(``enable_thinking``, set from a space's setting), asks for it — both lanes, every
module; asking gets the model's own default.

Before 1.4.8.5, each lane had its own hidden defaults: the proxy lane sent temperature
0.7, topP 0.95, topK 40 and an env max-token default, while vertex_ai sent none of
them, so the same space behaved differently per deployment.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

ENV_LLM_TEMPERATURE = "LLM_TEMPERATURE"
ENV_LLM_MAX_TOKENS = "LLM_MAX_TOKENS"
ENV_LLM_REQUEST_TIMEOUT_SECONDS = "LLM_REQUEST_TIMEOUT_SECONDS"
ENV_LLM_QUERY_TIMEOUT_SECONDS = "LLM_QUERY_TIMEOUT_SECONDS"

DEFAULT_TEMPERATURE = 1.0
DEFAULT_MAX_TOKENS = 8192
DEFAULT_REQUEST_TIMEOUT_SECONDS = 300.0
# One LLM call made while a user waits, busy-provider waits included (core/deadline.py).
DEFAULT_QUERY_TIMEOUT_SECONDS = 120.0

#: Gemini 3+ degrades below this. Google/LiteLLM warn that a lower temperature
#: "can cause infinite loops, degraded reasoning performance, and failure on
#: complex tasks" — observed as ~18s generations on gemini-3.5-flash at 0.7.
GEMINI3_MIN_TEMPERATURE = 1.0


def _env_number(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if value <= 0 and name != ENV_LLM_TEMPERATURE:
        logger.warning("%s=%r must be positive; using %s", name, raw, default)
        return default
    return value


def llm_temperature() -> float:
    return _env_number(ENV_LLM_TEMPERATURE, DEFAULT_TEMPERATURE)


def llm_max_tokens() -> int:
    return int(_env_number(ENV_LLM_MAX_TOKENS, DEFAULT_MAX_TOKENS))


def request_timeout() -> float:
    return _env_number(ENV_LLM_REQUEST_TIMEOUT_SECONDS, DEFAULT_REQUEST_TIMEOUT_SECONDS)


def query_timeout() -> float:
    """One LLM call on a user request, in all (attempts and busy waits together)."""
    return _env_number(ENV_LLM_QUERY_TIMEOUT_SECONDS, DEFAULT_QUERY_TIMEOUT_SECONDS)


def request_timeout_for(params: Any = None) -> float:
    """One request's timeout: the call's own (GenerationParams.timeout), else the
    lane's; inside a user request, cut to the time it has left (core/deadline.py)."""
    from .deadline import cap  # noqa: PLC0415 — deadline imports this module

    return cap(getattr(params, "timeout", None) or request_timeout())


def gemini_major_version(model: str) -> Optional[int]:
    """Major Gemini version in a model string (``vertex_ai/gemini-3.5-flash`` → 3),
    or None if it isn't Gemini."""
    match = re.search(r"gemini-(\d+)", model or "")
    return int(match.group(1)) if match else None


def floor_temperature(model: str, temperature: float, *, lane_name: str = "llm") -> float:
    """``temperature``, raised to the model's floor if it documents one."""
    major = gemini_major_version(model)
    if major is not None and major >= 3 and temperature < GEMINI3_MIN_TEMPERATURE:
        logger.warning(
            "[%s] raising temperature %.2f -> %.2f for %s: Gemini %d degrades below %.1f "
            "(infinite loops / slow, low-quality generations)",
            lane_name, temperature, GEMINI3_MIN_TEMPERATURE, model, major,
            GEMINI3_MIN_TEMPERATURE,
        )
        return GEMINI3_MIN_TEMPERATURE
    return temperature


def temperature_for(model: str, params: Any = None) -> float:
    """The temperature a call sends: the call's (or space's) own value, else
    ``LLM_TEMPERATURE``; floored for Gemini 3+ unless the call marked it exact."""
    value = getattr(params, "temperature", None)
    if value is not None and getattr(params, "exact_temperature", False):
        return value
    return floor_temperature(model, llm_temperature() if value is None else value)


def max_tokens_for(params: Any = None) -> int:
    return getattr(params, "max_tokens", None) or llm_max_tokens()


def thinking_on(params: Any = None, default: bool = False) -> bool:
    """Off unless asked for: by the call (``GenerationParams(thinking=...)`` wins), else
    by the client (``client.enable_thinking``, e.g. the wiki's per-space switch)."""
    value = getattr(params, "thinking", None)
    return default if value is None else value is True


def proxy_thinking_config(model: str) -> dict:
    """``thinkingConfig`` for "thinking off" on the proxy (Gemini API shape): budget 0,
    what production has always sent (THINKING_BUDGET=0 until 1.4.8.5).

    Not ``thinkingLevel: MINIMAL``, although the connector guide lists it:
    gemini-3.8-flash answers 400 to it through the connector. Never level and budget
    together: the connector answers 400 to that as well.
    """
    return {"thinkingBudget": 0}
