"""Every task is always somewhere known, and nothing waits silently.

Real ROUTER/DEALER sockets on localhost; a fake clock drives the timeouts so the
failure cases are instant. Each test is one way an ingest used to sit at
"pending" for an hour.
"""

from __future__ import annotations

import json

import pytest
import zmq

from app.dispatch import Dispatcher


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def ctx():
    c = zmq.Context()
    yield c
    c.destroy(linger=0)


@pytest.fixture
def rig(ctx):
    router = ctx.socket(zmq.ROUTER)
    router.setsockopt(zmq.ROUTER_MANDATORY, 1)
    port = router.bind_to_random_port("tcp://127.0.0.1")
    clock = Clock()
    failed = []
    d = Dispatcher(router, on_fail=lambda task, err: failed.append((task["conversion_id"], err)),
                   job_silence_s=60, no_worker_s=120, worker_ttl_s=30, clock=clock)

    def worker():
        s = ctx.socket(zmq.DEALER)
        s.connect(f"tcp://127.0.0.1:{port}")
        return s

    return d, clock, failed, worker


def ready(sock, worker_id, finished=None):
    sock.send_string(json.dumps({"type": "ready", "worker_id": worker_id, "finished": finished}))


def pump(d, n=5):
    for _ in range(n):
        d.step(poll_ms=50)


def received(sock):
    if sock.poll(500):
        return json.loads(sock.recv_string())["conversion_id"]
    return None


def task(i):
    return {"conversion_id": i, "file_key": f"s/raw/{i}.pdf"}


def test_a_worker_gets_one_task_at_a_time(rig):
    d, _, _, worker = rig
    w = worker()
    d.submit(task("a"))
    d.submit(task("b"))
    ready(w, "w1")
    pump(d)

    assert received(w) == "a"
    assert received(w) is None, "a second task was pushed at a busy worker"
    assert d.state("b") == {"state": "queued", "queue_position": 1, "workers": 1}

    ready(w, "w1", finished="a")
    pump(d)
    assert received(w) == "b"


def test_an_idle_ping_that_crossed_a_task_does_not_free_the_worker(rig):
    d, _, _, worker = rig
    w = worker()
    d.submit(task("a"))
    d.submit(task("b"))
    ready(w, "w1")
    pump(d)
    assert received(w) == "a"

    ready(w, "w1")  # periodic ping sent before "a" arrived — not "finished a"
    pump(d)
    assert received(w) is None


def test_a_worker_that_dies_mid_task_is_detected_and_the_task_moves(rig):
    d, clock, failed, worker = rig
    w1, w2 = worker(), worker()
    d.submit(task("a"))
    ready(w1, "w1")
    pump(d)
    assert received(w1) == "a"

    clock.t += 30
    d.note_progress("a")          # heartbeat: alive
    clock.t += 50
    pump(d)
    assert d.state("a")["state"] == "assigned", "requeued despite a recent heartbeat"

    clock.t += 61                 # w1 went silent (OOMKilled, node lost…)
    ready(w2, "w2")
    pump(d)
    assert received(w2) == "a"
    assert failed == []


def test_a_task_that_keeps_losing_its_worker_fails_instead_of_looping(rig):
    d, clock, failed, worker = rig
    d.submit(task("a"))
    for n in (1, 2):
        w = worker()
        ready(w, f"w{n}")
        pump(d)
        assert received(w) == "a"
        clock.t += 61
        pump(d)

    assert failed == [("a", "conversion worker stopped responding (2 attempts)")]
    assert d.state("a") is None


def test_with_no_worker_queued_tasks_fail_and_new_ones_are_refused(rig):
    d, clock, failed, _ = rig
    assert d.has_live_worker(), "startup must get a grace period"
    d.submit(task("a"))
    clock.t += 121
    pump(d)

    assert failed == [("a", "no conversion worker connected for 120s")]
    assert not d.has_live_worker()


def test_a_worker_that_disconnected_is_not_handed_tasks(rig, ctx):
    d, _, failed, worker = rig
    gone = worker()
    ready(gone, "gone")
    pump(d)                        # registered as idle
    gone.close(linger=0)
    # Let the ROUTER notice the disconnect.
    for _ in range(20):
        d.socket.poll(25)

    d.submit(task("a"))
    live = worker()
    ready(live, "live")
    pump(d, n=10)

    assert received(live) == "a"
    assert failed == []


def test_a_busy_worker_on_a_long_task_still_counts_as_a_worker(rig):
    """Seen on the cluster: one worker 11 minutes into a docling run, heartbeating,
    and the API refusing new work as "no conversion worker" — heartbeats refreshed
    the task but not the worker. A task queued behind it must wait, not fail."""
    d, clock, failed, worker = rig
    w = worker()
    d.submit(task("long"))
    ready(w, "w1")
    pump(d)
    assert received(w) == "long"

    for _ in range(10):            # 10 minutes of heartbeats
        clock.t += 60
        d.note_progress("long")
        pump(d)
    assert d.has_live_worker()

    d.submit(task("next"))
    clock.t += 60
    d.note_progress("long")
    pump(d)
    assert failed == []
    assert d.state("next")["state"] == "queued"
