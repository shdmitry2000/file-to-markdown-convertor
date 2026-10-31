"""The converter sends no generation settings of its own (1.4.8.5).

Temperature, max tokens and thinking are the shared layer's (shared/llm_factory:
LLM_TEMPERATURE, LLM_MAX_TOKENS, thinking off), the same for every module. Every
page call used to send temperature=0.0, then VLM_TEMPERATURE; on Gemini 3 litellm
logged a "temperature < 1.0 can cause infinite loops" warning per call, and each
LiteLLM line came out twice (its own handler + the root one).
"""

import importlib
import inspect
import logging

from unittest.mock import patch

import pytest

import app.converters.vlm as vlm


def test_page_call_passes_no_generation_settings():
    seen = {}

    class Client:
        def vision_sync(self, prompt, images, **kw):
            seen.update(kw)
            return "page"

    with patch("app.converters.vlm_lane.client", return_value=Client()):
        assert vlm.VLMConverter()._transcribe_page("img") == "page"
    assert seen == {}


def test_the_converter_has_no_settings_of_its_own():
    params = set(inspect.signature(vlm.VLMConverter.__init__).parameters)
    assert params == {"self", "user_prompt", "on_progress"}
    assert "openai" not in inspect.getsource(vlm)


@pytest.mark.parametrize("module, cls", [("app.converters.dbank", "DbankConverter"),
                                         ("app.converters.slides", "SlidesConverter")])
def test_dbank_and_slides_build_the_vlm_plainly(monkeypatch, module, cls):
    made = []
    monkeypatch.setattr(vlm, "VLMConverter", lambda **kw: made.append(kw))
    conv = getattr(importlib.import_module(module), cls)()
    conv._ensure_vlm()
    assert set(made[0]) <= {"user_prompt"}


def test_litellm_logs_once_at_warning(monkeypatch):
    from app.config import configure_litellm_logging

    lg = logging.getLogger("LiteLLM")
    monkeypatch.setattr(lg, "level", lg.level)
    monkeypatch.setattr(lg, "propagate", lg.propagate)
    monkeypatch.delenv("LITELLM_LOG", raising=False)
    configure_litellm_logging()
    assert (lg.level, lg.propagate) == (logging.WARNING, False)

    monkeypatch.setenv("LITELLM_LOG", "error")
    configure_litellm_logging()
    assert lg.level == logging.ERROR
