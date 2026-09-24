"""A ZeroMQ stand-in for tests of the API, where the queue is not the subject.

It behaves like a bound socket with no peer: nothing ever arrives and sends go
nowhere. It WAITS like one, too. The fakes this replaces returned from
``recv_json`` at once, so every background listener started by a TestClient
became a busy loop; they leaked across tests, and after a few files six of them
starved the test thread and a one-second test never finished.

``app.api.main`` is imported once per session, with whichever fake the first
importing test installed — so every API test must install this same one.
"""

from __future__ import annotations

import time

import zmq


class FakeSocket:
    def setsockopt(self, option, value): pass
    def bind(self, address): pass
    def connect(self, address): pass
    def close(self, linger=None): pass

    def poll(self, timeout=None, flags=zmq.POLLIN):
        time.sleep(min(timeout or 0, 1000) / 1000)
        return 0

    def recv_json(self, flags=0): raise zmq.Again()
    def recv_multipart(self, flags=0): raise zmq.Again()
    def send_string(self, data, flags=0): pass
    def send_multipart(self, frames, flags=0): pass


class FakeContext:
    def socket(self, socket_type): return FakeSocket()
    def term(self): pass
