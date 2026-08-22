# Draft reply to Wexa AI

**To:** hr@wexa.ai
**Subject:** CognoDB Assignment 1 – <Your Name>

---

Dear HR Team,

Thank you for the assignment. My submission is here:

**https://github.com/VEER-TARGARYEN/graphbench-free** (public)

I want to be straightforward about what is and isn't finished, because the brief asks
for honest caveats and I would rather you hear the gaps from me.

## What is complete

A reproducible benchmark harness and a full measurement of **CognoDB Cloud (c0)** against
every metric in section 5.2 — ingest throughput, 1/2/3-hop traversal latency, point and
indexed lookups, aggregation, a concurrent read/write mixed workload swept across 1/10/40
clients, and footprint. 300 iterations per read workload, p50/p75/p90/p95/p99/p99.9
reported, every run passing the validity gate with zero errors.

Everything runs from one command. `python gbf.py selftest` verifies the entire harness
against in-process mock engines with no network and no credentials, so anyone cloning the
repo can confirm it works before spending metered cloud time.

## What is not complete

**The full five-platform sweep is still running.** I have CognoDB measured properly and
self-hosted engines under way on a resource-capped parity track; I did not want to submit
four additional platforms measured carelessly in order to hit a number. The harness takes
a new database as one adapter file, and I will push results as they land.

I would rather show you a defensible method with one platform fully characterised than a
complete-looking matrix I could not defend in an interview.

## Three findings I think are worth your time

**1. The brief's spec for CognoDB does not match the pricing page.** The assignment states
the free `c0` tier is 256 MB; cognodb.com/pricing stated 512 MB when I checked it on
20 Aug 2026. This matters because capping competitors at 256 MB while the system under
test runs at 512 MB would hand the field a 2× handicap in CognoDB's favour — the exact
inverse of the fairness the brief asks for. I sized the parity track to the published
figure and documented both.

**2. Most of a managed-tier benchmark is geography, and almost nobody reports it.** My
first run measured from India against an instance that provisioned in us-east: a 266 ms
round trip, of which roughly 2 ms was the database. Every workload — a point lookup and a
3-hop traversal alike — reported ~271 ms. Re-running from a client in Ashburn, the same
city as the instance, moved the baseline to 3.9 ms and ingest from 3,392 to 28,052
relationships/sec. Both runs are committed. I think the comparison is more interesting
than either number alone.

**3. CognoDB's caching behaviour is visible in the data.** Point lookups, indexed lookups
and all three traversal depths show no measurable cold-versus-hot difference — they are
index-served, so there is nothing to warm. The aggregation, a full label scan over 18,265
nodes, goes **390.4 ms cold to 80.2 ms hot**, a 4.9× swing. That is page-cache warming,
and it independently corroborates your own description of the engine as disk-backed with
working-set caching.

## On methodology

Four decisions drove the design, all documented in `FAIRNESS.md`:

- **Open-loop measurement.** Latency is measured from when a request *should* have been
  sent, not when it was. A closed-loop `for` loop stalls with the database and never
  issues the requests that would have queued, which systematically flatters whichever
  platform stalls hardest. I record corrected and uncorrected latency and publish the gap.
- **Curated query parameters.** `ORDER BY rand() LIMIT 100` measures a graph's degree
  distribution, not its database. Start nodes are curated offline and frozen to CSV so
  every platform receives byte-identical parameters. I also run an uncurated control: same
  median, 5.8× worse at p95, and it failed the validity gate where the curated run passed.
- **Proved query equivalence.** Every read query is executed once per platform and its
  result canonicalised and SHA-256 hashed before any timing runs. I could not find a
  published graph-database benchmark that does this.
- **A validity gate, adopted from LDBC SNB Interactive v2.** A run in which fewer than 95%
  of operations start within 1s of schedule is marked invalid rather than reported as a
  comparable measurement.

The repository is LDBC-inspired, not LDBC-audited, and says so. No vendor-published
benchmark result is cited as evidence anywhere in it.

## One thing I flagged rather than acted on

Neo4j's Acceptable Use Policy appears to prohibit using the Service to benchmark it, and
it governs AuraDB Free. I have left Aura disabled and documented three possible
resolutions in `docs/LEGAL.md` rather than publish numbers against terms I have not
cleared. I would welcome your view on how you would prefer that handled.

Happy to walk through any part of the code or the reasoning.

Best regards,
<Your Name>
<phone / LinkedIn>
