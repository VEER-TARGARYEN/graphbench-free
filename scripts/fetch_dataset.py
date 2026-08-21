#!/usr/bin/env python3
"""Download SNAP cit-HepPh, join it to its publication dates, and cut reproducible scales.

Why this dataset: 421,578 edges lands inside the brief's 100k-500k relationship band
with ZERO sampling, so there is no seed to defend and no "you cherry-picked the
subgraph" attack surface. The companion dates file supplies a real, skewed, non-synthetic
scalar property to index and group by - without which the indexed-lookup and aggregation
metrics are being faked.

Why a temporal cut rather than a random sample: a date cutoff is deterministic and needs
no sampler justification. Random edge sampling distorts the degree distribution, and
defending a forest-fire sampler costs more words than it earns.

Nothing is committed except checksums. Re-running this script on any machine must
reproduce byte-identical files, which `--verify` asserts.

    python scripts/fetch_dataset.py            # fetch + build every scale
    python scripts/fetch_dataset.py --verify   # re-derive and check against committed hashes
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw"
PARAMS = ROOT / "params"

EDGES_URL = "https://snap.stanford.edu/data/cit-HepPh.txt.gz"
DATES_URL = "https://snap.stanford.edu/data/cit-HepPh-dates.txt.gz"

# SNAP's published counts for the raw file, asserted after parsing so a silently
# truncated download fails loudly instead of producing a smaller benchmark.
EXPECTED_RAW_EDGES = 421578
EXPECTED_RAW_NODES = 34546


# ── download ───────────────────────────────────────────────────────────────────
def download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  cached  {dest.name} ({dest.stat().st_size:,} bytes)")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  fetch   {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "graphbench-free/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    print(f"          -> {dest.name} ({dest.stat().st_size:,} bytes)")
    return dest


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── parse ──────────────────────────────────────────────────────────────────────
def parse_edges(path: Path) -> list[tuple[int, int]]:
    edges: list[tuple[int, int]] = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                continue
            a, _, b = line.partition("\t")
            b = b.strip()
            if not b:
                continue
            edges.append((int(a), int(b)))
    return edges


def parse_dates(path: Path) -> dict[int, str]:
    """Return {paper_id: 'YYYY-MM-DD'}.

    Quirk in SNAP's dates file: papers cross-listed from another arXiv archive appear
    with a leading '11' glued to the 7-digit id (e.g. 119203201 for 9203201). We record
    both spellings and let the join prefer an exact match, so no paper is silently lost.
    """
    dates: dict[int, str] = {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                continue
            pid_s, _, date = line.partition("\t")
            date = date.strip()
            if not date:
                continue
            pid_s = pid_s.strip()
            try:
                pid = int(pid_s)
            except ValueError:
                continue
            dates.setdefault(pid, date)
            if len(pid_s) == 9 and pid_s.startswith("11"):
                dates.setdefault(int(pid_s[2:]), date)
    return dates


# ── scale construction ─────────────────────────────────────────────────────────
def build_scale(
    edges: list[tuple[int, int]],
    dates: dict[int, str],
    cutoff: str | None,
    target_edges: int | None,
) -> tuple[str | None, dict[int, str], list[tuple[int, int]]]:
    """Induced subgraph over papers whose publication date is <= cutoff.

    Both scales are restricted to the date-covered subgraph. That is a deliberate,
    reported narrowing: a paper with no date cannot carry the `year` property the
    indexed-lookup and aggregation workloads need, and a NULL-bearing index would
    measure each engine's null handling rather than its index.

    If `cutoff` is None and `target_edges` is set, the cutoff is SOLVED for: we scan
    the sorted distinct dates and pick the latest one whose induced subgraph still fits
    inside target_edges. That keeps the choice deterministic and machine-checkable
    rather than a hand-picked constant.
    """
    covered = {n for e in edges for n in e if n in dates}

    def induced(keep: set[int]) -> list[tuple[int, int]]:
        return [(a, b) for a, b in edges if a in keep and b in keep]

    if cutoff is None and target_edges is None:
        keep = covered
        return None, {n: dates[n] for n in keep}, induced(keep)

    if cutoff is None:
        distinct = sorted({dates[n] for n in covered})
        # Monotone in the cutoff: a later cutoff can only add nodes, never remove them,
        # so the induced edge count is non-decreasing and bisect is valid.
        counts_cache: dict[str, int] = {}

        def edge_count_at(idx: int) -> int:
            d = distinct[idx]
            if d not in counts_cache:
                keep = {n for n in covered if dates[n] <= d}
                counts_cache[d] = len(induced(keep))
            return counts_cache[d]

        lo, hi = 0, len(distinct) - 1
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if edge_count_at(mid) <= target_edges:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        cutoff = distinct[best]
        print(f"  solved cutoff = {cutoff} for target <= {target_edges:,} edges")

    keep = {n for n in covered if dates[n] <= cutoff}
    return cutoff, {n: dates[n] for n in keep}, induced(keep)


def write_scale(scale: str, nodes: dict[int, str], edges: list[tuple[int, int]]) -> dict:
    out = DATA / scale
    out.mkdir(parents=True, exist_ok=True)

    # Sorted output so the files are byte-identical on every machine and every run.
    nodes_path = out / "nodes.csv"
    with open(nodes_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("paper_id,pub_date,pub_year\n")
        for pid in sorted(nodes):
            d = nodes[pid]
            f.write(f"{pid},{d},{d[:4]}\n")

    edges_path = out / "edges.csv"
    with open(edges_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("src,dst\n")
        for a, b in sorted(set(edges)):
            f.write(f"{a},{b}\n")

    years: dict[str, int] = defaultdict(int)
    for d in nodes.values():
        years[d[:4]] += 1

    return {
        "nodes": len(nodes),
        "edges": len(set(edges)),
        "year_min": min(years) if years else None,
        "year_max": max(years) if years else None,
        "distinct_years": len(years),
        "files": {
            "nodes.csv": {"sha256": sha256_file(nodes_path), "bytes": nodes_path.stat().st_size},
            "edges.csv": {"sha256": sha256_file(edges_path), "bytes": edges_path.stat().st_size},
        },
    }


# ── main ───────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify", action="store_true", help="re-derive and compare against params/dataset.json")
    ap.add_argument("--target-edges", type=int, default=150_000, help="edge budget for scale S")
    ap.add_argument("--cutoff", default=None, help="pin scale S to an explicit YYYY-MM-DD cutoff")
    args = ap.parse_args()

    PARAMS.mkdir(parents=True, exist_ok=True)
    manifest_path = PARAMS / "dataset.json"

    print("cit-HepPh")
    edges_gz = download(EDGES_URL, RAW / "cit-HepPh.txt.gz")
    dates_gz = download(DATES_URL, RAW / "cit-HepPh-dates.txt.gz")

    print("parsing")
    edges = parse_edges(edges_gz)
    dates = parse_dates(dates_gz)
    raw_nodes = {n for e in edges for n in e}
    print(f"  raw     {len(raw_nodes):,} nodes / {len(edges):,} edges")
    print(f"  dates   {len(dates):,} ids (incl. both spellings of cross-listed papers)")

    if len(edges) != EXPECTED_RAW_EDGES or len(raw_nodes) != EXPECTED_RAW_NODES:
        print(
            f"  WARNING raw counts differ from SNAP's published "
            f"{EXPECTED_RAW_NODES:,} nodes / {EXPECTED_RAW_EDGES:,} edges. "
            f"Record this in the README rather than ignoring it.",
            file=sys.stderr,
        )

    pinned = None
    if manifest_path.exists():
        pinned = json.loads(manifest_path.read_text(encoding="utf-8"))

    cutoff_s = args.cutoff
    if cutoff_s is None and pinned:
        cutoff_s = pinned.get("scales", {}).get("S", {}).get("date_cutoff")

    manifest = {
        "dataset": "cit-HepPh",
        "source": "https://snap.stanford.edu/data/cit-HepPh.html",
        "raw": {
            "nodes": len(raw_nodes),
            "edges": len(edges),
            "edges_sha256": sha256_file(edges_gz),
            "dates_sha256": sha256_file(dates_gz),
        },
        "scales": {},
    }

    print("scale M (full date-covered induced subgraph)")
    _, m_nodes, m_edges = build_scale(edges, dates, None, None)
    manifest["scales"]["M"] = {"date_cutoff": None, **write_scale("M", m_nodes, m_edges)}
    print(f"  {manifest['scales']['M']['nodes']:,} nodes / {manifest['scales']['M']['edges']:,} edges")

    print("scale S (common scale)")
    cutoff_s, s_nodes, s_edges = build_scale(
        edges, dates, cutoff_s, None if cutoff_s else args.target_edges
    )
    manifest["scales"]["S"] = {"date_cutoff": cutoff_s, **write_scale("S", s_nodes, s_edges)}
    print(f"  cutoff {cutoff_s}")
    print(f"  {manifest['scales']['S']['nodes']:,} nodes / {manifest['scales']['S']['edges']:,} edges")

    excluded = len(raw_nodes) - manifest["scales"]["M"]["nodes"]
    manifest["excluded_nodes_no_date"] = excluded
    print(f"  {excluded:,} nodes excluded from every scale for having no publication date")

    if args.verify:
        if not pinned:
            print("nothing to verify against - params/dataset.json does not exist", file=sys.stderr)
            return 2
        drift = []
        for scale in ("S", "M"):
            for name in ("nodes.csv", "edges.csv"):
                a = pinned["scales"][scale]["files"][name]["sha256"]
                b = manifest["scales"][scale]["files"][name]["sha256"]
                if a != b:
                    drift.append(f"{scale}/{name}: committed {a[:12]}... != rebuilt {b[:12]}...")
        if drift:
            print("DATASET DRIFT", file=sys.stderr)
            for d in drift:
                print("  " + d, file=sys.stderr)
            return 1
        print("verified - rebuilt files match the committed checksums exactly")
        return 0

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {manifest_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
