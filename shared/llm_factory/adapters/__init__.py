"""Framework adapters that consume an `LLMClient` and produce framework-specific objects.

- `pydantic_ai`  → `pydantic_ai.models.Model`
- `langchain`    → `langchain_core.language_models.chat_models.BaseChatModel`
- `openai_compat` → an OpenAI-SDK-compatible client surface (added in Phase 3)

Adapters depend only on `core.protocol.LLMClient` — never on a backend class —
so they work identically against `litellm` or `legacy` backends.
"""
