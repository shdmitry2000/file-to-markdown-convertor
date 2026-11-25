"""Core protocol, config, and factory.

`LLMClient` is the seam every backend must implement. `LLMFactory.build()`
parses provider strings and dispatches to the active backend.
"""

from .config import (
    LLMProviderNotConfigured,
    default_gemini_model_id,
    required_provider,
)
from .protocol import LLMClient, LLMResult

__all__ = [
    "LLMClient",
    "LLMResult",
    "LLMProviderNotConfigured",
    "required_provider",
    "default_gemini_model_id",
]
