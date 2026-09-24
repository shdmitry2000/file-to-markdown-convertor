"""The real worker loop against the real dispatcher, over real sockets.

Only the converter is faked. Checks the protocol both halves must agree on:
ready -> one task -> heartbeats while it runs -> result -> ready(finished) -> next.
"""

from __future__ import annotations

import threading
import time

import pytest
import zmq

from app.dispatch import Dispatcher


def test_worker_and_dispatcher_agree(monkeypatch):
    from app.workers import worker

    monkeypatch.setattr(worker.settings, "WORKER_READY_INTERVAL_SECONDS", 0.2)

    def slow_convert(file_path, conversion_id, socket, converter_type="docling"):
        time.sleep(1.0)  # long enough for several heartbeats
        socket.send_json({"conversion_id": conversion_id, "status": "completed"})

    monkeypatch.setattr(worker, "convert_file_to_markdown", slow_convert)

    ctx = zmq.Context()
    router = ctx.socket(zmq.ROUTER)
    router.setsockopt(zmq.ROUTER_MANDATORY, 1)
    task_port = router.bind_to_random_port("tcp://127.0.0.1")
    results = ctx.socket(zmq.PULL)
    result_port = results.bind_to_random_port("tcp://127.0.0.1")

    d = Dispatcher(router, on_fail=lambda t, e: pytest.fail(f"{t}: {e}"))
    stop = threading.Event()
    threading.Thread(target=d.run, args=(stop, 50), daemon=True).start()

    dealer = ctx.socket(zmq.DEALER)
    dealer.connect(f"tcp://127.0.0.1:{task_port}")
    push = ctx.socket(zmq.PUSH)
    push.connect(f"tcp://127.0.0.1:{result_port}")
    heartbeat = [None]
    threading.Thread(
        target=worker._serve,
        args=(ctx, dealer, push, f"tcp://127.0.0.1:{result_port}", "w1", heartbeat),
        daemon=True,
    ).start()

    d.submit({"conversion_id": "a", "file_path": "/tmp/a.pdf"})
    d.submit({"conversion_id": "b", "file_path": "/tmp/b.pdf"})

    beats, done = 0, []
    deadline = time.time() + 10
    while len(done) < 2 and time.time() < deadline:
        if results.poll(100):
            msg = results.recv_json()
            if msg.get("type") == "heartbeat":
                beats += 1
                d.note_progress(msg["job_id"])
            elif msg.get("status") == "completed":
                done.append(msg["conversion_id"])
                d.note_done(msg["conversion_id"])

    stop.set()
    if heartbeat[0] is not None:
        heartbeat[0].stop()
    ctx.destroy(linger=0)

    assert done == ["a", "b"], "tasks did not flow through in order"
    assert beats >= 4, f"only {beats} heartbeats during two 1s conversions"
