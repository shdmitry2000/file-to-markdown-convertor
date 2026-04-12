from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import zmq
import uuid
import os
import json
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

    with open(converted_file_path, "r") as f:
        content = f.read()

    return {"content": content}


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


@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring with diagnostic information."""
    # Check if paths are accessible
    projects_base = settings.PROJECTS_BASE_PATH or "not set"
    projects_accessible = os.path.exists(projects_base) if projects_base != "not set" else False
    converted_accessible = os.path.exists(settings.CONVERTED_FILES_DIR)
    
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
        }
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
