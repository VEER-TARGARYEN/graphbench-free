#!/usr/bin/env python3
"""Curate query parameters offline and freeze them to CSV.

THE PROBLEM. Every benchmark that picks start nodes with `ORDER BY rand() LIMIT 100`
is measuring the graph's degree distribution, not the database. On a skewed graph the
resulting latency distribution is multimodal - a handful of hub papers dominate the
tail - and the reported percentiles move run to run depending on which hubs happened to
be drawn. LDBC SNB Interactive v2 (S 2.4) states the failure plainly: with uniform random
sampling, query runtimes are unstable and often multimodal. Gubichev & Boncz (TPCTC 2014)
is the dedicated treatment.

THE FIX. Precompute the DISTINCT k-hop reachable-set size for every candidate node,
keep only nodes whose frontier falls inside a narrow band around the median, and sample
from that band with a fixed seed. Freeze the result to CSV and commit it, so every
platform receives byte-identical parameters and the numbers are reproducible by anyone.

We also emit an UNCURATED uniform-random sample of the same size. Running both and
overlaying the two latency histograms is the chart that proves the curation mattered -
and it is the one chart guaranteed to work regardless of what the cloud does that day.

    python scripts/curate_params.py --scale S
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PARAMS = ROOT / "params"

SEED = 42
SAMPLE_SIZE = 200
# Keep the middle band of the frontier-size distribution. Narrow enough that the
# workload measures traversal cost rather than fan-out variance, wide enough that
# we are not benchmarking a single degree value.
BAND_LO_PCT, BAND_HI_PCT = 40, 60


def load_graph(scale: str) -> tuple[dict[int, list[int]], dict[int, str], dict[int, str]]:
    edges_path = DATA / scale / "edges.csv"
    nodes_path = DATA / scale / "nodes.csv"
    if not edges_path.exists():
        raise SystemExit(f"missing {edges_path} - run scripts/fetch_dataset.py first")

    adj: dict[int, list[int]] = defaultdict(list)
    with open(edges_path, encoding="utf-8") as f:
        r = csv.reader(f)
        next(r)
        for src, dst in r:
            adj[int(src)].append(int(dst))

    years: dict[int, str] = {}
    dates: dict[int, str] = {}
    with open(nodes_path, encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            pid = int(row["paper_id"])
            years[pid] = row["pub_year"]
            dates[pid] = row["pub_date"]

    return adj, years, dates


def frontier_sizes(adj: dict[int, list[int]], nodes: list[int], depth: int) -> dict[int, int]:
    """DISTINCT reachable-node count at exactly `depth` hops, following out-edges.

    DISTINCT reachable set, not path count, on purpose. Path counts differ across
    engines for reasons that have nothing to do with performance: Cypher applies
    no-repeated-edge semantics per MATCH clause while Gremlin applies homomorphism,
    so the same pattern legitimately returns different cardinalities. Counting the
    distinct destination set is the one definition every engine agrees on.
    """
    out: dict[int, int] = {}
    for n in nodes:
        frontier = {n}
        for _ in range(depth):
            nxt: set[int] = set()
            for u in frontier:
                nxt.update(adj.get(u, ()))
            frontier = nxt
            if not frontier:
                break
        out[n] = len(frontier)
    return out


def pick_band(sizes: dict[int, int], rng: random.Random) -> tuple[list[int], dict]:
    """Sample from the middle band of the frontier-size distribution."""
    live = {n: s for n, s in sizes.items() if s > 0}
    if not live:
        raise SystemExit("no node has a non-empty frontier at this depth")

    ordered = sorted(live.values())
    lo = ordered[int(len(ordered) * BAND_LO_PCT / 100)]
    hi = ordered[min(len(ordered) - 1, int(len(ordered) * BAND_HI_PCT / 100))]
    if hi < lo:
        lo, hi = hi, lo

    band = sorted(n for n, s in live.items() if lo <= s <= hi)
    if len(band) < SAMPLE_SIZE:
        # Widen symmetrically rather than silently returning a short sample.
        band = sorted(live, key=lambda n: abs(live[n] - statistics.median(ordered)))[: SAMPLE_SIZE * 2]

    chosen = rng.sample(band, min(SAMPLE_SIZE, len(band)))
    chosen_sizes = [live[n] for n in chosen]

    stats = {
        "band_lo": lo,
        "band_hi": hi,
        "candidates_in_band": len(band),
        "chosen": len(chosen),
        "frontier_min": min(chosen_sizes),
        "frontier_max": max(chosen_sizes),
        "frontier_median": statistics.median(chosen_sizes),
        "frontier_stdev": round(statistics.pstdev(chosen_sizes), 2),
        "population_median": statistics.median(ordered),
        "population_stdev": round(statistics.pstdev(ordered), 2),
        "population_max": ordered[-1],
    }
    return sorted(chosen), stats


def pick_uniform(sizes: dict[int, int], rng: random.Random) -> tuple[list[int], dict]:
    """The naive control: uniform random over every node with a non-empty frontier."""
    live = {n: s for n, s in sizes.items() if s > 0}
    chosen = rng.sample(sorted(live), min(SAMPLE_SIZE, len(live)))
    cs = [live[n] for n in chosen]
    return sorted(chosen), {
        "chosen": len(chosen),
        "frontier_min": min(cs),
        "frontier_max": max(cs),
        "frontier_median": statistics.median(cs),
        "frontier_stdev": round(statistics.pstdev(cs), 2),
    }


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(",".join(header) + "\n")
        for row in rows:
            f.write(",".join(str(c) for c in row) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scale", default="S", choices=["S", "M"])
    args = ap.parse_args()

    adj, years, dates_by_node = load_graph(args.scale)
    nodes = sorted(years)
    print(f"scale {args.scale}: {len(nodes):,} nodes, {sum(len(v) for v in adj.values()):,} out-edges")

    summary: dict = {"scale": args.scale, "seed": SEED, "sample_size": SAMPLE_SIZE,
                     "band_percentiles": [BAND_LO_PCT, BAND_HI_PCT], "depths": {}}

    for depth in (1, 2, 3):
        rng = random.Random(SEED + depth)
        print(f"  computing {depth}-hop frontiers ...", end="", flush=True)
        sizes = frontier_sizes(adj, nodes, depth)
        print(" done")

        curated, cstats = pick_band(sizes, rng)
        uniform, ustats = pick_uniform(sizes, random.Random(SEED + depth))

        write_csv(
            PARAMS / f"start_nodes_{depth}hop.csv",
            ["paper_id", "pub_year", "frontier_size"],
            [[n, years[n], sizes[n]] for n in curated],
        )
        write_csv(
            PARAMS / f"start_nodes_{depth}hop_uncurated.csv",
            ["paper_id", "pub_year", "frontier_size"],
            [[n, years[n], sizes[n]] for n in uniform],
        )

        summary["depths"][str(depth)] = {"curated": cstats, "uncurated": ustats}
        print(
            f"    curated   frontier {cstats['frontier_min']}-{cstats['frontier_max']} "
            f"(median {cstats['frontier_median']}, sd {cstats['frontier_stdev']})"
        )
        print(
            f"    uncurated frontier {ustats['frontier_min']}-{ustats['frontier_max']} "
            f"(median {ustats['frontier_median']}, sd {ustats['frontier_stdev']})"
        )

    # Two different scalar properties for two different metrics, deliberately:
    #
    #   pub_date (~thousands of distinct values, low cardinality each) drives the
    #     INDEXED LOOKUP. An equality predicate on a selective scalar is the one
    #     predicate every engine can serve from its general-purpose index. A predicate
    #     that returns thousands of rows would be measuring scan speed wearing an
    #     index's clothes.
    #
    #   pub_year (8 distinct values, large groups) drives the AGGREGATION. A group-by
    #     wants few, fat groups.
    dcount: dict[str, int] = defaultdict(int)
    for d in dates_by_node.values():
        dcount[d] += 1
    write_csv(
        PARAMS / "dates.csv",
        ["pub_date", "node_count"],
        [[d, dcount[d]] for d in sorted(dcount)],
    )
    dsel = sorted(dcount.values())
    summary["dates"] = {
        "distinct": len(dcount),
        "min": min(dcount),
        "max": max(dcount),
        "rows_per_probe_median": statistics.median(dsel),
        "rows_per_probe_max": dsel[-1],
    }
    print(
        f"  dates: {len(dcount)} distinct ({min(dcount)}..{max(dcount)}), "
        f"median {statistics.median(dsel):.0f} rows per probe  [indexed lookup]"
    )

    ycount: dict[str, int] = defaultdict(int)
    for y in years.values():
        ycount[y] += 1
    write_csv(
        PARAMS / "years.csv",
        ["year", "node_count"],
        [[y, ycount[y]] for y in sorted(ycount)],
    )
    summary["years"] = {"distinct": len(ycount), "min": min(ycount), "max": max(ycount)}
    print(f"  years: {len(ycount)} distinct ({min(ycount)}-{max(ycount)})  [aggregation group-by]")

    (PARAMS / "curation.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {(PARAMS / 'curation.json').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
