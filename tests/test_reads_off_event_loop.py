"""Converted markdown is read off the event loop.

The handlers are async; a synchronous open().read() of a large converted file inside one
stalled every other request on the API (health, polls, every ingest's result fetch).
"""

import asyncio
import builtins

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def mock_zmq(monkeypatch):
    from tests.zmq_fakes import FakeContext

    monkeypatch.setattr("zmq.Context", FakeContext)


@pytest.fixture
def client():
    from app.api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def md_reads(monkeypatch):
    """Record, for every .md opened for reading, whether it ran on an event-loop thread."""
    real_open = builtins.open
    on_loop: list[bool] = []

    def spy(path, mode="r", *args, **kwargs):
        if str(path).endswith(".md") and "r" in mode:
            try:
                asyncio.get_running_loop()
                on_loop.append(True)
            except RuntimeError:
                on_loop.append(False)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    return on_loop


def test_debug_result_reads_in_a_thread(client, tmp_path, md_reads):
    from app.api import main

    out = tmp_path / "result.md"
    out.write_text("# converted", encoding="utf-8")
    cid = "off-loop-1"
    main.conversion_status_db[cid] = "completed"
    main.conversion_details_db[cid] = {"output_path": str(out), "filename": "x.pdf"}
    try:
        resp = client.get(f"/debug/conversion/{cid}/result")
        dl = client.get(f"/debug/conversion/{cid}/result?download=true")
    finally:
        main.conversion_status_db.pop(cid, None)
        main.conversion_details_db.pop(cid, None)

    assert resp.status_code == 200 and resp.json()["markdown_content"] == "# converted"
    assert dl.status_code == 200 and dl.text == "# converted"
    assert md_reads == [False, False]


def test_legacy_by_name_lookup_reads_in_a_thread(client, tmp_path, md_reads, monkeypatch):
    from app.api import main

    (tmp_path / "doc.md").write_text("# by name", encoding="utf-8")
    monkeypatch.setattr(main.settings, "CONVERTED_FILES_DIR", str(tmp_path))

    resp = client.get("/converted/doc.md")

    assert resp.status_code == 200 and resp.json()["content"] == "# by name"
    assert md_reads == [False]
