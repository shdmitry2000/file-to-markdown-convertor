"""Import-time registration of all chunkers (mirrors converters/)."""

# Decorator runs on import → registers in app.registry.
from app.chunkers import docling_hybrid  # noqa: F401
