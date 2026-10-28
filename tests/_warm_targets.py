"""Module-level warm-worker loop targets for the liveness tests.

These stand in for `_warm_docling_loop`. They must live at module level (not as
closures) so the spawn start method can pickle them, matching the pattern in
`_timeout_targets.py`.
"""

import os
import time


def ready_then_die(in_q, out_q, *args):
    """Signal ready, take one job, then die without producing a result.

    Simulates the OOM kill of a warm child: the daemon gets nothing back on
    `out_q` and must notice via liveness polling rather than blocking for the
    full timeout.
    """
    out_q.put(("ready", None))
    in_q.get()
    os._exit(137)  # 128+9, the shell's rendering of SIGKILL/OOM


def ready_then_hang(in_q, out_q, *args):
    """Signal ready, take one job, then hang while staying alive.

    This is the case the timeout path (not the liveness path) must catch.
    """
    out_q.put(("ready", None))
    in_q.get()
    time.sleep(300)


def die_before_ready(in_q, out_q, *args):
    """Exit while "loading models", before any status: an import or config error
    in the child, or an OOM kill during model load."""
    os._exit(1)


def hang_before_ready(in_q, out_q, *args):
    """Stay alive and never report ready: the start timeout must still apply."""
    time.sleep(300)


def slow_vlm_docling_loop(in_q, out_q, *args):
    """The real warm docling loop, with a VLM that takes 1 s per picture.

    Patched here, inside the spawned child, because parent patches do not cross
    `spawn`. Lets a test drive a slow picture-description run through the real
    stall watch.
    """
    from unittest.mock import patch

    from app.workers.worker import _warm_docling_loop

    def slow(self, img_b64):
        time.sleep(1)
        return "תיאור"

    with patch("app.converters.vlm.VLMConverter.__init__", return_value=None), \
         patch("app.converters.vlm.VLMConverter.transcribe_page", slow):
        _warm_docling_loop(in_q, out_q, *args)
