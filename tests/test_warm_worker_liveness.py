"""Tests that a dead warm worker is detected immediately, not after the timeout.

Regression cover for the wedge where an OOM-killed docling child left
`_WarmDoclingWorker.convert()` blocked on a bare `out_q.get(timeout=...)` while
holding the worker lock. Because every conversion in the service funnels through
that one lock, a single 163-page PDF stalled all conversions for hours.

The warm loop target is swapped for a module-level stand-in (see
`_warm_targets`); patches in the parent are not inherited across `spawn`, so the
target has to be picklable rather than a MagicMock.
"""

import multiprocessing
import time

import pytest

from app.workers import worker as worker_mod
from app.workers.worker import _WarmDoclingWorker
from tests import _warm_targets as targets


@pytest.fixture(autouse=True, scope="module")
def _force_spawn():
    """Match the production start method so tests catch spawn-only bugs."""
    try:
        multiprocessing.set_start_method("spawn", force=True)
    except RuntimeError:
        pass


class TestWarmWorkerLiveness:
    def test_dead_child_surfaces_immediately(self, monkeypatch):
        """A child that dies without a result must raise in seconds, not after
        `timeout_seconds` — the whole point of the liveness poll."""
        monkeypatch.setattr(worker_mod, "_warm_docling_loop", targets.ready_then_die)
        w = _WarmDoclingWorker()

        started = time.monotonic()
        with pytest.raises(Exception) as exc:
            # A 300s timeout that we expect NOT to be waited out.
            w.convert("/tmp/whatever.pdf", "conv-1", False, True, 300)
        elapsed = time.monotonic() - started

        assert elapsed < 30, f"took {elapsed:.1f}s — liveness poll did not fire"
        assert "died" in str(exc.value)
        # The exit code is the operator's OOM signal; it must survive into the message.
        assert "exit=" in str(exc.value)

    def test_dead_child_does_not_wedge_the_next_job(self, monkeypatch):
        """After a death the worker must respawn cleanly rather than stay poisoned."""
        monkeypatch.setattr(worker_mod, "_warm_docling_loop", targets.ready_then_die)
        w = _WarmDoclingWorker()

        with pytest.raises(Exception):
            w.convert("/tmp/whatever.pdf", "conv-1", False, True, 300)
        assert w._proc is None, "dead worker was not reaped"

        started = time.monotonic()
        with pytest.raises(Exception):
            w.convert("/tmp/whatever.pdf", "conv-2", False, True, 300)
        assert time.monotonic() - started < 30, "second job blocked on the dead worker"

    def test_live_but_hung_child_still_times_out(self, monkeypatch):
        """The liveness poll must not cannibalise the timeout path: a child that
        is alive and stuck still has to hit TimeoutError."""
        monkeypatch.setattr(worker_mod, "_warm_docling_loop", targets.ready_then_hang)
        w = _WarmDoclingWorker()

        started = time.monotonic()
        with pytest.raises(TimeoutError):
            w.convert("/tmp/whatever.pdf", "conv-3", False, True, 5)
        elapsed = time.monotonic() - started

        assert 5 <= elapsed < 30, f"timeout fired at {elapsed:.1f}s, expected ~5s"


class TestWarmWorkerStart:
    """The same wedge during start-up: a child that dies before reporting ready
    used to hold the worker lock for DOCLING_WARM_START_TIMEOUT (300s) per attempt."""

    @pytest.mark.parametrize("worker_cls", ["docling", "dbank"])
    def test_child_dead_before_ready_surfaces_immediately(self, monkeypatch, worker_cls):
        loop = "_warm_docling_loop" if worker_cls == "docling" else "_warm_dbank_loop"
        monkeypatch.setattr(worker_mod, loop, targets.die_before_ready)
        w = _WarmDoclingWorker() if worker_cls == "docling" else worker_mod._WarmDbankWorker()

        started = time.monotonic()
        with pytest.raises(Exception) as exc:
            if worker_cls == "docling":
                w.ensure_started(False, True)
            else:
                w._ensure()
        elapsed = time.monotonic() - started

        assert elapsed < 30, f"took {elapsed:.1f}s — start waited out its timeout"
        assert "exit=1" in str(exc.value) and "before becoming ready" in str(exc.value)
        assert w._proc is None, "dead child was not reaped"

    def test_child_that_never_reports_ready_hits_the_start_timeout(self, monkeypatch):
        monkeypatch.setattr(worker_mod, "_warm_docling_loop", targets.hang_before_ready)
        monkeypatch.setenv("DOCLING_WARM_START_TIMEOUT", "4")
        w = _WarmDoclingWorker()

        started = time.monotonic()
        with pytest.raises(Exception) as exc:
            w.ensure_started(False, True)
        elapsed = time.monotonic() - started

        assert 4 <= elapsed < 30, f"start gave up at {elapsed:.1f}s, expected ~4s"
        assert "did not become ready in 4s" in str(exc.value)
        assert w._proc is None, "hung child was left running"
