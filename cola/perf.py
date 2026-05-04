"""Lightweight runtime perf instrumentation.

Enabled only when the GIT_COLA_PERF environment variable is set at startup.
When disabled, ``time_method`` returns the original function unchanged so
the call sites have zero overhead (no extra stack frame, no branches).

Usage::

    from cola import perf

    @perf.time_method('GraphDelegate.paint')
    def paint(self, painter, option, index): ...

    with perf.timer('inner-loop'):
        do_work()

    perf.dump()  # writes a sorted summary to stderr and resets

Activated by running ``GIT_COLA_PERF=1 ./bin/git-dag``.
"""
from __future__ import annotations
import os
import sys
import time
from collections import defaultdict
from typing import Callable
from typing import TextIO
from typing import TypeVar


ENABLED = bool(os.environ.get('GIT_COLA_PERF'))

F = TypeVar('F', bound=Callable)


class _Stats:
    __slots__ = ('count', 'total_ns', 'min_ns', 'max_ns', 'samples')

    def __init__(self) -> None:
        self.count = 0
        self.total_ns = 0
        self.min_ns = 1 << 63
        self.max_ns = 0
        self.samples: list[int] = []

    def record(self, ns: int) -> None:
        self.count += 1
        self.total_ns += ns
        if ns < self.min_ns:
            self.min_ns = ns
        if ns > self.max_ns:
            self.max_ns = ns
        if len(self.samples) < 4096:
            self.samples.append(ns)
        else:
            # Keep memory bounded; cycle through the slots so we always
            # retain a recent-ish distribution for percentiles.
            self.samples[self.count % 4096] = ns


_REGISTRY: dict[str, _Stats] = defaultdict(_Stats)


class _Timer:
    __slots__ = ('name', 'start')

    def __init__(self, name: str) -> None:
        self.name = name
        self.start = 0

    def __enter__(self) -> '_Timer':
        self.start = time.perf_counter_ns()
        return self

    def __exit__(self, *exc) -> None:
        _REGISTRY[self.name].record(time.perf_counter_ns() - self.start)


class _NullCM:
    __slots__ = ()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_NULL = _NullCM()


def timer(name: str):
    """Return a context manager that records elapsed time under ``name``.

    Returns a no-op singleton when instrumentation is disabled.
    """
    if not ENABLED:
        return _NULL
    return _Timer(name)


def time_method(name: str):
    """Decorator that times every call of the wrapped function.

    When instrumentation is disabled this returns the original callable
    unchanged so there is truly zero overhead at the call site.
    """
    def deco(fn: F) -> F:
        if not ENABLED:
            return fn

        def wrapper(*args, **kwargs):
            t0 = time.perf_counter_ns()
            try:
                return fn(*args, **kwargs)
            finally:
                _REGISTRY[name].record(time.perf_counter_ns() - t0)

        wrapper.__name__ = fn.__name__
        wrapper.__qualname__ = getattr(fn, '__qualname__', fn.__name__)
        wrapper.__doc__ = fn.__doc__
        return wrapper  # type: ignore[return-value]

    return deco


def dump(stream: TextIO | None = None) -> None:
    """Write a summary of recorded timings sorted by total time desc.

    The registry is reset after each dump so subsequent reports show the
    delta from the previous dump.
    """
    if stream is None:
        stream = sys.stderr
    if not _REGISTRY:
        stream.write('[git-cola perf] no samples recorded\n')
        stream.flush()
        return
    rows = []
    for entry_name, stats in _REGISTRY.items():
        if stats.count == 0:
            continue
        sorted_samples = sorted(stats.samples)
        if sorted_samples:
            p95_idx = max(0, int(len(sorted_samples) * 0.95) - 1)
            p95 = sorted_samples[p95_idx]
        else:
            p95 = 0
        mean = stats.total_ns // stats.count if stats.count else 0
        rows.append(
            (stats.total_ns, entry_name, stats.count, mean, p95, stats.max_ns)
        )
    rows.sort(reverse=True)
    stream.write('\n=== git-cola perf counters (since last dump) ===\n')
    header = (
        f'{"name":42s} {"count":>8s} {"total":>10s} '
        f'{"mean":>10s} {"p95":>10s} {"max":>10s}\n'
    )
    stream.write(header)
    for total_ns, entry_name, count, mean, p95, max_ns in rows:
        stream.write(
            f'{entry_name:42s} {count:>8d} {_fmt(total_ns):>10s} '
            f'{_fmt(mean):>10s} {_fmt(p95):>10s} {_fmt(max_ns):>10s}\n'
        )
    stream.write('=================================================\n')
    stream.flush()
    reset()


def reset() -> None:
    """Discard all recorded samples."""
    _REGISTRY.clear()


def _fmt(ns: int) -> str:
    if ns >= 1_000_000_000:
        return f'{ns / 1e9:.2f}s'
    if ns >= 1_000_000:
        return f'{ns / 1e6:.2f}ms'
    if ns >= 1_000:
        return f'{ns / 1e3:.1f}us'
    return f'{ns}ns'
