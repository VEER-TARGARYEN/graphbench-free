"""Cross-engine result equivalence.

Comparing timings before comparing RESULTS is the silent killer of multi-engine
benchmarks. The same query legitimately returns different rows on different engines:
Cypher applies no-repeated-edge semantics per MATCH clause while Gremlin applies
homomorphism, `id()` yields a STRING on some engines and an INTEGER on others, RETURN is
bag semantics in Cypher but not everywhere, and openCypher subsets differ. If the row
counts disagree, every latency number published alongside them is meaningless.

No published graph-database benchmark surveyed for this project publishes per-query
result checksums across engines. LDBC's driver has the closest analogue - generate
validation parameters from a reference implementation, then replay against every other
implementation - and the openCypher TCK compares results as UNORDERED sets unless the
query has ORDER BY. This module borrows from both.

ONE DELIBERATE DESIGN CHOICE: divergence is RECORDED AND REPORTED, never fatal. A gate
that deletes a platform from the results because an integer came back as a float is a
gate that deletes the submission. The divergence table is the artifact; aborting is not.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence


def _canon_scalar(v: Any) -> Any:
    """Normalise a single value so cosmetic type differences do not read as divergence.

    Integers-as-floats, ids-as-strings and Decimal-like numerics are the three real
    offenders. Anything genuinely different - a different count, a different set of
    years - still hashes differently, which is the point.
    """
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        # 3.0 and 3 must agree; 3.5 must not silently become 3.
        return int(v) if v.is_integer() else round(v, 6)
    if isinstance(v, str):
        s = v.strip()
        # Engines disagree on whether node ids are strings or integers.
        if s.lstrip("-").isdigit():
            return int(s)
        return s
    if isinstance(v, (list, tuple)):
        return [_canon_scalar(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _canon_scalar(x) for k, x in sorted(v.items())}
    # Temporal / spatial / node / relationship objects - fall back to their string form.
    return str(v)


def canonicalise(rows: Sequence[dict], *, ordered: bool = False) -> list:
    """Return a stable, comparable form of a result set.

    Rows are sorted unless the query itself carried ORDER BY, matching the openCypher
    TCK convention that results are unordered sets absent an explicit ordering.
    """
    canon = [
        [[str(k), _canon_scalar(v)] for k, v in sorted(r.items())]
        for r in rows
    ]
    if not ordered:
        canon.sort(key=lambda row: json.dumps(row, sort_keys=True, default=str))
    return canon


def checksum(rows: Sequence[dict], *, ordered: bool = False) -> dict:
    canon = canonicalise(rows, ordered=ordered)
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "row_count": len(rows),
        "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
        # A tiny preview so a human can see WHAT diverged without re-running anything.
        "preview": canon[:3],
    }


def compare(table: dict[str, dict[str, dict]]) -> dict:
    """table = {workload_id: {platform_id: checksum_dict}} -> divergence report."""
    report: dict = {"agree": [], "diverge": {}}
    for workload, by_platform in sorted(table.items()):
        hashes = {p: c["sha256"] for p, c in by_platform.items()}
        distinct = set(hashes.values())
        if len(distinct) <= 1:
            report["agree"].append(workload)
            continue
        groups: dict[str, list[str]] = {}
        for platform, h in sorted(hashes.items()):
            groups.setdefault(h, []).append(platform)
        report["diverge"][workload] = {
            "groups": [
                {
                    "sha256": h[:16],
                    "platforms": members,
                    "row_count": by_platform[members[0]]["row_count"],
                    "preview": by_platform[members[0]]["preview"],
                }
                for h, members in sorted(groups.items(), key=lambda kv: -len(kv[1]))
            ]
        }
    report["verdict"] = "all platforms agree" if not report["diverge"] else (
        f"{len(report['diverge'])} of {len(table)} workloads diverge - "
        f"latency numbers for those workloads are NOT comparable and are flagged in the results"
    )
    return report
