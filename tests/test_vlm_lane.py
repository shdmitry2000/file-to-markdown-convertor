"""A caller's LLM connector picks the lane for this job's VLM calls — not the output."""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from app.converters import vlm_lane
from app.workers import worker
from tests import _timeout_targets as targets

ENV_CLIENT = MagicMock(name="env-client")
PROXY_CLIENT = MagicMock(name="proxy-client")
PROXY = {"model": "vertex_proxy/gemini-3.5-flash", "base_url": "https://proxy",
         "connector_id": "c-1", "auth_token": "SECRET-TOKEN", "project": "p"}


@pytest.fixture(autouse=True)
def pod_model(monkeypatch):
    monkeypatch.setenv("VLM_MODEL", "vertex_ai/gemini-3.8-flash")
    vlm_lane._clients.clear()


def test_a_gemini_lane_keeps_the_pods_vision_model():
    assert vlm_lane.vlm_spec("vertex_proxy/gemini-3.5-flash") == "vertex_proxy/gemini-3.8-flash"
    assert vlm_lane.vlm_spec("vertex_ai/gemini-3.5-flash") == "vertex_ai/gemini-3.8-flash"


def test_another_provider_keeps_its_own_model():
    assert vlm_lane.vlm_spec("openai/gpt-4o") == "openai/gpt-4o"


def test_no_connector_means_the_pods_env_lane():
    with patch("shared.llm_factory.LLMFactory.build", return_value=ENV_CLIENT) as build:
        assert vlm_lane.client() is ENV_CLIENT
        with vlm_lane.job(None):
            assert vlm_lane.client() is ENV_CLIENT
    build.assert_called_once_with("vertex_ai/gemini-3.8-flash")  # built once, cached


def test_the_connector_is_pinned_for_the_job_only():
    with patch("shared.space_llm.pinned_client", return_value=PROXY_CLIENT) as pin, \
         patch("shared.llm_factory.LLMFactory.build", return_value=ENV_CLIENT):
        with vlm_lane.job(PROXY):
            assert vlm_lane.client() is PROXY_CLIENT
            assert vlm_lane.client() is PROXY_CLIENT  # built once per connector
        assert vlm_lane.client() is ENV_CLIENT
    creds, spec = pin.call_args.args
    assert pin.call_count == 1 and spec == "vertex_proxy/gemini-3.8-flash"
    assert (creds.base_url, creds.connector_id, creds.auth_token, creds.source) == (
        "https://proxy", "c-1", "SECRET-TOKEN", "space")


def test_vlm_calls_go_through_the_jobs_lane():
    env, lane = MagicMock(), MagicMock()
    env.vision_sync.return_value = "env"
    lane.vision_sync.return_value = "proxy"
    from app.converters.vlm import VLMConverter
    vlm = VLMConverter(backend="factory")
    with patch("shared.space_llm.pinned_client", return_value=lane), \
         patch("shared.llm_factory.LLMFactory.build", return_value=env):
        assert vlm.transcribe_page("aW1n") == "env"
        with vlm_lane.job(PROXY):
            assert vlm.transcribe_page("aW1n") == "proxy"


def test_the_cold_worker_child_receives_the_connector(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    result = worker._run_converter_with_timeout(
        str(pdf), "cid", "pymupdf", timeout_seconds=30,
        llm_connector=PROXY, _target=targets.report_connector)
    assert json.loads(result["markdown"]) == PROXY


def test_the_api_puts_the_connector_on_the_task_and_never_logs_it(tmp_path, monkeypatch, caplog):
    from fastapi.testclient import TestClient

    from tests.zmq_fakes import FakeContext
    monkeypatch.setattr("zmq.Context", FakeContext)
    from app.api import main

    sent = []
    monkeypatch.setattr(main, "enqueue_task", sent.append)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    caplog.set_level(logging.DEBUG)
    with TestClient(main.app) as c:
        assert c.post("/convert", json={"file_path": str(pdf), "llm_connector": PROXY}).status_code == 200
        assert c.post("/convert", json={"file_path": str(tmp_path / "missing.pdf")}).status_code == 404
    assert sent[0]["llm_connector"] == PROXY
    assert "SECRET-TOKEN" not in caplog.text


def test_another_model_on_the_same_lane():
    # env lane: the pod's lane, the asked-for model
    env = MagicMock()
    with patch("shared.llm_factory.LLMFactory.build", return_value=env) as build:
        vlm_lane.client("gemini-9-pro")
    build.assert_called_with("vertex_ai/gemini-9-pro")
    # a caller's lane: its connector type, the asked-for model
    with patch("shared.space_llm.pinned_client", return_value=PROXY_CLIENT) as pin, vlm_lane.job(PROXY):
        vlm_lane.client("gemini-9-pro")
    assert pin.call_args.args[1] == "vertex_proxy/gemini-9-pro"


# --- the lane on a pod with no proxy env (OCP: "missing PROXY_AUTH_TOKEN") -----------
# These build the real client: the defect lived in what pinned_client does with the
# connector it is given, which the mocks above never reach.

_PROXY_ENV = ("PROXY_AUTH_TOKEN", "PROXY_BASE_URL", "PROXY_CONNECTOR_ID", "PROXY_URL")


@pytest.fixture
def no_proxy_env(monkeypatch):
    for name in _PROXY_ENV:
        monkeypatch.delenv(name, raising=False)


def test_a_complete_connector_needs_no_proxy_env_on_this_pod(no_proxy_env):
    from shared.llm_factory.backends.litellm.vertex_proxy import VertexProxyClient

    with vlm_lane.job(PROXY):
        c = vlm_lane.client()
    assert isinstance(c, VertexProxyClient)
    assert (c._base_url, c._connector_id, c._auth_token) == ("https://proxy", "c-1", "SECRET-TOKEN")
    assert c.provider == "vertex_proxy/gemini-3.8-flash"     # the pod's vision model


def test_a_lane_sent_without_credentials_falls_back_to_the_pods_lane(no_proxy_env, caplog):
    """What a 1.4.7.8–1.4.7.9 template sends on the deployment's connector: every page
    failed with 'vertex_proxy: missing required env var PROXY_AUTH_TOKEN'."""
    caplog.set_level(logging.WARNING)
    from shared.llm_factory import LLMFactory
    real = LLMFactory.build

    def build(spec, creds=None):  # the real check for the proxy, a stub for the pod
        return real(spec, creds) if spec.startswith("vertex_proxy/") else ENV_CLIENT

    with patch("shared.llm_factory.LLMFactory.build", side_effect=build), \
         vlm_lane.job({"model": "vertex_proxy/gemini-3.5-flash", "project": "p"}):
        assert vlm_lane.client() is ENV_CLIENT
    assert "came without credentials" in caplog.text


def test_vlm_calls_on_a_lane_without_credentials_use_the_pods_lane(no_proxy_env):
    env = MagicMock()
    env.vision_sync.return_value = "env"
    from app.converters.vlm import VLMConverter
    vlm = VLMConverter(backend="factory")
    with patch("shared.llm_factory.LLMFactory.build", return_value=env), \
         vlm_lane.job({"model": "vertex_proxy/gemini-3.5-flash"}):
        assert vlm.transcribe_page("aW1n") == "env"


def test_a_complete_connector_that_cannot_be_built_still_fails():
    """Only a lane with no identity falls back: a broken one with its own token is an
    error to see, not one to route around."""
    with patch("shared.space_llm.pinned_client", side_effect=ValueError("bad endpoint")):
        with vlm_lane.job(PROXY), pytest.raises(ValueError):
            vlm_lane.client()
