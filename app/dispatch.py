"""Hand tasks to workers one at a time, and notice when a task stops moving.

The API used to PUSH tasks blindly. Once a task left, the API could not tell
"queued behind a 40-minute docling job" from "lost on a dead link" from "the
worker died mid-conversion" — all three read as ``pending`` until the caller's
one-hour timeout, which is what an ingest stuck at 0/2 was.

Now workers ask for work. A worker sends ``ready`` (DEALER) and this dispatcher
(ROUTER) gives it exactly one task, so every task is always in a known place:
queued here, or assigned to a named worker since a known time. Busy workers send
heartbeats (they arrive on the result socket and are fed in via ``note_progress``).

The watchdog turns every silent failure into an explicit one:
  - an assigned task with no heartbeat for ``job_silence_s`` is requeued once,
    then failed ("conversion worker stopped responding");
  - queued tasks fail once no worker has been seen for ``no_worker_s``;
  - ``has_live_worker()`` lets the API refuse new work up front in that state.

Threading: the ROUTER socket is touched only by the thread running ``run()``.
Every other method only touches state under ``_lock``.
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Optional

import zmq

logger = logging.getLogger(__name__)


def task_id(task: dict) -> str:
    return task.get("chunk_id") if task.get("type") == "chunk" else task.get("conversion_id")


@dataclass
class _Worker:
    ident: bytes
    last_seen: float
    idle: bool = False


@dataclass
class _Assignment:
    task: dict
    worker_id: str
    assigned_at: float
    last_progress: float
    attempts: int


@dataclass
class _Queued:
    task: dict
    queued_at: float
    attempts: int = 0


@dataclass
class Dispatcher:
    socket: zmq.Socket
    on_fail: Callable[[dict, str], None]
    job_silence_s: float = 120.0
    no_worker_s: float = 120.0
    worker_ttl_s: float = 30.0
    max_attempts: int = 2
    clock: Callable[[], float] = time.monotonic

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _queue: Deque[_Queued] = field(default_factory=collections.deque, init=False)
    _workers: Dict[str, _Worker] = field(default_factory=dict, init=False)
    _assigned: Dict[str, _Assignment] = field(default_factory=dict, init=False)
    _last_worker_seen: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        # Startup counts as "a worker was just seen": workers need a moment to
        # connect, and refusing everything during it would be wrong.
        self._last_worker_seen = self.clock()

    # ── called from request handlers / the result listener ──────────────────

    def has_live_worker(self) -> bool:
        with self._lock:
            return self.clock() - self._last_worker_seen < self.no_worker_s

    def submit(self, task: dict) -> None:
        with self._lock:
            self._queue.append(_Queued(task, self.clock()))

    def note_progress(self, job_id: str) -> None:
        """A heartbeat or status update for *job_id*: the task is still moving."""
        with self._lock:
            a = self._assigned.get(job_id)
            if a is not None:
                now = self.clock()
                a.last_progress = now
                # A heartbeat is also proof of the WORKER. Without this, one
                # worker deep in a 40-minute docling run read as "no worker" after
                # NO_WORKER_SECONDS, and every new /convert was refused with 503.
                self._last_worker_seen = now
                w = self._workers.get(a.worker_id)
                if w is not None:
                    w.last_seen = now

    def note_done(self, job_id: str) -> None:
        with self._lock:
            self._assigned.pop(job_id, None)

    def state(self, job_id: str) -> Optional[dict]:
        """Where *job_id* is, for the status endpoint; None once it is not tracked."""
        with self._lock:
            for pos, q in enumerate(self._queue, start=1):
                if task_id(q.task) == job_id:
                    return {"state": "queued", "queue_position": pos,
                            "workers": len(self._workers)}
            a = self._assigned.get(job_id)
            if a is not None:
                return {"state": "assigned", "worker_id": a.worker_id,
                        "seconds_since_progress": round(self.clock() - a.last_progress, 1)}
        return None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "workers": len(self._workers),
                "idle_workers": sum(1 for w in self._workers.values() if w.idle),
                "queued": len(self._queue),
                "assigned": len(self._assigned),
                "seconds_since_worker_seen": round(self.clock() - self._last_worker_seen, 1),
            }

    # ── the dispatcher thread ───────────────────────────────────────────────

    def run(self, stop: threading.Event, poll_ms: int = 500) -> None:
        logger.info("Dispatcher thread started")
        while not stop.is_set():
            try:
                self.step(poll_ms)
            except zmq.ZMQError as e:
                if e.errno == zmq.ETERM:
                    logger.info("Context terminated, dispatcher shutting down.")
                    return
                logger.error("ZeroMQ error in dispatcher: %s", e, exc_info=True)
                stop.wait(1.0)
            except Exception as e:  # noqa: BLE001 — the loop must outlive one bad message
                logger.error("Dispatcher error: %s", e, exc_info=True)
                stop.wait(1.0)

    def step(self, poll_ms: int = 0) -> None:
        if self.socket.poll(poll_ms):
            while True:
                try:
                    ident, raw = self.socket.recv_multipart(zmq.NOBLOCK)
                except zmq.Again:
                    break
                self._on_worker_message(ident, raw)
        self._dispatch()
        self._watchdog()

    def _on_worker_message(self, ident: bytes, raw: bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            logger.warning("Dropping non-JSON worker message")
            return
        if msg.get("type") != "ready":
            return
        worker_id = str(msg.get("worker_id") or ident.hex())
        finished = msg.get("finished")
        now = self.clock()
        with self._lock:
            self._last_worker_seen = now
            w = self._workers.get(worker_id)
            if w is None:
                logger.info("Worker %s connected", worker_id)
                w = self._workers[worker_id] = _Worker(ident, now)
            w.ident, w.last_seen = ident, now
            mine = [j for j, a in self._assigned.items() if a.worker_id == worker_id]
            if finished in mine:
                # Its result travels on the other socket and may arrive after
                # this; the worker saying it is done is enough to free it.
                del self._assigned[finished]
                mine.remove(finished)
            # A periodic idle ping that crossed a task on the wire must not make
            # the worker look free while it is busy with that task.
            w.idle = not mine

    def _dispatch(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    return
                free = next(((wid, w) for wid, w in self._workers.items() if w.idle), None)
                if free is None:
                    return
                wid, w = free
                q = self._queue.popleft()
                w.idle = False
                now = self.clock()
                jid = task_id(q.task)
                self._assigned[jid] = _Assignment(q.task, wid, now, now, q.attempts + 1)
            try:
                self.socket.send_multipart([w.ident, json.dumps(q.task).encode()], zmq.NOBLOCK)
                logger.info("Assigned %s to worker %s (attempt %d)", jid, wid, q.attempts + 1)
            except zmq.ZMQError as e:
                # ROUTER_MANDATORY: the worker is gone. Forget it and put the task back.
                logger.warning("Worker %s unreachable (%s); requeueing %s", wid, e, jid)
                with self._lock:
                    self._workers.pop(wid, None)
                    self._assigned.pop(jid, None)
                    self._queue.appendleft(q)

    def _watchdog(self) -> None:
        now = self.clock()
        failed: list[tuple[dict, str]] = []
        with self._lock:
            for wid, w in list(self._workers.items()):
                if now - w.last_seen > self.worker_ttl_s and not any(
                        a.worker_id == wid for a in self._assigned.values()):
                    logger.warning("Worker %s silent for %.0fs; dropping it", wid, now - w.last_seen)
                    del self._workers[wid]

            for jid, a in list(self._assigned.items()):
                if now - a.last_progress <= self.job_silence_s:
                    continue
                del self._assigned[jid]
                self._workers.pop(a.worker_id, None)
                if a.attempts < self.max_attempts:
                    logger.warning("No progress on %s from %s for %.0fs; requeueing",
                                   jid, a.worker_id, now - a.last_progress)
                    self._queue.appendleft(_Queued(a.task, now, a.attempts))
                else:
                    failed.append((a.task, "conversion worker stopped responding "
                                   f"({a.attempts} attempts)"))

            if not self._workers and now - self._last_worker_seen > self.no_worker_s:
                while self._queue:
                    failed.append((self._queue.popleft().task,
                                   f"no conversion worker connected for {self.no_worker_s:.0f}s"))

        for task, error in failed:
            logger.error("Failing %s: %s", task_id(task), error)
            self.on_fail(task, error)
