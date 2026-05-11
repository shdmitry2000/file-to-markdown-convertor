"""Subprocess targets used by test_timeout_mechanism.

Module-level so they survive the `spawn` start method (closures and
locally-defined functions cannot be pickled across the process boundary).
"""

import signal
import time


def succeed_immediately(file_path, converter_type, do_ocr, result_queue):
    """Return a tiny success payload right away."""
    result_queue.put((
        "success",
        {
            "markdown": "# fake",
            "num_pages": 1,
            "doc_name": "fake.pdf",
            "doc_origin": "test",
            "converter_used": converter_type,
        },
    ))


def succeed_with_large_payload(file_path, converter_type, do_ocr, result_queue):
    """Put a multi-MB payload; exercises the queue-drain ordering."""
    big = "x" * (5 * 1024 * 1024)  # 5 MB
    result_queue.put((
        "success",
        {
            "markdown": big,
            "num_pages": 100,
            "doc_name": "big.pdf",
            "doc_origin": "test",
            "converter_used": converter_type,
        },
    ))


def sleep_forever(file_path, converter_type, do_ocr, result_queue):
    """Block until killed — exercises the timeout path."""
    while True:
        time.sleep(1)


def raise_value_error(file_path, converter_type, do_ocr, result_queue):
    """Mimic `_convert_in_subprocess`: catch the exception and put ('error', ...)."""
    try:
        raise ValueError("Intentional converter failure")
    except Exception as e:
        import traceback
        result_queue.put(("error", str(e), traceback.format_exc()))


def crash_silently(file_path, converter_type, do_ocr, result_queue):
    """Exit non-zero without putting anything on the queue."""
    import os
    os._exit(7)


def ignore_sigterm_then_sleep(file_path, converter_type, do_ocr, result_queue):
    """Trap SIGTERM, then sleep — forces the SIGKILL fallback path."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
