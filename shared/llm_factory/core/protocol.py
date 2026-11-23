"""LLM client contract.

All concrete clients (claude_code, litellm, gemini_proxy, vertex_proxy, legacy bridges)
implement this Protocol. Callers depend only on the Protocol — never on a concrete class —
so swapping providers or backends is a factory decision, not a caller decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class LLMResult:
    """Unified return type across every provider.

    - `text`  : the raw model response (always populated).
    - `data`  : parsed JSON (only populated by the JSON methods).
    - `cost_usd`: billing cost when the provider reports it; 0.0 otherwise.
    - `model` : the model identifier actually used (useful for logging).
    - `raw`   : the provider-native response object, for debugging only.
    - `truncated`: the reply stopped at the output-token cap (set by the JSON methods).
      A cut-off JSON reply is repaired and parses, so without it the caller cannot
      tell a short answer from a cut one.
    """

    text: str
    data: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    model: str = ""
    raw: object | None = None
    truncated: bool = False


@dataclass(frozen=True)
class GenerationParams:
    """Per-call generation parameters, already resolved by the caller.

    Lives here rather than in rag_core so provider clients can accept it without
    depending on the settings layer. Every field is optional: None means "use the
    provider's own default", which is what lets a caller override one knob without
    restating the rest.

    Exists because the proxy lane could not be told any of this — it built its
    generationConfig from env vars and hardcoded constants, so per-space settings
    were silently discarded in exactly the deployment that uses it.
    """

    #: None = LLM_TEMPERATURE. Raised to 1.0 on Gemini 3+ unless exact_temperature.
    temperature: float | None = None
    #: True = send temperature as given, no Gemini 3 floor (the judge's 0.0).
    exact_temperature: bool = False
    #: None = LLM_MAX_TOKENS.
    max_tokens: int | None = None
    #: Off unless True; True = the model's own default.
    thinking: bool | None = None
    #: Seconds for this call's HTTP request; None = LLM_REQUEST_TIMEOUT_SECONDS. For a
    #: caller with its own longer deadline (the wiki's compile).
    timeout: float | None = None
    top_p: float | None = None
    top_k: int | None = None


@runtime_checkable
class LLMClient(Protocol):
    """Contract every provider client must satisfy.

    All methods are async. Providers that have only sync SDKs should wrap
    them with `asyncio.to_thread` rather than forcing callers to think about it.
    """

    provider: str  # human-readable provider label (e.g. "gemini/gemini-2.5-flash")

    async def text(self, prompt: str, system: str = "") -> LLMResult:
        """Plain text completion."""
        ...

    async def json(self, prompt: str, system: str = "") -> LLMResult:
        """JSON-mode completion. The returned LLMResult.data is parsed JSON."""
        ...

    async def embedding(self, text: str | list[str]) -> list[list[float]]:
        """Return one embedding vector per input string (always a list of lists)."""
        ...
