"""LiteLLM-backed client — the generic path for every non-Claude-Code provider.

LiteLLM routes to 100+ providers behind a single `acompletion` call:
OpenAI, Gemini (direct API via GOOGLE_API_KEY), Vertex AI, Anthropic API,
Groq, Mistral, Cohere, Bedrock, Azure, Ollama, vLLM, LM Studio, etc.

Provider string format is LiteLLM's native model string:
    openai/gpt-4o-mini
    gemini/gemini-2.5-flash
    vertex_ai/gemini-2.5-pro
    anthropic/claude-opus-4-7
    groq/llama-3.3-70b-versatile
    ollama/llama3.1:8b

See https://docs.litellm.ai/docs/providers for the full list.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from ...core.protocol import GenerationParams, LLMClient, LLMResult
from ...core import generation
from ...core.resilience import resilient, resilient_stream, resilient_sync, within_deadline
from ...core.json_loads import loads_tolerant
from ._embedding_batch import embed_in_batches
from . import _adc_timeout

# Bound the ADC token-refresh timeout as soon as this backend is in play.
# google-auth would otherwise block 120s per refresh, which is how one
# network blip cost 11 of 30 evaluation questions. See _adc_timeout.
_adc_timeout.install()


def generation_kwargs(
    params: "GenerationParams | None", model: str = "", *, thinking: bool = False,
) -> dict[str, Any]:
    """GenerationParams → litellm call kwargs, with the shared defaults filled in
    (core/generation.py): temperature and max tokens are always sent, and thinking
    is off unless the call asked for it — the same as the proxy lane sends.

    ``model`` is the model actually called: the Gemini 3 temperature floor needs it.
    ``thinking`` is the client's default (``enable_thinking``) when the call sets none.
    top_p / top_k are sent only when set.
    """
    kwargs: dict[str, Any] = {
        "temperature": generation.temperature_for(model, params),
        "max_tokens": generation.max_tokens_for(params),
    }
    for field in ("top_p", "top_k", "timeout"):
        value = getattr(params, field, None)
        if value is not None:
            kwargs[field] = value
    # Gemini only: other providers (openai gpt-4o / gpt-4.1-mini, ...) reject
    # reasoning_effort outright, and their thinking is not on by default anyway.
    if not generation.thinking_on(params, thinking) and generation.gemini_major_version(model):
        kwargs.update(thinking_off_kwargs())
    return kwargs


def _with_defaults(model: str, extra: dict[str, Any], thinking: bool = False) -> dict[str, Any]:
    """``extra`` (litellm kwargs a caller already built) over the shared defaults.
    A caller that built kwargs decided thinking itself — the answer path leaves the
    thinking keys out when the space turned thinking ON — so the default thinking
    setting is added only when there is no caller's kwargs at all."""
    defaults = generation_kwargs(None, model, thinking=thinking)
    if extra:
        defaults = {k: v for k, v in defaults.items() if k not in THINKING_KEYS}
    return {**defaults, **extra}


def thinking_off_kwargs() -> dict[str, Any]:
    """Canonical litellm params that turn Gemini extended thinking off.

    Google's own ``thinkingConfig: {thinkingBudget: 0}``, handed to litellm as is
    (litellm keeps a ``thinkingConfig`` param and puts it in generationConfig, on
    vertex_ai and gemini alike) — the same value the proxy lane sends
    (core/generation.py ``proxy_thinking_config``). This is the ONLY place the
    incantation is spelled; every lane that disables thinking (the answer path's
    per-call generation kwargs, this client's ``disable_thinking`` flag) must route
    through here so a provider-side change lands in one edit.

    Measured 2026-10-31 on Vertex (`global`), thinking tokens of one reasoning prompt:

        param                      gemini-3.8-flash   gemini-3.5-flash
        (none)                     487                490
        thinkingConfig budget 0    0                  0
        reasoning_effort="none"    400 "Thinking level is unsupported: MINIMAL"   0
        thinking budget_tokens=0   438 (ignored)      490 (ignored)

    gemini-3.8-flash cannot turn thinking fully off: with budget 0 (or level LOW) an
    arithmetic question still thinks 34-62 tokens, against 300-600 with thinking left on.

    reasoning_effort="none" (until 1.4.8.5) is mapped by litellm to thinkingLevel
    MINIMAL on 3.x, which gemini-3.8-flash refuses — every thinking-off call on the
    vertex_ai lane failed with 3.8.

    Earlier this sent thinking={"type":"enabled","budget_tokens":0}, which is
    Gemini 2.5 semantics: 3.x takes a thinkingLevel, ignores the budget, and says
    nothing about it. Measured on gemini-3.5-flash — reasoning tokens, not latency,
    because latency alone cannot tell "honored" from "ignored":

        budget_tokens=0        reasoning 340-490  (baseline 347-484) — NO EFFECT
        reasoning_effort=none  reasoning 0/0/0                       — off

    Every judge call in an evaluation was therefore paying full extended thinking
    while the code read as though it had already been turned off.

    NB the near-misses that silently leave thinking ON: thinking={"type":"disabled"}
    leaves the budget unset, and reasoning_effort="minimal" keeps includeThoughts
    on for 3.x (and is a small NON-zero budget on 2.5).
    """
    return {"thinkingConfig": {"thinkingBudget": 0}}


# Every key that sets thinking. litellm rejects a request that carries both a
# thinking budget and a thinking level, so callers merging their own params must
# check ALL of these before adding ours — and the retry paths that strip thinking
# on an unsupported-param error must strip all of them.
THINKING_KEYS = ("thinkingConfig", "reasoning_effort", "thinking")


class LiteLLMClient(LLMClient):
    """Thin wrapper around litellm.acompletion / litellm.aembedding.

    All provider-specific auth (OPENAI_API_KEY, GOOGLE_API_KEY, ANTHROPIC_API_KEY,
    GROQ_API_KEY, ...) is picked up by LiteLLM from standard env vars.
    """

    def __init__(
        self,
        model: str,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        embedding_model: str | None = None,
    ) -> None:
        """Initialise the client.

        Args:
            model: LiteLLM model string, e.g. "gemini/gemini-2.5-flash".
            api_base: Override the provider's base URL (useful for compatible
                endpoints, self-hosted gateways, or the Raw Connector proxy).
            api_key: Override the API key (otherwise read from env by LiteLLM).
            extra_headers: Extra HTTP headers to send on every request.
            embedding_model: Optional separate model for embeddings. Defaults
                to a provider-appropriate embedding model.
        """
        self.provider = model
        self._model = model
        self._api_base = api_base
        self._api_key = api_key
        self._extra_headers = extra_headers or {}
        self._embedding_model = embedding_model or _default_embedding_model(model)
        # Optional pinned connector creds (set by build_space_clients for out-of-request
        # callers like the evaluator). When set, used instead of the request contextvar
        # so per-space creds survive thread pools / framework internals.
        self.pinned_creds = None
        # Opt-in: force Gemini thinking off on every call, via whatever
        # thinking_off_kwargs() currently spells (do not restate the mechanism here —
        # a comment naming the wrong param is what let "thinking is off" read as true
        # while every call still paid for it). Used by the evaluator's LLM-as-judge,
        # where thinking dominates latency for no scoring benefit. Off by default so
        # the answer lane is unaffected.
        self.disable_thinking = False
        # Opt-in the other way: thinking ON when a call sets none (it is off by default,
        # core/generation.py). The wiki sets it from its per-space switch.
        self.enable_thinking = False

    def _apply_thinking(self, kwargs: dict) -> dict:
        """Apply the client-wide thinking-off flag, if set.

        A caller that already asked for a thinking setting wins — by ANY of the
        spellings in THINKING_KEYS, not just the one we happen to emit. Adding ours
        on top would either duplicate a keyword at the call site (TypeError) or send
        litellm a budget and a level together, which it rejects outright.
        """
        if self.disable_thinking and not any(k in kwargs for k in THINKING_KEYS):
            return {**kwargs, **thinking_off_kwargs()}
        return kwargs

    def _call_kwargs(self, model: str | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"timeout": generation.request_timeout()}
        if self._api_base:
            kwargs["api_base"] = self._api_base
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._extra_headers:
            kwargs["extra_headers"] = self._extra_headers
        # Some Vertex models are only served on a non-default regional endpoint
        # (e.g. gemini-3.x is global-only). Pin the region per-model so direct
        # vertex_ai calls don't 404 when the platform default is regional.
        loc = vertex_location_for(model or self._model)
        if loc:
            kwargs["vertex_location"] = loc
            proj = os.getenv("VERTEXAI_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
            if proj:
                kwargs["vertex_project"] = proj
        return kwargs

    async def _call_kwargs_async(self, model: str | None = None) -> dict[str, Any]:
        """`_call_kwargs` plus per-space GCP project/location for the ``vertex_ai``
        lane, resolved from the request-bound SPACE. Falls back to the env-based
        values when there is no space / row / KEK. Non-vertex_ai schemes and the
        sync ``vision_sync`` path keep the plain env-based `_call_kwargs`.
        """
        kwargs = self._call_kwargs(model)
        m = model or self._model
        scheme = m.split("/", 1)[0] if "/" in m else ""
        from ...core.provider_profiles import PROVIDER_PROFILES

        profile = PROVIDER_PROFILES.get(scheme)
        token_scheme = bool(profile and profile.token_env) and "api_key" not in kwargs
        if scheme != "vertex_ai" and not token_scheme:
            return kwargs
        try:
            from shared.request_context import get_current_space
            from shared.connector_credentials import resolve_llm_connector, space_token_for

            # Pinned creds (out-of-request callers) win; else resolve from the
            # request-bound SPACE (template > space).
            creds = self.pinned_creds or await resolve_llm_connector(get_current_space())
            if token_scheme:
                # The space's own key when it was entered for this type (openai,
                # gemini, anthropic); otherwise litellm reads the type's env variable.
                token = space_token_for(creds, scheme)
                if token:
                    kwargs["api_key"] = token
                return kwargs
            if creds.project:
                kwargs["vertex_project"] = creds.project
            # gemini-3.x is pinned to `global` by _call_kwargs — don't override
            # its region; otherwise honor the resolved location.
            if creds.location and "vertex_location" not in kwargs:
                kwargs["vertex_location"] = creds.location
        except Exception:  # pragma: no cover - never break an LLM call
            pass
        return kwargs

    def set_embedding_spec(self, spec: str) -> None:
        """Embed with ``spec`` (``<provider>/<model>``, as litellm routes it)."""
        self._embedding_model = spec

    # -- context cache (core/context_cache.py): vertex_ai only -------------------
    # Created with our own call, then named per request via litellm's
    # ``cached_content``. Not litellm's ``cache_control`` route: that LISTs every
    # cache of the project on every call to find one by display name.

    async def _cache_target(self) -> tuple[str, str] | None:
        """(project, location) the cache and its calls go to; None off vertex_ai."""
        if not self._model.startswith("vertex_ai/"):
            return None
        kwargs = await self._call_kwargs_async()
        project = kwargs.get("vertex_project") or os.getenv("VERTEXAI_PROJECT") \
            or os.getenv("GOOGLE_CLOUD_PROJECT")
        location = kwargs.get("vertex_location") or os.getenv("VERTEXAI_LOCATION") \
            or os.getenv("VERTEX_LOCATION") or "us-central1"
        if not project:
            _, project = await asyncio.to_thread(_adc_token)
        return (project, location) if project else None

    async def create_cache(self, document: str, system: str, ttl_seconds: int) -> tuple[int, str, str | None]:
        """``POST .../cachedContents`` — (status, body, cache name)."""
        import httpx

        target = await self._cache_target()
        if target is None:
            return 400, f"context caching needs the vertex_ai lane, not {self._model}", None
        project, location = target
        token, _ = await asyncio.to_thread(_adc_token)
        body: dict[str, Any] = {
            "model": f"projects/{project}/locations/{location}/publishers/google/models/"
                     f"{self._model.split('/', 1)[1]}",
            "contents": [{"role": "user", "parts": [{"text": document}]}],
            "ttl": f"{ttl_seconds}s",
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        async with httpx.AsyncClient(timeout=generation.request_timeout()) as client:
            response = await client.post(
                f"{_vertex_host(location)}/v1/projects/{project}/locations/{location}/cachedContents",
                json=body, headers={"Authorization": f"Bearer {token}"})
        name = response.json().get("name") if response.is_success else None
        return response.status_code, response.text, name

    async def delete_cache(self, name: str) -> None:
        import httpx

        token, _ = await asyncio.to_thread(_adc_token)
        async with httpx.AsyncClient(timeout=generation.request_timeout()) as client:
            response = await client.delete(f"{_vertex_host(_cache_location(name))}/v1/{name}",
                                           headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()

    async def extend_cache(self, name: str, ttl_seconds: int) -> None:
        """Set the cache's TTL to ``ttl_seconds`` from now."""
        import httpx

        token, _ = await asyncio.to_thread(_adc_token)
        async with httpx.AsyncClient(timeout=generation.request_timeout()) as client:
            response = await client.patch(
                f"{_vertex_host(_cache_location(name))}/v1/{name}?updateMask=ttl",
                json={"ttl": f"{ttl_seconds}s"}, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()

    @staticmethod
    def _with_cache(kwargs: dict, cached: str | None) -> dict:
        if cached:
            kwargs["cached_content"] = cached
        return kwargs

    @resilient
    async def text(
        self,
        prompt: str,
        system: str = "",
        *,
        generation: GenerationParams | None = None,
        cached: str | None = None,
    ) -> LLMResult:
        import litellm

        resp = await litellm.acompletion(
            model=self._model,
            messages=_messages(system, prompt),
            **self._with_cache(self._apply_thinking(
                {**await self._call_kwargs_async(),
                 **generation_kwargs(generation, self._model, thinking=self.enable_thinking)}
            ), cached),
        )
        content = (resp.choices[0].message.content or "")
        cost = _cost_from_response(resp)
        return LLMResult(text=content, cost_usd=cost, model=self._model, raw=resp)

    @resilient_stream
    async def stream(self, prompt: str, system: str = "", **extra):
        """Stream text deltas via litellm ``stream=True``.

        Yields non-empty content deltas as they arrive, so the shared answer path
        (:func:`shared.rag_core.answer.stream_from_hits`) emits real token-by-token
        SSE for every litellm provider (vertex_ai, gemini, openai, …) — not just
        vertex_proxy. Same model/region/auth kwargs as :meth:`text`; ``extra`` carries
        per-call generation kwargs (max_tokens/temperature/thinking) from the caller.
        """
        import litellm

        # Merge before expanding: `**a, **b` with a shared key (both can carry
        # `thinking`) is a TypeError at the call site, not a last-wins merge.
        call_kwargs = self._apply_thinking(
            {**await self._call_kwargs_async(), **_with_defaults(self._model, extra, self.enable_thinking)}
        )
        resp = await litellm.acompletion(
            model=self._model,
            messages=_messages(system, prompt),
            stream=True,
            **call_kwargs,
        )
        async for chunk in resp:
            try:
                delta = chunk.choices[0].delta.content
            except (AttributeError, IndexError, TypeError):
                continue
            if delta:
                yield delta

    @resilient
    async def json(
        self,
        prompt: str,
        system: str = "",
        *,
        generation: GenerationParams | None = None,
        cached: str | None = None,
    ) -> LLMResult:
        """JSON-mode completion.

        ``generation`` was added because json() is the enricher's only entry point,
        and without it the enricher's configured model was the sole setting that
        could take effect — temperature and thinking had nowhere to go, so ingest
        ran at the provider default no matter what the space configured.
        """
        import litellm

        # Merge before expanding: both the caller's params and _apply_thinking can
        # carry `thinking`, and `**a, **b` with a shared key is a TypeError at the
        # call site rather than a last-wins merge.
        call_kwargs = self._with_cache(self._apply_thinking(
            {**await self._call_kwargs_async(),
                 **generation_kwargs(generation, self._model, thinking=self.enable_thinking)}
        ), cached)
        resp = await litellm.acompletion(
            model=self._model,
            messages=_messages(system, prompt),
            response_format={"type": "json_object"},
            **call_kwargs,
        )
        content = (resp.choices[0].message.content or "")
        data = loads_tolerant(
            content,
            model=self._model,
            source=" despite response_format=json_object",
        )
        cost = _cost_from_response(resp)
        return LLMResult(text=content, data=data, cost_usd=cost, model=self._model, raw=resp)

    @resilient_sync
    def vision_sync(
        self,
        prompt: str,
        images_b64: list[str],
        *,
        system: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Blocking multimodal completion: prompt + base64 PNG page image(s) -> text.

        Synchronous on purpose — the PDF→markdown converters run in a sync
        subprocess and fan pages out across threads, so a blocking call avoids
        per-page event-loop churn. Model, region (vertex_location_for) and
        provider auth all come from this client, so callers don't re-derive them.
        """
        import litellm

        content: list[dict] = [{"type": "text", "text": prompt}]
        for b64 in images_b64:
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"}})
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

        kwargs = {**self._call_kwargs(), **generation_kwargs(
            GenerationParams(temperature=temperature, max_tokens=max_tokens), self._model,
            thinking=self.enable_thinking)}

        try:
            resp = litellm.completion(model=self._model, messages=messages, **kwargs)
        except Exception as exc:
            # Not every model can disable thinking (gemini-2.5-pro rejects
            # thinking_budget=0). Drop the thinking setting and retry once.
            msg = str(exc).lower()
            if any(k in kwargs for k in THINKING_KEYS) and ("thinking" in msg or "reasoning" in msg):
                for key in THINKING_KEYS:
                    kwargs.pop(key, None)
                resp = litellm.completion(model=self._model, messages=messages, **kwargs)
            else:
                raise
        return resp.choices[0].message.content or ""

    async def embedding(self, text: str | list[str]) -> list[list[float]]:
        import litellm

        inputs = [text] if isinstance(text, str) else list(text)

        async def _one(batch: list[str]) -> list[list[float]]:
            resp = await within_deadline(litellm.aembedding(
                model=self._embedding_model,
                input=batch,
                **await self._call_kwargs_async(self._embedding_model),
            ), generation.request_timeout_for(None), f"{type(self).__name__}.embedding")
            return [item["embedding"] for item in resp.data]

        return await embed_in_batches(inputs, _one)


_adc_credentials = None
_adc_project: str | None = None


def _adc_token() -> tuple[str, str | None]:
    """(access token, ADC project) — the identity litellm's vertex_ai calls use.
    Blocking (a refresh is an HTTP call); run it in a thread."""
    global _adc_credentials, _adc_project
    import google.auth
    from google.auth.transport.requests import Request

    if _adc_credentials is None:
        _adc_credentials, _adc_project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not _adc_credentials.valid:
        _adc_credentials.refresh(Request())
    return _adc_credentials.token, _adc_project


def _vertex_host(location: str) -> str:
    return ("https://aiplatform.googleapis.com" if location == "global"
            else f"https://{location}-aiplatform.googleapis.com")


def _cache_location(name: str) -> str:
    """``projects/{p}/locations/{loc}/cachedContents/{id}`` → loc."""
    parts = name.split("/")
    return parts[3] if len(parts) > 3 else "global"


def _messages(system: str, prompt: str) -> list[dict]:
    msgs: list[dict] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    return msgs


def _cost_from_response(resp: Any) -> float:
    try:
        hidden = getattr(resp, "_hidden_params", None) or {}
        cost = hidden.get("response_cost")
        if isinstance(cost, (int, float)):
            return float(cost)
    except Exception:
        pass
    try:
        import litellm
        return float(litellm.completion_cost(completion_response=resp) or 0.0)
    except Exception:
        return 0.0


def vertex_location_for(model: str) -> str | None:
    """Return the Vertex region a model must use, or None for the env default.

    gemini-3.x is currently served only on the `global` endpoint (not the
    regional ones like us-central1), so direct vertex_ai calls for those models
    must pin `vertex_location="global"`. Everything else (2.5-*, embeddings)
    uses the platform's default region via the env.
    """
    prefix, _, name = model.partition("/")
    if prefix == "vertex_ai" and name.startswith("gemini-3"):
        return "global"
    return None


def _default_embedding_model(completion_model: str) -> str:
    """Pick a reasonable embedding model for a given completion provider."""
    prefix = completion_model.split("/", 1)[0] if "/" in completion_model else ""
    if prefix == "openai":
        return "openai/text-embedding-3-small"
    if prefix in ("gemini", "vertex_ai"):
        return f"{prefix}/gemini-embedding-001"
    if prefix == "cohere":
        return "cohere/embed-english-v3.0"
    return "openai/text-embedding-3-small"
