"""markdown-api only serves the platform: no file outside its own directories, and
with INTERNAL_SERVICE_TOKEN set nothing but the probes without X-Internal-Auth."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def mock_zmq(monkeypatch):
    from tests.zmq_fakes import FakeContext

    monkeypatch.setattr("zmq.Context", FakeContext)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FILE_STORAGE_BACKEND", "pvc")
    monkeypatch.setenv("PROJECTS_BASE_PATH", str(tmp_path / "projects"))
    (tmp_path / "projects").mkdir()
    converted = tmp_path / "converted"
    converted.mkdir()
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)

    from app.api import main
    monkeypatch.setattr(main.settings, "CONVERTED_FILES_DIR", str(converted))
    with TestClient(main.app) as c:
        yield c


def test_converted_never_reads_outside_its_directory(client, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("TOKEN=abc")
    for path in (f"/converted/{secret}", f"/converted//{secret}",
                 "/converted/../secret", "/converted/%2E%2E%2Fsecret",
                 "/converted//proc/self/environ"):
        r = client.get(path)
        assert r.status_code == 404, path
        assert "TOKEN" not in r.text


def test_converted_still_serves_its_own_files(client, tmp_path):
    (tmp_path / "converted" / "deck.md").write_text("# legacy")
    assert client.get("/converted/deck.pptx").json()["content"] == "# legacy"
    assert client.get("/converted/deck.md").json()["content"] == "# legacy"


def test_convert_refuses_a_path_outside_the_projects_root(client, tmp_path):
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4")
    r = client.post("/convert", json={"file_path": str(outside)})
    assert r.status_code == 400
    r = client.post("/convert", json={"file_path": str(tmp_path / "projects" / ".." / "outside.pdf")})
    assert r.status_code == 400


def test_token_set_refuses_calls_without_it(client, monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "s3cret")
    assert client.get("/health").status_code == 200
    for method, path in (("get", "/converted/x.md"), ("get", "/debug/queue"),
                         ("get", "/llm"), ("get", "/docs"), ("get", "/openapi.json")):
        assert getattr(client, method)(path).status_code == 403, path
        assert getattr(client, method)(path, headers={"x-internal-auth": "wrong"}).status_code == 403
    assert client.get("/debug/queue", headers={"x-internal-auth": "s3cret"}).status_code != 403
