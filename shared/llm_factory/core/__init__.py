"""Core protocol, config, and factory.

`LLMClient` is the seam every backend must implement. `LLMFactory.build()`
parses provider strings and dispatches to the active backend.
"""

from .config import (
    DEFAULT_OPENAI_ROUTE,
    LLMProviderNotConfigured,
    default_gemini_model_id,
    required_provider,
    selected_openai_route,
)
from .protocol import LLMClient, LLMResult

__all__ = [
    "LLMClient",
    "LLMResult",
    "LLMProviderNotConfigured",
    "required_provider",
    "DEFAULT_OPENAI_ROUTE",
    "default_gemini_model_id",
    "selected_openai_route",
]
