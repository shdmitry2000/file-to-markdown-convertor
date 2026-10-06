"""MARKDOWN_EMBEDDED_WORKERS: in-container workers for charts that publish one port."""

import sys
import time

import pytest

from app import embedded_workers
from app.embedded_workers import EmbeddedWorkers


@pytest.mark.parametrize("raw, expected", [
    (None, 0), ("", 0), ("0", 0), ("2", 2), (" 3 ", 3), ("-1", 0), ("two", 0),
])
def test_configured_count(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(embedded_workers.ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(embedded_workers.ENV_VAR, raw)
    assert embedded_workers.configured_count() == expected


def test_workers_connect_over_loopback_whatever_the_pod_env_says(monkeypatch):
    # The API pod may carry ZMQ_HOST=markdown-api for the separate Deployment;
    # --host wins over it in the worker, so the embedded ones still use loopback.
    monkeypatch.setenv("ZMQ_HOST", "markdown-api")
    cmd = embedded_workers.worker_command()
    assert cmd[1:] == ["-m", "app.workers.worker", "--host", "127.0.0.1"]

    seen = {}

    class _Proc:
        pid = 1
        def poll(self):
            return None

    def popen(command, env):
        seen.update(env)
        return _Proc()

    w = EmbeddedWorkers(1, popen=popen)
    w._spawn(0)
    assert seen["MARKDOWN_ZMQ_PEER_HOST"] == "127.0.0.1"


def _sleeper(seconds):
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _wait_for(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_starts_n_workers_and_stops_them_with_the_api():
    w = EmbeddedWorkers(2, command=_sleeper(60), poll_seconds=0.05)
    w.start()
    try:
        assert w.snapshot() == {"configured": 2, "running": 2, "restarts": 0}
        procs = list(w._procs)
    finally:
        w.stop()
    assert all(p.poll() is not None for p in procs)
    assert w.snapshot()["running"] == 0


def test_a_worker_that_exits_is_restarted_after_backoff():
    w = EmbeddedWorkers(1, command=_sleeper(0), poll_seconds=0.05, backoff_start=0.1)
    w.start()
    try:
        assert _wait_for(lambda: w.snapshot()["restarts"] >= 2)
    finally:
        w.stop()


def test_a_worker_that_cannot_start_is_retried_not_fatal():
    calls = []

    def popen(command, env):
        calls.append(command)
        raise OSError("exec format error")

    w = EmbeddedWorkers(1, popen=popen, backoff_start=0.0)
    w._spawn(0)
    w.check_once()
    assert len(calls) == 2
    assert w.snapshot()["running"] == 0
