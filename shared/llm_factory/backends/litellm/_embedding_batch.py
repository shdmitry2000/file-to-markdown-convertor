"""Shared batch-size cap for embedding clients.

Vertex AI / Gemini embedding endpoints reject any request carrying more than
250 instances ("batchSize value of N but the supported range is from 1 to 251
exclusive"). Callers hand the factory arbitrarily long chunk lists, so every
embedding client splits its input into sub-batches before hitting the provider.

The batch size is 100 — what every request actually carried before 1.4.8.5: the
embedder plugin split into 100s before this module's 250 ceiling (the setting
EMBEDDING_BATCH_SIZE was removed in 1.4.8.5). Vertex also caps tokens per request, so
a larger batch of long chunks can be refused where 100 never was.
"""

from __future__ import annotations

from typing import Awaitable, Callable

# Vertex AI / Gemini hard limit: requests must carry < 251 instances.
MAX_EMBEDDING_BATCH_SIZE = 250


_BATCH_SIZE = 100


def embedding_batch_size() -> int:
    return min(_BATCH_SIZE, MAX_EMBEDDING_BATCH_SIZE)


async def embed_in_batches(
    inputs: list[str],
    embed_one_batch: Callable[[list[str]], Awaitable[list[list[float]]]],
) -> list[list[float]]:
    """Embed ``inputs`` by calling ``embed_one_batch`` over chunks of the
    configured batch size, returning one vector per input in order."""
    if not inputs:
        return []
    size = embedding_batch_size()
    if len(inputs) <= size:
        return await embed_one_batch(inputs)
    out: list[list[float]] = []
    for i in range(0, len(inputs), size):
        out.extend(await embed_one_batch(inputs[i : i + size]))
    return out
