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
