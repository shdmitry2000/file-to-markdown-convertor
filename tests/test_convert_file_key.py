"""`/convert` addressed by storage key rather than by host path.

`file_path` requires this pod and every worker pod to see the same filesystem —
true on the shared claim, false the moment documents live in an object store. A
`file_key` is resolved through the platform's FileStore instead, so the same
request works either way and this service never learns which.

It also closes a hole the path contract cannot: `file_path` opens whatever absolute
path a caller sends. A key that is absolute or climbs out of its prefix is refused.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def mock_zmq(monkeypatch):
    """The queue is not what these tests are about; /convert only has to reach it."""
    from tests.zmq_fakes import FakeContext

    monkeypatch.setattr("zmq.Context", FakeContext)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FILE_STORAGE_BACKEND", "pvc")
    monkeypatch.setenv("PROJECTS_BASE_PATH", str(tmp_path))
    monkeypatch.setenv("CONVERTED_FILES_DIR", str(tmp_path / "converted"))

    from app import storage
    from shared.file_storage import reset_file_store_cache

    storage.reset()
    reset_file_store_cache()

    from app.api.main import app
    with TestClient(app) as c:
        yield c

    storage.reset()
    reset_file_store_cache()


@pytest.fixture
def stored_pdf(tmp_path):
    from shared.file_storage import file_store

    key = "space1/raw/report.pdf"
    file_store().write_bytes(key, b"%PDF-1.4 minimal")
    return key


def test_exactly_one_addressing_mode_is_required(client):
    assert client.post("/convert", json={}).status_code == 400
    both = client.post(
        "/convert", json={"file_path": "/tmp/a.pdf", "file_key": "space1/raw/a.pdf"}
    )
    assert both.status_code == 400


def test_a_stored_object_is_accepted_and_queued(client, stored_pdf):
    response = client.post("/convert", json={"file_key": stored_pdf})
    assert response.status_code == 200
    assert response.json()["conversion_id"]


def test_a_key_with_no_object_is_404(client):
    response = client.post("/convert", json={"file_key": "space1/raw/absent.pdf"})
    assert response.status_code == 404


@pytest.mark.parametrize("hostile", [
    "/etc/passwd",
    "../../etc/passwd",
    "space1/../../etc/passwd",
])
def test_keys_that_escape_are_refused(client, hostile):
    """The whole point of a key: file_path would have opened these."""
    response = client.post("/convert", json={"file_key": hostile})
    assert response.status_code == 400


def test_the_path_contract_still_works(client, tmp_path):
    """The shared-claim lane is unchanged — this is what every caller sends today."""
    pdf = tmp_path / "legacy.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    response = client.post("/convert", json={"file_path": str(pdf)})
    assert response.status_code == 200


def test_in_flight_sharing_keys_on_the_identity_that_was_sent(client, stored_pdf):
    """Two callers converting the same object share one conversion, as they already
    do for the same path — otherwise a key-addressed ingest loses the dedup that
    keeps a re-ingest from doubling the work."""
    first = client.post("/convert", json={"file_key": stored_pdf}).json()["conversion_id"]
    second = client.post("/convert", json={"file_key": stored_pdf}).json()["conversion_id"]
    assert first == second
