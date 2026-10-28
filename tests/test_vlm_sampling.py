"""VLM temperature comes from VLM_TEMPERATURE; unset means it is not sent at all.

Every page call used to send temperature=0.0. On Gemini 3 litellm then logged a
"temperature < 1.0 can cause infinite loops" warning and a DeprecationWarning per
call, and each LiteLLM line came out twice (its own handler + the root one).
"""

import importlib
import logging

import pytest

import app.converters.vlm as vlm


@pytest.fixture
def reload_vlm(monkeypatch):
    def load(value):
        if value is None:
            monkeypatch.delenv("VLM_TEMPERATURE", raising=False)
        else:
            monkeypatch.setenv("VLM_TEMPERATURE", value)
        return importlib.reload(vlm)

    yield load
    monkeypatch.delenv("VLM_TEMPERATURE", raising=False)
    importlib.reload(vlm)


@pytest.mark.parametrize("value, expected", [(None, None), ("", None), ("0", 0.0), ("0.4", 0.4)])
def test_env_sets_the_default(reload_vlm, value, expected):
    assert reload_vlm(value)._DEFAULT_TEMPERATURE == expected


def _converter(backend, temperature):
    conv = object.__new__(vlm.VLMConverter)
    conv._backend, conv._temperature = backend, temperature
    conv._max_tokens, conv._reasoning_effort, conv._user_prompt = 100, None, None
    return conv


def test_factory_call_omits_temperature_when_unset():
    seen = {}

    class Client:
        def vision_sync(self, prompt, images, **kw):
            seen.update(kw)
            return "page"

    conv = _converter("factory", None)
    conv._client = Client()
    assert conv._transcribe_page("img") == "page"
    assert seen["temperature"] is None  # vision_sync sends nothing for None


@pytest.mark.parametrize("temperature, sent", [(None, {}), (0.0, {"temperature": 0.0})])
def test_openai_call_sends_temperature_only_when_set(temperature, sent):
    seen = {}

    def create(**kw):
        seen.update(kw)
        msg = type("M", (), {"content": "page"})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()

    conv = _converter("openai", temperature)
    conv._model = "m"
    conv._client = type("O", (), {"chat": type("Ch", (), {"completions": type("Co", (), {"create": staticmethod(create)})()})()})()
    assert conv._transcribe_page("img") == "page"
    assert {k: v for k, v in seen.items() if k == "temperature"} == sent


@pytest.mark.parametrize("module, cls", [("app.converters.dbank", "DbankConverter"),
                                         ("app.converters.slides", "SlidesConverter")])
def test_dbank_and_slides_follow_the_env(monkeypatch, module, cls):
    made = []
    monkeypatch.setattr(vlm, "VLMConverter", lambda **kw: made.append(kw))
    # dbank reads the pod model to decide on an escalation VLM; without one set
    # that read raises before the VLM under test is built.
    monkeypatch.setenv("VLM_MODEL", "vertex_ai/gemini-3.8-flash")
    conv = getattr(importlib.import_module(module), cls)()
    conv._ensure_vlm()
    assert "temperature" not in made[0]


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
