from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import zmq
import uuid
import os
import json
from typing import Dict
import threading
import logging
from contextlib import asynccontextmanager

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


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

# Socket to send tasks to workers
task_socket = context.socket(zmq.PUSH)
task_socket.bind("tcp://*:5555")

# Socket to receive results from workers
result_socket = context.socket(zmq.PULL)
result_socket.bind("tcp://*:5556")


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


@app.post("/convert")
async def convert_file(request: ConversionRequest):
    logger.info(f"Received conversion request for file: {request.file_path}")
    file_path = request.file_path
    if not os.path.exists(file_path):
        logger.warning(f"File not found at path: {file_path}")
        raise HTTPException(status_code=404, detail="File not found")

    conversion_id = str(uuid.uuid4())
    logger.info(f"Generated conversion ID {conversion_id} for file {file_path}")
    conversion_status_db[conversion_id] = "pending"

    task = {"conversion_id": conversion_id, "file_path": file_path}

    logger.info(f"Sending task {conversion_id} to the ZeroMQ queue.")
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


@app.get("/converted/{file_path:path}")
async def get_converted_file(file_path: str):
    logger.info(f"Request to retrieve converted file: {file_path}")
    # Rag-template requests by original name (e.g. 439.pdf); worker writes <stem>.md
    base, ext = os.path.splitext(file_path)
    if ext.lower() in (".pdf", ".docx", ".doc") and not file_path.lower().endswith(".md"):
        lookup_path = os.path.join("converted_files", os.path.basename(base) + ".md")
    else:
        lookup_path = os.path.join("converted_files", file_path)
    if not os.path.exists(lookup_path):
        logger.warning(f"Converted file not found: {lookup_path}")
        raise HTTPException(status_code=404, detail="Converted file not found")
    converted_file_path = lookup_path

    with open(converted_file_path, "r") as f:
        content = f.read()

    return {"content": content}


@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring."""
    return {"status": "healthy", "service": "markdown-api"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
