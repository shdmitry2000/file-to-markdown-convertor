"""A caller's LLM connector picks the lane for this job's VLM calls — not the output."""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from app.converters import vlm_lane
from app.workers import worker
from tests import _timeout_targets as targets

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
    assert vlm_lane.client() is None
    with vlm_lane.job(None):
        assert vlm_lane.client() is None


def test_the_connector_is_pinned_for_the_job_only():
    with patch("shared.space_llm.pinned_client", return_value="proxy-client") as pin:
        with vlm_lane.job(PROXY):
            assert vlm_lane.client() == "proxy-client"
            assert vlm_lane.client() == "proxy-client"  # built once per connector
        assert vlm_lane.client() is None
    creds, spec = pin.call_args.args
    assert pin.call_count == 1 and spec == "vertex_proxy/gemini-3.8-flash"
    assert (creds.base_url, creds.connector_id, creds.auth_token, creds.source) == (
        "https://proxy", "c-1", "SECRET-TOKEN", "space")


def test_vlm_calls_go_through_the_jobs_lane():
    env, lane = MagicMock(), MagicMock()
    env.vision_sync.return_value = "env"
    lane.vision_sync.return_value = "proxy"
    with patch("shared.llm_factory.LLMFactory.build", return_value=env):
        from app.converters.vlm import VLMConverter
        vlm = VLMConverter(backend="factory")
    with patch("shared.space_llm.pinned_client", return_value=lane):
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
        from app.converters.vlm import VLMConverter
        VLMConverter(backend="factory", model_name="gemini-9-pro")
    build.assert_called_with("vertex_ai/gemini-9-pro")
    # a caller's lane: its connector type, the asked-for model
    with patch("shared.space_llm.pinned_client", return_value="c") as pin, vlm_lane.job(PROXY):
        vlm_lane.client("gemini-9-pro")
    assert pin.call_args.args[1] == "vertex_proxy/gemini-9-pro"
