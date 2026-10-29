from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel
import zmq
import uuid
import os
import re
import json
import time
import asyncio
from typing import Any, Dict, Optional
import threading
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from app import __version__, embedded_workers, llm_settings, storage

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Import configuration and registry
from app.cancellation import clear_cancel, request_cancel
from app.cancellation import sweep as sweep_cancel_markers
from app.config import configure_litellm_logging, get_converted_files_dir, get_settings
from app.format_routes import converter_for
from app.registry import registry
from app.dispatch import Dispatcher, task_id
from app.zmq_keepalive import apply_keepalive
configure_litellm_logging()

# Import all converters so their @register_converter decorators run
import app.converters.pymupdf      # noqa: F401
import app.converters.markitdown   # noqa: F401
import app.converters.vlm          # noqa: F401
import app.converters.dbank        # noqa: F401
import app.converters.docling      # noqa: F401

# Import chunkers so their @register_chunker decorators run
import app.chunkers                # noqa: F401

# Load settings
settings = get_settings()
logger.info(f"Starting in {settings.ENVIRONMENT} mode")
logger.info(f"Converted files directory: {settings.CONVERTED_FILES_DIR}")


def _cancel_marker_sweeper():
    """Sweep stale cancel markers at startup and then hourly. Never raises: a
    housekeeping thread that dies must not take conversions with it."""
    interval = int(os.getenv("CANCEL_MARKER_SWEEP_INTERVAL_SECONDS", "3600"))
    while True:
        try:
            sweep_cancel_markers()
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel-marker sweep failed: %s", exc)
        time.sleep(interval)


_FINISHED = ("completed", "success", "failed", "error", "cancelled")


def abandon_unpolled(now: Optional[float] = None) -> list:
    """Cancel unfinished jobs whose poll lease ran out. Returns their ids.

    Cancelled the way the last caller's DELETE cancels: the marker stops a queued
    job before it starts and a running one at the worker's next check. The waiter
    count does not matter here — callers that stopped polling are gone, however
    many said they wanted the job.
    """
    if _ABANDONED_SECONDS <= 0:
        return []
    now = time.time() if now is None else now
    cancelled = []
    for conversion_id, polled in list(last_polled_at.items()):
        if now - polled <= _ABANDONED_SECONDS:
            continue
        if conversion_status_db.get(conversion_id) in _FINISHED or conversion_id not in conversion_status_db:
            last_polled_at.pop(conversion_id, None)
            continue
        request_cancel(conversion_id)
        conversion_status_db[conversion_id] = "cancelled"
        conversion_waiters[conversion_id] = 0
        last_polled_at.pop(conversion_id, None)
        logger.warning("Conversion %s cancelled: nobody polled it for %.0fs (caller gone)",
                       conversion_id, now - polled)
        cancelled.append(conversion_id)
    return cancelled


def _abandoned_conversion_sweeper():
    """Cancel jobs nobody waits for, every quarter lease (at least each minute).
    Never raises: housekeeping that dies must not take conversions with it."""
    interval = max(15.0, min(60.0, _ABANDONED_SECONDS / 4))
    while True:
        time.sleep(interval)
        try:
            abandon_unpolled()
        except Exception as exc:  # noqa: BLE001
            logger.warning("abandoned-conversion sweep failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manages application lifespan events for startup and shutdown."""
    # Startup logic. /health reports these threads, so one that dies is a pod
    # restart instead of a service that accepts work and never finishes it.
    global _listener_thread, _dispatcher_thread
    if not storage.keys_available():
        # Not fatal: uploads and file_path conversions still work. But every
        # storage-key /convert — what the templates send — will answer 503.
        logger.error(
            "shared.file_storage is not in this image: storage-key /convert requests "
            "will be refused with 503. Rebuild with shared/ vendored."
        )
    stop = threading.Event()
    logger.info("Starting result listener background thread.")
    _listener_thread = threading.Thread(target=result_listener, args=(stop,), daemon=True)
    _listener_thread.start()
    logger.info("Starting dispatcher background thread.")
    _dispatcher_thread = threading.Thread(target=dispatcher.run, args=(stop,), daemon=True)
    _dispatcher_thread.start()

    # Markers are cleared when a conversion reaches a terminal status, so this is
    # only for the ones that never do — a cancel whose job was never dispatched,
    # or a restart landing between the cancel and the worker reporting back.
    # Nothing else collects those, and they live on a shared volume.
    logger.info("Starting cancel-marker sweeper background thread.")
    threading.Thread(target=_cancel_marker_sweeper, daemon=True).start()
    if _ABANDONED_SECONDS > 0:
        logger.info("Starting abandoned-conversion sweeper (lease %.0fs).", _ABANDONED_SECONDS)
        threading.Thread(target=_abandoned_conversion_sweeper, daemon=True).start()

    # Started after the sockets above are bound, so the workers connect at once.
    global _embedded_workers
    count = embedded_workers.configured_count()
    if count:
        _embedded_workers = embedded_workers.EmbeddedWorkers(count)
        _embedded_workers.start()

    yield

    # Shutdown logic
    if _embedded_workers is not None:
        logger.info("Stopping embedded conversion workers.")
        _embedded_workers.stop()
    logger.info("Shutting down: closing ZeroMQ sockets and context.")
    stop.set()
    _listener_thread.join(timeout=2)
    _dispatcher_thread.join(timeout=2)
    task_socket.close()
    result_socket.close()
    context.term()


_listener_thread: Optional[threading.Thread] = None
_dispatcher_thread: Optional[threading.Thread] = None
_embedded_workers: Optional[embedded_workers.EmbeddedWorkers] = None


app = FastAPI(version=__version__, lifespan=lifespan)

# Guarded like every other shared/ import in this service (see vlm.py,
# worker.py): the platform libs are vendored only when the image is built from
# the enterprise repo, and are absent in a standalone submodule build. An
# unconditional import here made the container exit at startup with
# ModuleNotFoundError instead of simply running without the config report.
try:
    from shared.config_report_api import register_config_report
except ImportError:
    pass
else:
    register_config_report(app, "markdown-api")

# In-memory database to store conversion status
conversion_status_db: Dict[str, str] = {}
conversion_details_db: Dict[str, Dict] = {}  # Stores detailed info about conversions
pending_conversions_db: Dict[str, Dict] = {}  # Pending conversions with metadata
active_conversions_db: Dict[str, Dict] = {}  # Active conversions with metadata
# (file_path, converter_type) -> conversion_id, for conversions not yet finished.
# Ingesting one file to N RAG systems fans out N concurrent /convert calls for the
# SAME file; without this every one of them queues its own full conversion behind
# the others. Callers share a conversion_id instead and all poll the same job.
inflight_conversions: Dict[tuple, str] = {}
# display name -> latest conversion_id. Output is written under an internal md5
# name (reported as `output_file`), so the legacy by-name /converted route maps a
# name to its newest conversion here.
latest_conversion_by_name: Dict[str, str] = {}
# conversion_id -> how many callers are waiting on it. The sharing above means a
# cancel is not necessarily "nobody wants this any more": one space cancelling its
# ingest must not stop the conversion another space is still waiting for. Only the
# LAST waiter to leave cancels the work.
conversion_waiters: Dict[str, int] = {}
# conversion_id -> when a caller last asked about it (created, polled, result read).
# Callers poll every couple of seconds until the job is over, so a lease that runs
# out means nobody is waiting: a template pod that died mid-run, a space deleted
# under its ingest. abandon_unpolled() cancels those jobs, which nothing else could:
# their ids were in the dead caller's memory.
last_polled_at: Dict[str, float] = {}
#: Seconds without a poll before an unfinished job is cancelled; 0 turns it off.
_ABANDONED_SECONDS = float(os.getenv("CONVERSION_ABANDONED_SECONDS", "900"))
# output file name -> callers that have not fetched it yet. The output is a hand-off,
# not an archive: the caller stores its own copy, so the file is deleted once the
# last caller has read it (see _release_output). Keyed by FILE, not job: two jobs
# for identical bytes write the same <md5>.md.
_output_readers: Dict[str, int] = {}
last_completion_timestamp = time.time()  # Track last successful conversion

# In-memory database for chunking tasks
chunk_status_db: Dict[str, str] = {}
chunk_results_db: Dict[str, Dict] = {}  # Stores chunk results

# ZeroMQ setup
context = zmq.Context()

# Tasks go to workers that ask for them: each worker's DEALER says "ready" and the
# dispatcher's ROUTER hands it exactly one task — see app/dispatch.py for why a
# blind PUSH could leave an ingest "pending" for an hour. LINGER 0: on shutdown,
# undelivered tasks are dropped rather than holding the pod open until SIGKILL.
task_socket = apply_keepalive(context.socket(zmq.ROUTER))
task_socket.setsockopt(zmq.ROUTER_MANDATORY, 1)
task_socket.setsockopt(zmq.LINGER, 0)
task_socket.bind(f"tcp://*:{settings.ZMQ_TASK_PORT}")
logger.info(f"Task socket bound to port {settings.ZMQ_TASK_PORT}")


def _fail_task(task: dict, error: str) -> None:
    """The dispatcher gave up on *task*: report it exactly as a worker failure."""
    # retryable: the service lost the task, the document did nothing wrong. The
    # caller must not dead-letter the file for it.
    if task.get("type") == "chunk":
        _apply_result({"type": "chunk", "chunk_id": task.get("chunk_id"),
                       "status": "failed", "error": error, "retryable": True})
    else:
        _apply_result({"conversion_id": task.get("conversion_id"),
                       "status": "failed", "error": error, "retryable": True})


dispatcher = Dispatcher(
    task_socket,
    on_fail=_fail_task,
    job_silence_s=settings.JOB_SILENCE_SECONDS,
    no_worker_s=settings.NO_WORKER_SECONDS,
)


class WorkerUnavailable(Exception):
    """No worker has been seen for NO_WORKER_SECONDS."""


def enqueue_task(task: dict) -> None:
    """Queue *task* for the next free worker. Never blocks.

    Refused up front when no worker has asked for work in NO_WORKER_SECONDS: the
    caller gets a 503 it can retry now, rather than a task that would only be
    failed by the watchdog later.
    """
    if not dispatcher.has_live_worker():
        raise WorkerUnavailable()
    dispatcher.submit(task)


def _no_worker_error() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=(
            "no conversion worker connected for "
            f"{settings.NO_WORKER_SECONDS:g}s; retry later"
        ),
    )

# Socket to receive results from workers
result_socket = apply_keepalive(context.socket(zmq.PULL))
result_socket.setsockopt(zmq.LINGER, 0)
result_socket.bind(f"tcp://*:{settings.ZMQ_RESULT_PORT}")
logger.info(f"Result socket bound to port {settings.ZMQ_RESULT_PORT}")


def _apply_result(result: dict) -> None:
    """Record one message from a worker — or a failure the dispatcher decided on —
    in the job tables."""
    global last_completion_timestamp
    result_type = result.get("type")

    if result_type == "heartbeat":
        job_id = result.get("job_id")
        if isinstance(job_id, str):
            dispatcher.note_progress(job_id)
            # Pages docling has finished so far (its 'assemble' stage), for the status.
            if isinstance(result.get("pages_done"), int):
                d = conversion_details_db.get(job_id)
                if isinstance(d, dict):
                    d["pages_done"] = result["pages_done"]
        return

    if result_type == "chunk":
        # Handle chunking results
        chunk_id = result.get("chunk_id")
        status = result.get("status")
        if isinstance(chunk_id, str) and isinstance(status, str):
            logger.info(f"Received chunk status update for {chunk_id}: {status}")
            chunk_status_db[chunk_id] = status
            chunk_results_db[chunk_id] = result
            dispatcher.note_progress(chunk_id)

            if status in ["completed", "success", "failed", "error"]:
                last_completion_timestamp = time.time()
                dispatcher.note_done(chunk_id)
        return

    # Handle conversion results (existing logic)
    conversion_id = result.get("conversion_id")
    status = result.get("status")
    if isinstance(conversion_id, str) and isinstance(status, str):
        logger.info(f"Received status update for {conversion_id}: {status}")
        conversion_status_db[conversion_id] = status
        # Merged, not replaced: the request side recorded the filename and start time
        # here, which the result lookup and the status's elapsed_seconds need once the
        # caller no longer holds them (a polling caller, after /debug/convert wait=false).
        prior = conversion_details_db.get(conversion_id)
        conversion_details_db[conversion_id] = {**(prior if isinstance(prior, dict) else {}), **result}
        dispatcher.note_progress(conversion_id)

        # Promote pending -> active so /health reflects real work.
        # Without this the counters only ever moved for /debug/convert,
        # which is why /health reported active:0 while a job was wedged.
        if status == "processing":
            meta = pending_conversions_db.pop(conversion_id, {})
            active_conversions_db[conversion_id] = {
                **meta, "started_at": time.time(),
            }

        # Update last completion timestamp if conversion finished.
        # `cancelled` belongs here: a worker that declines a job it
        # was told to skip is DONE with it. Without this the entry
        # stayed in active_conversions_db forever, so /debug/queue
        # reported a conversion nobody was running — and an empty
        # queue after a cancel could not be told from a busy one.
        if status in ["completed", "success", "failed", "error", "cancelled"]:
            last_completion_timestamp = time.time()
            dispatcher.note_done(conversion_id)

            # The job is over, so the marker has nothing left to
            # stop; leaving it behind would accumulate on a shared
            # volume with no one to clean it up.
            clear_cancel(conversion_id)

            # Move from active to completed
            if conversion_id in active_conversions_db:
                del active_conversions_db[conversion_id]
            if conversion_id in pending_conversions_db:
                del pending_conversions_db[conversion_id]
            waiters = conversion_waiters.pop(conversion_id, None)
            last_polled_at.pop(conversion_id, None)
            output_file = result.get("output_file")
            if waiters and isinstance(output_file, str) and _CONVERSION_OUTPUT.match(output_file):
                _output_readers[output_file] = _output_readers.get(output_file, 0) + waiters
            # A /debug/convert upload is a temporary copy of the input: the job is
            # over, so nothing reads it again.
            debug_input = (conversion_details_db.get(conversion_id) or {}).get("debug_input")
            if debug_input:
                _unlink_quietly(debug_input)
            # Finished, so it can no longer be shared by a new caller.
            for key, cid in list(inflight_conversions.items()):
                if cid == conversion_id:
                    del inflight_conversions[key]


def result_listener(stop: threading.Event):
    """Listens for results from workers and updates the status database.

    Only shutdown ends this loop. It used to `break` on any other ZeroMQ error,
    and the API went on accepting conversions whose results nobody would ever
    read — each one "pending" until the caller's hour-long timeout.
    """
    logger.info("Result listener thread started")
    while not stop.is_set():
        try:
            if not result_socket.poll(500):
                continue
            result = result_socket.recv_json()
            if isinstance(result, dict):
                _apply_result(result)
            else:
                logger.warning(f"Received non-dict message: {result}")
        except zmq.ZMQError as e:
            if e.errno == zmq.ETERM:
                logger.info("Context terminated, result listener shutting down.")
                break
            logger.error(f"ZeroMQ error in result listener: {e}", exc_info=True)
            stop.wait(1.0)
        except Exception as e:
            logger.error(
                f"An unexpected error occurred in result listener: {e}", exc_info=True
            )


class ConversionRequest(BaseModel):
    # Exactly one of these. `file_key` addresses the platform's FileStore, so the
    # document can live on the shared claim or in an object store and this service
    # does not need to know which. `file_path` is the original contract: a host path
    # both this pod and the worker pods must be able to see, which is only true when
    # they share a filesystem.
    file_path: Optional[str] = None
    file_key: Optional[str] = None
    converter_type: str = "docling"  # Default to docling for backward compatibility
    # Optional: the LLM for this job's calls — {"mode": "space" | "params", "model",
    # "base_url", "connector_id", "auth_token", "project", "location"} (see
    # app/converters/vlm_lane.py). space = the caller's space connector only;
    # params = this pod's lane with these fields on top. Holds a token: it rides on
    # the task to the worker and is never logged or stored.
    llm: Optional[dict] = None
    # Before 1.4.9: the caller's connector with no mode (vlm_lane's no-mode rules).
    llm_connector: Optional[dict] = None


def _job_lane(llm: Optional[dict], llm_connector: Optional[dict] = None) -> Optional[dict]:
    """The lane a caller sent: ``llm`` (mode defaults to params), else the pre-1.4.9
    ``llm_connector`` as it came (no mode). None = the converter LLM."""
    if llm:
        mode = llm.get("mode") or "params"
        if mode not in ("space", "params"):
            raise HTTPException(status_code=400,
                                detail=f"llm.mode must be 'space' or 'params', got {mode!r}")
        return {**llm, "mode": mode}
    return llm_connector or None


@app.post("/convert")
async def convert_file(request: ConversionRequest):
    logger.info(
        "Received conversion request for %s with converter: %s",
        request.file_key or request.file_path, request.converter_type,
    )
    if bool(request.file_path) == bool(request.file_key):
        raise HTTPException(
            status_code=400,
            detail="send exactly one of file_key (preferred) or file_path",
        )
    lane = llm_settings.job_lane(_job_lane(request.llm, request.llm_connector))

    if request.file_key:
        try:
            file_key = storage.normalize_key(request.file_key)
        except storage.StorageUnavailable as exc:
            # 503, not 400: the request is well-formed, this build cannot serve it.
            raise HTTPException(status_code=503, detail=str(exc))
        except ValueError as exc:
            # Absolute or upward-traversing. Refusing here is the point of keys —
            # the file_path branch below opens whatever it is handed.
            raise HTTPException(status_code=400, detail=str(exc))
        store = storage.file_store()
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="storage keys unavailable in this build; send file_path",
            )
        if not store.exists(file_key):
            logger.warning("No object at key: %s", file_key)
            # Name the key: a bare "File not found" gave the caller nothing to act on.
            raise HTTPException(status_code=404, detail=f"File not found in storage at key {file_key!r}")
        file_path = None
        identity = file_key
        display_name = os.path.basename(file_key)
    else:
        file_path = request.file_path
        if not os.path.exists(file_path):
            logger.warning(f"File not found at path: {file_path}")
            raise HTTPException(status_code=404, detail=f"File not found at path {file_path!r}")
        file_key = None
        identity = file_path
        display_name = os.path.basename(file_path)

    # Share an already-queued conversion of the same file rather than duplicating
    # it — see inflight_conversions. Both callers poll the same id and get the
    # same result, which the polling client already handles unchanged.
    inflight_key = (identity, request.converter_type)
    existing_id = inflight_conversions.get(inflight_key)
    if existing_id and conversion_status_db.get(existing_id) in ("pending", "processing"):
        logger.info(f"Reusing in-flight conversion {existing_id} for {identity}")
        conversion_waiters[existing_id] = conversion_waiters.get(existing_id, 1) + 1
        last_polled_at[existing_id] = time.time()
        return {"conversion_id": existing_id}

    conversion_id = str(uuid.uuid4())
    logger.info(f"Generated conversion ID {conversion_id} for file {identity}")
    conversion_status_db[conversion_id] = "pending"
    inflight_conversions[inflight_key] = conversion_id
    conversion_waiters[conversion_id] = 1
    last_polled_at[conversion_id] = time.time()
    pending_conversions_db[conversion_id] = {
        "filename": display_name,
        "queued_at": time.time(),
    }
    latest_conversion_by_name[display_name] = conversion_id
    _prune_converted_outputs()
    _prune_finished_records()

    task = {
        "conversion_id": conversion_id,
        "file_path": file_path,
        "file_key": file_key,
        "converter_type": request.converter_type,
        **({"llm_connector": lane} if lane else {}),
    }

    logger.info(f"Sending task {conversion_id} to the ZeroMQ queue with converter {request.converter_type}.")
    try:
        enqueue_task(task)
    except WorkerUnavailable:
        # Undo the bookkeeping, or the next request for this file would
        # "reuse" a conversion that was never queued and poll it forever.
        conversion_status_db.pop(conversion_id, None)
        if inflight_conversions.get(inflight_key) == conversion_id:
            del inflight_conversions[inflight_key]
        conversion_waiters.pop(conversion_id, None)
        pending_conversions_db.pop(conversion_id, None)
        logger.error(f"No worker took conversion {conversion_id}; refusing with 503")
        raise _no_worker_error()

    return {"conversion_id": conversion_id}


@app.get("/convert/{conversion_id}")
async def get_status(conversion_id: str):
    logger.info(f"Request for status of conversion ID: {conversion_id}")
    status = conversion_status_db.get(conversion_id)
    if status is None:
        logger.warning(f"Conversion ID not found: {conversion_id}")
        raise HTTPException(status_code=404, detail="Conversion ID not found")
    if conversion_id in last_polled_at:
        last_polled_at[conversion_id] = time.time()
    response: Dict[str, Any] = {"status": status}
    details = conversion_details_db.get(conversion_id)
    if isinstance(details, dict) and details.get("started_at"):
        response["elapsed_seconds"] = round(time.time() - details["started_at"], 1)
    if isinstance(details, dict) and details.get("pages_done"):
        response["pages_done"] = details["pages_done"]
    if isinstance(details, dict) and details.get("error"):
        response["error"] = details["error"]
    if isinstance(details, dict) and details.get("retryable"):
        response["retryable"] = True
    # Where it is while unfinished: queued (and at what position) or assigned
    # (to whom, and how long since it last showed progress).
    where = dispatcher.state(conversion_id)
    if where:
        response.update(where)
    return response


@app.delete("/convert/{conversion_id}")
async def cancel_conversion(conversion_id: str):
    """Cancel a conversion.

    Writes a marker the workers read, so a job that has not started never starts.
    This used to set a status and nothing more: the caller stopped waiting while
    the worker converted the document to completion anyway, so a cancelled batch
    still occupied the whole pool for its full duration.

    A conversion already RUNNING is not interrupted by this — see the worker's
    poll loop for that half. Cancelling a queued job is where the compute is: a
    cancelled 300-file batch has almost all of it still in the queue.
    """
    logger.info(f"Request to cancel conversion ID: {conversion_id}")
    if conversion_id not in conversion_status_db:
        logger.warning(f"Conversion ID not found for cancellation: {conversion_id}")
        raise HTTPException(status_code=404, detail="Conversion ID not found")

    current_status = conversion_status_db[conversion_id]
    if current_status in ["completed", "success", "failed"]:
        logger.info(f"Conversion {conversion_id} already finished with status: {current_status}")
        # This caller leaves without fetching the output: one reader fewer.
        _release_output(conversion_id)
        return {"conversion_id": conversion_id, "status": current_status, "message": "Already finished"}

    # One conversion can be shared by several callers (see inflight_conversions).
    # The caller leaves either way — that is their business — but the WORK only
    # stops when the last one has gone. Without this, one space cancelling its
    # ingest silently killed the conversion another space was still waiting for,
    # and that space would see the file fail for no reason it could observe.
    remaining = max(0, conversion_waiters.get(conversion_id, 1) - 1)
    conversion_waiters[conversion_id] = remaining
    if remaining:
        logger.info(
            f"Conversion {conversion_id} still wanted by {remaining} other caller(s); "
            "not cancelling the work"
        )
        return {
            "conversion_id": conversion_id,
            "status": current_status,
            "message": f"Stopped waiting; {remaining} other caller(s) still want this conversion",
        }

    marked = request_cancel(conversion_id)
    conversion_status_db[conversion_id] = "cancelled"
    logger.info(f"Conversion {conversion_id} marked as cancelled (marker={marked})")
    return {"conversion_id": conversion_id, "status": "cancelled"}


#: Hours an output nobody fetched is kept (a caller that died or timed out, a
#: /debug result); 0 keeps it forever. A fetched output is deleted at once — the
#: templates own their converted markdown (their manifest); this directory is only
#: the service's hand-off, and it holds every space's document text.
_CONVERTED_RETENTION_SECONDS = float(os.getenv("CONVERTED_FILES_RETENTION_HOURS", "24")) * 3600
_CONVERSION_OUTPUT = re.compile(r"^[0-9a-f]{32}\.md$")
#: What the prune may remove: internal outputs and leftover /debug uploads.
_PRUNABLE = re.compile(r"^(?:[0-9a-f]{32}\.md|debug_.+)$")


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError as exc:
        logger.debug("remove %s: %s", path, exc)


def _release_output(conversion_id: Optional[str]) -> None:
    """One caller is done with this job's output; delete it when it was the last.

    An output with no count recorded (the API restarted, or it was read before the
    result arrived here) counts as one reader. Never raises.
    """
    path = _output_path(conversion_id)
    if path is None:
        return
    name = os.path.basename(path)
    left = _output_readers.get(name, 1) - 1
    if left > 0:
        _output_readers[name] = left
        return
    _output_readers.pop(name, None)
    _unlink_quietly(path)


# Finished conversions (and chunk results) kept for status polls and by-name lookups,
# oldest dropped first. These maps only ever grew: one entry per conversion and the
# full chunk list per chunk task, for the life of the pod.
_MAX_FINISHED_CONVERSIONS = 5000
_MAX_FINISHED_CHUNKS = 200
_FINISHED = ("completed", "success", "failed", "error", "cancelled")


def _drop_oldest_finished(status_db: Dict[str, str], keep: int, *details_dbs: Dict) -> list:
    """Drop the oldest finished ids beyond `keep` (dicts keep insertion order = age);
    a pending or processing one is never dropped. Returns the dropped ids."""
    finished = [k for k, v in status_db.items() if v in _FINISHED]
    dropped = finished[: max(0, len(finished) - keep)]
    for k in dropped:
        status_db.pop(k, None)
        for db in details_dbs:
            db.pop(k, None)
    return dropped


def _prune_finished_records() -> None:
    dropped = set(_drop_oldest_finished(
        conversion_status_db, _MAX_FINISHED_CONVERSIONS, conversion_details_db))
    if dropped:
        for name in [n for n, cid in latest_conversion_by_name.items() if cid in dropped]:
            del latest_conversion_by_name[name]
    _drop_oldest_finished(chunk_status_db, _MAX_FINISHED_CHUNKS, chunk_results_db)


def _prune_converted_outputs() -> None:
    """Delete md5-named outputs older than the retention (none when it is 0). Never raises."""
    if _CONVERTED_RETENTION_SECONDS <= 0:
        return
    cutoff = time.time() - _CONVERTED_RETENTION_SECONDS
    try:
        for entry in os.scandir(settings.CONVERTED_FILES_DIR):
            if _PRUNABLE.match(entry.name) and entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)
    except OSError as exc:
        logger.debug("prune converted outputs: %s", exc)


def _read_converted(path: str) -> Dict[str, str]:
    if not os.path.exists(path):
        logger.warning(f"Converted file not found: {path}")
        raise HTTPException(status_code=404, detail="Converted file not found")
    with open(path, "r", encoding="utf-8") as f:
        return {"content": f.read()}


def _output_path(conversion_id: Optional[str]) -> Optional[str]:
    """Where a finished conversion's markdown is: the internal name its worker reported."""
    details = conversion_details_db.get(conversion_id) if conversion_id else None
    name = details.get("output_file") if isinstance(details, dict) else None
    if not name or not _CONVERSION_OUTPUT.match(name):
        return None
    return os.path.join(settings.CONVERTED_FILES_DIR, name)


@app.get("/convert/{conversion_id}/content")
async def get_conversion_content(conversion_id: str):
    """The markdown one conversion produced — addressed by its job, so a caller can
    never receive another job's output for a file with the same name. The internal
    md5 file name is never part of the contract."""
    path = _output_path(conversion_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Converted file not found")
    content = _read_converted(path)
    _release_output(conversion_id)
    return content


@app.get("/converted/{file_path:path}")
async def get_converted_file(file_path: str):
    """Legacy by-name lookup, kept for clients older than /convert/{id}/content."""
    logger.info(f"Request to retrieve converted file: {file_path}")
    converted_dir = settings.CONVERTED_FILES_DIR
    name = os.path.basename(file_path)
    latest_id = latest_conversion_by_name.get(name)
    latest = _output_path(latest_id)
    if latest and os.path.exists(latest):
        content = _read_converted(latest)
        _release_output(latest_id)
        return content
    # Pre-id outputs were <stem>.md for EVERY source type (the old extension list
    # left out .pptx, .csv, images, so those always 404'd here).
    base, ext = os.path.splitext(file_path)
    if ext and ext.lower() != ".md":
        lookup_path = os.path.join(converted_dir, os.path.basename(base) + ".md")
    else:
        lookup_path = os.path.join(converted_dir, file_path)
    if not os.path.exists(lookup_path):
        logger.warning(f"Converted file not found: {lookup_path}")
        raise HTTPException(status_code=404, detail="Converted file not found")
    converted_file_path = lookup_path

    with open(converted_file_path, "r", encoding="utf-8") as f:
        content = f.read()

    return {"content": content}


# ==================== DEBUG ENDPOINTS ====================

class ConverterLLM(BaseModel):
    """The converter LLM, as the fields a space connector has."""
    model: str  # "<connection type>/<model>", e.g. vertex_proxy/gemini-3.8-flash
    base_url: Optional[str] = None
    connector_id: Optional[str] = None
    auth_token: Optional[str] = None
    project: Optional[str] = None
    location: Optional[str] = None


# A 1x1 PNG: the smallest image a vision call accepts.
_PROBE_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
              "hKmMIQAAAABJRU5ErkJggg==")


@app.get("/llm")
async def get_converter_llm():
    """The converter LLM in use (runtime or env) — never the token itself."""
    return llm_settings.describe()


@app.post("/llm/test")
async def test_converter_llm():
    """One tiny vision call on the converter LLM: does it answer, and how fast."""
    from app.converters import vlm_lane

    def probe():
        started = time.time()
        with vlm_lane.job(llm_settings.job_lane(None)):
            spec = vlm_lane.model()
            vlm_lane.client().vision_sync("Reply with the word OK.", [_PROBE_PNG], max_tokens=16)
        return spec, round((time.time() - started) * 1000)

    try:
        spec, ms = await asyncio.to_thread(probe)
    except Exception as exc:  # noqa: BLE001 - the answer IS the error
        # Whole message on one line: the factory puts the missing setting on line two.
        return {"ok": False, "error": f"{type(exc).__name__}: {' '.join(str(exc).split())[:500]}"}
    return {"ok": True, "model": spec, "latency_ms": ms}


def _require_runtime_change() -> None:
    if not llm_settings.runtime_change_enabled():
        raise HTTPException(
            status_code=403,
            detail="the converter LLM is set by this deployment (env/Helm); "
                   "LLM_RUNTIME_CHANGE=true enables changing it here")


@app.put("/llm")
async def put_converter_llm(body: ConverterLLM):
    """Replace the converter LLM until restart (LLM_RUNTIME_CHANGE only)."""
    from app.converters import vlm_lane

    _require_runtime_change()
    value = {k: v for k, v in body.model_dump().items() if v}
    if "/" not in value["model"]:
        raise HTTPException(status_code=400,
                            detail="model is '<connection type>/<model>', e.g. vertex_proxy/gemini-3.8-flash")
    missing = vlm_lane._missing(value, value["model"])
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"{value['model']} needs {', '.join(missing)}")
    try:
        llm_settings.store().set(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info("Converter LLM set at runtime: %s", value["model"])
    return llm_settings.describe()


@app.delete("/llm")
async def delete_converter_llm():
    """Back to the env default (LLM_RUNTIME_CHANGE only)."""
    _require_runtime_change()
    llm_settings.store().clear()
    logger.info("Converter LLM back to the env default")
    return llm_settings.describe()


@app.post("/debug/convert")
async def debug_convert_file(
    file: UploadFile = File(...),
    converter_type: Optional[str] = Form(None),
    wait: bool = Form(False),
    llm: Optional[str] = Form(None),
):
    """Upload a document and test conversion with timing.

    Routes by format so the check exercises the same converter the ingest path
    would pick (.xlsx/.xlsm/.xls → excel, everything else → docling). Pass
    `converter_type` to force a specific one, and `llm` (the JSON object /convert
    takes) to convert with another LLM than the converter LLM.

    Returns as soon as the job is queued (the default, ``wait=false``); poll
    ``GET /convert/{id}`` and fetch ``/debug/conversion/{id}/result``. A large PDF
    takes minutes on CPU (61 pages ≈ 5 min), longer than any proxy in front of a
    single held request allows (an OpenShift Route cuts at 30 s), so no caller should
    wait inline. ``wait=true`` holds the request until done (up to 10 min) — only
    for a direct call that no proxy sits in front of.
    """
    logger.info(f"Debug conversion request for file: {file.filename}")
    try:
        sent = json.loads(llm) if llm else None
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"llm is not JSON: {exc.msg}")
    lane = llm_settings.job_lane(_job_lane(sent))
    start_time = time.time()
    selected_converter = (converter_type or "").strip() or converter_for(file.filename)
    
    # Save uploaded file temporarily
    # get_converted_files_dir() creates the directory: on a fresh PVC (OCP) nothing
    # else has yet, and only the worker used to, so this upload raised FileNotFoundError.
    temp_path = get_converted_files_dir() / f"debug_{file.filename}"
    with open(temp_path, "wb") as f:
        content = await file.read()
        f.write(content)
    
    file_size = temp_path.stat().st_size
    conversion_id = str(uuid.uuid4())
    logger.info(f"Generated debug conversion ID {conversion_id} for {file.filename}")
    
    # Send to worker
    task = {
        "conversion_id": conversion_id,
        "file_path": str(temp_path),
        "converter_type": selected_converter,
        **({"llm_connector": lane} if lane else {}),
    }
    logger.info(f"Debug conversion {conversion_id} routed to converter {selected_converter}")

    try:
        enqueue_task(task)
    except WorkerUnavailable:
        _unlink_quietly(str(temp_path))
        raise _no_worker_error()
    _prune_finished_records()
    conversion_status_db[conversion_id] = "pending"
    last_polled_at[conversion_id] = time.time()
    conversion_details_db[conversion_id] = {
        "filename": file.filename,
        "file_size": file_size,
        "started_at": start_time,
        "debug_input": str(temp_path),
    }
    pending_conversions_db[conversion_id] = {
        "filename": file.filename,
        "queued_at": start_time
    }
    if not wait:
        return {
            "conversion_id": conversion_id,
            "filename": file.filename,
            "file_size": file_size,
            "status": "pending",
            "converter_type": selected_converter,
        }

    # Poll for completion (max 10 minutes)
    max_polls = 300  # 10 min / 2s = 300 polls
    poll_interval = 2.0
    
    for i in range(max_polls):
        await asyncio.sleep(poll_interval)
        status = conversion_status_db.get(conversion_id)
        
        if status in ["completed", "success", "failed", "error"]:
            elapsed = time.time() - start_time
            
            # Try to find output path. The worker names the output after the
            # file it was given — which here is the debug_-prefixed temp copy —
            # so check that first; the unprefixed name is kept for callers that
            # dropped the file in themselves. Checking only the stripped name is
            # why output_available used to report false on every success.
            output_path = None
            converted_dir = Path(settings.CONVERTED_FILES_DIR)
            stem = temp_path.stem.replace("debug_", "")
            reported = _output_path(conversion_id)
            for candidate in ((Path(reported) if reported else converted_dir / "-"),
                              converted_dir / f"debug_{stem}.md", converted_dir / f"{stem}.md"):
                if candidate.exists():
                    output_path = str(candidate)
                    conversion_details_db[conversion_id]["output_path"] = output_path
                    break
            
            return {
                "conversion_id": conversion_id,
                "filename": file.filename,
                "file_size": file_size,
                "status": status,
                "conversion_time_seconds": round(elapsed, 2),
                "converter_type": selected_converter,
                "output_available": output_path is not None
            }
        
        # Move to active on first poll
        if i == 0 and conversion_id in pending_conversions_db:
            active_conversions_db[conversion_id] = pending_conversions_db.pop(conversion_id)
            active_conversions_db[conversion_id]["started_at"] = time.time()
    
    return {
        "conversion_id": conversion_id,
        "filename": file.filename,
        "status": "timeout",
        "conversion_time_seconds": max_polls * poll_interval,
        "error": "Conversion timed out after 10 minutes"
    }


@app.get("/debug/conversion/{conversion_id}/result")
async def get_converted_result(conversion_id: str, download: bool = False):
    """View or download converted markdown."""
    logger.info(f"Request for conversion result: {conversion_id}")
    
    if conversion_id not in conversion_status_db:
        raise HTTPException(status_code=404, detail="Conversion ID not found")
    if conversion_id in last_polled_at:
        last_polled_at[conversion_id] = time.time()
    
    status = conversion_status_db.get(conversion_id)
    if status not in ["completed", "success"]:
        raise HTTPException(status_code=400, detail=f"Conversion not completed (status: {status})")
    
    details = conversion_details_db.get(conversion_id, {})
    output_path = details.get("output_path")
    
    # Try to find output file if not stored
    if not output_path or not Path(output_path).exists():
        # Try to reconstruct path from filename
        filename = details.get("filename", "")
        if filename:
            converted_dir = Path(settings.CONVERTED_FILES_DIR)
            stem = Path(filename).stem
            reported = _output_path(conversion_id)
            potential_paths = [
                Path(reported) if reported else converted_dir / "-",
                converted_dir / f"{stem}.md",
                converted_dir / f"debug_{stem}.md"
            ]
            for p in potential_paths:
                if p.exists():
                    output_path = str(p)
                    break
    
    if not output_path or not Path(output_path).exists():
        raise HTTPException(status_code=404, detail="Converted file not found")
    
    with open(output_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    if download:
        return Response(
            content=content,
            media_type="text/markdown",
            headers={"Content-Disposition": f"attachment; filename={Path(output_path).name}"}
        )
    
    return {
        "conversion_id": conversion_id,
        "filename": details.get("filename"),
        "markdown_length": len(content),
        "markdown_content": content,
        "output_path": output_path
    }


@app.get("/debug/queue")
async def get_queue_status():
    """View pending and active conversions."""
    logger.info("Request for queue status")
    
    current_time = time.time()
    
    return {
        "pending": [
            {
                "conversion_id": conv_id,
                "filename": details.get("filename", "unknown"),
                "queued_at": details.get("queued_at"),
                "wait_time_seconds": int(current_time - details.get("queued_at", current_time))
            }
            for conv_id, details in pending_conversions_db.items()
        ],
        "active": [
            {
                "conversion_id": conv_id,
                "filename": details.get("filename", "unknown"),
                "started_at": details.get("started_at"),
                "duration_seconds": int(current_time - details.get("started_at", current_time))
            }
            for conv_id, details in active_conversions_db.items()
        ],
        "stats": {
            "pending_count": len(pending_conversions_db),
            "active_count": len(active_conversions_db),
            "total_tracked": len(conversion_status_db)
        }
    }


@app.post("/debug/queue/{conversion_id}/cancel")
async def cancel_conversion_debug(conversion_id: str):
    """Cancel a pending or active conversion."""
    logger.info(f"Request to cancel conversion: {conversion_id}")
    
    if conversion_id in pending_conversions_db:
        del pending_conversions_db[conversion_id]
        conversion_status_db[conversion_id] = "cancelled"
        logger.info(f"Removed {conversion_id} from pending queue")
        return {"success": True, "message": "Removed from queue"}
    
    if conversion_id in active_conversions_db:
        del active_conversions_db[conversion_id]
        conversion_status_db[conversion_id] = "cancelled"
        logger.info(f"Marked {conversion_id} as cancelled (was active)")
        return {"success": True, "message": "Marked as cancelled"}
    
    if conversion_id in conversion_status_db:
        status = conversion_status_db[conversion_id]
        return {"success": False, "message": f"Conversion already {status}"}
    
    raise HTTPException(status_code=404, detail="Conversion not found")


# ==================== END DEBUG ENDPOINTS ====================


@app.get("/capabilities")
async def get_capabilities():
    """Return all available PDF converters.
    
    This endpoint exposes the registered converters so clients can discover
    what conversion methods are available. The response updates automatically
    as new converters are added and decorated with @register_converter.
    
    Response example::
    
        {
          "converters": [
            {
              "name": "pymupdf",
              "label": "PyMuPDF",
              "description": "Fast, lightweight. Best for standard digital PDFs with selectable text."
            },
            {
              "name": "markitdown",
              "label": "MarkItDown",
              "description": "Microsoft MarkItDown. Simple and reliable for standard PDFs."
            },
            {
              "name": "vlm",
              "label": "VLM (Vision-Language Model)",
              "description": "Rasterises each page and sends it to an OpenAI-compatible VLM. Best quality for scanned PDFs. Requires a running model endpoint."
            },
            {
              "name": "docling",
              "label": "Docling",
              "description": "Advanced document understanding. Best for complex documents with tables, figures."
            }
          ]
        }
    
    Usage:
        GET http://localhost:8000/capabilities
    """
    return registry.get_capabilities()


# ==================== CHUNK ENDPOINTS ====================


class ChunkRequest(BaseModel):
    """Body for POST /chunk.

    Either `markdown` (text) is provided directly, or the caller uploads a file
    in a future variant. For now JSON-only with `markdown` field is supported.
    """
    markdown: str
    chunker: str = "docling_hybrid"
    params: Dict = {}


class ChunkResponse(BaseModel):
    chunks: list


class ChunkAsyncRequest(BaseModel):
    """Body for POST /chunk-async (async PDF chunking via ZeroMQ)."""
    chunk_id: str
    file_path: str
    chunker: str = "docling_hybrid"
    params: Dict = {}


class ChunkStatusResponse(BaseModel):
    """Response for GET /chunk-status/{chunk_id}."""
    chunk_id: str
    status: str  # pending, processing, completed, failed
    chunks: list = []
    error: str = ""


@app.get("/chunk/capabilities")
async def chunk_capabilities():
    """List registered chunkers.

    Response example::

        {
          "chunkers": [
            {"name": "docling_hybrid",
             "label": "Docling HybridChunker",
             "description": "Context-aware token-aware chunking..."}
          ]
        }
    """
    return registry.get_chunker_capabilities()


@app.post("/chunk", response_model=ChunkResponse)
async def chunk(request: ChunkRequest):
    """Run a registered chunker over a markdown string.

    Synchronous (chunking is fast vs PDF conversion; no ZeroMQ worker dispatch
    needed). For Docling HybridChunker: markdown → DoclingDocument →
    HybridChunker → list of chunks with heading_path + token_count + page.

    Body::

        {
          "markdown": "# Title\\n\\nBody text...",
          "chunker": "docling_hybrid",
          "params": {"max_tokens": 512, "merge_peers": true}
        }

    Response::

        {
          "chunks": [
            {
              "text": "...",
              "heading_path": ["Title", "Subsection"],
              "token_count": 487,
              "page": 3,
              "contextualized_text": "Title > Subsection\\n...",
            },
            ...
          ]
        }
    """
    chunker_impl = registry.get_chunker(request.chunker)
    if chunker_impl is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown chunker {request.chunker!r}. "
                f"Available: see GET /chunk/capabilities"
            ),
        )
    if not request.markdown:
        return {"chunks": []}
    try:
        chunks = await asyncio.to_thread(chunker_impl.chunk, request.markdown, request.params)
    except RuntimeError as exc:
        # Dependency error (e.g. docling not installed).
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.exception("Chunker %s failed", request.chunker)
        raise HTTPException(status_code=500, detail=f"Chunker failed: {exc}")
    return {"chunks": chunks}


@app.post("/chunk-async")
async def submit_chunk_task(request: ChunkAsyncRequest):
    """Submit async chunking task to ZeroMQ workers.
    
    This endpoint is for PDF → chunks flow (no markdown intermediary).
    The worker processes PDF directly via Docling HybridChunker.
    
    Body::
    
        {
          "chunk_id": "unique-id",
          "file_path": "/path/to/document.pdf",
          "chunker": "docling_hybrid",
          "params": {"max_tokens": 512, "merge_peers": true}
        }
    
    Response::
    
        {
          "chunk_id": "unique-id",
          "status": "pending"
        }
    
    Poll GET /chunk-status/{chunk_id} for results.
    """
    task = {
        "type": "chunk",
        "chunk_id": request.chunk_id,
        "file_path": request.file_path,
        "chunker": request.chunker,
        "params": request.params
    }
    
    try:
        enqueue_task(task)
    except WorkerUnavailable:
        raise _no_worker_error()
    _prune_finished_records()
    chunk_status_db[request.chunk_id] = "pending"
    
    logger.info(f"Submitted chunk task {request.chunk_id} for {request.file_path}")
    return {"chunk_id": request.chunk_id, "status": "pending"}


@app.get("/chunk-status/{chunk_id}", response_model=ChunkStatusResponse)
async def get_chunk_status(chunk_id: str):
    """Poll chunking task status.
    
    Response statuses:
    - pending: Task queued
    - processing: Worker is chunking
    - completed: Chunks ready (includes chunks array)
    - failed: Error occurred (includes error message)
    
    Response::
    
        {
          "chunk_id": "unique-id",
          "status": "completed",
          "chunks": [
            {
              "text": "...",
              "index": 0,
              "metadata": {
                "heading_path": ["Title"],
                "page": 1,
                "token_count": 487
              },
              "contextualized_text": "Title\\n..."
            },
            ...
          ]
        }
    """
    if chunk_id not in chunk_status_db:
        raise HTTPException(status_code=404, detail="Chunk ID not found")
    
    status = chunk_status_db[chunk_id]
    response = {"chunk_id": chunk_id, "status": status, "chunks": [], "error": ""}
    
    if status == "completed":
        result = chunk_results_db.get(chunk_id, {})
        response["chunks"] = result.get("chunks", [])
    elif status == "failed":
        result = chunk_results_db.get(chunk_id, {})
        response["error"] = result.get("error", "Unknown error")
    
    return response


# ==================== END CHUNK ENDPOINTS ====================


def _models_status() -> dict:
    """Where docling's models come from. In an air-gapped cluster a conversion can only
    succeed when they are baked into the image (DOCLING_ARTIFACTS_PATH) — otherwise
    docling tries the HuggingFace Hub on the first PDF and fails."""
    path = os.environ.get("DOCLING_ARTIFACTS_PATH") or ""
    present = bool(path) and os.path.isdir(path) and any(os.scandir(path))
    return {
        "docling_artifacts_path": path or None,
        "docling_models_baked": present,
        "hf_home": os.environ.get("HF_HOME"),
        "hf_offline": (os.environ.get("HF_HUB_OFFLINE") or "").lower() in ("1", "true", "yes"),
    }


@app.get("/health")
async def health_check(response: Response):
    """Health check endpoint for monitoring with diagnostic information."""
    # Check if paths are accessible
    projects_base = settings.PROJECTS_BASE_PATH or "not set"
    projects_accessible = os.path.exists(projects_base) if projects_base != "not set" else False
    converted_accessible = os.path.exists(settings.CONVERTED_FILES_DIR)
    
    # Calculate last activity
    seconds_since_last = int(time.time() - last_completion_timestamp)

    # A dead listener or dispatcher means work is accepted and never finished;
    # 503 makes the liveness probe restart the pod instead of leaving it so.
    threads_alive = all(t is not None and t.is_alive()
                        for t in (_listener_thread, _dispatcher_thread))
    if not threads_alive:
        response.status_code = 503

    return {
        "status": "healthy" if threads_alive else "unhealthy",
        "threads_alive": threads_alive,
        "dispatch": dispatcher.snapshot(),
        "embedded_workers": _embedded_workers.snapshot() if _embedded_workers else None,
        "service": "markdown-api",
        "environment": settings.ENVIRONMENT,
        "configuration": {
            "projects_base_path": projects_base,
            "projects_accessible": projects_accessible,
            "converted_files_dir": settings.CONVERTED_FILES_DIR,
            "converted_dir_accessible": converted_accessible,
            "storage_keys_available": storage.keys_available(),
            "zeromq_host": settings.ZEROMQ_HOST
        },
        "zmq_ports": {
            "task_queue": settings.ZMQ_TASK_PORT,
            "result_queue": settings.ZMQ_RESULT_PORT
        },
        "models": _models_status(),
        "queue": {
            "pending": len(pending_conversions_db),
            "active": len(active_conversions_db),
            "last_completion_seconds_ago": seconds_since_last
        }
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
