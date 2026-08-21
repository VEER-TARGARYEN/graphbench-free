"""Open-loop, scheduled-dispatch load generation.

Every operation is assigned a send time BEFORE the run starts. Workers pull from a
shared schedule and wait until an operation is due; if the pool is saturated and the
operation is already overdue, it is sent immediately and the lag is recorded. Latency is
then measured from the SCHEDULED time, per wrk2's rule:

    "Rather than measure response latency from the time that the actual transmission of
    a request occurred, wrk2 measures response latency from the time the transmission
    should have occurred according to the constant throughput configured for the run."

This is the difference between a benchmark and a for-loop. Schroeder, Wierman and
Harchol-Balter (NSDI 2006) showed mean response time under an open model can exceed a
closed model by an order of magnitude at identical load - so a closed-loop harness does
not merely lose precision, it systematically favours whichever system stalls hardest.

Client concurrency and offered rate are two INDEPENDENT axes here, as in BenchBase and
FalkorDB's own Rust harness. "40 clients" says how much parallelism exists; "200 ops/s"
says how much work is offered. Conflating them is how a concurrency sweep turns into a
throughput sweep by accident.
"""
from __future__ import annotations

import random
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from .recorder import LatencyRecorder

# ── timer resolution ───────────────────────────────────────────────────────────
# Windows' default system timer granularity is ~15.6 ms. Both time.sleep() and
# Event.wait() round up to it, so a scheduler asking for a 5 ms gap gets 15.6 ms and the
# offered rate silently becomes whatever the timer allows. That would show up in the
# results as the DATABASE failing to keep up, when in fact the client never dispatched.
#
# timeBeginPeriod(1) drops it to ~1 ms process-wide. We pair it with a short spin for
# sub-millisecond waits. Both are needed: neither alone gives accurate pacing.
_TIMER_PERIOD_SET = False


def set_timer_resolution() -> dict:
    """Raise timer resolution on Windows. Idempotent; safe no-op elsewhere."""
    global _TIMER_PERIOD_SET
    info = {"platform": sys.platform, "raised": False}
    if sys.platform == "win32" and not _TIMER_PERIOD_SET:
        try:
            import ctypes

            if ctypes.WinDLL("winmm").timeBeginPeriod(1) == 0:  # TIMERR_NOERROR
                _TIMER_PERIOD_SET = True
                info["raised"] = True
        except Exception as e:  # pragma: no cover - depends on the host
            info["error"] = type(e).__name__
    info["sleep_granularity_ms"] = round(measure_sleep_granularity() * 1000, 3)
    return info


def measure_sleep_granularity(samples: int = 15) -> float:
    """Empirically measure the shortest sleep this machine can actually honour.

    Reported in the results so a reader can tell whether a sub-millisecond latency
    figure is meaningful on the client that produced it.
    """
    worst = 0.0
    for _ in range(samples):
        t0 = time.perf_counter()
        time.sleep(0.001)
        worst = max(worst, time.perf_counter() - t0)
    return worst


# Below this, spin instead of sleeping - a sleep this short cannot be honoured.
_SPIN_THRESHOLD_SEC = 0.0015


def _wait_until(deadline: float, stop: threading.Event) -> bool:
    """Block until `deadline`. Returns False if the run was aborted.

    Sleeps for the bulk of the wait, then spins for the last ~1.5 ms. The spin is
    bounded so that even 40 workers cannot meaningfully steal the client's CPU - which
    matters here, because the client is a 4-core laptop and client saturation would be
    published as database latency.
    """
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return True
        if stop.is_set():
            return False
        if remaining > _SPIN_THRESHOLD_SEC:
            stop.wait(remaining - _SPIN_THRESHOLD_SEC)
        else:
            time.sleep(0)  # yield the GIL without a timer round-trip


@dataclass(slots=True)
class Op:
    seq: int
    offset: float          # seconds after run start at which this op is due
    workload_id: str
    params: dict


def build_schedule(
    n_ops: int,
    rate: float | None,
    *,
    arrival: str = "uniform",
    seed: int = 42,
) -> list[float]:
    """Return per-operation send offsets in seconds.

    rate=None means a SATURATION run: every operation is due immediately and workers
    dispatch as fast as they can. A saturation run cannot satisfy the on-time gate by
    construction, so it is reported separately and never mixed into the rated results.
    """
    if rate is None:
        return [0.0] * n_ops
    if rate <= 0:
        raise ValueError("rate must be positive or None")

    if arrival == "uniform":
        step = 1.0 / rate
        return [i * step for i in range(n_ops)]

    if arrival == "poisson":
        # Poisson arrivals - exponential inter-arrival gaps. Closer to real traffic than
        # a metronome, and the arrival model BenchBase exposes for the same reason.
        rng = random.Random(seed)
        out, t = [], 0.0
        for _ in range(n_ops):
            out.append(t)
            t += rng.expovariate(rate)
        return out

    raise ValueError(f"unknown arrival model: {arrival!r}")


class _Cursor:
    """Thread-safe hand-out of operations from a shared schedule."""

    __slots__ = ("_ops", "_i", "_lock")

    def __init__(self, ops: Sequence[Op]):
        self._ops = ops
        self._i = 0
        self._lock = threading.Lock()

    def next(self) -> Op | None:
        with self._lock:
            if self._i >= len(self._ops):
                return None
            op = self._ops[self._i]
            self._i += 1
            return op


def run_open_loop(
    ops: Sequence[Op],
    *,
    workers: int,
    execute: Callable[[int, Op], object],
    on_time_tolerance_sec: float,
    per_query_timeout_sec: float,
    deadline_sec: float | None = None,
    on_result: Callable[[int, Op, object], None] | None = None,
) -> LatencyRecorder:
    """Dispatch `ops` across `workers` threads and return a merged recorder.

    `execute(worker_id, op)` runs one operation and returns whatever the adapter
    produced. It must raise on failure; timeouts are detected by elapsed time so that a
    driver which swallows its own timeout still gets counted honestly.

    The neo4j driver is blocking, so threads (not asyncio) are the correct primitive -
    and at <=40 clients the GIL is not the constraint; the network is.
    """
    set_timer_resolution()
    recorders = [LatencyRecorder() for _ in range(workers)]
    cursor = _Cursor(ops)
    t0 = time.perf_counter()
    stop = threading.Event()

    def worker(wid: int) -> None:
        rec = recorders[wid]
        while not stop.is_set():
            op = cursor.next()
            if op is None:
                return

            due = t0 + op.offset
            if not _wait_until(due, stop):
                return
            started = time.perf_counter()

            try:
                result = execute(wid, op)
            except Exception:
                elapsed = time.perf_counter() - started
                rec.record_error(timeout=elapsed >= per_query_timeout_sec)
                continue

            completed = time.perf_counter()
            if (completed - started) >= per_query_timeout_sec:
                rec.record_error(timeout=True)
                continue

            rec.record(
                scheduled_at=due,
                started_at=started,
                completed_at=completed,
                on_time_tolerance_sec=on_time_tolerance_sec,
            )
            if on_result is not None:
                on_result(wid, op, result)

            if deadline_sec is not None and (completed - t0) >= deadline_sec:
                stop.set()
                return

    threads = [threading.Thread(target=worker, args=(i,), daemon=True, name=f"gbf-w{i}") for i in range(workers)]
    for t in threads:
        t.start()

    # A hard wall-clock ceiling so one wedged platform cannot consume the run budget.
    join_deadline = None if deadline_sec is None else deadline_sec + per_query_timeout_sec + 30
    for t in threads:
        t.join(timeout=join_deadline)
    stop.set()

    merged = LatencyRecorder()
    for r in recorders:
        merged.merge(r)
    return merged


def weighted_op_stream(
    total: int,
    weights: dict[str, int],
    param_pools: dict[str, Sequence[dict]],
    *,
    seed: int = 42,
) -> list[tuple[str, dict]]:
    """Build a deterministic mixed-workload stream from a declared weight vector.

    Frozen to a list before the run rather than sampled live, so the exact same request
    sequence is replayed against every platform. Freezing the stream is a stronger and
    more auditable fairness guarantee than sharing an RNG seed.
    """
    rng = random.Random(seed)
    ids = sorted(weights)
    cum: list[tuple[int, str]] = []
    running = 0
    for wid in ids:
        running += weights[wid]
        cum.append((running, wid))
    if running <= 0:
        raise ValueError("mixed workload weights sum to zero")

    stream: list[tuple[str, dict]] = []
    for _ in range(total):
        r = rng.randrange(running)
        wid = next(w for threshold, w in cum if r < threshold)
        pool = param_pools.get(wid) or [{}]
        stream.append((wid, pool[rng.randrange(len(pool))]))
    return stream


def as_ops(stream: Iterable[tuple[str, dict]], offsets: Sequence[float]) -> list[Op]:
    return [
        Op(seq=i, offset=offsets[i], workload_id=wid, params=params)
        for i, (wid, params) in enumerate(stream)
    ]
