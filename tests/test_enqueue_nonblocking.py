"""Handing a task to the worker must never freeze the API.

A PUSH send with no worker attached blocks. It used to run on the event loop,
so one /convert while the worker link was down froze /health and every status
poll until the liveness probe killed the pod — losing the in-memory job table.
Now /convert only queues; the dispatcher thread does all socket work.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest


@pytest.fixture(autouse=True)
def mock_zmq(monkeypatch):
    from tests.zmq_fakes import FakeContext

    monkeypatch.setattr("zmq.Context", FakeContext)


@pytest.fixture
def main(tmp_path, monkeypatch):
    monkeypatch.setenv("FILE_STORAGE_BACKEND", "pvc")
    monkeypatch.setenv("PROJECTS_BASE_PATH", str(tmp_path))
    monkeypatch.setenv("CONVERTED_FILES_DIR", str(tmp_path / "converted"))
    from app import storage
    from shared.file_storage import file_store, reset_file_store_cache

    storage.reset()
    reset_file_store_cache()
    file_store().write_bytes("space1/raw/report.pdf", b"%PDF-1.4 minimal")

    import app.api.main as main
    # In-memory job tables outlive the module import; a queued conversion from
    # an earlier test would otherwise be "reused" here.
    for table in (main.conversion_status_db, main.inflight_conversions,
                  main.conversion_waiters, main.pending_conversions_db):
        table.clear()
    main.dispatcher._queue.clear()
    yield main
    main.dispatcher._queue.clear()
    storage.reset()
    reset_file_store_cache()


def _post_convert(main):
    async def go():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await asyncio.wait_for(
                c.post("/convert", json={"file_key": "space1/raw/report.pdf"}), timeout=2)
            s = None
            if r.status_code == 200:
                s = await c.get(f"/convert/{r.json()['conversion_id']}")
            return r, s
    return asyncio.run(go())


def test_convert_queues_without_touching_the_socket(main, monkeypatch):
    class ExplodingSocket:
        def __getattr__(self, name):
            raise AssertionError(f"request handler touched the task socket ({name})")

    monkeypatch.setattr(main.dispatcher, "socket", ExplodingSocket())
    r, status = _post_convert(main)

    assert r.status_code == 200
    body = status.json()
    assert body["status"] == "pending"
    assert body["state"] == "queued" and body["queue_position"] == 1


def test_no_worker_is_503_and_leaves_nothing_to_reuse(main, monkeypatch):
    monkeypatch.setattr(main.dispatcher, "has_live_worker", lambda: False)

    first, _ = _post_convert(main)
    second, _ = _post_convert(main)

    assert first.status_code == 503
    # Not a "reused" conversion id pointing at a task that was never queued.
    assert second.status_code == 503
    assert not main.inflight_conversions
    assert main.dispatcher.snapshot()["queued"] == 0
