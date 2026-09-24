"""Keep the API <-> worker ZeroMQ links alive across long idle periods.

Without this, a link that carries no traffic for hours is silently dropped by the
network (conntrack / Service NAT idle expiry) while both processes still believe
it is open. The worker's PULL socket then waits forever, the API's PUSH socket
keeps "sending" into the dead link, and every conversion times out at the API's
10-minute poll — seen on GKE after ~40h idle: worker ESTABLISHED on :5555, API
side with no connection at all.

ZMTP heartbeats (PING/PONG every few seconds) keep the link from ever going idle
and, if the peer does stop answering, close it so ZeroMQ reconnects on its own.
TCP keepalive is the second layer for the same failure below ZeroMQ.
"""

import zmq

HEARTBEAT_IVL_MS = 10_000
HEARTBEAT_TIMEOUT_MS = 30_000


def apply_keepalive(sock: zmq.Socket) -> zmq.Socket:
    """Set heartbeat + TCP keepalive on *sock*; call before bind/connect."""
    sock.setsockopt(zmq.HEARTBEAT_IVL, HEARTBEAT_IVL_MS)
    sock.setsockopt(zmq.HEARTBEAT_TIMEOUT, HEARTBEAT_TIMEOUT_MS)
    sock.setsockopt(zmq.HEARTBEAT_TTL, HEARTBEAT_TIMEOUT_MS)
    sock.setsockopt(zmq.TCP_KEEPALIVE, 1)
    sock.setsockopt(zmq.TCP_KEEPALIVE_IDLE, 60)
    sock.setsockopt(zmq.TCP_KEEPALIVE_INTVL, 15)
    sock.setsockopt(zmq.TCP_KEEPALIVE_CNT, 4)
    return sock
