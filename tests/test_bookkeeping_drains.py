"""Every per-job map in the API drains once its jobs are over.

The API is the long-lived half of the service: it holds one entry per conversion in
a handful of dicts (status, details, pending/active, in-flight sharing, waiter
counts, poll leases) and a temp copy of every /debug/convert upload. One map that
keeps an entry per finished job is a leak that grows with every file the platform
has ever converted, until the pod is OOM-killed and every ingest stops with it.
This drives many jobs through the real request and result paths — shared, failed,
cancelled — and checks nothing is left behind but the bounded finished records.
"""

import pytest
from fastapi.testclient import TestClient

JOBS = 120
MAX_FINISHED = 10


@pytest.fixture(autouse=True)
def mock_zmq(monkeypatch):
    from tests.zmq_fakes import FakeContext

    monkeypatch.setattr("zmq.Context", FakeContext)


@pytest.fixture
def api(tmp_path, monkeypatch):
    from app.api import main

    monkeypatch.setattr(main.settings, "CONVERTED_FILES_DIR", str(tmp_path / "converted"))
    (tmp_path / "converted").mkdir()
    for name in ("conversion_status_db", "conversion_details_db", "pending_conversions_db",
                 "active_conversions_db", "inflight_conversions", "latest_conversion_by_name",
                 "conversion_waiters", "last_polled_at", "_output_readers"):
        monkeypatch.setattr(main, name, {})
    monkeypatch.setattr(main, "_MAX_FINISHED_CONVERSIONS", MAX_FINISHED)
    queued: list[dict] = []
    monkeypatch.setattr(main, "enqueue_task", queued.append)
    with TestClient(main.app) as c:
        yield c, main, tmp_path, queued


def _finish(main, task, status):
    cid = task["conversion_id"]
    main._apply_result({"conversion_id": cid, "status": "processing"})
    main._apply_result({"conversion_id": cid, "status": status, "error": "x" if status == "failed" else None})


def test_many_jobs_leave_nothing_behind(api):
    client, main, tmp, queued = api
    sources = []
    for i in range(JOBS):
        p = tmp / f"doc{i}.pdf"
        p.write_bytes(b"%PDF-1.4 minimal")
        sources.append(p)

    ids = []
    for i, p in enumerate(sources):
        r = client.post("/convert", json={"file_path": str(p)})
        assert r.status_code == 200
        ids.append(r.json()["conversion_id"])
        if i % 4 == 0:  # a second caller of the same file shares the queued job
            again = client.post("/convert", json={"file_path": str(p)})
            assert again.json()["conversion_id"] == ids[-1]
    for i in range(JOBS // 4):
        r = client.post("/debug/convert", files={"file": (f"upload{i}.pdf", b"%PDF-1.4 minimal")})
        assert r.status_code == 200

    assert len(queued) == JOBS + JOBS // 4, "a shared job was queued twice"
    statuses = ["completed", "failed", "cancelled", "error"]
    for n, task in enumerate(queued):
        _finish(main, task, statuses[n % len(statuses)])

    assert main.pending_conversions_db == {}
    assert main.active_conversions_db == {}
    assert main.inflight_conversions == {}
    assert main.conversion_waiters == {}
    assert main.last_polled_at == {}
    leftovers = [p.name for p in (tmp / "converted").iterdir() if p.name.startswith("debug_")]
    assert leftovers == [], f"debug uploads left on disk: {leftovers[:5]}"

    # The finished records are kept for late pollers, but only the newest few.
    client.post("/convert", json={"file_path": str(sources[0])})   # any request prunes
    assert len(main.conversion_status_db) <= MAX_FINISHED + 1
    assert len(main.conversion_details_db) <= MAX_FINISHED + 1
    assert len(main.latest_conversion_by_name) <= MAX_FINISHED + 1


def test_a_finished_shared_job_is_not_reused_by_the_next_caller(api):
    """After the job ends, the same file must convert again, not return a dead id."""
    client, main, tmp, queued = api
    p = tmp / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 minimal")
    first = client.post("/convert", json={"file_path": str(p)}).json()["conversion_id"]
    _finish(main, queued[-1], "completed")
    second = client.post("/convert", json={"file_path": str(p)}).json()["conversion_id"]
    assert second != first and len(queued) == 2
