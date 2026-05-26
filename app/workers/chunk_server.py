"""Chunking ROUTER server — synchronous REQ/REP over ZeroMQ.

Why a dedicated server (instead of sharing the PUSH/PULL convert queue):

Conversion is naturally asynchronous fire-and-forget — callers submit a PDF,
the result goes into a database keyed by conversion_id, and any client can
poll later. PUSH/PULL with a result-listener fits that perfectly.

Chunking is naturally synchronous — the indexer pipeline blocks on the
chunks for the PDF it's currently ingesting. The right ZeroMQ pattern for
"one request, one reply" is REQ↔ROUTER: the server binds a ROUTER socket,
any number of clients connect a REQ (or DEALER) socket, and ZeroMQ
multiplexes replies back to the right client by identity frame. No
ingress proxy, no reply_to hack, no per-request reply socket binding.

Protocol (over ROUTER multipart frames; the ROUTER identity frame is
handled by zmq and isn't part of the JSON):

  Request:  {"chunker": "docling_hybrid",
             "file_path": "/path/to.pdf",
             "params":    {"max_tokens": 512, "merge_peers": true,
                           "tokenizer": "minishlab/potion-multilingual-128M"}}

  Reply (success):  {"status": "completed", "chunks": [...], "chunk_id": "..."}
  Reply (failure):  {"status": "failed",    "error":  "...",  "chunk_id": "..."}

The chunk_id field is generated server-side and echoed back for logging /
tracing. Clients don't need to supply one (REQ/REP already correlates).

Concurrency: this is a single-process serial loop. Docling parsing is CPU
+ MPS-bound at ~5-10s per PDF on typical Hebrew PDFs — for the indexer
workload (one PDF at a time) serial is fine. If multiple concurrent
ingestion clients become a need, switch to a DEALER<->ROUTER load-balancer
pattern in front of N worker processes; the client-side protocol doesn't
change.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import uuid
from pathlib import Path

import zmq

from app.config import get_settings

# Import chunkers to trigger @register_chunker decorators.
import app.chunkers  # noqa: F401
from app.registry import registry

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


def _handle_request(payload: bytes) -> dict:
    """Decode one request and run the chunker. Always returns a dict; the
    caller wraps it back into a multipart reply."""
    chunk_id = str(uuid.uuid4())
    try:
        task = json.loads(payload)
    except Exception as exc:
        return {"status": "failed", "chunk_id": chunk_id,
                "error": f"invalid JSON request: {exc}"}

    chunker_type = task.get("chunker", "docling_hybrid")
    file_path = task.get("file_path")
    params = task.get("params") or {}

    if not file_path:
        return {"status": "failed", "chunk_id": chunk_id,
                "error": "missing 'file_path'"}
    if not os.path.exists(file_path):
        return {"status": "failed", "chunk_id": chunk_id,
                "error": f"file not found: {file_path}"}

    chunker = registry.get_chunker(chunker_type)
    if chunker is None:
        return {"status": "failed", "chunk_id": chunk_id,
                "error": f"unknown chunker: {chunker_type}"}

    logger.info("[%s] chunking %s with %s", chunk_id, Path(file_path).name, chunker_type)
    try:
        chunks = chunker.chunk(file_path, params)
        logger.info("[%s] produced %d chunks", chunk_id, len(chunks))
        return {"status": "completed", "chunk_id": chunk_id, "chunks": chunks}
    except Exception as exc:
        logger.exception("[%s] chunking failed: %s", chunk_id, exc)
        return {"status": "failed", "chunk_id": chunk_id, "error": str(exc)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Chunking ROUTER server")
    parser.add_argument(
        "--bind", type=str, default=None,
        help="Bind address (default: tcp://*:<ZMQ_CHUNK_PORT>)",
    )
    args = parser.parse_args()

    settings = get_settings()
    bind_addr = args.bind or f"tcp://*:{settings.ZMQ_CHUNK_PORT}"

    ctx = zmq.Context()
    sock = ctx.socket(zmq.ROUTER)
    sock.bind(bind_addr)
    logger.info("chunk_server: ROUTER bound on %s", bind_addr)

    try:
        while True:
            # ROUTER frames: [identity, empty_delimiter, payload]
            # REQ clients prepend identity + empty automatically; the server
            # must echo both back when replying.
            frames = sock.recv_multipart()
            if len(frames) < 2:
                logger.warning("chunk_server: malformed message (%d frames)", len(frames))
                continue
            identity = frames[0]
            payload = frames[-1]
            reply = _handle_request(payload)
            sock.send_multipart([identity, b"", json.dumps(reply).encode("utf-8")])
    except KeyboardInterrupt:
        logger.info("chunk_server: shutting down")
    finally:
        sock.close(0)
        ctx.term()


if __name__ == "__main__":
    main()
