"""Direct Gemini client via google-genai SDK (bypasses litellm).

Use factory scheme ``google_genai/<model>`` — not ``gemini/``, which is reserved
for LiteLLM's model string.

Uses ``GOOGLE_API_KEY`` or ``GEMINI_API_KEY``. No proxy, no LiteLLM routing.
"""
from __future__ import annotations

import json
import os
import re

from ...core import config
from ...core import generation as _generation
from ...core.protocol import GenerationParams, LLMClient, LLMResult
from ...core.resilience import resilient, within_deadline
from ._embedding_batch import embed_in_batches


class GeminiClient:
    """LLMClient implementation using the google-genai SDK directly."""

    def __init__(self, model: str | None = None) -> None:
        mid = (model or "").strip() or config.default_gemini_model_id()
        self.model = mid
        self.provider = f"gemini/{mid}"
        self._clients: dict[str, object] = {}  # one SDK client per API key
        # Optional pinned connector creds (out-of-request callers), as on the other clients.
        self.pinned_creds = None

    async def _sdk(self):
        """The SDK client for this call's key: the space's own google_genai key when it
        has one, else GOOGLE_API_KEY / GEMINI_API_KEY. Never another type's token."""
        from google import genai

        key = None
        try:
            from shared.connector_credentials import resolve_llm_connector, space_token_for
            from shared.request_context import get_current_space

            creds = self.pinned_creds or await resolve_llm_connector(get_current_space())
            key = space_token_for(creds, "google_genai")
        except Exception:  # pragma: no cover - resolve is fail-open
            key = None
        key = key or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("no Google API key: set the space's key, GOOGLE_API_KEY or GEMINI_API_KEY")
        client = self._clients.get(key)
        if client is None:
            client = self._clients[key] = genai.Client(api_key=key)
        return client

    @resilient
    async def text(
        self, prompt: str, system: str = "", *, generation: GenerationParams | None = None,
    ) -> LLMResult:
        from google.genai import types

        cfg = types.GenerateContentConfig(
            system_instruction=system or None,
            temperature=_generation.temperature_for(self.model, generation),
            max_output_tokens=_generation.max_tokens_for(generation),
            http_options=types.HttpOptions(timeout=int(_generation.request_timeout_for(generation) * 1000)),
            # Off unless asked, as on every lane: budget 0, the payload the proxy sends.
            thinking_config=(None if _generation.thinking_on(generation)
                             else types.ThinkingConfig(thinking_budget=0)),
        )
        response = await (await self._sdk()).aio.models.generate_content(
            model=self.model,
            contents=prompt,
            config=cfg,
        )
        text = response.text or ""
        cost = self._estimate_cost(response)
        return LLMResult(text=text, data={}, cost_usd=cost, model=self.model, raw=response)

    @resilient
    async def json(
        self, prompt: str, system: str = "", *, generation: GenerationParams | None = None,
    ) -> LLMResult:
        result = await self.text(prompt, system, generation=generation)
        result.data = _parse_json(result.text)
        return result

    async def embedding(self, text: str | list[str]) -> list[list[float]]:
        inputs = [text] if isinstance(text, str) else list(text)

        async def _one(batch: list[str]) -> list[list[float]]:
            response = await within_deadline((await self._sdk()).aio.models.embed_content(
                model="gemini-embedding-001",
                contents=batch,
            ), _generation.request_timeout_for(None), "GeminiClient.embedding")
            return [e.values for e in response.embeddings]

        return await embed_in_batches(inputs, _one)

    @staticmethod
    def _estimate_cost(response) -> float:
        try:
            meta = response.usage_metadata
            in_tok = meta.prompt_token_count or 0
            out_tok = meta.candidates_token_count or 0
            return (in_tok * 0.075 + out_tok * 0.30) / 1_000_000
        except Exception:
            return 0.0


def _parse_json(text: str) -> dict:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as e:
        return {"__parse_error__": str(e), "__raw_text_preview__": text[:500]}
