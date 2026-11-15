"""Concrete backend implementations.

- `litellm` — the only backend: LiteLLM for most providers, plus a google-genai
  direct client and the Raw Connector (vertex_proxy) client.

The former `legacy` backend (restored gemini_proxy_core + direct SDK clients) and the
claude_code client are archived in ``archive/llm_factory_legacy/``.
"""
