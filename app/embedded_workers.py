"""Conversion workers run inside the API container — opt-in via MARKDOWN_EMBEDDED_WORKERS.

The default topology is unchanged: workers are a separate Deployment that reaches
the API's ZMQ sockets (5555/5556) through the ``markdown-api`` Service. That needs
the Service to publish three ports, and a chart whose ``expose`` takes one port
cannot express it — the workers then never connect and every conversion is refused
with 503 after NO_WORKER_SECONDS.

With ``MARKDOWN_EMBEDDED_WORKERS=N`` (N > 0) the API starts N worker processes
itself, connected over 127.0.0.1 — the standalone ``start.sh`` layout, made fit for
a pod: a worker that exits is restarted with backoff, and all of them are stopped
with the API. Nothing crosses the network, so no Service port or NetworkPolicy is
involved. The separate worker Deployment can be scaled to 0, or left running — the
dispatcher hands a task to whichever worker asks for one.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ENV_VAR = "MARKDOWN_EMBEDDED_WORKERS"
LOOPBACK = "127.0.0.1"
_POLL_SECONDS = 2.0
_BACKOFF_START = 5.0
_BACKOFF_MAX = 60.0
_STOP_GRACE_SECONDS = 10.0


def configured_count() -> int:
    """Workers to embed. Unset, empty, 0 or not a number all mean none."""
    raw = (os.environ.get(ENV_VAR) or "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("%s=%r is not a number; no embedded workers started", ENV_VAR, raw)
        return 0


def worker_command() -> list[str]:
    return [sys.executable, "-m", "app.workers.worker", "--host", LOOPBACK]


class EmbeddedWorkers:
    """Starts, watches and stops the in-container worker processes."""

    def __init__(self, count: int, *, command: Optional[list[str]] = None,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen,
                 backoff_start: float = _BACKOFF_START, poll_seconds: float = _POLL_SECONDS):
        self.count = count
        self._command = command or worker_command()
        self._popen = popen
        self._backoff_start = backoff_start
        self._poll_seconds = poll_seconds
        self._procs: list[Optional[subprocess.Popen]] = [None] * count
        self._next_start = [0.0] * count
        self._backoff = [backoff_start] * count
        self._restarts = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _spawn(self, slot: int) -> None:
        env = dict(os.environ, MARKDOWN_ZMQ_PEER_HOST=LOOPBACK)
        try:
            self._procs[slot] = self._popen(self._command, env=env)
            logger.info("embedded worker %d started (pid %s)", slot, self._procs[slot].pid)
        except OSError as exc:
            self._procs[slot] = None
            self._next_start[slot] = time.monotonic() + self._backoff[slot]
            logger.error("embedded worker %d failed to start: %s", slot, exc)

    def start(self) -> None:
        logger.info("starting %d embedded conversion worker(s) on %s", self.count, LOOPBACK)
        for slot in range(self.count):
            self._spawn(slot)
        self._thread = threading.Thread(target=self._watch, name="embedded-workers", daemon=True)
        self._thread.start()

    def check_once(self) -> None:
        """Restart any worker that has exited, once its backoff has elapsed."""
        now = time.monotonic()
        for slot, proc in enumerate(self._procs):
            if proc is not None:
                code = proc.poll()
                if code is None:
                    continue
                self._procs[slot] = None
                self._restarts += 1
                self._next_start[slot] = now + self._backoff[slot]
                logger.warning("embedded worker %d exited with code %s; restarting in %.0fs",
                               slot, code, self._backoff[slot])
                self._backoff[slot] = min(self._backoff[slot] * 2, _BACKOFF_MAX)
                continue
            if now >= self._next_start[slot]:
                self._spawn(slot)

    def _watch(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001 — the watcher must outlive any one failure
                logger.warning("embedded worker watch failed: %s", exc)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_seconds + 1)
        live = [p for p in self._procs if p is not None and p.poll() is None]
        for p in live:
            p.terminate()
        deadline = time.monotonic() + _STOP_GRACE_SECONDS
        for p in live:
            try:
                p.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()

    def snapshot(self) -> dict:
        running = sum(1 for p in self._procs if p is not None and p.poll() is None)
        return {"configured": self.count, "running": running, "restarts": self._restarts}
