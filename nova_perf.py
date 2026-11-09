"""
Lightweight performance timings for Nova's Diagnostics page.

Timings are kept in memory only (the last 50 of each kind) and contain no
content: just durations and counts. Optionally they are also written to
the log (Settings › Diagnostics › "Log performance timings").
"""
from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque
from contextlib import contextmanager

log = logging.getLogger("nova.perf")
_lock = threading.Lock()
_samples = {}
_units = {}
LOG_TIMINGS = False
KEEP = 50


def record(name, value, unit="ms"):
    with _lock:
        _samples.setdefault(name, deque(maxlen=KEEP)).append(float(value))
        _units[name] = unit
    if LOG_TIMINGS:
        log.info("%s: %.1f %s", name, value, unit)


@contextmanager
def timed(name):
    started = time.perf_counter()
    try:
        yield
    finally:
        record(name, (time.perf_counter() - started) * 1000)


def summary():
    """[(name, last, median, count, unit)] sorted by name."""
    with _lock:
        rows = [(name, values[-1], statistics.median(values), len(values), _units[name])
                for name, values in _samples.items() if values]
    return sorted(rows)


def clear():
    with _lock:
        _samples.clear()
        _units.clear()
