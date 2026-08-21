#!/usr/bin/env python3
"""Render the charts from a completed run.

Chart choices are deliberate. Every other submission will show a bar chart of average
latency. These show distributions:

  1. percentile_<workload>.png  - log-x percentile-distribution curves, one line per
     platform, extended past p99.9. This is where a free tier's tail actually lives, and
     a bar chart of means cannot show it.
  2. curation_effect.png        - the flagship. The same 3-hop workload driven by curated
     versus uniform-random start nodes. If the uncurated distribution is visibly
     multimodal and the curated one is not, the methodology argument is proven rather
     than asserted.
  3. coordinated_omission.png   - corrected versus uncorrected tail per platform: exactly
     how much throttling a closed-loop harness would have hidden.
  4. concurrency_sweep.png      - throughput and p95 against client count, with INVALID
     points marked rather than silently plotted.
  5. ingest.png                 - load throughput, split by phase.

    python scripts/make_charts.py                 # newest run
    python scripts/make_charts.py --run 20260821T104500
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import config as cfg  # noqa: E402
from harness.recorder import decode_buckets  # noqa: E402

# Colour-blind-safe qualitative palette (Okabe-Ito), so the charts survive both a
# greyscale print and the ~8% of male readers with a colour vision deficiency.
PALETTE = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#000000"]
GRID = {"alpha": 0.25, "linewidth": 0.6}


def newest_run() -> Path:
    runs = sorted((p for p in cfg.RESULTS.iterdir() if p.is_dir()), key=lambda p: p.name)
    if not runs:
        raise SystemExit("no runs in results/ - run scripts/run_benchmark.py first")
    return runs[-1]


def load_run(run_dir: Path) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    return manifest


def _percentile_series(buckets: list[list[int]]) -> tuple[list[float], list[float]]:
    """Return (x = 1/(1-percentile), y = latency ms) for a percentile-distribution plot."""
    total = sum(c for _, c in buckets)
    if not total:
        return [], []
    xs, ys, seen = [], [], 0
    for value, count in buckets:
        seen += count
        pct = seen / total
        if pct >= 1.0:
            pct = 1.0 - 1.0 / (total * 10)
        xs.append(1.0 / (1.0 - pct))
        ys.append(value / 1000.0)
    return xs, ys


def _finish(ax, title: str, xlabel: str, ylabel: str) -> None:
    ax.set_title(title, fontsize=11, loc="left")
    ax.set_xlabel(xlabel, fontsize=9)
    ax.set_ylabel(ylabel, fontsize=9)
    ax.grid(True, **GRID)
    ax.tick_params(labelsize=8)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


# Set when the run being charted contains self-test fixtures. Mock numbers must never
# reach the README, and an unlabelled PNG in charts/ is exactly how they would: someone
# drags the file into a document three days later with no memory of where it came from.
SELFTEST_RUN = False


def _save(fig, name: str) -> None:
    cfg.CHARTS.mkdir(parents=True, exist_ok=True)
    if SELFTEST_RUN:
        fig.text(0.5, 0.5, "SELF-TEST DATA\nNOT A RESULT", fontsize=34, color="#B00020",
                 alpha=0.16, ha="center", va="center", rotation=24, weight="bold",
                 transform=fig.transFigure, zorder=10)
    path = cfg.CHARTS / name
    fig.tight_layout()
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {path.relative_to(cfg.ROOT)}")


def _reads(entry: dict) -> list[dict]:
    return entry.get("read") or []


def chart_percentiles(manifest: dict) -> None:
    workloads: dict[str, list[tuple[str, dict]]] = {}
    for pid, entry in manifest["platforms"].items():
        for r in _reads(entry):
            if r.get("parameters") != "curated" or r.get("warmup_mode") != "hot":
                continue
            workloads.setdefault(r["workload"], []).append((entry.get("label", pid), r))

    for workload, series in sorted(workloads.items()):
        fig, ax = plt.subplots(figsize=(7.6, 4.4))
        plotted = False
        for i, (label, r) in enumerate(series):
            if not r.get("histogram"):
                continue
            xs, ys = _percentile_series(decode_buckets(r["histogram"]))
            if not xs:
                continue
            suffix = "" if r.get("valid", True) else "  [INVALID]"
            ax.plot(xs, ys, label=f"{label}{suffix}", color=PALETTE[i % len(PALETTE)],
                    linewidth=1.6, linestyle="-" if r.get("valid", True) else "--")
            plotted = True
        if not plotted:
            plt.close(fig)
            continue
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xticks([1, 2, 10, 100, 1000, 10000])
        ax.set_xticklabels(["p0", "p50", "p90", "p99", "p99.9", "p99.99"])
        _finish(ax, f"{workload} - latency by percentile (hot, curated parameters)",
                "percentile", "latency (ms, log scale)")
        ax.legend(fontsize=8, frameon=False)
        _save(fig, f"percentile_{workload}.png")


def chart_curation(manifest: dict) -> None:
    """The flagship: curated versus uniform-random start nodes on the same workload."""
    fig, ax = plt.subplots(figsize=(7.6, 4.4))
    plotted = False
    ci = 0
    for pid, entry in manifest["platforms"].items():
        pairs = {r.get("parameters"): r for r in _reads(entry)
                 if r["workload"] == "traversal_3hop" and r.get("warmup_mode") == "hot"}
        if "curated" not in pairs or "uncurated" not in pairs:
            continue
        label = entry.get("label", pid)
        colour = PALETTE[ci % len(PALETTE)]
        ci += 1
        for style, key in (("-", "curated"), ("--", "uncurated")):
            r = pairs[key]
            if not r.get("histogram"):
                continue
            xs, ys = _percentile_series(decode_buckets(r["histogram"]))
            ax.plot(xs, ys, style, color=colour, linewidth=1.6, label=f"{label} - {key}")
            plotted = True
    if not plotted:
        plt.close(fig)
        print("  (skipped curation_effect.png - run without --no-uncurated to produce it)")
        return
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xticks([1, 2, 10, 100, 1000])
    ax.set_xticklabels(["p0", "p50", "p90", "p99", "p99.9"])
    _finish(ax, "Curated vs uniform-random start nodes - 3-hop traversal, same graph, same engine",
            "percentile", "latency (ms, log scale)")
    ax.legend(fontsize=8, frameon=False)

    # The caption states the DESIGN rationale and lets the curves say what they say.
    # Asserting an outcome in a caption is how benchmarks end up arguing with their own
    # data. The frontier-size figures below are a property of the dataset and the
    # sampler, measured before any engine was involved.
    caption = "Solid = curated start nodes, dashed = uniform random."
    try:
        cur = json.loads((cfg.PARAMS / "curation.json").read_text(encoding="utf-8"))
        d3 = cur["depths"]["3"]
        caption += (
            f"  Curated 3-hop frontier {d3['curated']['frontier_min']}-{d3['curated']['frontier_max']}"
            f" (sd {d3['curated']['frontier_stdev']}); uniform random"
            f" {d3['uncurated']['frontier_min']}-{d3['uncurated']['frontier_max']}"
            f" (sd {d3['uncurated']['frontier_stdev']})."
        )
    except Exception:
        pass
    fig.text(0.01, -0.03, caption, fontsize=7.5, color="#444")
    _save(fig, "curation_effect.png")


def chart_coordinated_omission(manifest: dict) -> None:
    labels, corrected, uncorrected = [], [], []
    for pid, entry in manifest["platforms"].items():
        for r in _reads(entry):
            if r["workload"] == "traversal_3hop" and r.get("warmup_mode") == "hot" \
                    and r.get("parameters") == "curated":
                co = r.get("coordinated_omission_delta_ms", {}).get("p99")
                if co:
                    labels.append(entry.get("label", pid))
                    corrected.append(co["corrected"])
                    uncorrected.append(co["uncorrected"])
    if not labels:
        print("  (skipped coordinated_omission.png - no data)")
        return

    fig, ax = plt.subplots(figsize=(7.6, 0.6 * len(labels) + 2.2))
    y = range(len(labels))
    ax.barh([i + 0.19 for i in y], corrected, height=0.36, color=PALETTE[1],
            label="corrected (honest)")
    ax.barh([i - 0.19 for i in y], uncorrected, height=0.36, color=PALETTE[0],
            label="uncorrected (what a for-loop reports)")
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=8)
    _finish(ax, "p99 latency: how much a closed-loop harness would hide", "p99 latency (ms)", "")
    ax.legend(fontsize=8, frameon=False)
    _save(fig, "coordinated_omission.png")


def chart_concurrency(manifest: dict) -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10.4, 4.2))
    plotted = False
    for i, (pid, entry) in enumerate(manifest["platforms"].items()):
        rows = sorted(entry.get("mixed") or [], key=lambda r: r["num_workers"])
        if not rows:
            continue
        label = entry.get("label", pid)
        colour = PALETTE[i % len(PALETTE)]
        xs = [r["num_workers"] for r in rows]
        ax1.plot(xs, [r["throughput_qps"] for r in rows], "o-", color=colour, label=label, linewidth=1.6)
        ax2.plot(xs, [r["latency_stats"].get("p95", 0) for r in rows], "o-", color=colour,
                 label=label, linewidth=1.6)
        # Mark invalid points rather than plotting them as if they were comparable.
        for r in rows:
            if not r.get("valid", True):
                ax1.plot(r["num_workers"], r["throughput_qps"], "x", color="#B00020", markersize=10)
                ax2.plot(r["num_workers"], r["latency_stats"].get("p95", 0), "x",
                         color="#B00020", markersize=10)
        plotted = True
    if not plotted:
        plt.close(fig)
        print("  (skipped concurrency_sweep.png - no mixed-workload data)")
        return
    _finish(ax1, "Mixed workload throughput", "concurrent clients", "queries/sec")
    # Log y-axis: a queued run can sit four orders of magnitude above a healthy one, and
    # on a linear axis that single point flattens everything worth looking at into the
    # x-axis. Spreads above ~10x belong on a log scale.
    ax2.set_yscale("log")
    _finish(ax2, "Mixed workload p95 latency", "concurrent clients", "p95 latency (ms, log scale)")
    ax1.legend(fontsize=8, frameon=False)
    fig.text(0.01, -0.02, "Red x = run FAILED the 95%-on-time validity gate; the point is "
                          "shown but is not a comparable measurement.", fontsize=7.5, color="#B00020")
    _save(fig, "concurrency_sweep.png")


def chart_ingest(manifest: dict) -> None:
    labels, nps, rps = [], [], []
    for pid, entry in manifest["platforms"].items():
        ing = entry.get("ingest")
        if not ing or ing.get("nodes_per_sec") is None:
            continue
        labels.append(entry.get("label", pid))
        nps.append(ing["nodes_per_sec"])
        rps.append(ing["relationships_per_sec"] or 0)
    if not labels:
        print("  (skipped ingest.png - no load data)")
        return

    fig, ax = plt.subplots(figsize=(7.6, 0.6 * len(labels) + 2.2))
    y = range(len(labels))
    ax.barh([i + 0.19 for i in y], nps, height=0.36, color=PALETTE[2], label="nodes/sec")
    ax.barh([i - 0.19 for i in y], rps, height=0.36, color=PALETTE[4], label="relationships/sec")
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xscale("log")
    _finish(ax, "Ingest throughput (driver-side UNWIND batching, identical everywhere)",
            "items/sec (log scale)", "")
    ax.legend(fontsize=8, frameon=False)
    fig.text(0.01, -0.02,
             "Managed tiers load over TLS/WAN, parity containers over loopback. These two "
             "tracks are NOT comparable with each other.", fontsize=7.5, color="#444")
    _save(fig, "ingest.png")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default=None, help="run id under results/ (default: newest)")
    args = ap.parse_args()

    run_dir = (cfg.RESULTS / args.run) if args.run else newest_run()
    print(f"charting {run_dir.name}")
    manifest = load_run(run_dir)

    global SELFTEST_RUN
    SELFTEST_RUN = any(
        e.get("track") == "SELFTEST" for e in manifest.get("platforms", {}).values()
    )
    if SELFTEST_RUN:
        print("  NOTE: this run contains self-test fixtures, not databases.")
        print("        Charts are watermarked and their numbers must not be published.")

    chart_percentiles(manifest)
    chart_curation(manifest)
    chart_coordinated_omission(manifest)
    chart_concurrency(manifest)
    chart_ingest(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
