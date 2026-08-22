#!/usr/bin/env python3
"""Generate the README's markdown tables from a run manifest.

Hand-typing a results matrix out of JSON is how transcription errors get published, and
the tables have to be regenerated every time a platform is added or a run is repeated.
This makes that step mechanical and auditable: every number in the README traces to a
committed results file.

RTT ADJUSTMENT. On a WAN-reached managed tier, the round trip dominates. Measured from
Varanasi against a us-east instance, a point lookup and a 3-hop traversal both report
~271 ms - the difference between them is under 2 ms. Reporting only the raw figure would
say "all workloads cost the same", which is true of the network and false of the engine.

So every latency is reported twice: raw, and minus that platform's own measured baseline
round trip (a `RETURN 1` with no data access). The adjusted column is an ESTIMATE of
server-side time and is labelled as one - it assumes the baseline is a constant additive
offset, which is a reasonable approximation for a stable link and a poor one for a
congested one. Both columns are published so a reader can disagree with the adjustment.

    python scripts/summarise.py --run cognodb-01
    python scripts/summarise.py --run cognodb-01 --out docs/RESULTS.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows consoles default to cp1252, which cannot encode the arrows and em-dashes in
# these tables. Without this the script dies on print() while the --out file would have
# been written fine, which is a maddening way to lose a run's summary.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

from harness import config as cfg  # noqa: E402

READ_ORDER = [
    ("lookup_point", "Point lookup"),
    ("lookup_indexed", "Indexed lookup"),
    ("traversal_1hop", "1-hop traversal"),
    ("traversal_2hop", "2-hop traversal"),
    ("traversal_3hop", "3-hop traversal"),
    ("aggregation_by_year", "Aggregation (group-by)"),
]


def baseline_rtt(platform_id: str) -> float | None:
    """That platform's own measured `RETURN 1` round trip, from the last probe."""
    p = cfg.RESULTS / "probe.json"
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        for entry in doc.get("platforms", []):
            if entry.get("id") == platform_id and entry.get("baseline_rtt_ms"):
                return entry["baseline_rtt_ms"]["p50"]
    except Exception:
        pass
    return None


def fmt(v, suffix: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.1f}{suffix}"
    return f"{v:,}{suffix}"


def adj(raw: float | None, rtt: float | None) -> str:
    if raw is None or rtt is None:
        return "—"
    v = raw - rtt
    return f"{v:.1f}" if v >= 0 else "~0"


def table_reads(manifest: dict) -> str:
    lines = [
        "| Platform | Workload | Warm-up | p50 (ms) | p95 (ms) | p99 (ms) | p50 − RTT (ms) | Valid | n |",
        "|---|---|---|---|---:|---:|---:|:--:|---:|",
    ]
    for pid, entry in manifest["platforms"].items():
        rtt = baseline_rtt(pid)
        rows = {r["workload"]: r for r in (entry.get("read") or [])
                if r.get("parameters") == "curated" and r.get("warmup_mode") == "hot"}
        cold = {r["workload"]: r for r in (entry.get("read") or [])
                if r.get("parameters") == "curated" and r.get("warmup_mode") == "cold"}
        for wid, label in READ_ORDER:
            for mode, src in (("hot", rows), ("cold", cold)):
                r = src.get(wid)
                if not r:
                    continue
                ls = r["latency_stats"]
                lines.append(
                    f"| {entry.get('label', pid)} | {label} | {mode} | "
                    f"{fmt(ls.get('p50'))} | {fmt(ls.get('p95'))} | {fmt(ls.get('p99'))} | "
                    f"{adj(ls.get('p50'), rtt)} | {'yes' if r.get('valid') else '**NO**'} | "
                    f"{ls.get('iterations', 0)} |"
                )
    return "\n".join(lines)


def table_mixed(manifest: dict) -> str:
    lines = [
        "| Platform | Clients | Offered (q/s) | Achieved (q/s) | p50 (ms) | p95 (ms) | On-time | Valid |",
        "|---|---:|---:|---:|---:|---:|---:|:--:|",
    ]
    for pid, entry in manifest["platforms"].items():
        for m in entry.get("mixed") or []:
            ls = m["latency_stats"]
            lines.append(
                f"| {entry.get('label', pid)} | {m['num_workers']} | "
                f"{fmt(m.get('target_rate'))} | {fmt(m.get('throughput_qps'))} | "
                f"{fmt(ls.get('p50'))} | {fmt(ls.get('p95'))} | "
                f"{m['on_time_pct']:.1f}% | {'yes' if m.get('valid') else '**NO**'} |"
            )
    return "\n".join(lines)


def table_ingest(manifest: dict) -> str:
    lines = [
        "| Platform | Nodes/sec | Rels/sec | Total wall clock (s) | Counts match | Index DDL |",
        "|---|---:|---:|---:|:--:|---|",
    ]
    any_row = False
    for pid, entry in manifest["platforms"].items():
        ing = entry.get("ingest")
        if not ing:
            continue
        any_row = True
        ddl = "<br>".join(f"`{d}`" for d in ing.get("index_ddl", [])) or "—"
        lines.append(
            f"| {entry.get('label', pid)} | {fmt(ing.get('nodes_per_sec'))} | "
            f"{fmt(ing.get('relationships_per_sec'))} | "
            f"{fmt(ing.get('total_wall_clock_sec'))} | "
            f"{'yes' if ing.get('counts_match') else '**NO**'} | {ddl} |"
        )
    return "\n".join(lines) if any_row else "_No load phase in this run._"


def table_curation(manifest: dict) -> str:
    lines = [
        "| Platform | Parameters | p50 (ms) | p90 (ms) | p95 (ms) | p99 (ms) | On-time | Valid |",
        "|---|---|---:|---:|---:|---:|---:|:--:|",
    ]
    any_row = False
    for pid, entry in manifest["platforms"].items():
        for r in entry.get("read") or []:
            if r["workload"] != "traversal_3hop" or r.get("warmup_mode") != "hot":
                continue
            any_row = True
            ls = r["latency_stats"]
            lines.append(
                f"| {entry.get('label', pid)} | {r.get('parameters')} | "
                f"{fmt(ls.get('p50'))} | {fmt(ls.get('p90'))} | {fmt(ls.get('p95'))} | "
                f"{fmt(ls.get('p99'))} | {r['on_time_pct']:.1f}% | "
                f"{'yes' if r.get('valid') else '**NO**'} |"
            )
    return "\n".join(lines) if any_row else "_No curated/uncurated pair in this run._"


def table_ledger(manifest: dict) -> str:
    lines = [
        "| Platform | vCPU | RAM | Disk | Max conns | Specs published? | Measured RTT p50 | Footprint observable | Source |",
        "|---|---|---|---|---|:--:|---:|:--:|---|",
    ]
    for pid, entry in manifest["platforms"].items():
        a = entry.get("advertised", {}) or {}
        rtt = baseline_rtt(pid)
        fp = entry.get("footprint", {}) or {}
        src = a.get("source", "—")
        lines.append(
            f"| {entry.get('label', pid)} | {a.get('vcpu') or '—'} | "
            f"{fmt(a.get('ram_mb'), ' MB') if a.get('ram_mb') else '—'} | "
            f"{fmt(a.get('disk_gb'), ' GB') if a.get('disk_gb') is not None else '—'} | "
            f"{a.get('max_connections') or '—'} | "
            f"{'yes' if a.get('published') else '**no**'} | "
            f"{fmt(rtt)} | {'yes' if fp.get('observable') else 'no'} | "
            f"{src if src.startswith('http') else src} |"
        )
    return "\n".join(lines)


def section_equivalence(manifest: dict) -> str:
    rep = manifest.get("equivalence_report")
    if not rep:
        return "_No equivalence pass in this run._"
    out = [f"**Verdict:** {rep['verdict']}", ""]
    if rep.get("agree"):
        out.append("Workloads where every platform produced an identical canonicalised "
                   f"result checksum: `{'`, `'.join(rep['agree'])}`.")
    for wid, d in (rep.get("diverge") or {}).items():
        out.append(f"\n**`{wid}` diverges** — latencies for this workload are not comparable:\n")
        out.append("| SHA-256 (16) | Rows | Platforms |")
        out.append("|---|---:|---|")
        for g in d["groups"]:
            out.append(f"| `{g['sha256']}` | {g['row_count']} | {', '.join(g['platforms'])} |")
    return "\n".join(out)


def section_env(manifest: dict) -> str:
    env = manifest.get("environment") or {}
    if not env:
        return "_Environment not captured in this run._"
    p, cpu, loc = env.get("platform", {}), env.get("cpu", {}), env.get("client_location", {})
    where = ", ".join(str(loc[k]) for k in ("city", "region", "country") if loc.get(k)) or "unknown"
    return "\n".join([
        "| | |", "|---|---|",
        f"| Client OS | {p.get('system')} {p.get('release')} ({p.get('machine')}) |",
        f"| Cores (logical) | {cpu.get('logical')} |",
        f"| RAM | {fmt(env.get('ram_total_mb'), ' MB')} |",
        f"| Python | {env.get('python')} |",
        f"| Neo4j driver | {env.get('neo4j_driver')} |",
        f"| Measured sleep granularity | {env.get('timer', {}).get('sleep_granularity_ms')} ms |",
        f"| Client location | {where} |",
        f"| Run started (UTC) | {manifest.get('started_utc')} |",
    ])


def section_calibration(manifest: dict) -> str:
    lines = ["| Platform | Probe p50 (ms) | 1-client capacity (q/s) | Offered rates (q/s) |",
             "|---|---:|---:|---|"]
    any_row = False
    for pid, entry in manifest["platforms"].items():
        c = entry.get("calibration")
        if not c or not c.get("enabled") or c.get("failed"):
            continue
        any_row = True
        rates = ", ".join(f"{k}→{v}" for k, v in sorted(c["rates"].items(), key=lambda kv: int(kv[0])))
        lines.append(f"| {entry.get('label', pid)} | {c['probe_p50_ms']:.1f} | "
                     f"{c['single_client_capacity_qps']:.1f} | {rates} |")
    return "\n".join(lines) if any_row else "_Rate calibration disabled in this run._"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--out", default=None, help="write markdown here instead of stdout")
    args = ap.parse_args()

    run_dir = cfg.RESULTS / args.run
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    ds = manifest.get("dataset", {}).get("scales", {}).get(manifest.get("scale", "S"), {})
    doc = f"""<!-- Generated by scripts/summarise.py from results/{args.run}/manifest.json.
     Do not hand-edit: regenerate instead, so every number traces to a committed file. -->

## Environment

{section_env(manifest)}

Dataset: **cit-HepPh**, scale {manifest.get('scale')} — {fmt(ds.get('nodes'))} nodes /
{fmt(ds.get('edges'))} relationships (date cutoff {ds.get('date_cutoff')}).

## Offered-rate calibration

The configured rates are ceilings. Achievable throughput is bounded by
`concurrency ÷ round-trip`, so the runner probes capacity first and offers a fraction of
it. Without this, an impossible offered rate would queue instantly and be misreported as
platform throttling.

{section_calibration(manifest)}

## Ingest

{table_ingest(manifest)}

## Read workloads

`p50 − RTT` subtracts each platform's own measured `RETURN 1` baseline. It is an
**estimate of server-side time**, not a measurement: it assumes the round trip is a
constant additive offset. Both columns are given so the adjustment can be disputed.

{table_reads(manifest)}

## Mixed workload — concurrency sweep

{table_mixed(manifest)}

## Effect of parameter curation (3-hop traversal, hot)

{table_curation(manifest)}

## Cross-engine result equivalence

{section_equivalence(manifest)}

## Fairness ledger

{table_ledger(manifest)}
"""

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(doc, encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(doc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
