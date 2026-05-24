from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel
import zmq
import uuid
import os
import json
import time
import asyncio
from typing import Dict
import threading
import logging
from pathlib import Path
from contextlib import asynccontextmanager

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Import configuration and registry
from app.config import get_settings
from app.registry import registry

# Import all converters so their @register_converter decorators run
import app.converters.pymupdf      # noqa: F401
import app.converters.markitdown   # noqa: F401
import app.converters.vlm          # noqa: F401
import app.converters.docling      # noqa: F401
import app.converters.marker       # noqa: F401

# Import chunkers so their @register_chunker decorators run
import app.chunkers                # noqa: F401

# Load settings
settings = get_settings()
logger.info(f"Starting in {settings.ENVIRONMENT} mode")
logger.info(f"Converted files directory: {settings.CONVERTED_FILES_DIR}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manages application lifespan events for startup and shutdown."""
    # Startup logic
    logger.info("Starting result listener background thread.")
    thread = threading.Thread(target=result_listener, daemon=True)
    thread.start()

    yield

    # Shutdown logic
    logger.info("Shutting down: closing ZeroMQ sockets and context.")
    task_socket.close()
    result_socket.close()
    context.term()


app = FastAPI(lifespan=lifespan)

# In-memory database to store conversion status
conversion_status_db: Dict[str, str] = {}
conversion_details_db: Dict[str, Dict] = {}  # Stores detailed info about conversions
pending_conversions_db: Dict[str, Dict] = {}  # Pending conversions with metadata
active_conversions_db: Dict[str, Dict] = {}  # Active conversions with metadata
last_completion_timestamp = time.time()  # Track last successful conversion

# ZeroMQ setup
context = zmq.Context()

# Socket to send tasks to workers (load ports from settings)
task_socket = context.socket(zmq.PUSH)
task_socket.bind(f"tcp://*:{settings.ZMQ_TASK_PORT}")
logger.info(f"Task socket bound to port {settings.ZMQ_TASK_PORT}")

# Socket to receive results from workers
result_socket = context.socket(zmq.PULL)
result_socket.bind(f"tcp://*:{settings.ZMQ_RESULT_PORT}")
logger.info(f"Result socket bound to port {settings.ZMQ_RESULT_PORT}")


def result_listener():
    """Listens for results from workers and updates the status database."""
    global last_completion_timestamp
    logger.info("Result listener thread started")
    while True:
        try:
            result = result_socket.recv_json()
            if isinstance(result, dict):
                conversion_id = result.get("conversion_id")
                status = result.get("status")
                if isinstance(conversion_id, str) and isinstance(status, str):
                    logger.info(f"Received status update for {conversion_id}: {status}")
                    conversion_status_db[conversion_id] = status
                    
                    # Update last completion timestamp if conversion finished
                    if status in ["completed", "success", "failed", "error"]:
                        last_completion_timestamp = time.time()
                        
                        # Move from active to completed
                        if conversion_id in active_conversions_db:
                            del active_conversions_db[conversion_id]
                        if conversion_id in pending_conversions_db:
                            del pending_conversions_db[conversion_id]
            else:
                logger.warning(f"Received non-dict message: {result}")
        except zmq.ZMQError as e:
            if e.errno == zmq.ETERM:
                logger.info("Context terminated, result listener shutting down.")
                break  # Exit loop if context is terminated
            else:
                logger.error(f"ZeroMQ error in result listener: {e}", exc_info=True)
                break
        except Exception as e:
            logger.error(
                f"An unexpected error occurred in result listener: {e}", exc_info=True
            )


class ConversionRequest(BaseModel):
    file_path: str
    converter_type: str = "docling"  # Default to docling for backward compatibility


@app.post("/convert")
async def convert_file(request: ConversionRequest):
    logger.info(f"Received conversion request for file: {request.file_path} with converter: {request.converter_type}")
    file_path = request.file_path
    if not os.path.exists(file_path):
        logger.warning(f"File not found at path: {file_path}")
        raise HTTPException(status_code=404, detail="File not found")

    conversion_id = str(uuid.uuid4())
    logger.info(f"Generated conversion ID {conversion_id} for file {file_path}")
    conversion_status_db[conversion_id] = "pending"

    task = {
        "conversion_id": conversion_id,
        "file_path": file_path,
        "converter_type": request.converter_type
    }

    logger.info(f"Sending task {conversion_id} to the ZeroMQ queue with converter {request.converter_type}.")
    task_socket.send_string(json.dumps(task))

    return {"conversion_id": conversion_id}


@app.get("/convert/{conversion_id}")
async def get_status(conversion_id: str):
    logger.info(f"Request for status of conversion ID: {conversion_id}")
    status = conversion_status_db.get(conversion_id)
    if status is None:
        logger.warning(f"Conversion ID not found: {conversion_id}")
        raise HTTPException(status_code=404, detail="Conversion ID not found")
    return {"status": status}


@app.delete("/convert/{conversion_id}")
async def cancel_conversion(conversion_id: str):
    """Cancel a conversion (mark as cancelled; worker will ignore result)."""
    logger.info(f"Request to cancel conversion ID: {conversion_id}")
    if conversion_id not in conversion_status_db:
        logger.warning(f"Conversion ID not found for cancellation: {conversion_id}")
        raise HTTPException(status_code=404, detail="Conversion ID not found")
    
    current_status = conversion_status_db[conversion_id]
    if current_status in ["completed", "success", "failed"]:
        logger.info(f"Conversion {conversion_id} already finished with status: {current_status}")
        return {"conversion_id": conversion_id, "status": current_status, "message": "Already finished"}
    
    conversion_status_db[conversion_id] = "cancelled"
    logger.info(f"Conversion {conversion_id} marked as cancelled")
    return {"conversion_id": conversion_id, "status": "cancelled"}


@app.get("/converted/{file_path:path}")
async def get_converted_file(file_path: str):
    logger.info(f"Request to retrieve converted file: {file_path}")
    # Use configured converted files directory
    converted_dir = settings.CONVERTED_FILES_DIR
    # Rag-template requests by original name (e.g. 439.pdf); worker writes <stem>.md
    base, ext = os.path.splitext(file_path)
    if ext.lower() in (".pdf", ".docx", ".doc") and not file_path.lower().endswith(".md"):
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

@app.post("/debug/convert")
async def debug_convert_file(file: UploadFile = File(...)):
    """Upload a PDF and test conversion with timing."""
    logger.info(f"Debug conversion request for file: {file.filename}")
    start_time = time.time()
    
    # Save uploaded file temporarily
    temp_path = Path(settings.CONVERTED_FILES_DIR) / f"debug_{file.filename}"
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
        "converter_type": "docling"
    }
    
    task_socket.send_string(json.dumps(task))
    conversion_status_db[conversion_id] = "pending"
    conversion_details_db[conversion_id] = {
        "filename": file.filename,
        "file_size": file_size,
        "started_at": start_time
    }
    pending_conversions_db[conversion_id] = {
        "filename": file.filename,
        "queued_at": start_time
    }
    
    # Poll for completion (max 10 minutes)
    max_polls = 300  # 10 min / 2s = 300 polls
    poll_interval = 2.0
    
    for i in range(max_polls):
        await asyncio.sleep(poll_interval)
        status = conversion_status_db.get(conversion_id)
        
        if status in ["completed", "success", "failed", "error"]:
            elapsed = time.time() - start_time
            
            # Try to find output path
            output_path = None
            converted_dir = Path(settings.CONVERTED_FILES_DIR)
            stem = temp_path.stem.replace("debug_", "")
            potential_output = converted_dir / f"{stem}.md"
            if potential_output.exists():
                output_path = str(potential_output)
                conversion_details_db[conversion_id]["output_path"] = output_path
            
            return {
                "conversion_id": conversion_id,
                "filename": file.filename,
                "file_size": file_size,
                "status": status,
                "conversion_time_seconds": round(elapsed, 2),
                "converter_type": "docling",
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
            potential_paths = [
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


# ==================== END CHUNK ENDPOINTS ====================


@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring with diagnostic information."""
    # Check if paths are accessible
    projects_base = settings.PROJECTS_BASE_PATH or "not set"
    projects_accessible = os.path.exists(projects_base) if projects_base != "not set" else False
    converted_accessible = os.path.exists(settings.CONVERTED_FILES_DIR)
    
    # Calculate last activity
    seconds_since_last = int(time.time() - last_completion_timestamp)
    
    return {
        "status": "healthy",
        "service": "markdown-api",
        "environment": settings.ENVIRONMENT,
        "configuration": {
            "projects_base_path": projects_base,
            "projects_accessible": projects_accessible,
            "converted_files_dir": settings.CONVERTED_FILES_DIR,
            "converted_dir_accessible": converted_accessible,
            "zeromq_host": settings.ZEROMQ_HOST
        },
        "zmq_ports": {
            "task_queue": settings.ZMQ_TASK_PORT,
            "result_queue": settings.ZMQ_RESULT_PORT
        },
        "queue": {
            "pending": len(pending_conversions_db),
            "active": len(active_conversions_db),
            "last_completion_seconds_ago": seconds_since_last
        }
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
