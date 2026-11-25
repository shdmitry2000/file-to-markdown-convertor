"""Unified LLM factory — single entry point for every provider.

Public surface:

    LLMFactory                — entry point (`build`, `from_env`, `embedding_client`)
    LLMClient, LLMResult      — Protocol + dataclass every backend implements
    default_gemini_model_id   — env-derived default model id helper
    get_llm_for_pydantic_ai   — pydantic-ai adapter
    get_gemini_llm            — langchain adapter (despite the name, any provider)
    get_embedding* / get_embedding_client / get_embedding_config — embedding helpers

Architecture:

    core/        — Protocol, config, factory, introspection
    backends/    — concrete LLMClient implementations
        litellm/   — LiteLLM-routed clients (the only backend; the legacy backend
                     and the claude_code client are archived in archive/llm_factory_legacy/)
    adapters/    — framework adapters (consume LLMClient, produce framework objects)
        pydantic_ai/
        langchain/      (the openai_compat adapter is archived in archive/llm_factory_legacy/)

See README.md and docs/ for usage and configuration.
"""

from .adapters.pydantic_ai import (
    get_embedding,
    get_embedding_client,
    get_embedding_config,
    get_embeddings_batch,
    get_llm_for_pydantic_ai,
)
from .backends.litellm import (
    GeminiClient,
    LiteLLMClient,
    VertexProxyClient,
    build_gemini_proxy_client,
    build_vertex_proxy_client,
    vertex_location_for,
)
from .core import config
from .core.config import default_gemini_model_id
from .core.factory import LLMFactory
from .core.protocol import LLMClient, LLMResult

# LangChain adapter is NOT eagerly imported at top level — it requires
# ``langchain_core`` which is not installed in every service. Use:
#   from shared.llm_factory.adapters.langchain import get_gemini_llm
# (or the back-compat shim ``from shared.llm_factory.langchain_proxy import …``)

__all__ = [
    # Core
    "LLMFactory",
    "LLMClient",
    "LLMResult",
    "config",
    "default_gemini_model_id",
    # Litellm-backend concrete clients (kept exported for direct use)
    "GeminiClient",
    "LiteLLMClient",
    "VertexProxyClient",
    "build_gemini_proxy_client",
    "build_vertex_proxy_client",
    # Pydantic AI adapter (function-style; pydantic_ai SDK is lazy-loaded)
    "get_llm_for_pydantic_ai",
    "get_embedding_config",
    "get_embedding",
    "get_embeddings_batch",
    "get_embedding_client",
]
