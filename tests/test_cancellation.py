"""Cancelling a conversion so the compute is actually reclaimed.

Measured before this existed: `DELETE /convert/{id}` set a status, the caller
stopped waiting, and the worker converted the document to completion anyway. The
slot freed 12 seconds later because the work FINISHED, not because it was
cancelled. On a 40-minute PDF that is 40 minutes of a three-worker pool spent on
a result nobody will read.

These cover the queued case — the one where the compute actually is, since a
cancelled batch of 300 files has almost all of them still in the queue.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app import cancellation


@pytest.fixture(autouse=True)
def _isolated_markers(tmp_path, monkeypatch):
    monkeypatch.setenv("CANCEL_MARKER_DIR", str(tmp_path / "cancelled"))
    yield


@pytest.fixture(autouse=True)
def _mock_zmq(monkeypatch):
    """`app.api.main` binds its ZeroMQ sockets at IMPORT time, so importing it here
    would bind the ports a running service already holds — and hang. Same approach
    as tests/test_markdown_api.py."""
    class MockSocket:
        def bind(self, address): pass
        def send_string(self, data): pass
        def recv_json(self): return {}
        def close(self): pass

    class MockContext:
        def socket(self, socket_type): return MockSocket()
        def term(self): pass

    monkeypatch.setattr("zmq.Context", lambda: MockContext())


# ------------------------------------------------------------------ the marker

def test_a_conversion_is_not_cancelled_until_it_is():
    assert cancellation.is_cancelled("abc") is False


def test_marking_and_reading_back_across_what_would_be_two_processes():
    """The API writes, a worker reads. Nothing is shared but the filesystem."""
    assert cancellation.request_cancel("abc") is True
    assert cancellation.is_cancelled("abc") is True


def test_only_the_conversion_named_is_cancelled():
    cancellation.request_cancel("abc")
    assert cancellation.is_cancelled("def") is False


def test_clearing_lets_the_id_convert_again():
    cancellation.request_cancel("abc")
    cancellation.clear_cancel("abc")
    assert cancellation.is_cancelled("abc") is False


def test_clearing_something_never_cancelled_is_not_an_error():
    """Called on EVERY terminal status, so the uncancelled case is the common one."""
    cancellation.clear_cancel("never-existed")


def test_cancelling_twice_is_idempotent():
    """The UI will do this — a second click, or a retry of the same request."""
    assert cancellation.request_cancel("abc") is True
    assert cancellation.request_cancel("abc") is True
    assert cancellation.is_cancelled("abc") is True


def test_an_unwritable_marker_directory_does_not_raise(monkeypatch, tmp_path):
    """Losing the ability to reclaim compute must not also break cancelling. The
    caller still stops waiting; only the worker-side saving is lost."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("CANCEL_MARKER_DIR", str(blocker / "cancelled"))

    assert cancellation.request_cancel("abc") is False
    assert cancellation.is_cancelled("abc") is False


# ------------------------------------------------------------------ the worker

class _StopTheLoop(Exception):
    """Breaks out of the worker's `while True` once its tasks are consumed."""


def _run_worker_on(tasks: list[dict], monkeypatch) -> tuple[list[str], list[dict]]:
    """Drive the REAL `worker.main()` over `tasks`; report what it converted and
    what it reported back.

    Deliberately not a re-implementation of main()'s body. An earlier version of
    this helper mirrored the dispatch logic instead of calling it, and when the
    cancellation check was deleted from the worker the test still passed — it was
    exercising the copy. Driving main() means the check has to be where the
    conversions actually come from.
    """
    import zmq

    from app.workers import worker

    monkeypatch.setenv("DOCLING_WARM_WORKER", "false")  # no model loading here
    monkeypatch.setattr("sys.argv", ["worker"])

    remaining = list(tasks)
    reported: list[dict] = []

    class _TaskSocket:
        def connect(self, addr): pass
        def recv_string(self):
            if not remaining:
                raise _StopTheLoop()
            return json.dumps(remaining.pop(0))

    class _ResultSocket:
        def connect(self, addr): pass
        def send_json(self, payload): reported.append(payload)

    class _Context:
        def socket(self, socket_type):
            return _TaskSocket() if socket_type == zmq.PULL else _ResultSocket()

    monkeypatch.setattr(worker.zmq, "Context", lambda: _Context())

    converted: list[str] = []

    def _fake_convert(file_path, conversion_id, socket, converter_type="docling"):
        converted.append(conversion_id)
        socket.send_json({"conversion_id": conversion_id, "status": "completed"})

    monkeypatch.setattr(worker, "convert_file_to_markdown", _fake_convert)

    with pytest.raises(_StopTheLoop):
        worker.main()

    return converted, reported


def test_a_cancelled_job_never_reaches_the_converter(monkeypatch):
    """The whole point: the expensive part must not run."""
    cancellation.request_cancel("job-1")

    converted, reported = _run_worker_on(
        [{"conversion_id": "job-1", "file_path": "/tmp/a.pdf"}], monkeypatch)

    assert converted == [], "the converter ran for a job nobody is waiting for"
    assert reported == [{"conversion_id": "job-1", "status": "cancelled"}]


def test_an_uncancelled_job_converts_exactly_as_before(monkeypatch):
    converted, _ = _run_worker_on(
        [{"conversion_id": "job-2", "file_path": "/tmp/a.pdf"}], monkeypatch)

    assert converted == ["job-2"]


def test_the_queue_keeps_moving_past_a_cancelled_job(monkeypatch):
    """Cancelling one job must not cost the ones behind it — freeing the queue for
    the next conversion is the entire reason this exists."""
    cancellation.request_cancel("cancelled-1")
    cancellation.request_cancel("cancelled-2")

    converted, _ = _run_worker_on([
        {"conversion_id": "cancelled-1", "file_path": "/tmp/a.pdf"},
        {"conversion_id": "wanted-1", "file_path": "/tmp/b.pdf"},
        {"conversion_id": "cancelled-2", "file_path": "/tmp/c.pdf"},
        {"conversion_id": "wanted-2", "file_path": "/tmp/d.pdf"},
    ], monkeypatch)

    assert converted == ["wanted-1", "wanted-2"]


# --------------------------------------------------------------------- the api

def test_the_result_listener_treats_cancelled_as_terminal(monkeypatch):
    """`active_conversions_db` clears only on a terminal status, so without this a
    cancelled job stays 'active' forever and /debug/queue reports a conversion
    nobody is running. That is not a cosmetic problem: an emptied queue is how you
    check whether a cancel freed the pool, and this made the answer unreadable."""
    import zmq

    from app.api import main

    main.conversion_status_db["job-3"] = "processing"
    main.active_conversions_db["job-3"] = {"filename": "a.pdf", "started_at": 0}
    main.inflight_conversions[("/tmp/a.pdf", "docling")] = "job-3"
    cancellation.request_cancel("job-3")

    class _OneResultThenShutdown:
        def __init__(self):
            self.sent = False

        def recv_json(self):
            if self.sent:
                raise zmq.ZMQError(zmq.ETERM)
            self.sent = True
            return {"conversion_id": "job-3", "status": "cancelled"}

    monkeypatch.setattr(main, "result_socket", _OneResultThenShutdown())
    main.result_listener()

    assert "job-3" not in main.active_conversions_db, "still shown as running"
    assert ("/tmp/a.pdf", "docling") not in main.inflight_conversions, (
        "a finished conversion must not stay shareable with new callers"
    )
    assert cancellation.is_cancelled("job-3") is False, (
        "the marker outlived its job — on a shared volume nothing else collects it"
    )


async def test_cancelling_writes_the_marker_the_worker_reads():
    """End of the wire the API owns: the status flip alone changed nothing about
    what the pool was doing."""
    from app.api import main

    main.conversion_status_db["job-4"] = "pending"
    response = await main.cancel_conversion("job-4")

    assert response["status"] == "cancelled"
    assert cancellation.is_cancelled("job-4") is True


async def test_cancelling_a_finished_conversion_marks_nothing():
    """There is no work to stop, and the marker would outlive the job."""
    from app.api import main

    main.conversion_status_db["job-5"] = "completed"
    response = await main.cancel_conversion("job-5")

    assert response["status"] == "completed"
    assert cancellation.is_cancelled("job-5") is False


async def test_cancelling_an_unknown_conversion_is_a_404():
    """Unchanged behaviour, pinned because the marker write sits next to it."""
    from fastapi import HTTPException

    from app.api import main

    with pytest.raises(HTTPException) as exc:
        await main.cancel_conversion("never-existed")
    assert exc.value.status_code == 404


# ------------------------------------------------ a conversion already running

def test_the_poll_loop_notices_a_cancel_and_says_so(monkeypatch):
    """`_await_warm_result` is the only point in the service awake while a
    conversion runs, so it is the only place a cancel can be noticed. docling
    offers no hook of its own — convert() takes no cancellation token, and its
    internal stage stop() documents that a blocked stage is simply abandoned."""
    import queue as queue_module

    from app.workers import worker

    cancellation.request_cancel("running-1")

    class _NeverAnswers:
        def get(self, timeout=None): raise queue_module.Empty()

    class _StillAlive:
        def is_alive(self): return True

    item, reason = worker._await_warm_result(
        _NeverAnswers(), _StillAlive(), timeout_seconds=60,
        conversion_id="running-1")

    assert reason == "cancelled" and item is None


def test_the_poll_loop_leaves_an_uncancelled_conversion_alone():
    """It must keep waiting — a conversion legitimately runs for half an hour."""
    import queue as queue_module

    from app.workers import worker

    class _AnswersOnce:
        def __init__(self): self.calls = 0
        def get(self, timeout=None):
            self.calls += 1
            if self.calls < 3:
                raise queue_module.Empty()
            return ("success", {"markdown": "# ok"})

    class _StillAlive:
        def is_alive(self): return True

    item, reason = worker._await_warm_result(
        _AnswersOnce(), _StillAlive(), timeout_seconds=60,
        conversion_id="not-cancelled")

    assert reason is None and item[0] == "success"


def test_cancelling_mid_conversion_kills_the_worker_holding_it(monkeypatch):
    """Killing the child is the only way to stop docling, and freeing the slot is
    the whole point. The loaded models die with it, so the next job pays a cold
    start — cheap against the minutes of conversion reclaimed."""
    from app.cancellation import ConversionCancelled
    from app.workers import worker

    cancellation.request_cancel("running-2")
    warm = worker._WarmDoclingWorker()
    killed = []

    monkeypatch.setattr(warm, "_ensure", lambda *a, **k: None)
    monkeypatch.setattr(warm, "_kill", lambda: killed.append(True))
    warm._in = type("Q", (), {"put": lambda self, v: None})()
    monkeypatch.setattr(
        worker, "_await_warm_result", lambda *a, **k: (None, "cancelled"))

    with pytest.raises(ConversionCancelled):
        warm.convert("/tmp/a.pdf", "running-2", False, True, 60)

    assert killed == [True], "the slot stays busy until the child is killed"


# --------------------------------------------------- one conversion, N callers

async def test_one_caller_leaving_does_not_cancel_anothers_work():
    """markdown-api deliberately shares ONE conversion between callers of the same
    file. Cancelling used to stop the work outright, so one space cancelling its
    ingest killed the conversion another space was still waiting for — and that
    space saw the file fail for no reason it could observe."""
    from app.api import main

    main.conversion_status_db["shared-1"] = "processing"
    main.conversion_waiters["shared-1"] = 2

    first = await main.cancel_conversion("shared-1")

    assert first["status"] == "processing", "the work must continue"
    assert cancellation.is_cancelled("shared-1") is False
    assert main.conversion_status_db["shared-1"] == "processing"

    second = await main.cancel_conversion("shared-1")

    assert second["status"] == "cancelled", "the last caller left, so stop the work"
    assert cancellation.is_cancelled("shared-1") is True


async def test_a_sole_caller_cancels_immediately():
    from app.api import main

    main.conversion_status_db["sole-1"] = "processing"
    main.conversion_waiters["sole-1"] = 1

    assert (await main.cancel_conversion("sole-1"))["status"] == "cancelled"
    assert cancellation.is_cancelled("sole-1") is True


# --------------------------------------------------------------- the sweeper

def test_a_marker_whose_job_never_reported_back_is_swept(tmp_path):
    """Terminal statuses clear their own marker, so this is for the ones that
    never get one — a cancel for a job never dispatched, or a restart landing
    between the cancel and the worker reporting back. Observed within minutes of
    the queued-cancel change shipping."""
    import os
    import time as _time

    cancellation.request_cancel("ancient")
    cancellation.request_cancel("recent")

    old = cancellation.cancel_dir() / "ancient"
    stale = _time.time() - (cancellation.marker_ttl_seconds() + 60)
    os.utime(old, (stale, stale))

    assert cancellation.sweep() == 1
    assert cancellation.is_cancelled("ancient") is False
    assert cancellation.is_cancelled("recent") is True, "a live cancel was swept"


def test_sweeping_an_absent_directory_is_not_an_error(monkeypatch, tmp_path):
    """It runs on startup, before anything has ever been cancelled."""
    monkeypatch.setenv("CANCEL_MARKER_DIR", str(tmp_path / "never-created"))
    assert cancellation.sweep() == 0
