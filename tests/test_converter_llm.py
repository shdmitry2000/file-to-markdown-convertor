"""The converter LLM: markdown-api's own LLM, used when a job brings none.

Default from env under the names spaces use; GET /llm shows it (never the token);
POST /llm/test calls it once; PUT/DELETE /llm change it until restart, only with
LLM_RUNTIME_CHANGE (off in the product, where it comes from Helm). A job that sends
its own llm (a space) keeps it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import llm_settings
from app.converters import vlm_lane

POD = {"LLM_CONNECTOR_TYPE": "vertex_proxy", "LLM_TYPE": "gemini-3.8-flash",
       "PROXY_BASE_URL": "https://pod-proxy", "PROXY_CONNECTOR_ID": "pod-c",
       "PROXY_AUTH_TOKEN": "POD-TOKEN"}
RUNTIME = {"model": "vertex_proxy/gemini-3.5-flash", "base_url": "https://rt",
           "connector_id": "rt-c", "auth_token": "RT-TOKEN"}


@pytest.fixture(autouse=True)
def pod(monkeypatch, tmp_path):
    for k in ("VLM_MODEL", "LLM_FACTORY_PROVIDER", "LLM_RUNTIME_CHANGE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in POD.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("CONVERTED_FILES_DIR", str(tmp_path / "converted"))
    llm_settings.store().clear()
    vlm_lane._clients.clear()
    yield
    llm_settings.store().clear()


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


@pytest.fixture
def changeable(monkeypatch):
    monkeypatch.setenv("LLM_RUNTIME_CHANGE", "true")


# --- GET /llm --------------------------------------------------------------------

def test_the_default_is_the_env_under_the_space_names(api, caplog):
    c, _, _ = api
    caplog.set_level(logging.DEBUG)
    body = c.get("/llm").json()
    assert body == {"source": "env", "model": "vertex_proxy/gemini-3.8-flash",
                    "base_url": "https://pod-proxy", "connector_id": "pod-c",
                    "project": None, "location": None, "token_set": True,
                    "runtime_change": False, "store": "memory"}
    assert "POD-TOKEN" not in json.dumps(body) + caplog.text


def test_vlm_model_still_wins_for_the_model(api, monkeypatch):
    monkeypatch.setenv("VLM_MODEL", "vertex_ai/gemini-3.5-flash-lite")
    assert api[0].get("/llm").json()["model"] == "vertex_ai/gemini-3.5-flash-lite"


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


# --- PUT / DELETE /llm ------------------------------------------------------------

def test_the_product_cannot_change_it(api):
    c, _, _ = api
    assert c.put("/llm", json=RUNTIME).status_code == 403
    assert c.delete("/llm").status_code == 403
    assert llm_settings.runtime() is None


def test_a_standalone_deployment_can_change_it_until_restart(api, changeable, caplog):
    c, _, _ = api
    caplog.set_level(logging.DEBUG)
    body = c.put("/llm", json=RUNTIME).json()
    assert body["source"] == "runtime" and body["model"] == "vertex_proxy/gemini-3.5-flash"
    assert body["base_url"] == "https://rt" and body["token_set"] is True
    assert "RT-TOKEN" not in json.dumps(body) + caplog.text
    assert c.delete("/llm").json()["source"] == "env"


@pytest.mark.parametrize("bad, says", [
    ({"model": "gemini-3.5-flash"}, "connection type"),
    ({"model": "vertex_proxy/gemini-3.5-flash", "base_url": "https://rt"}, "connector_id, auth_token"),
    ({"model": "vertex_ai/gemini-3.5-flash"}, "project"),
])
def test_an_incomplete_llm_is_refused(api, changeable, bad, says):
    r = api[0].put("/llm", json=bad)
    assert r.status_code == 400 and says in r.json()["detail"]
    assert llm_settings.runtime() is None


# --- which LLM a job gets ---------------------------------------------------------

def test_a_job_without_llm_runs_on_the_runtime_llm(api, changeable):
    c, sent, pdf = api
    c.post("/convert", json={"file_path": pdf})
    assert "llm_connector" not in sent[-1]  # env default: nothing to carry
    c.put("/llm", json=RUNTIME)
    other = Path(pdf).with_name("b.pdf")  # the same file in flight would be shared
    other.write_bytes(b"%PDF-1.4")
    c.post("/convert", json={"file_path": str(other)})
    assert sent[-1]["llm_connector"] == {"mode": "params", **RUNTIME}


def test_a_space_keeps_its_own_llm(api, changeable):
    c, sent, pdf = api
    c.put("/llm", json=RUNTIME)
    space = {"mode": "space", "model": "vertex_proxy/x", "base_url": "https://s",
             "connector_id": "s", "auth_token": "S"}
    c.post("/convert", json={"file_path": pdf, "llm": space})
    assert sent[-1]["llm_connector"] == space


def test_the_converter_menu_can_send_an_llm_or_use_the_converter_llm(api, changeable):
    c, sent, _ = api
    files = {"file": ("a.pdf", b"%PDF-1.4", "application/pdf")}
    c.post("/debug/convert", files=files)
    assert "llm_connector" not in sent[-1]
    c.put("/llm", json=RUNTIME)
    c.post("/debug/convert", files=files)
    assert sent[-1]["llm_connector"] == {"mode": "params", **RUNTIME}
    own = {"model": "vertex_ai/gemini-3.8-flash", "project": "p"}
    c.post("/debug/convert", files=files, data={"llm": json.dumps(own)})
    assert sent[-1]["llm_connector"] == {"mode": "params", **own}


def test_the_converter_menu_refuses_bad_llm_json(api):
    files = {"file": ("a.pdf", b"%PDF-1.4", "application/pdf")}
    r = api[0].post("/debug/convert", files=files, data={"llm": "{not json"})
    assert r.status_code == 400 and "not JSON" in r.json()["detail"]
    assert api[1] == []


def test_a_runtime_vertex_llm_uses_its_own_project():
    lane = llm_settings.job_lane(None)
    assert lane is None
    llm_settings.store().set({"model": "vertex_ai/gemini-3.5-flash", "project": "rt-proj"})
    with patch("shared.space_llm.pinned_client", return_value=MagicMock()) as pin, \
         vlm_lane.job(llm_settings.job_lane(None)):
        vlm_lane.client()
    creds, spec = pin.call_args.args
    assert spec == "vertex_ai/gemini-3.5-flash" and creds.project == "rt-proj"


# --- POST /llm/test ---------------------------------------------------------------

def test_the_test_call_reports_success_and_the_model(api):
    client = MagicMock()
    client.vision_sync.return_value = "OK"
    with patch("shared.llm_factory.LLMFactory.build", return_value=client):
        body = api[0].post("/llm/test").json()
    assert body["ok"] is True and body["model"] == "vertex_proxy/gemini-3.8-flash"
    assert client.vision_sync.call_count == 1


def test_the_test_call_reports_the_reason_it_failed(api, monkeypatch):
    monkeypatch.delenv("PROXY_AUTH_TOKEN")  # OCP's markdown pod before the secret
    body = api[0].post("/llm/test").json()
    assert body["ok"] is False and "PROXY_AUTH_TOKEN" in body["error"]


# --- the file store (ready, not used) ---------------------------------------------

def test_memory_is_the_store():
    assert isinstance(llm_settings.store(), llm_settings.MemoryStore)


def test_the_file_store_seals_the_token(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from shared import secret_box

    monkeypatch.setenv("CONNECTOR_KEK", Fernet.generate_key().decode())
    secret_box.reset()
    store = llm_settings.FileStore(tmp_path / "llm.json")
    store.set(RUNTIME)
    assert "RT-TOKEN" not in (tmp_path / "llm.json").read_text()
    assert store.get() == RUNTIME
    store.clear()
    assert store.get() is None
    secret_box.reset()


def test_the_file_store_never_writes_a_token_in_clear(tmp_path, monkeypatch):
    from shared import secret_box

    monkeypatch.delenv("CONNECTOR_KEK", raising=False)
    secret_box.reset()
    with pytest.raises(ValueError, match="CONNECTOR_KEK"):
        llm_settings.FileStore(tmp_path / "llm.json").set(RUNTIME)
    assert not (tmp_path / "llm.json").exists()
