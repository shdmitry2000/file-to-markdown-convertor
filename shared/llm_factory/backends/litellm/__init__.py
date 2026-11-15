"""LiteLLM-based backend.

Concrete `LLMClient` implementations that go through (or alongside) LiteLLM
for routing to OpenAI, Gemini, Vertex AI, Anthropic, and many more providers.
"""

from ...core.protocol import LLMClient, LLMResult
from .gemini_client import GeminiClient
from .gemini_proxy import build_gemini_proxy_client
from .litellm_client import LiteLLMClient, vertex_location_for
from .vertex_proxy import VertexProxyClient, build_vertex_proxy_client

__all__ = [
    "LLMClient",
    "LLMResult",
    "GeminiClient",
    "LiteLLMClient",
    "VertexProxyClient",
    "build_gemini_proxy_client",
    "build_vertex_proxy_client",
    "vertex_location_for",
]


def build_litellm_client(provider: str):
    """Build a litellm-backend client from a provider string.

    Used by `LLMFactory.build()`.
    """
    from ...core import config

    provider = provider.strip()
    if not provider:
        raise ValueError("provider string is empty")

    scheme, _, model = provider.partition("/")
    model_part = (model or "").strip()

    if scheme == "claude_code":
        raise ValueError(
            "claude_code is archived (archive/llm_factory_legacy/) — use Claude through the "
            "Anthropic API: connection type 'anthropic' with ANTHROPIC_API_KEY")

    if scheme == "google_genai":
        return GeminiClient(model=model_part or None)

    if scheme == "gemini_proxy":
        return build_gemini_proxy_client(model=model_part or None)

    if scheme == "vertex_proxy":
        return build_vertex_proxy_client(model=model_part or None)

    if scheme == "gemini" and not model_part:
        return LiteLLMClient(model=f"gemini/{config.default_gemini_model_id()}")

    if scheme == "vertex_ai":
        config.ensure_litellm_vertex_env_from_gcp_env()
    return LiteLLMClient(model=provider)
