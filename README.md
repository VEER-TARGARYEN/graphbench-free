# graphbench-free

**A reproducible benchmark of managed graph databases on the tiers you actually get for free.**

Every number in this repository was produced by one command against free or entry tiers
that anyone can create without a credit card. The harness, the dataset cut, the query
parameters and the raw latency histograms are all committed, so the results can be
re-derived rather than trusted.

I sell nothing. I am not affiliated with any database vendor.

> **Status:** harness complete and self-tested; results pending platform provisioning.
> Sections marked *(pending)* are filled by the run, not by hand.

---

## TL;DR results matrix *(pending)*

<!-- One table, every platform x every required metric, p50/p95. Readers who bounce
     still see the payload. Generated from results/<run>/manifest.json. -->

| Platform | Tier (advertised) | Ingest nodes/s | Ingest rels/s | 1-hop p50/p95 | 2-hop p50/p95 | 3-hop p50/p95 | Point lookup p50/p95 | Indexed lookup p50/p95 | Aggregation p50/p95 | Mixed @40 q/s | Footprint |
|---|---|---|---|---|---|---|---|---|---|---|---|
| CognoDB Cloud (c0) | 0.5 vCPU burst / 512 MB / 1 GB | | | | | | | | | | |

---

## Why you should distrust this benchmark

This section is deliberately **above** the results.

Public graph-database benchmarks have a bad record, and the most-discussed artifact in
the genre is a takedown of one rather than one. Here is the list of attacks that have
sunk previous benchmarks, and what this one does about each.

| Attack | Applies here? | Response |
|---|---|---|
| "This is marketing for your own product." | No | I have no product. |
| "In-memory vs on-disk isn't a fair comparison." | **Yes** | Unavoidable — Memgraph and FalkorDB are in-memory, CognoDB and Neo4j are disk-backed. Rather than hide it, it is the analytical spine: a RAM cap is a page-cache constraint for one architecture and an absolute ceiling for the other. Both tracks report it. |
| "The dataset is tiny; come back at a billion edges." | **Yes** | 150k relationships. That is the *point* — the brief's tiers cap at 100 MB–2 GB RAM. This benchmark measures what a free tier does, not what a cluster does. Scale M (347k edges) is run wherever it fits. |
| "Cypher-only excludes Gremlin/AQL/GSQL vendors." | **Yes** | Deliberate. One client library and one query text across five engines is the fairness guarantee; adding a second query language would add a confound worth more than the extra vendor. Named as a limitation, not a feature. |
| "Load time isn't measured." | No | Ingest is metric #1, split by phase, including index build. |
| "No reference point." | No | A relational baseline (Postgres recursive CTE) runs on the same data at the same cap. |
| "Peak memory is meaningless across architectures." | **Yes** | Which is why footprint is reported as whatever each platform exposes, and `not observable` where it exposes nothing. No figure is estimated. |
| "You cherry-picked percentiles." | No | p50/p75/p90/p95/p99/p99.9 for every workload, plus the full histogram embedded so you can compute your own. |
| "Unequal iteration counts across systems." | No | Read once from config; the runner cannot vary them per platform. |
| "Undisclosed or ancient hardware." | No | Client specs, region, measured RTT to each endpoint and measured timer granularity are in every run manifest. |
| "Too short to cross a checkpoint/GC boundary." | Partly | Mixed workload runs 120s per level. Shorter read workloads are noted as such. |
| "You benchmarked it wrong / configured mine badly." | Possibly | Every query, index DDL and connection setting is in the repo. Corrections are welcome and will be published. |
| "Result caching, not query performance." | No | Result caches disabled; cold and hot reported separately. |

Two things this benchmark does that I could not find in any published graph-DB benchmark:
**cross-engine result checksums** (§ equivalence) and a **terms-of-service audit**
([docs/LEGAL.md](docs/LEGAL.md)).

---

## The fairness ledger *(pending)*

Advertised specs versus what was actually observed. The "specs published at all?" column
is not snark — it is a sourced observation about what these vendors disclose.

| Platform | vCPU | RAM | Disk | Max conns | Specs published? | Observed RAM | Observed throttling | Idle/delete policy | Verified |
|---|---|---|---|---|---|---|---|---|---|
| CognoDB Cloud (c0) | burst 0.5 | 512 MB | 1 GB | 200 | **Yes** (all four) | *(pending)* | *(pending)* | none | 2026-08-20 |
| Neo4j AuraDB Free | — | — | — | — | **No** | | | pauses after 72h | 2026-08-20 |
| Memgraph Cloud | — | 2 GB | — | — | Partial | | | 14-day trial | 2026-08-20 |
| FalkorDB Cloud Free | — | 100 MB | none | — | Partial | | | stops 1d, deleted 7d | 2026-08-20 |

**The tiers are not comparable, and that is the finding.** These four vendors sell in four
incommensurable currencies: 100 MB, 512 MB, 2 GB, and "we don't say". That is why there
are two tracks.

- **Track A — as sold.** Managed tiers exactly as a developer receives them. Answers
  "what do I actually get for $0?" Explicitly **not** apples-to-apples.
- **Track B — parity.** Every engine self-hosted under an identical cgroup cap
  (`--cpus=0.5 --memory=512m --memory-swap=512m`). This is the track that supports
  engine-to-engine claims. CognoDB is cloud-only, so it cannot appear here — which is
  itself worth recording: a managed-only engine is an unauditable one.

> **Note on the brief.** The assignment states CognoDB's free tier is 256 MB.
> [cognodb.com/pricing](https://cognodb.com/pricing) stated 512 MB when checked on
> 2026-08-20. Capping five competitors at 256 MB while the vendor under evaluation runs
> at 512 would handicap the field 2× in the vendor's favour. The parity cap therefore
> follows the vendor's published figure, and the console screenshot is in `docs/`.

---

## Dataset

**SNAP cit-HepPh** — the arXiv high-energy-physics citation network — joined with its
publication-dates file.

| | Nodes | Relationships |
|---|---|---|
| Raw (SNAP published) | 34,546 | 421,578 |
| Scale M (date-covered induced subgraph) | 30,558 | 347,268 |
| **Scale S (common scale, cutoff 1999-02-26)** | **18,265** | **149,969** |

Why this dataset:

- 421,578 edges is inside the brief's 100k–500k band **with no sampling at all** — no
  seed to defend, no sampler to justify, no "you cherry-picked the subgraph" objection.
- The dates file supplies a real, skewed, non-synthetic scalar to index and group by.
  Without it the indexed-lookup and aggregation metrics would be measuring a property
  invented for the benchmark.
- As a citation DAG it is near-acyclic, which largely neutralises the relationship-
  uniqueness semantics that make multi-hop counts differ across engines.
- Scale S is sized to the **tightest published cap in the lineup**, so no platform is
  excluded for capacity reasons.

3,988 nodes carry no publication date and are excluded from every scale. Scale S is cut
by a **published date cutoff**, not a random sample: a temporal cut is deterministic and
needs no seed. `scripts/fetch_dataset.py --verify` re-derives both scales and checks them
against the committed SHA-256 in `params/dataset.json`.

### Schema

```cypher
(:Paper {id: INTEGER, pub_date: STRING, pub_year: INTEGER})
(:Paper)-[:CITES]->(:Paper)
```

---

## Methodology

Full contract in [FAIRNESS.md](FAIRNESS.md). The four decisions that matter:

### 1. Open-loop dispatch

Send times are computed before the run. Latency is measured from the **scheduled** send
time, not the actual one. The naive alternative —

```python
for i in range(100):
    t = now(); query(); record(now() - t)
```

— is a closed loop: when the database stalls, the client stalls with it and never issues
the requests that would have queued behind the stall, so the stall never appears in the
tail. On a throttled free tier this is perverse, because it flatters whichever platform
stalls hardest. Both corrected and uncorrected latencies are recorded and the delta is
published.

A run in which fewer than **95% of operations start within 1s of schedule** is marked
`valid: false` (rule adopted from LDBC SNB Interactive v2). A platform that cannot sustain
the offered rate fails visibly instead of reporting a flattering number.

### 2. Curated start nodes

`ORDER BY rand() LIMIT 100` measures the degree distribution, not the database. Start
nodes are curated offline — keep the 40th–60th percentile band of k-hop frontier size —
and frozen to CSV so every platform gets byte-identical parameters.

On this dataset, at 3 hops:

| | Frontier size range | σ |
|---|---|---|
| Uniform random | 1 – 1,238 | 289.6 |
| **Curated** | **52 – 144** | **26.5** |

A uniform-random control set is also run, so `charts/curation_effect.png` shows the
effect rather than asserting it.

### 3. Proved query equivalence

Every read query runs once on every platform before any timing. Results are canonicalised
and SHA-256 hashed; the comparison table ships in the run manifest. Divergence is
**reported, not fatal** — a gate that drops a platform because an integer arrived as a
float would drop the submission.

k-hop is defined as the DISTINCT reachable-node count, and 1/2/3-hop are written as three
**fixed-length** patterns. Variable-length paths (`[:CITES*1..3]`) do not mean the same
thing on any two engines.

### 4. Interleaved, randomised run order

Platforms are never run in contiguous blocks — that bakes in both time-of-day effects and
burstable CPU-credit state, so every platform after the first runs under different
conditions. Order is seeded-random, with a 120s recovery idle between platforms.

---

## Reproducing

Free-tier accounts on the platforms you want to measure are the only prerequisite.

```bash
git clone <repo-url> && cd graphbench-free
python -m venv .venv && .venv/Scripts/activate        # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                   # then fill in your connection details
```

```bash
python scripts/smoke_test.py
```

Verifies the whole harness — load, equivalence, open-loop dispatch, percentile recording,
the validity gate — against two in-process mock engines. No network, no credentials. Run
this before spending metered cloud time.

```bash
python scripts/fetch_dataset.py && python scripts/curate_params.py --scale S
```

```bash
python scripts/probe_platforms.py --all
```

**Run this before anything else.** It proves each endpoint is reachable, measures baseline
RTT, and builds a per-engine Cypher capability matrix. A platform that fails here is
excluded in hour one, when exclusion is a documented result, rather than in hour thirty.

```bash
python scripts/run_benchmark.py --dry-run
```

Multiplies out the run matrix and prints a time estimate before connecting to anything.

```bash
python scripts/run_benchmark.py --only cognodb && python scripts/make_charts.py
```

---

## Adding a database

One file. `harness/adapters/base.py` defines the contract; there is no `if platform == …`
anywhere in the runner. Per-engine differences live in adapters and in the dialect tables
of `harness/workloads/cit_hepph.py`.

1. Implement `Adapter` (or reuse `BoltAdapter` if the engine speaks Bolt).
2. Add index DDL to `DIALECTS` if the syntax differs.
3. Add a platform block to `config/platforms.yaml` with its **advertised specs and the
   URL you read them from**.
4. `python scripts/probe_platforms.py --only <your-id>`.

---

## Results *(pending)*

<!-- One subsection per metric family, each with p50/p95, variance across repeats, and
     the on-time validity column. Never a bare mean. -->

## Analysis *(pending)*

<!-- Root cause, not a leaderboard. The starting hypothesis, to be confirmed or
     falsified: CognoDB is disk-backed with working-set caching (~80 bytes per edge, per
     the vendor), so a memory cap is a page-cache constraint; Memgraph and FalkorDB are
     in-memory, so the same cap is an absolute dataset ceiling; Neo4j is disk-backed but
     pays a JVM floor that may not fit inside the cap at all. -->

## What went wrong *(pending)*

<!-- Failed runs, timeouts, exclusions, surprises. Credibility compounds here. -->

---

## Credits and honesty notes

- Methodology follows Raasveldt et al., *Fair Benchmarking Considered Difficult*
  (DBTest'18); LDBC SNB Interactive v2 (Püroja, Waudby, Boncz & Szárnyas, TPCTC 2023);
  and Hoefler & Belli, *Twelve Ways to Tell the Masses* (SC'15).
- Harness architecture — the adapter/workload split, the results schema, the warm-up
  taxonomy — is modelled on Memgraph's `mgbench`. Measurement semantics are modelled on
  the LDBC SNB Interactive v2 driver and on wrk2. **Architecture only: no vendor-published
  benchmark result is cited as evidence anywhere in this repository.**
- These are **not** official LDBC results. Not audited or endorsed by LDBC.
- No vendor paid for, reviewed, or approved this benchmark except where explicitly noted.

## Licence

MIT for the harness. The cit-HepPh dataset belongs to SNAP/Stanford under its own terms.
