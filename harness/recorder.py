"""Latency recording with coordinated-omission correction.

THE PROBLEM. A closed-loop driver - `for i in range(100): t=now(); q(); record(now()-t)` -
systematically under-samples the tail. When the database stalls, the client stalls with
it and simply stops issuing the requests that would have queued behind the stall, so the
stall never appears in the measurement. Gil Tene named this coordinated omission; wrk2
exists specifically to fix it. On a throttled free tier the effect is perverse: it makes
the WORST platform look best, because the worst platform stalls the most.

THE FIX. Each operation is given a scheduled send time before the run starts. We record
two latencies for every operation:

    corrected   = completed_at - scheduled_send_at    <- the honest number
    uncorrected = completed_at - actual_send_at       <- what a naive harness reports

Publishing both, and the delta between them, is a measurement nobody else will have.

One HdrHistogram per worker, merged at the end. Merging histograms is exact, so there is
no lock on the hot path and no sampling error from per-worker aggregation.
"""
from __future__ import annotations

import base64
import gzip
import json
from dataclasses import dataclass, field

from hdrh.histogram import HdrHistogram

# 1 us .. 300 s at 3 significant digits. Only non-empty buckets are serialised, so a
# few hundred samples compress to well under a kilobyte - small enough to embed in the
# results JSON so a reader can recompute ANY percentile we did not print.
LOWEST_US = 1
HIGHEST_US = 300_000_000
SIGFIGS = 3

PERCENTILES = (50, 75, 90, 95, 99, 99.9)


def _new_hist() -> HdrHistogram:
    return HdrHistogram(LOWEST_US, HIGHEST_US, SIGFIGS)


def merge_histograms(dst: HdrHistogram, src: HdrHistogram) -> None:
    """Add `src` into `dst`. Exact, and portable to 64-bit Windows.

    HdrHistogram.add() delegates to the pyhdrh C extension, which passes the counts
    buffer ADDRESS through a C `long`. On Windows `long` is 32 bits even on x64, so any
    heap address above 2 GB raises `OverflowError: Python int too large to convert to
    C long`. The upstream merge is therefore unusable on the machine this benchmark
    runs from.

    This performs the identical merge in Python over the counts array, then rescans
    `dst` and hands the library its own bookkeeping routine. The rescan is necessary
    because set_internal_tacking_values ASSIGNS min/max/total rather than accumulating
    them - passing only the incoming range would silently raise dst's minimum.

    Cost: two passes over 20,480 slots, once per run. Immaterial.
    """
    if src.total_count == 0:
        return
    for i in range(src.counts_len):
        c = src.counts[i]
        if c:
            dst.counts[i] += c

    min_idx, max_idx, total = -1, -1, 0
    for i in range(dst.counts_len):
        c = dst.counts[i]
        if c:
            if min_idx < 0:
                min_idx = i
            max_idx = i
            total += c
    dst.set_internal_tacking_values(min_idx, max_idx, total)


def dump_buckets(h: HdrHistogram) -> list[list[int]]:
    """Exact, lossless [[value_us, count], ...] over the non-empty buckets.

    HdrHistogram's own `encode()` is unusable here for the same 32-bit-address reason
    as `add()`. That is no loss: the upstream format needs the library to read back,
    whereas this is self-describing, diffable in a pull request, and decodable with two
    lines of Python by anyone who wants to recompute a percentile we did not print.
    """
    return [[h.get_value_from_index(i), h.counts[i]] for i in range(h.counts_len) if h.counts[i]]


def encode_buckets(h: HdrHistogram) -> str:
    """gzip + base64 of the bucket dump, for embedding in results JSON."""
    blob = json.dumps(dump_buckets(h), separators=(",", ":")).encode("utf-8")
    return base64.b64encode(gzip.compress(blob, 6)).decode("ascii")


def decode_buckets(encoded: str) -> list[list[int]]:
    """Inverse of encode_buckets. Used by the charting script and by any reader."""
    return json.loads(gzip.decompress(base64.b64decode(encoded)).decode("utf-8"))


def percentile_from_buckets(buckets: list[list[int]], pct: float) -> float:
    """Recompute any percentile from a dumped histogram, without the library."""
    total = sum(c for _, c in buckets)
    if not total:
        return 0.0
    target = max(1, int(round(total * pct / 100.0)))
    seen = 0
    for value, count in buckets:
        seen += count
        if seen >= target:
            return value
    return buckets[-1][0]


@dataclass
class LatencyRecorder:
    """Accumulates corrected and uncorrected latencies plus the on-time census."""

    corrected: HdrHistogram = field(default_factory=_new_hist)
    uncorrected: HdrHistogram = field(default_factory=_new_hist)

    count: int = 0
    errors: int = 0
    timeouts: int = 0
    on_time: int = 0
    max_schedule_lag_us: int = 0
    total_schedule_lag_us: int = 0

    def record(
        self,
        *,
        scheduled_at: float,
        started_at: float,
        completed_at: float,
        on_time_tolerance_sec: float,
    ) -> None:
        corrected_us = max(1, int((completed_at - scheduled_at) * 1_000_000))
        uncorrected_us = max(1, int((completed_at - started_at) * 1_000_000))
        self.corrected.record_value(min(corrected_us, HIGHEST_US))
        self.uncorrected.record_value(min(uncorrected_us, HIGHEST_US))
        self.count += 1

        lag_us = int((started_at - scheduled_at) * 1_000_000)
        if lag_us > 0:
            self.total_schedule_lag_us += lag_us
            self.max_schedule_lag_us = max(self.max_schedule_lag_us, lag_us)
        if (started_at - scheduled_at) <= on_time_tolerance_sec:
            self.on_time += 1

    def record_error(self, *, timeout: bool = False) -> None:
        self.errors += 1
        if timeout:
            self.timeouts += 1

    def merge(self, other: "LatencyRecorder") -> None:
        merge_histograms(self.corrected, other.corrected)
        merge_histograms(self.uncorrected, other.uncorrected)
        self.count += other.count
        self.errors += other.errors
        self.timeouts += other.timeouts
        self.on_time += other.on_time
        self.total_schedule_lag_us += other.total_schedule_lag_us
        self.max_schedule_lag_us = max(self.max_schedule_lag_us, other.max_schedule_lag_us)

    # ── reporting ──────────────────────────────────────────────────────────────
    @staticmethod
    def _stats(h: HdrHistogram) -> dict:
        """Latencies in milliseconds. Never a bare mean: Hoefler & Belli Rule 3 reserves
        the arithmetic mean for costs, and Rule 8 says lead with the tail. The mean is
        emitted only so a reader can see how far it sits from the median."""
        if h.get_total_count() == 0:
            return {"iterations": 0}
        out = {
            "iterations": h.get_total_count(),
            "min": round(h.get_min_value() / 1000, 3),
            "max": round(h.get_max_value() / 1000, 3),
            "mean": round(h.get_mean_value() / 1000, 3),
        }
        for p in PERCENTILES:
            key = f"p{p:g}".replace(".", "_")
            out[key] = round(h.get_value_at_percentile(p) / 1000, 3)
        return out

    def on_time_pct(self) -> float:
        return 100.0 * self.on_time / self.count if self.count else 0.0

    def summary(self, *, on_time_threshold_pct: float) -> dict:
        pct = self.on_time_pct()
        return {
            "count": self.count,
            "errors": self.errors,
            "timeouts": self.timeouts,
            # LDBC SNB Interactive v2's validity rule, adopted verbatim: a run in which
            # fewer than 95% of operations started on time is INVALID. A platform that
            # cannot sustain the offered rate fails here rather than quietly reporting a
            # flattering latency built from requests it never had to queue.
            "on_time_pct": round(pct, 2),
            "valid": pct >= on_time_threshold_pct and self.count > 0,
            "schedule_lag_ms": {
                "max": round(self.max_schedule_lag_us / 1000, 3),
                "mean": round(self.total_schedule_lag_us / self.count / 1000, 3) if self.count else 0.0,
            },
            "latency_stats": self._stats(self.corrected),
            "latency_stats_uncorrected": self._stats(self.uncorrected),
            "coordinated_omission_delta_ms": self._co_delta(),
            # Exact histogram, embedded so a reader can recompute ANY percentile we
            # did not print - including ones we did not think to print.
            "histogram_format": "gzip+base64 JSON [[value_us, count], ...]",
            "histogram": encode_buckets(self.corrected),
        }

    def _co_delta(self) -> dict:
        """How much a naive closed-loop harness would have under-reported the tail.

        This is the headline of the methodology section: the gap between these two
        numbers is exactly the amount of throttling a for-loop would have hidden.
        """
        if self.corrected.get_total_count() == 0:
            return {}
        out = {}
        for p in (95, 99, 99.9):
            c = self.corrected.get_value_at_percentile(p) / 1000
            u = self.uncorrected.get_value_at_percentile(p) / 1000
            key = f"p{p:g}".replace(".", "_")
            out[key] = {"corrected": round(c, 3), "uncorrected": round(u, 3), "delta": round(c - u, 3)}
        return out

    def write_hgrm(self, path) -> None:
        """Emit a percentile distribution in the classic .hgrm column layout.

        Hand-rolled rather than delegating to output_percentile_distribution(), which
        writes bytes to a text handle and is awkward across platforms. The column
        layout is what hdr-plot and the HdrHistogram plotters expect.
        """
        h = self.corrected
        total = h.get_total_count()
        nl = chr(10)
        with open(path, "w", encoding="utf-8", newline=nl) as f:
            f.write("       Value     Percentile TotalCount 1/(1-Percentile)" + nl + nl)
            if not total:
                return
            for pct in (0, 10, 25, 50, 75, 90, 95, 97.5, 99, 99.9, 99.99, 100):
                v = h.get_value_at_percentile(pct) / 1000.0
                seen = int(round(total * pct / 100.0))
                inv = "inf" if pct >= 100 else f"{1.0 / (1.0 - pct / 100.0):.2f}"
                f.write(f"{v:12.3f} {pct / 100.0:14.6f} {seen:10d} {inv:>16}" + nl)
            f.write(f"#[Mean    = {h.get_mean_value() / 1000.0:12.3f}]" + nl)
            f.write(f"#[Max     = {h.get_max_value() / 1000.0:12.3f}]" + nl)
            f.write(f"#[Total   = {total}]" + nl)
