"""Per-process cache of query embeddings, on every embedding client the factory hands out.

A search embeds its question once; the same question asked again — a user retrying,
an eval run over golden questions, a load test's fixed question set — gets the vector
from here instead of another embedding call. Without it a burst of repeated questions
is a burst of identical Vertex calls, which ends in 429s.

Only a single text is cached (a str, or a one-item list: LightRAG passes its query
that way). Ingest batches pass straight through: they are new text by nature, and
letting them in would evict the questions that do repeat.

Per space: keyed by the request's space (shared.request_context), the embedding spec
and the text. A space holds at most PER_SPACE_ENTRIES and loses its own oldest first,
so one busy space cannot evict another's questions; the process holds at most
MAX_ENTRIES and loses the oldest of all. The spec is in the key because spaces embed
with their own connector's model, and a vector from another model has the wrong
dimension or the wrong space. A call outside any request (a startup probe) is filed
under NO_SPACE.

Concurrent misses on one key share a single call (single flight). Vectors are kept
as float32 (stdlib array: every module imports this, not all of them have numpy) —
~12 KB each at 3072 dims, so the cache tops out near 12 MB.
"""

from __future__ import annotations

import asyncio
import hashlib
from array import array
from collections import OrderedDict

from .deadline import LLMBudgetSpent

MAX_ENTRIES = 1000
PER_SPACE_ENTRIES = 200
NO_SPACE = "-"

# (space, key) in least-recently-used order, across every space.
_cache: OrderedDict[tuple[str, str], array] = OrderedDict()
_inflight: dict[tuple[int, str, str], asyncio.Future] = {}
# Per space — hits: served from the cache; misses: an embedding call; shared: waited
# on another request's call for the same question; errors: a call that failed.
_counts: dict[str, dict[str, int]] = {}


def _space() -> str:
    from shared.request_context import get_current_space

    return str(get_current_space() or NO_SPACE)


def _key(spec: str, text: str) -> str:
    return hashlib.md5(f"{spec}\0{text}".encode("utf-8"), usedforsecurity=False).hexdigest()


def _count(space: str, what: str) -> None:
    c = _counts.setdefault(space, {"hits": 0, "misses": 0, "shared": 0, "errors": 0})
    c[what] += 1


def clear() -> None:
    """Forget every cached vector and reset the counters (tests)."""
    _cache.clear()
    _counts.clear()


def forget_space(space: str) -> int:
    """Drop one space's vectors and counters (its data was deleted). Returns how many."""
    gone = [k for k in _cache if k[0] == str(space)]
    for k in gone:
        del _cache[k]
    _counts.pop(str(space), None)
    return len(gone)


def _rate(c: dict) -> float | None:
    served = c["hits"] + c["shared"]
    asked = served + c["misses"]
    return round(served / asked, 3) if asked else None


def stats() -> dict:
    """This process's cache, in total and per space, since it started (health checks)."""
    entries: dict[str, int] = {}
    for space, _ in _cache:
        entries[space] = entries.get(space, 0) + 1
    totals = {k: sum(c[k] for c in _counts.values()) for k in ("hits", "misses", "shared", "errors")}
    spaces = {s: {"entries": entries.get(s, 0), **c, "hit_rate": _rate(c)}
              for s, c in sorted(_counts.items())}
    return {"entries": len(_cache), "max_entries": MAX_ENTRIES, "per_space_entries": PER_SPACE_ENTRIES,
            **totals, "hit_rate": _rate(totals), "spaces": spaces}


def _put(space: str, key: str, vec: array) -> None:
    _cache[(space, key)] = vec
    _cache.move_to_end((space, key))
    mine = [k for k in _cache if k[0] == space]
    for k in mine[: max(0, len(mine) - PER_SPACE_ENTRIES)]:
        del _cache[k]
    while len(_cache) > MAX_ENTRIES:
        _cache.popitem(last=False)


async def _one(spec: str, text: str, embed) -> list[float]:
    space, key = _space(), _key(spec, text)
    vec = _cache.get((space, key))
    if vec is not None:
        _cache.move_to_end((space, key))
        _count(space, "hits")
        return vec.tolist()
    # Futures belong to a loop; a call on another loop (a worker thread's) embeds itself.
    slot = (id(asyncio.get_running_loop()), space, key)
    pending = _inflight.get(slot)
    if pending is not None:
        try:
            vec = await asyncio.shield(pending)
            _count(space, "shared")
            return vec.tolist()
        except asyncio.CancelledError:
            if not pending.cancelled():
                raise
            # The leader's caller went away, not ours: embed it ourselves.
        except (Exception, LLMBudgetSpent):
            # The leader's call failed, or its request ran out of time (ours may not
            # have): ours gets its own attempt, and its own error.
            pass
    fut = asyncio.get_running_loop().create_future()
    _inflight[slot] = fut
    _count(space, "misses")
    try:
        vec = array("f", (await embed(text))[0])
    except asyncio.CancelledError:
        fut.cancel()
        raise
    except BaseException as e:
        _count(space, "errors")
        fut.set_exception(e)
        fut.exception()  # retrieved: no "never retrieved" warning when nobody waited
        raise
    finally:
        _inflight.pop(slot, None)
    fut.set_result(vec)
    _put(space, key, vec)
    return vec.tolist()


def install(client, spec: str):
    """Give ``client.embedding`` the cache, on the same object (pinned creds and other
    attributes callers set on it keep working). Idempotent."""
    if getattr(client, "_query_cache_spec", None) == spec:
        return client
    embed = getattr(client, "_uncached_embedding", None) or client.embedding

    async def embedding(text: str | list[str]) -> list[list[float]]:
        if isinstance(text, str):
            return [await _one(spec, text, embed)]
        if isinstance(text, list) and len(text) == 1 and isinstance(text[0], str):
            return [await _one(spec, text[0], embed)]
        return await embed(text)

    client._uncached_embedding = embed
    client._query_cache_spec = spec
    client.embedding = embedding
    return client
