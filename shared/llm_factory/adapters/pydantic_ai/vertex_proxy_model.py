"""Custom pydantic-ai Model for Vertex AI format proxy.

This implements the pydantic-ai Model protocol directly for the Raw Connector
proxy that only supports Vertex AI format endpoints, not Google AI Studio v1beta.
"""

from __future__ import annotations

import httpx
import json
import os
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from ...core import config
from ...core import generation
from pydantic_ai.models import (
    Model, ModelSettings, ModelRequestParameters, RunContext,
    StreamedResponse, ModelResponsePartsManager,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponseStreamEvent,
    TextPart,
    SystemPromptPart,
    UserPromptPart,
    ToolCallPart,
    ToolReturnPart,
)

logger = logging.getLogger(__name__)


class VertexProxyError(Exception):
    """Base exception for Vertex Proxy errors."""
    pass


class VertexProxyConnectionError(VertexProxyError):
    """Connection error to the proxy."""
    pass


class VertexProxyAuthenticationError(VertexProxyError):
    """Authentication error with the proxy."""
    pass


class VertexProxyRateLimitError(VertexProxyError):
    """Rate limit exceeded."""
    pass


class VertexProxyAPIError(VertexProxyError):
    """API error from the proxy/model."""
    def __init__(self, message: str, status_code: int | None = None, response_body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


@dataclass
class _VertexProxyStreamedResponse(StreamedResponse):
    """StreamedResponse implementation for VertexProxyModel."""

    http_client: httpx.AsyncClient
    stream_url: str
    payload: dict[str, Any]
    _model_name: str
    _timestamp: Any = None

    def __post_init__(self):
        from datetime import datetime, timezone
        if self._timestamp is None:
            self._timestamp = datetime.now(timezone.utc)

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def provider_name(self) -> str | None:
        return "vertex_proxy"

    @property
    def provider_url(self) -> str | None:
        return None

    @property
    def timestamp(self):
        return self._timestamp

    async def _get_event_iterator(self) -> AsyncIterator[ModelResponseStreamEvent]:
        async with self.http_client.stream("POST", self.stream_url, json=self.payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.strip():
                    continue
                if line.startswith("data: "):
                    line = line[6:]
                try:
                    chunk_data = json.loads(line)
                    items = chunk_data if isinstance(chunk_data, list) else [chunk_data]
                    for item_index, item in enumerate(items):
                        candidates = item.get("candidates") if isinstance(item, dict) else None
                        if not candidates:
                            continue
                        parts = candidates[0].get("content", {}).get("parts", [])
                        for part_index, part in enumerate(parts):
                            vendor_part_id = (item_index, part_index)

                            # Text deltas
                            if "text" in part and part["text"]:
                                for event in self._parts_manager.handle_text_delta(
                                    vendor_part_id=vendor_part_id,
                                    content=part["text"],
                                ):
                                    yield event

                            # Tool/function calls are critical for streamed tool execution.
                            elif "functionCall" in part:
                                func_call = part.get("functionCall") or {}
                                tool_name = func_call.get("name")
                                if not tool_name:
                                    continue
                                args = func_call.get("args", {})
                                event = self._parts_manager.handle_tool_call_part(
                                    vendor_part_id=vendor_part_id,
                                    tool_name=tool_name,
                                    args=args,
                                    tool_call_id=None,
                                )
                                yield event
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue


class VertexProxyModel(Model):
    """Custom pydantic-ai Model for Vertex AI format proxy.

    This class implements the pydantic-ai Model protocol to work with
    the Raw Connector proxy that uses Vertex AI format endpoints.
    """

    def __init__(
        self,
        base_url: str,
        connector_id: str,
        auth_token: str,
        project_id: str | None,
        location: str,
        model_name: str,
        max_retries: int = 3,
        timeout: float | None = None
    ):
        self._base_url = base_url.rstrip("/")
        self._connector_id = connector_id
        self._auth_token = auth_token
        self._project_id = project_id
        self._location = location
        self._model_name = model_name
        self._max_retries = max_retries

        # The litellm proxy client's path: Gemini-API, no project/region, as the
        # on-prem connector wants.
        root = f"{self._base_url}/api/connectors/{self._connector_id}"
        self._api_url = f"{root}/{config.vertex_proxy_studio_path(self._model_name, 'generateContent')}"

        logger.info(f"VertexProxyModel initialized: {self._model_name} via {self._base_url}")
        logger.debug(f"API URL: {self._api_url}")

        self._http_client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self._auth_token}"},
            timeout=timeout if timeout is not None else generation.request_timeout(),
            verify=config.tls_verify(),
        )

    def model_name(self) -> str:
        return self._model_name

    def system(self) -> str | None:
        return None

    def _build_generation_config(
        self,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> dict[str, Any]:
        """The generationConfig for one request.

        One builder for both request() and request_stream(): they held byte-identical
        copies, so every change to one — the thinking budget, the tool-calling
        temperature floor — had to be remembered twice.

        Thinking is off, temperature and max tokens default to LLM_TEMPERATURE /
        LLM_MAX_TOKENS (core/generation.py), like every other lane. Nothing currently
        passes model_settings at all: `rag_agent` is a module-level `Agent(get_llm_model())`
        built at import time from the environment. So per-space generation settings
        do not reach the agent lane; wiring them needs the Agent built per request,
        which is a separate change from the payload construction here.
        """
        gen_config: dict[str, Any] = {
            "thinkingConfig": generation.proxy_thinking_config(self._model_name),
            "maxOutputTokens": generation.llm_max_tokens(),
        }

        def _setting(name: str):
            # ModelSettings is dict-like or object-like depending on pydantic-ai version
            if isinstance(model_settings, dict):
                return model_settings.get(name)
            return getattr(model_settings, name, None)

        temp = _setting("temperature") if model_settings else None
        if temp is not None:
            gen_config["temperature"] = temp
        elif model_request_parameters.function_tools:
            # Default to 0.1 when tools are available, for reliable tool calling
            # (matches old gemini_proxy_core behavior).
            gen_config["temperature"] = 0.1
        else:
            gen_config["temperature"] = generation.temperature_for(self._model_name)

        if model_settings:
            for setting_name, vertex_name in (
                ("max_tokens", "maxOutputTokens"),
                ("top_p", "topP"),
                # topK was silently dropped: both copies read temperature/max_tokens/
                # top_p and neither read top_k, so it was unsettable on this lane.
                ("top_k", "topK"),
            ):
                value = _setting(setting_name)
                if value is not None:
                    gen_config[vertex_name] = value

        return gen_config

    def _build_tools(
        self, model_request_parameters: ModelRequestParameters
    ) -> list[dict[str, Any]] | None:
        """CRITICAL: One Tool object with all functionDeclarations — multiple Tool
        objects (each with one declaration) returns 400 from Vertex Gemini."""
        declarations: list[dict[str, Any]] = []
        for tool in model_request_parameters.function_tools:
            func_decl = {
                "name": tool.name,
                "description": tool.description or "",
            }
            if tool.parameters_json_schema:
                # Convert JSON schema to Vertex AI format
                params_schema = tool.parameters_json_schema
                func_decl["parameters"] = {
                    "type": params_schema.get("type", "object"),
                    "properties": params_schema.get("properties", {}),
                    "required": params_schema.get("required", []),
                }
            declarations.append(func_decl)
        return [{"functionDeclarations": declarations}] if declarations else None

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters
    ) -> ModelResponse:
        contents = self._convert_messages(messages)
        gen_config = self._build_generation_config(model_settings, model_request_parameters)
        payload: dict[str, Any] = {"contents": contents, "generationConfig": gen_config}

        logger.debug(f"REQUEST PAYLOAD generationConfig: {gen_config}")

        tools = self._build_tools(model_request_parameters)
        if tools:
            payload["tools"] = tools
            logger.info(
                f"Declaring {len(tools[0]['functionDeclarations'])} tools to Vertex AI "
                f"in a single Tool object"
            )

        last_error = None
        for attempt in range(self._max_retries):
            try:
                if attempt > 0:
                    import asyncio
                    delay = 2 ** (attempt - 1)
                    logger.info(f"Retry attempt {attempt + 1}/{self._max_retries} after {delay}s delay")
                    await asyncio.sleep(delay)

                logger.debug(f"Making request to {self._api_url} (attempt {attempt + 1})")
                response = await self._http_client.post(self._api_url, json=payload)
                response.raise_for_status()

                data = response.json()
                logger.debug(f"Received successful response")

                return self._convert_response(data)

            except httpx.TimeoutException as e:
                last_error = e
                logger.warning(f"Timeout on attempt {attempt + 1}: {e}")
                if attempt < self._max_retries - 1:
                    continue

            except httpx.ConnectError as e:
                last_error = e
                logger.warning(f"Connection error on attempt {attempt + 1}: {e}")
                if attempt < self._max_retries - 1:
                    continue

            except httpx.HTTPStatusError as e:
                last_error = e
                status_code = e.response.status_code

                if 400 <= status_code < 500 and status_code != 429:
                    break
                if attempt < self._max_retries - 1:
                    logger.warning(f"HTTP {status_code} on attempt {attempt + 1}, will retry")
                    continue

            except Exception as e:
                last_error = e
                logger.error(f"Unexpected error on attempt {attempt + 1}: {type(e).__name__}: {e}")
                break

        if isinstance(last_error, httpx.TimeoutException):
            logger.error(f"Timeout calling Vertex proxy after {self._max_retries} attempts")
            raise VertexProxyConnectionError(
                f"Request to proxy timed out after {self._max_retries} attempts"
            ) from last_error

        elif isinstance(last_error, httpx.ConnectError):
            logger.error(f"Connection error to Vertex proxy: {last_error}")
            raise VertexProxyConnectionError(
                f"Failed to connect to proxy at {self._api_url}"
            ) from last_error

        elif isinstance(last_error, httpx.HTTPStatusError):
            status_code = last_error.response.status_code
            try:
                error_body = last_error.response.text
            except Exception:
                error_body = "<unable to read response body>"

            logger.error(f"HTTP {status_code} from Vertex proxy: {error_body[:500]}")

            if status_code == 401 or status_code == 403:
                raise VertexProxyAuthenticationError(
                    f"Authentication failed (HTTP {status_code}). "
                    "Check PROXY_AUTH_TOKEN and connector permissions."
                ) from last_error
            elif status_code == 429:
                raise VertexProxyRateLimitError(
                    f"Rate limit exceeded (HTTP {status_code}). Please retry later."
                ) from last_error
            elif status_code == 404:
                raise VertexProxyAPIError(
                    f"Endpoint not found (HTTP 404). Check PROXY_BASE_URL, PROXY_CONNECTOR_ID, "
                    f"and model name. URL: {self._api_url}",
                    status_code=status_code,
                    response_body=error_body
                ) from last_error
            elif status_code >= 500:
                raise VertexProxyAPIError(
                    f"Proxy server error (HTTP {status_code}). The proxy or upstream service may be down.",
                    status_code=status_code,
                    response_body=error_body
                ) from last_error
            else:
                raise VertexProxyAPIError(
                    f"HTTP {status_code} error from proxy: {error_body[:200]}",
                    status_code=status_code,
                    response_body=error_body
                ) from last_error

        else:
            logger.error(f"Unexpected error calling Vertex proxy: {type(last_error).__name__}: {last_error}")
            raise VertexProxyError(
                f"Unexpected error calling proxy: {type(last_error).__name__}: {str(last_error)}"
            ) from last_error

    def _convert_messages(self, messages: list[ModelMessage]) -> list[dict[str, Any]]:
        contents = []
        system_prompt = None

        for msg in messages:
            if isinstance(msg, ModelRequest):
                user_parts = []
                for part in msg.parts:
                    if isinstance(part, SystemPromptPart):
                        system_prompt = part.content
                    elif isinstance(part, UserPromptPart):
                        text = part.content
                        if system_prompt:
                            text = f"{system_prompt}\n\n{text}"
                            system_prompt = None
                        user_parts.append({"text": text})
                    elif isinstance(part, TextPart):
                        user_parts.append({"text": part.content})
                    elif isinstance(part, ToolReturnPart):
                        # Tool returns go as function response parts in Vertex AI format
                        user_parts.append({
                            "functionResponse": {
                                "name": part.tool_name,
                                "response": {
                                    "content": part.content
                                }
                            }
                        })
                
                if user_parts:
                    contents.append({
                        "role": "user",
                        "parts": user_parts
                    })
                    
            elif isinstance(msg, ModelResponse):
                model_parts = []
                for part in msg.parts:
                    if isinstance(part, TextPart):
                        model_parts.append({"text": part.content})
                    elif isinstance(part, ToolCallPart):
                        # Tool calls go as functionCall parts in Vertex AI format
                        model_parts.append({
                            "functionCall": {
                                "name": part.tool_name,
                                "args": part.args.args_dict if hasattr(part.args, 'args_dict') else {}
                            }
                        })
                
                if model_parts:
                    contents.append({
                        "role": "model",
                        "parts": model_parts
                    })

        return contents

    def _convert_response(self, vertex_response: dict[str, Any]) -> ModelResponse:
        if "error" in vertex_response:
            error_msg = vertex_response.get("error", {})
            if isinstance(error_msg, dict):
                message = error_msg.get("message", str(error_msg))
                code = error_msg.get("code")
            else:
                message = str(error_msg)
                code = None

            logger.error(f"API error in response: {message}")
            raise VertexProxyAPIError(
                f"API error: {message}",
                status_code=code,
                response_body=str(vertex_response)
            )

        parts = []
        try:
            if vertex_response.get("candidates"):
                candidate = vertex_response["candidates"][0]

                finish_reason = candidate.get("finishReason")
                if finish_reason and finish_reason not in ["STOP", None]:
                    logger.warning(f"Response finish reason: {finish_reason}")
                    if finish_reason in ["SAFETY", "BLOCKED_REASON"]:
                        raise VertexProxyAPIError(
                            f"Content was blocked due to safety filters: {finish_reason}"
                        )

                # Parse all parts from the response
                if candidate.get("content", {}).get("parts"):
                    for vertex_part in candidate["content"]["parts"]:
                        # Handle text parts
                        if "text" in vertex_part:
                            parts.append(TextPart(content=vertex_part["text"]))
                        
                        # Handle function/tool calls
                        elif "functionCall" in vertex_part:
                            func_call = vertex_part["functionCall"]
                            tool_name = func_call.get("name", "")
                            args_dict = func_call.get("args", {})
                            
                            # Convert to ToolCallPart (args can be dict directly)
                            parts.append(ToolCallPart(
                                tool_name=tool_name,
                                args=args_dict,
                                tool_call_id=None  # Vertex AI doesn't provide IDs
                            ))
                            logger.info(f"Model called tool: {tool_name} with args: {args_dict}")

            if not parts:
                logger.warning("Empty response from model, adding empty text")
                parts.append(TextPart(content=""))

        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"Failed to parse response: {e}. Response: {vertex_response}")
            raise VertexProxyAPIError(
                f"Invalid response format from proxy: {type(e).__name__}: {str(e)}"
            ) from e

        return ModelResponse(
            parts=parts,
            model_name=self._model_name
        )

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        contents = self._convert_messages(messages)
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": self._build_generation_config(
                model_settings, model_request_parameters
            ),
        }

        tools = self._build_tools(model_request_parameters)
        if tools:
            payload["tools"] = tools

        stream_url = self._api_url.replace(":generateContent", ":streamGenerateContent") + "?alt=sse"
        yield _VertexProxyStreamedResponse(
            model_request_parameters=model_request_parameters,
            http_client=self._http_client,
            stream_url=stream_url,
            payload=payload,
            _model_name=self._model_name,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._http_client.aclose()


__all__ = [
    "VertexProxyModel",
    "VertexProxyError",
    "VertexProxyConnectionError",
    "VertexProxyAuthenticationError",
    "VertexProxyRateLimitError",
    "VertexProxyAPIError",
]
