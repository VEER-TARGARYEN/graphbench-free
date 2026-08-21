# Fairness contract

Rules fixed **before** any numbers were collected. Where this benchmark cannot satisfy a
rule, that is stated here rather than omitted — a benchmark that only lists the checks it
passes is marketing.

Structure adapted from Raasveldt, Holanda, Gubner & Mühleisen, *Fair Benchmarking
Considered Difficult* (DBTest'18) and the LDBC SNB Specification's auditing chapter.
This is **LDBC-inspired, not LDBC-audited** — "LDBC benchmark result" is a term reserved
for auditor-certified runs and is not claimed here.

---

## 1. Allowed

- Native indexes, declared per platform. The exact DDL that ran is recorded in every
  results file under `ingest.index_ddl` and reprinted in the README.
- `UNWIND`-based batch loading at a fixed batch size (1,000), identical everywhere.
- Connection pooling, with identical pool settings on every platform
  (`config/platforms.yaml → defaults.bolt`).
- Each engine's own query dialect for index DDL, where the syntax genuinely differs.
- Vendor-recommended tuning applied to **every** platform, not only to the one under
  evaluation. Raasveldt §3.2 names one-sided tuning as a pitfall in its own right.

## 2. Disallowed

- Precomputing or caching answers between iterations.
- Result caches. Buffer pools and page caches are fine — they are how the engine works.
  (Protocol borrowed from ClickBench: *result* caches off, *source-data* caching fine.)
- Hand-forced query plans or engine hints. LDBC prohibits explicit plans in read queries.
- Benchmark-shaped indexes: an index exists only if a normal application would create it.
- Any query variant tuned for one engine and not translated for the others.
- Raising the batch size for a platform that struggles at 1,000.
- Unequal iteration counts. This is the specific flaw that got the best-known graph-DB
  benchmark publicly dismantled, and the runner reads iteration counts once from
  `config/workloads.yaml` and cannot vary them per platform.

## 3. Query equivalence

Same logical query everywhere, and equivalence is **proved, not asserted**:

- Every read query runs once against every platform before any timing. Results are
  canonicalised (rows sorted, values normalised for int/float and string/integer id
  differences) and SHA-256 hashed into `equivalence_report` in the run manifest.
- Divergence is **recorded and reported, never fatal**. A gate that deletes a platform
  from the results because an integer came back as a float is a gate that deletes the
  submission. Workloads that diverge are flagged, and their latencies are marked
  not-comparable rather than silently published.
- k-hop is defined as the **DISTINCT reachable-node count**, not a path count. Path
  counts legitimately differ across engines — Cypher applies no-repeated-edge semantics
  per `MATCH` clause while Gremlin applies homomorphism — so a path count would compare
  semantics and call the difference performance.
- **No variable-length paths.** 1-, 2- and 3-hop are three explicit fixed-length
  patterns. openCypher mandates each edge at most once per solution; ISO GQL/SQL-PGQ
  formalise four distinct path modes. `[:CITES*1..3]` would not mean the same thing on
  any two engines.
- The indexed lookup uses **equality on a single selective scalar** (`pub_date`, 2,329
  distinct values, median 7 rows returned). Not `CONTAINS`, not a range — those measure
  which exotic index types an engine happens to ship.

## 4. Parameters

- Start nodes are **curated offline and frozen to CSV** (`params/start_nodes_*.csv`),
  byte-identical for every platform. Selection keeps nodes whose k-hop frontier size
  falls in the 40th–60th percentile band, sampled with seed 42.
- Rationale, with numbers from this dataset: at 3 hops the uncurated frontier size ranges
  1–1,238 (σ = 289.6); curated, 52–144 (σ = 26.5). Uniform-random start nodes would make
  the workload a measurement of the degree distribution.
- A uniform-random control set is also frozen and run, so the effect is shown rather than
  claimed (`charts/curation_effect.png`).

## 5. Measurement

- **Open-loop, scheduled dispatch.** Send times are computed before the run; latency is
  measured from the scheduled time, not the actual send. Both numbers are recorded and
  the delta is published.
- **Validity gate.** A run in which fewer than 95% of operations start within 1s of
  schedule is marked `valid: false` and is not presented as a comparable measurement.
  Adopted from LDBC SNB Interactive v2.
- Client concurrency and offered rate are independent axes.
- Percentiles from HdrHistogram at 3 significant figures; the full histogram is embedded
  in every result so any percentile can be recomputed by a reader.
- Warm-up: `cold` (no warm-up) and `hot` (200 warm-up operations) reported separately.
- Statistics follow Hoefler & Belli: arithmetic mean for costs only, never for rates;
  no summarising of ratios; no normality assumption, therefore no mean±stddev error bars.

## 6. Run protocol

- Platforms are **interleaved in a seeded random order**, never run in contiguous blocks.
  A contiguous block bakes in both time-of-day effects and burstable CPU-credit state.
- A 120s idle window separates platforms so a drained credit bucket does not hand the
  next platform an advantage.
- Timeout policy is **pre-declared** (30s per query). A timeout is a documented outcome,
  not a dropped run.
- The write set is deleted between mixed-workload levels so every level starts from an
  identical graph.

## 7. What this benchmark CANNOT do

Stated plainly, because these are the limits a reader should apply to every number here.

| Limitation | Consequence |
|---|---|
| Managed tiers cannot run a server-side bulk importer or read a server-local file. All ingest is driver-side `UNWIND` over the wire. | The ingest metric measures client RTT and PackStream serialization as much as the storage engine. **Track-A (WAN+TLS) and track-B (loopback) ingest figures are not comparable with each other.** |
| OS page caches cannot be dropped on a managed instance — caching also happens on the virtualization host (Raasveldt §3.6). | "Cold" means *cold cache after idle*, not a cold machine. A genuinely cold start requires destroying and recreating the instance, and is reported separately where measured. |
| Free tiers are burstable and the CPU-credit balance is not observable from the client. | Throttling is detected indirectly, via the on-time gate and schedule lag, not read from a counter. |
| Most managed tiers expose no memory or stored-size metric. | Footprint is reported as `not observable` where it is not observable. No figure is estimated. |
| The client is a 4-core laptop. | At 40 concurrent clients the client itself can become the constraint. Timer resolution is raised to 1 ms and measured sleep granularity is recorded in every run so a reader can judge whether a sub-millisecond figure is meaningful. |
| Free tiers differ by up to 20× in advertised RAM (100 MB to 2 GB). | The "as sold" track is explicitly **not** apples-to-apples and is labelled as such. Only the parity track supports engine-to-engine claims. |
| Vendor-published benchmark results exist for several of these engines. | None are cited as evidence. Their *architecture* is reused and credited; their numbers are not. |

## 8. Honest caveats log

Every failed run, timeout, exclusion and surprise is recorded in the run manifest and
summarised in the README. Exclusions are results:

- A platform whose Bolt endpoint could not be opened is reported as excluded, with the
  error text.
- A platform that could not hold the dataset is reported as unable to hold it.
- A platform that OOMed under the parity cap is reported as OOMing, with the exact
  configuration attempted and the smallest cap at which it survived.
