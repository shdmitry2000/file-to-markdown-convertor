"""Timing knobs read from the environment.

Every timeout in the ingest path goes through here. What counts as "too long"
depends entirely on the corpus and the hardware — a one-hour markdown conversion
is normal on this deployment and absurd on another — so these are settings, not
literals. A timeout that can only be changed by editing Python is one that stays
wrong in the deployment that needed it changed.

The literal passed to these functions is the DEFAULT, not the setting. The setting
lives in .env; see .env.example for the ingest timing block.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def env_seconds(name: str, default: float) -> float:
    """A duration in seconds from ``name``, or ``default``.

    A malformed value warns and falls back rather than refusing to start: a typo in
    a tuning knob should not take a service down, and the default is by definition
    a workable value.
    """
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def env_int(name: str, default: int) -> int:
    """A whole-number knob (poll counts, retry budgets) from ``name``."""
    return int(env_seconds(name, float(default)))
