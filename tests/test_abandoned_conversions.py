"""A conversion nobody is waiting for any more is cancelled.

Every caller polls GET /convert/{id} until the job finishes (every 2 s). When the
caller dies — a template pod restarted mid-run, a space deleted under its ingest —
its jobs stay queued, or keep converting, for no one: the new pod cannot cancel
them because their ids were in the dead process's memory. On the sandbox a killed
run's job was still being converted hours after its space was gone.

So each job carries a poll lease: set when it is created, renewed by every status
poll (and the tester's result fetch). A job whose lease ran out
(CONVERSION_ABANDONED_SECONDS, default 900) is cancelled the way the last caller's
DELETE would cancel it. 0 turns this off.
"""

import pytest

from app import cancellation


@pytest.fixture(autouse=True)
def _isolated_markers(tmp_path, monkeypatch):
    monkeypatch.setenv("CANCEL_MARKER_DIR", str(tmp_path / "cancelled"))
    yield


@pytest.fixture(autouse=True)
def _mock_zmq(monkeypatch):
    """`app.api.main` binds its ZeroMQ sockets at import time (see test_cancellation)."""
    from tests.zmq_fakes import FakeContext

    monkeypatch.setattr("zmq.Context", FakeContext)


@pytest.fixture
def main(monkeypatch):
    from app.api import main as m

    monkeypatch.setattr(m, "_ABANDONED_SECONDS", 900.0)
    for d in (m.conversion_status_db, m.conversion_waiters, m.last_polled_at):
        d.clear()
    return m


def _job(m, cid, status, *, polled_at, waiters=1):
    m.conversion_status_db[cid] = status
    m.conversion_waiters[cid] = waiters
    m.last_polled_at[cid] = polled_at


def test_an_unpolled_queued_job_is_cancelled(main):
    _job(main, "gone-1", "pending", polled_at=1000.0)

    cancelled = main.abandon_unpolled(now=1000.0 + 901)

    assert cancelled == ["gone-1"]
    assert main.conversion_status_db["gone-1"] == "cancelled"
    assert cancellation.is_cancelled("gone-1") is True


def test_a_running_job_nobody_polls_is_cancelled_too(main):
    """The worker reads the marker and stops it; otherwise the pool stays busy."""
    _job(main, "gone-2", "processing", polled_at=0.0)

    assert main.abandon_unpolled(now=5000.0) == ["gone-2"]
    assert cancellation.is_cancelled("gone-2") is True


def test_a_job_polled_recently_is_kept(main):
    _job(main, "live-1", "processing", polled_at=1000.0)

    assert main.abandon_unpolled(now=1000.0 + 60) == []
    assert main.conversion_status_db["live-1"] == "processing"
    assert cancellation.is_cancelled("live-1") is False


async def test_a_status_poll_renews_the_lease(main):
    _job(main, "live-2", "pending", polled_at=0.0)

    await main.get_status("live-2")

    assert main.abandon_unpolled() == []
    assert main.conversion_status_db["live-2"] == "pending"


def test_a_shared_job_nobody_polls_is_cancelled_whatever_the_waiter_count(main):
    """Waiters is a count of callers that SAID they want it; if none of them polls,
    none of them is still there."""
    _job(main, "shared-gone", "pending", polled_at=0.0, waiters=3)

    assert main.abandon_unpolled(now=5000.0) == ["shared-gone"]
    assert main.conversion_waiters["shared-gone"] == 0


def test_finished_jobs_are_left_alone(main):
    for status in ("completed", "success", "failed", "error", "cancelled"):
        _job(main, f"done-{status}", status, polled_at=0.0)

    assert main.abandon_unpolled(now=5000.0) == []
    assert not any(cancellation.is_cancelled(f"done-{s}")
                   for s in ("completed", "success", "failed", "error"))


def test_zero_turns_it_off(main, monkeypatch):
    monkeypatch.setattr(main, "_ABANDONED_SECONDS", 0.0)
    _job(main, "kept", "pending", polled_at=0.0)

    assert main.abandon_unpolled(now=10**9) == []
    assert main.conversion_status_db["kept"] == "pending"


def test_a_job_without_a_lease_is_left_alone(main):
    """A job created before this version (no lease recorded) is not guessed at."""
    main.conversion_status_db["legacy"] = "pending"
    main.conversion_waiters["legacy"] = 1

    assert main.abandon_unpolled(now=10**9) == []
