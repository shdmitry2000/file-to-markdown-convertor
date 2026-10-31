"""The converter LLM: markdown-api's own LLM, used when a job brings none.

It is the pod's env under the names spaces use; GET /llm shows it (never the token);
POST /llm/test calls it once. A job that sends its own llm (a space) keeps it.
Changing it at runtime (LLM_RUNTIME_CHANGE, PUT/DELETE /llm) was removed in 1.4.8.5.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from app.converters import vlm_lane

POD = {"LLM_CONNECTOR_TYPE": "vertex_proxy", "LLM_TYPE": "gemini-3.8-flash",
       "PROXY_BASE_URL": "https://pod-proxy", "PROXY_CONNECTOR_ID": "pod-c",
       "PROXY_AUTH_TOKEN": "POD-TOKEN"}


@pytest.fixture(autouse=True)
def pod(monkeypatch, tmp_path):
    for k, v in POD.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("CONVERTED_FILES_DIR", str(tmp_path / "converted"))
    vlm_lane._clients.clear()


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.zmq_fakes import FakeContext
    monkeypatch.setattr("zmq.Context", FakeContext)
    from app.api import main

    sent = []
    monkeypatch.setattr(main, "enqueue_task", sent.append)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with TestClient(main.app) as c:
        yield c, sent, str(pdf)


# --- GET /llm --------------------------------------------------------------------

def test_the_default_is_the_env_under_the_space_names(api, caplog):
    c, _, _ = api
    caplog.set_level(logging.DEBUG)
    body = c.get("/llm").json()
    assert body == {"source": "env", "model": "vertex_proxy/gemini-3.8-flash",
                    "base_url": "https://pod-proxy", "connector_id": "pod-c",
                    "project": None, "location": None, "token_set": True}
    assert "POD-TOKEN" not in json.dumps(body) + caplog.text


def test_a_per_pod_llm_type_is_the_model_and_vlm_model_is_ignored(api, monkeypatch):
    monkeypatch.setenv("LLM_TYPE", "gemini-3.5-flash-lite")
    monkeypatch.setenv("VLM_MODEL", "vertex_ai/gemini-3.8-flash")  # removed in 1.4.8.5
    assert api[0].get("/llm").json()["model"] == "vertex_proxy/gemini-3.5-flash-lite"


def test_any_type_is_the_default(api, monkeypatch):
    monkeypatch.setenv("LLM_CONNECTOR_TYPE", "vertex_ai")
    monkeypatch.setenv("LLM_TYPE", "gemini-3.5-flash")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_ID", "proj")
    body = api[0].get("/llm").json()
    assert body["model"] == "vertex_ai/gemini-3.5-flash" and body["project"] == "proj"


def test_an_unconfigured_pod_still_answers(api, monkeypatch):
    monkeypatch.delenv("LLM_CONNECTOR_TYPE")
    monkeypatch.delenv("LLM_TYPE")
    body = api[0].get("/llm").json()
    assert body["model"] is None and "LLM_CONNECTOR_TYPE" in body["error"]


def test_it_cannot_be_changed_at_runtime(api, monkeypatch):
    monkeypatch.setenv("LLM_RUNTIME_CHANGE", "true")  # removed in 1.4.8.5: ignored
    c, _, _ = api
    assert c.put("/llm", json={"model": "vertex_proxy/x"}).status_code == 405
    assert c.delete("/llm").status_code == 405


# --- which LLM a job gets ---------------------------------------------------------

def test_a_job_without_llm_runs_on_the_env_llm(api):
    c, sent, pdf = api
    c.post("/convert", json={"file_path": pdf})
    assert "llm_connector" not in sent[-1]  # env default: nothing to carry


def test_a_space_keeps_its_own_llm(api):
    c, sent, pdf = api
    space = {"mode": "space", "model": "vertex_proxy/x", "base_url": "https://s",
             "connector_id": "s", "auth_token": "S"}
    c.post("/convert", json={"file_path": pdf, "llm": space})
    assert sent[-1]["llm_connector"] == space


def test_the_converter_menu_can_send_an_llm_or_use_the_converter_llm(api):
    c, sent, _ = api
    files = {"file": ("a.pdf", b"%PDF-1.4", "application/pdf")}
    c.post("/debug/convert", files=files)
    assert "llm_connector" not in sent[-1]
    own = {"model": "vertex_ai/gemini-3.8-flash", "project": "p"}
    c.post("/debug/convert", files=files, data={"llm": json.dumps(own)})
    assert sent[-1]["llm_connector"] == {"mode": "params", **own}


def test_the_converter_menu_refuses_bad_llm_json(api):
    files = {"file": ("a.pdf", b"%PDF-1.4", "application/pdf")}
    r = api[0].post("/debug/convert", files=files, data={"llm": "{not json"})
    assert r.status_code == 400 and "not JSON" in r.json()["detail"]
    assert api[1] == []


# --- POST /llm/test ---------------------------------------------------------------

def test_the_test_call_reports_success_and_the_model(api):
    client = MagicMock()
    client.vision_sync.return_value = "OK"
    with patch("shared.llm_factory.LLMFactory.build", return_value=client):
        body = api[0].post("/llm/test").json()
    assert body["ok"] is True and body["model"] == "vertex_proxy/gemini-3.8-flash"
    assert client.vision_sync.call_count == 1
    # the probe's own tiny cap wins; everything else is the shared layer's
    assert client.vision_sync.call_args.kwargs == {"max_tokens": 16}


def test_the_test_call_reports_the_reason_it_failed(api, monkeypatch):
    monkeypatch.delenv("PROXY_AUTH_TOKEN")  # OCP's markdown pod before the secret
    body = api[0].post("/llm/test").json()
    assert body["ok"] is False and "PROXY_AUTH_TOKEN" in body["error"]


def test_the_test_call_does_not_wait_out_a_busy_model(api, monkeypatch):
    """One try: a busy lane answers "busy" now, not after the 180 s shared wait."""
    from shared.llm_factory.core import resilience
    from shared.llm_factory.core.resilience import resilient_sync

    class _Busy(Exception):
        status_code = 429

    class _Client:
        calls = 0

        @resilient_sync
        def vision_sync(self, prompt, images, **kw):
            _Client.calls += 1
            raise _Busy("RESOURCE_EXHAUSTED")

    sleeps = []
    monkeypatch.setattr(resilience.time, "sleep", sleeps.append)
    with patch("shared.llm_factory.LLMFactory.build", return_value=_Client()):
        body = api[0].post("/llm/test").json()
    assert body["ok"] is False and "RESOURCE_EXHAUSTED" in body["error"]
    assert _Client.calls == 1 and sleeps == []
