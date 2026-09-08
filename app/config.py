"""
Configuration management for file-to-markdown-convertor service.

Handles environment detection and path configuration for:
- Standalone mode (local development)
- Docker container mode
- Kubernetes deployment

Configuration priority:
1. Environment variables (highest)
2. .env file
3. Default values (lowest)
"""

import os
from pathlib import Path
from functools import lru_cache
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """The service cannot determine where to write. Raised at construction, because a
    converter that does not know its output directory has nothing useful to do."""


def detect_environment() -> str:
    """
    Detect the runtime environment.
    
    Returns:
        str: 'kubernetes', 'docker', or 'standalone'
    """
    if os.environ.get('KUBERNETES_SERVICE_HOST'):
        return 'kubernetes'
    elif os.path.exists('/.dockerenv') or os.environ.get('DOCKER_CONTAINER', '').lower() == 'true':
        return 'docker'
    else:
        return 'standalone'


class WorkerSettings(BaseSettings):
    """Worker service configuration with environment-aware defaults."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False
    )
    
    # ── Environment Detection ──────────────────────────────────
    ENVIRONMENT: str = detect_environment()
    """Detected environment: 'kubernetes', 'docker', or 'standalone'"""
    
    # ── File Paths ─────────────────────────────────────────────
    CONVERTED_FILES_DIR: str | None = None
    """Directory for converted markdown files. Auto-configured if not set."""
    
    PROJECTS_BASE_PATH: str | None = None
    """Base path for project files (used in Docker/K8s for shared volumes)"""
    
    # ── ZeroMQ Configuration ───────────────────────────────────
    ZEROMQ_HOST: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ZEROMQ_HOST", "ZMQ_HOST"),
    )
    """Peer hostname/IP for task + result sockets (worker connects here).

    Helm charts often set ``ZMQ_HOST``; standalone docs use ``ZEROMQ_HOST``.
    For Kubernetes the robust setup is **worker sidecar + ``127.0.0.1``**
    (see README — avoids kube-proxy / multi-replica split brain).
    """
    
    ZMQ_TASK_PORT: int = 5555
    """ZeroMQ port for task queue (PUSH/PULL). Configurable via env var."""
    
    ZMQ_RESULT_PORT: int = 5556
    """ZeroMQ port for result queue (PUSH/PULL). Configurable via env var."""

    ZMQ_CHUNK_PORT: int = 5557
    """ZeroMQ port for the chunking ROUTER service (REQ/REP). External
    clients (e.g. v2's DoclingHybridChunker plugin) open a REQ socket and
    talk directly to chunk_server. Symmetric topology — no ingress proxy
    or reply_to hack needed. Conversion still uses 5555/5556 PUSH/PULL.
    """
    
    # ── Logging ────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    """Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL"""
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._configure_paths()
        self._configure_zeromq()
    
    def _resolve_projects_base(self):
        """Make PROJECTS_BASE_PATH absolute before anything derives from it.

        Every deployed lane sets it absolutely (/app/projects), so this is a no-op
        there. A RELATIVE value resolves against each process's working directory,
        and this service is started from its own submodule directory while the
        gateway and templates run from the repo root — so the same configured value
        pointed at two different trees. markdown-api then read a store containing no
        spaces at all: every key 404'd, and the converted-file cache was written and
        looked for under a path nothing else could see.

        Delegated to shared.file_storage so the store and this settings object
        cannot disagree about where the root is. Guarded like every other shared
        import here: a standalone build has no shared/ and keeps the raw value.
        """
        base = self.PROJECTS_BASE_PATH
        if not base:
            return
        try:
            from shared.file_storage import _local_root
        except ImportError:
            return
        self.PROJECTS_BASE_PATH = str(_local_root(base))

    def _configure_paths(self):
        """Resolve CONVERTED_FILES_DIR. Does NOT create it — see get_converted_files_dir().

        The default used to be a bare "/app/converted_files" under both docker and
        kubernetes. That path is outside every volume we mount: the shared claim is
        mounted at PROJECTS_BASE_PATH ("/app/projects" in every lane), so "/app" is
        the image's own root filesystem and no lane runs this as root. Creating it
        raised EACCES — and because the mkdir sat in ``__init__``, that happened at
        import, killing the process before anything could report which path failed.

        So: derive from the volume we actually have, and when there is no volume to
        derive from, say which variables are missing instead of guessing a path that
        cannot work.
        """
        self._resolve_projects_base()
        if not self.CONVERTED_FILES_DIR:
            if self.PROJECTS_BASE_PATH:
                self.CONVERTED_FILES_DIR = str(
                    Path(self.PROJECTS_BASE_PATH) / ".cache" / "converted_files"
                )
            elif self.ENVIRONMENT == 'standalone':
                # Local development: a directory inside the checkout, always writable.
                project_root = Path(__file__).parent.parent
                self.CONVERTED_FILES_DIR = str(project_root / "data" / "converted_files")
            else:
                raise ConfigError(
                    f"Cannot place converted files in the {self.ENVIRONMENT} environment: "
                    "set CONVERTED_FILES_DIR, or set PROJECTS_BASE_PATH and it is derived "
                    "as <PROJECTS_BASE_PATH>/.cache/converted_files. Refusing to fall back "
                    "to /app/converted_files, which is outside every mounted volume."
                )
    
    def _configure_zeromq(self):
        """Auto-configure ZeroMQ host based on environment if not explicitly set."""
        peer_override = os.environ.get("MARKDOWN_ZMQ_PEER_HOST")
        if peer_override:
            self.ZEROMQ_HOST = peer_override.strip()
            return

        if self.ZEROMQ_HOST is not None:
            return

        if self.ENVIRONMENT == "kubernetes":
            # Compose historically used service name `api`; cluster DNS uses `markdown-api`.
            self.ZEROMQ_HOST = "markdown-api"
        elif self.ENVIRONMENT == "docker":
            self.ZEROMQ_HOST = "api"
        else:
            self.ZEROMQ_HOST = "localhost"
    
    @property
    def zeromq_task_url(self) -> str:
        """Full ZeroMQ URL for task queue."""
        return f"tcp://{self.ZEROMQ_HOST}:{self.ZMQ_TASK_PORT}"
    
    @property
    def zeromq_chunk_url(self) -> str:
        """Full ZeroMQ URL for the chunking ROUTER service."""
        return f"tcp://{self.ZEROMQ_HOST}:{self.ZMQ_CHUNK_PORT}"

    @property
    def zeromq_result_url(self) -> str:
        """Full ZeroMQ URL for result queue."""
        return f"tcp://{self.ZEROMQ_HOST}:{self.ZMQ_RESULT_PORT}"


@lru_cache
def get_settings() -> WorkerSettings:
    """
    Get cached settings instance.
    
    Returns:
        WorkerSettings: Singleton configuration instance
    """
    return WorkerSettings()


# Convenience function for getting converted files directory
def get_converted_files_dir() -> Path:
    """The converted files directory, created if absent.

    Creation happens here rather than in ``WorkerSettings.__init__`` so an
    unwritable directory is a request-time failure naming the path, not an import-time
    crash. Callers that already mkdir before writing (the worker, the cancel markers)
    are unaffected.
    """
    path = Path(get_settings().CONVERTED_FILES_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path
