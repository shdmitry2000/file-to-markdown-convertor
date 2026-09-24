"""The API <-> worker sockets must carry heartbeats, or an idle link dies silently."""

import zmq

from app.zmq_keepalive import HEARTBEAT_IVL_MS, HEARTBEAT_TIMEOUT_MS, apply_keepalive


def test_options_are_set():
    ctx = zmq.Context()
    try:
        sock = apply_keepalive(ctx.socket(zmq.PULL))
        assert sock.getsockopt(zmq.HEARTBEAT_IVL) == HEARTBEAT_IVL_MS
        assert sock.getsockopt(zmq.HEARTBEAT_TIMEOUT) == HEARTBEAT_TIMEOUT_MS
        assert sock.getsockopt(zmq.TCP_KEEPALIVE) == 1
        sock.close(0)
    finally:
        ctx.term()


def test_push_pull_still_delivers():
    ctx = zmq.Context()
    try:
        pull = apply_keepalive(ctx.socket(zmq.PULL))
        port = pull.bind_to_random_port("tcp://127.0.0.1")
        push = apply_keepalive(ctx.socket(zmq.PUSH))
        push.connect(f"tcp://127.0.0.1:{port}")
        push.send_string("hello")
        assert pull.poll(5000)
        assert pull.recv_string() == "hello"
        push.close(0)
        pull.close(0)
    finally:
        ctx.term()
