"""Gemini via the Raw Connector proxy.

Default (the on-prem connector guide) — Gemini-API path, no project or region in the
URL (the connector supplies those):
    {PROXY_BASE_URL}/api/connectors/{CONNECTOR_ID}/v1/models/{MODEL}:generateContent
    {PROXY_BASE_URL}/api/connectors/{CONNECTOR_ID}/v1/models/{MODEL}:streamGenerateContent?alt=sse
    {PROXY_BASE_URL}/api/connectors/{CONNECTOR_ID}/v1/models/{EMBEDDING_MODEL}:predict

Required env vars:
    PROXY_BASE_URL          - Raw Connector host
    PROXY_CONNECTOR_ID      - Connector ID
    PROXY_AUTH_TOKEN        - Bearer token

Optional env vars:
    GOOGLE_CLOUD_PROJECT_ID - GCP project ID (or GOOGLE_CLOUD_PROJECT); the proxy
                              does not need it
    EMBEDDING_TYPE          - Embedding model (default: gemini-embedding-001)
    LLM_TEMPERATURE / LLM_MAX_TOKENS / LLM_REQUEST_TIMEOUT_SECONDS - see core/generation.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, NamedTuple

from ...core import config, gemini_response
from ...core.json_loads import loads_tolerant
from ...core.protocol import GenerationParams, LLMClient, LLMResult
from ...core import generation as _generation
from ...core.resilience import resilient, resilient_stream, resilient_sync, within_deadline
from ._embedding_batch import embed_in_batches

logger = logging.getLogger(__name__)

# Embedding retries on 429/504 (settings until 1.4.8.5, never changed anywhere).
_EMBED_MAX_RETRIES = 5
_EMBED_RETRY_DELAY_SECONDS = 2.0


class _ResolvedCreds(NamedTuple):
    """Per-request Raw-Connector creds + GCP project/location (per-space or global)."""
    base_url: str
    connector_id: str
    auth_token: str
    project: str | None
    location: str


def _truncate_body(text: str, limit: int = 1200) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…"


def _raise_for_status_with_detail(response: Any, url: str, what: str) -> None:
    """Raise with HTTP status, URL (no secrets), and response body for debugging."""
    import httpx

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = response.text or ""
        except Exception:
            pass
        msg = (
            f"vertex_proxy {what} failed: HTTP {response.status_code} for {url!r}. "
            f"Response: {_truncate_body(body)}"
        )
        raise RuntimeError(msg) from e


def build_vertex_proxy_client(
    model: str | None = None,
    *,
    embedding_model: str | None = None,
) -> LLMClient:
    """Construct an LLMClient that uses Vertex AI format via Raw Connector."""
    mid = (model or "").strip() or config.default_gemini_model_id()
    emb = (
        (embedding_model or "").strip()
        or config.default_embedding_model_id()
    )

    base = os.environ.get("PROXY_BASE_URL")
    connector = os.environ.get("PROXY_CONNECTOR_ID")
    token = os.environ.get("PROXY_AUTH_TOKEN")
    project = config.gcp_project_id_for_vertex_proxy()
    location = os.environ.get("VERTEX_LOCATION", "us-central1")

    if not all([base, connector, token]):
        raise RuntimeError(
            "vertex_proxy requires PROXY_BASE_URL, PROXY_CONNECTOR_ID, and PROXY_AUTH_TOKEN."
        )

    return VertexProxyClient(
        base_url=base,
        connector_id=connector,
        auth_token=token,
        project_id=project,
        location=location,
        model=mid,
        embedding_model=emb,
    )


class VertexProxyClient:
    """LLM client for Raw Connector using Vertex AI format.

    Implements the LLMClient Protocol but makes HTTP requests directly
    in Vertex AI REST format instead of delegating to LiteLLM.
    """

    def __init__(
        self,
        base_url: str,
        connector_id: str,
        auth_token: str,
        project_id: str | None,
        location: str,
        model: str,
        embedding_model: str = "gemini-embedding-001",
    ):
        self.provider = f"vertex_proxy/{model}"
        self._base_url = base_url.rstrip("/")
        self._connector_id = connector_id
        self._auth_token = auth_token
        self._project_id = project_id
        self._location = location
        self._model = model
        self._embedding_model = embedding_model
        self._http_client = None  # Lazy-initialized persistent HTTP client
        # Optional pinned connector creds (set by build_space_clients for out-of-request
        # callers like the evaluator). Used instead of the request contextvar when set.
        self.pinned_creds = None
        # Thinking ON when a call sets none (off by default, core/generation.py); the
        # wiki sets it from its per-space switch.
        self.enable_thinking = False

    async def _resolve_creds(self) -> _ResolvedCreds:
        """Return the Raw-Connector creds + GCP project/location for this request.

        A fully-resolvable per-space connector wins for the connector identity;
        project/location resolve independently (a vertex_ai-style space may set a
        region with no connector token). Falls back to the client's build-time
        global values. Never raises — LLM calls must not break because the
        connector store is briefly unavailable.
        """
        base, connector, token = self._base_url, self._connector_id, self._auth_token
        project, location = self._project_id, self._location
        try:
            from shared.request_context import get_current_space
            from shared.connector_credentials import creds_serve_scheme, resolve_llm_connector

            # Pinned creds (out-of-request callers) win; else resolve from the
            # request-bound SPACE (template > space).
            creds = self.pinned_creds or await resolve_llm_connector(get_current_space())
            project = creds.project or project
            location = creds.location or location
            # The space's identity only when it was entered for a proxy type: a space
            # whose model is openai holds an OpenAI key, never sent to the proxy.
            if creds_serve_scheme(creds, "vertex_proxy") and creds.is_complete():
                base = creds.base_url.rstrip("/")
                connector = creds.connector_id
                token = creds.auth_token
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("vertex_proxy connector resolve failed, using global: %s", e)
        return _ResolvedCreds(base, connector, token, project, location)

    def _connector_root_url(
        self, base_url: str | None = None, connector_id: str | None = None
    ) -> str:
        return config.raw_connector_root_from_env(
            base_url if base_url is not None else self._base_url,
            connector_id if connector_id is not None else self._connector_id,
        )
    
    def _get_http_client(self) -> Any:
        """Get or create persistent HTTP client for connection reuse."""
        import httpx
        
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                verify=config.tls_verify(), timeout=_generation.request_timeout()
            )
            logger.debug("vertex_proxy: created persistent HTTP client for embedding requests")
        return self._http_client
    
    async def close(self):
        """Close the persistent HTTP client."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
            logger.debug("vertex_proxy: closed persistent HTTP client")

    def _build_api_url(
        self,
        base_url: str | None = None,
        connector_id: str | None = None,
        project: str | None = None,
        location: str | None = None,
    ) -> str:
        """Build ``:generateContent`` URL via Raw Connector (no project or region: the
        connector supplies them; ``project``/``location`` are accepted and unused)."""
        root = self._connector_root_url(base_url, connector_id)
        return f"{root}/{config.vertex_proxy_studio_path(self._model, 'generateContent')}"

    def _build_embedding_url(
        self,
        base_url: str | None = None,
        connector_id: str | None = None,
        project: str | None = None,
        location: str | None = None,
    ) -> str:
        """Build the ``:predict`` URL for the embedding model."""
        root = self._connector_root_url(base_url, connector_id)
        return f"{root}/{config.vertex_proxy_studio_path(self._embedding_model, 'predict')}"

    def _build_headers(self, auth_token: str | None = None) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_token if auth_token is not None else self._auth_token}",
        }

    def _build_payload(
        self,
        prompt: str,
        system: str = "",
        response_format: dict[str, str] | None = None,
        generation: "GenerationParams | None" = None,
        cached: str | None = None,
    ) -> dict[str, Any]:
        """Build the Vertex AI request payload.

        ``generation`` carries per-call parameters resolved by the caller. When it
        is None every value falls back to the previous behaviour (env + constants),
        so nothing changes for callers that do not pass it.

        This used to take no parameters at all: temperature/topP/topK were
        hardcoded and maxOutputTokens/thinkingBudget came from env. On the proxy
        lane — which is production — that silently discarded every per-space
        generation setting, so a space admin could change temperature in the UI,
        see it stored, and have 0.7 sent regardless.
        """
        contents = []
        if system:
            contents.append({
                "role": "user",
                "parts": [{"text": f"System: {system}"}]
            })
        contents.append({
            "role": "user",
            "parts": [{"text": prompt}]
        })

        gen = generation
        gen_config: dict[str, Any] = {
            "temperature": _generation.temperature_for(self._model, gen),
            "maxOutputTokens": _generation.max_tokens_for(gen),
        }
        if gen is not None and gen.top_p is not None:
            gen_config["topP"] = gen.top_p
        if gen is not None and gen.top_k is not None:
            gen_config["topK"] = gen.top_k
        # Thinking off unless the call asked: omitting thinkingConfig lets Gemini fall
        # back to its default (thinking ON), which dominates latency (~28s on 2.5-flash).
        if not _generation.thinking_on(gen, self.enable_thinking):
            gen_config["thinkingConfig"] = _generation.proxy_thinking_config(self._model)

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": gen_config,
        }

        if response_format and response_format.get("type") == "json_object":
            payload["generationConfig"]["responseMimeType"] = "application/json"
        if cached:
            # The cache holds the system prompt and the document (core/context_cache.py).
            payload["cachedContent"] = cached

        return payload

    def _cache_root(self, creds: _ResolvedCreds) -> str:
        root = self._connector_root_url(creds.base_url, creds.connector_id)
        return f"{root}/{config.vertex_proxy_studio_api_version()}"

    async def create_cache(self, document: str, system: str, ttl_seconds: int) -> tuple[int, str, str | None]:
        """``POST {root}/v1/cachedContents`` — (status, body, cache name). A proxy
        without the endpoint answers 404, which core/context_cache.py reads as
        "not supported"."""
        import httpx

        creds = await self._resolve_creds()
        body: dict[str, Any] = {
            "model": f"models/{self._model}",
            "contents": [{"role": "user", "parts": [{"text": document}]}],
            "ttl": f"{ttl_seconds}s",
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        async with httpx.AsyncClient(verify=config.tls_verify(), timeout=_generation.request_timeout()) as client:
            response = await client.post(f"{self._cache_root(creds)}/cachedContents",
                                         json=body, headers=self._build_headers(creds.auth_token))
        name = (response.json() or {}).get("name") if response.is_success else None
        return response.status_code, response.text, name

    async def extend_cache(self, name: str, ttl_seconds: int) -> None:
        """Set the cache's TTL to ``ttl_seconds`` from now."""
        import httpx

        creds = await self._resolve_creds()
        url = f"{self._cache_root(creds)}/{name}?updateMask=ttl"
        async with httpx.AsyncClient(verify=config.tls_verify(), timeout=_generation.request_timeout()) as client:
            response = await client.patch(url, json={"ttl": f"{ttl_seconds}s"},
                                          headers=self._build_headers(creds.auth_token))
        _raise_for_status_with_detail(response, url, "cache extend")

    async def delete_cache(self, name: str) -> None:
        import httpx

        creds = await self._resolve_creds()
        url = f"{self._cache_root(creds)}/{name}"
        async with httpx.AsyncClient(verify=config.tls_verify(), timeout=_generation.request_timeout()) as client:
            response = await client.delete(url, headers=self._build_headers(creds.auth_token))
        _raise_for_status_with_detail(response, url, "cache delete")

    @resilient
    async def text(
        self,
        prompt: str,
        system: str = "",
        *,
        generation: GenerationParams | None = None,
        cached: str | None = None,
    ) -> LLMResult:
        """Plain text completion.

        ``generation`` is optional so this still satisfies the LLMClient protocol;
        callers that resolve per-space parameters pass them, and the payload
        honours them instead of the env/constant defaults. ``cached`` names a
        context cache (core/context_cache.py).
        """
        import httpx

        creds = await self._resolve_creds()
        url = self._build_api_url(creds.base_url, creds.connector_id, creds.project, creds.location)
        headers = self._build_headers(creds.auth_token)
        payload = self._build_payload(prompt, system, generation=generation, cached=cached)

        async with httpx.AsyncClient(verify=config.tls_verify(), timeout=_generation.request_timeout_for(generation)) as client:
            response = await client.post(url, json=payload, headers=headers)
            _raise_for_status_with_detail(response, url, "text")
            data = response.json()

        text = gemini_response.answer_text(data)

        return LLMResult(
            text=text,
            cost_usd=0.0,
            model=self._model,
            raw=data,
        )

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

        Also parameterized: the enricher, wiki compiler and synthetic-question
        generation all go through here, so without it their settings are inert too.
        """
        import httpx

        creds = await self._resolve_creds()
        url = self._build_api_url(creds.base_url, creds.connector_id, creds.project, creds.location)
        headers = self._build_headers(creds.auth_token)
        payload = self._build_payload(
            prompt,
            system,
            generation=generation,
            response_format={"type": "json_object"},
            cached=cached,
        )

        async with httpx.AsyncClient(verify=config.tls_verify(), timeout=_generation.request_timeout_for(generation)) as client:
            response = await client.post(url, json=payload, headers=headers)
            _raise_for_status_with_detail(response, url, "json")
            resp_data = response.json()

        text = gemini_response.answer_text(resp_data)

        parsed = loads_tolerant(
            text,
            model=self._model,
            source=" despite responseMimeType=application/json",
        )

        return LLMResult(
            text=text,
            data=parsed,
            cost_usd=0.0,
            model=self._model,
            raw=resp_data,
        )

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

        The proxy-lane twin of ``LiteLLMClient.vision_sync``, which the PDF
        converters (vlm, dbank, slides) call from worker threads. Without it the
        VLM converter refused to start on this lane, so every page that needs
        vision failed. Like the litellm one it returns "" when Gemini gives no
        answer (a prompt blocked by its safety filter, say), so a converter's
        empty-answer handling sees the same thing on both lanes.
        """
        import httpx

        creds = asyncio.run(self._resolve_creds())
        url = self._build_api_url(creds.base_url, creds.connector_id, creds.project, creds.location)
        headers = self._build_headers(creds.auth_token)
        payload = self._build_payload(
            prompt, system,
            generation=GenerationParams(temperature=temperature, max_tokens=max_tokens),
        )
        payload["contents"][-1]["parts"].extend(
            {"inlineData": {"mimeType": "image/png", "data": b64}} for b64 in images_b64
        )

        with httpx.Client(verify=config.tls_verify(), timeout=_generation.request_timeout()) as client:
            response = client.post(url, json=payload, headers=headers)
            _raise_for_status_with_detail(response, url, "vision")
            data = response.json()

        try:
            return gemini_response.answer_text(data)
        except ValueError as exc:
            logger.warning("vertex_proxy vision: %s", exc)
            return ""

    async def embedding(self, text: str | list[str]) -> list[list[float]]:
        """Embed text via the Raw Connector (``:predict``).

        Vertex caps requests at 250 instances, so long inputs are split into
        sub-batches (see ``embed_in_batches``); each sub-batch retries on 429.
        """
        inputs = [text] if isinstance(text, str) else list(text)
        # Resolve once for the whole request, then reuse across sub-batches.
        creds = await self._resolve_creds()

        async def _one(batch: list[str]) -> list[list[float]]:
            return await self._embed_one_batch(batch, creds)

        return await embed_in_batches(inputs, _one)

    async def _embed_one_batch(
        self, inputs: list[str], creds: _ResolvedCreds | None = None
    ) -> list[list[float]]:
        c = creds if creds is not None else await self._resolve_creds()
        url = self._build_embedding_url(c.base_url, c.connector_id, c.project, c.location)
        headers = self._build_headers(c.auth_token)
        data = await self._post_embedding(
            url, {"instances": [{"content": s} for s in inputs]}, headers)
        try:
            return [pred["embeddings"]["values"] for pred in data["predictions"]]
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError(
                f"Unexpected Vertex predict response shape: {e}. "
                f"Body: {data!r}"
            ) from e

    async def _post_embedding(self, url: str, payload: dict, headers: dict) -> dict:
        """POST one embedding request, retrying 429 (rate limit) and 504 (timed out) with
        exponential backoff — the two statuses the connector guide says to retry."""
        max_retries, retry_delay = _EMBED_MAX_RETRIES, _EMBED_RETRY_DELAY_SECONDS

        for attempt in range(max_retries + 1):
            client = self._get_http_client()
            response = await within_deadline(
                client.post(url, json=payload, headers=headers),
                _generation.request_timeout(), "VertexProxyClient.embedding")
            if response.status_code in (429, 504) and attempt < max_retries:
                wait = retry_delay * (2 ** attempt)
                logger.warning(
                    "vertex_proxy embed: HTTP %d, retry %d/%d in %.1fs",
                    response.status_code,
                    attempt + 1,
                    max_retries,
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            _raise_for_status_with_detail(response, url, "embedding")
            return response.json()

        raise RuntimeError("vertex_proxy embedding retry loop exited unexpectedly")

    @resilient_stream
    async def stream(
        self,
        prompt: str,
        system: str = "",
        *,
        generation: GenerationParams | None = None,
    ):
        """Stream text deltas from Vertex AI via Raw Connector.

        Takes ``generation`` for the same reason text() does: without it the
        streaming answer path silently used env + hardcoded constants while the
        non-streaming one honoured the space's settings.
        
        Uses the :streamGenerateContent endpoint which returns SSE-style 
        line-delimited JSON chunks. Vertex AI streaming can return either
        single objects or arrays of objects per line.
        """
        import httpx

        creds = await self._resolve_creds()

        # Build streaming URL (replace :generateContent with :streamGenerateContent)
        root = self._connector_root_url(creds.base_url, creds.connector_id)
        url = f"{root}/{config.vertex_proxy_studio_path(self._model, 'streamGenerateContent')}?alt=sse"

        headers = self._build_headers(creds.auth_token)
        payload = self._build_payload(prompt, system, generation=generation)
        
        async with httpx.AsyncClient(verify=config.tls_verify(), timeout=_generation.request_timeout_for(generation)) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    await response.aread()  # the body carries the connector's reason
                _raise_for_status_with_detail(response, url, "stream")
                
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    # ?alt=sse (studio style) frames each chunk as "data: {...}".
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    
                    try:
                        chunk = json.loads(line)
                        # Handle both single object and array formats
                        items = chunk if isinstance(chunk, list) else [chunk]
                        
                        for item in items:
                            text = gemini_response.stream_delta(item)
                            if text:
                                yield text
                    except json.JSONDecodeError:
                        # Skip malformed chunks
                        continue
