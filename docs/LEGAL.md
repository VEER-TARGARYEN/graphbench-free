# Terms-of-service audit

> **Status: INCOMPLETE — this file gates `aura_free` in `config/platforms.yaml`.**
> Every clause below must be read from the primary document by a human and quoted from
> what was actually read before any of it is published. Do not paste a search-result
> summary into a public repository.

Publishing a benchmark of a commercial database service can violate that service's terms.
The restriction is old enough to have a name — the **DeWitt clause**, after David DeWitt,
whose Wisconsin Benchmark led Oracle to add contractual bans on publishing benchmark
results in the 1980s. It is not a historical curiosity: it is live on at least one of the
free tiers this project is most likely to measure.

No graph-database benchmark write-up surveyed for this project has ever published a
terms-of-service audit. Doing so costs an afternoon and serves methodology, fairness and
communication simultaneously.

---

## Audit table

| Platform | Benchmark clause? | Source | Read from primary? | Resolution |
|---|---|---|---|---|
| CognoDB Cloud | Not yet checked | cognodb.com terms | ☐ | — |
| Neo4j AuraDB Free | **Appears to prohibit** benchmarking the Service | legal.neo4j.com (Acceptable Use Policy, incorporated by the Aura Self-Serve ToS) | ☐ | **Pending** |
| Memgraph Cloud | Not yet checked | memgraph.com terms | ☐ | — |
| FalkorDB Cloud | Not yet checked | falkordb.com terms | ☐ | — |
| Neo4j Community (self-hosted) | GPLv3 — no benchmark restriction | neo4j.com licensing | ☐ | Free to publish |
| Memgraph Community (self-hosted) | BSL — check the additional use grant | memgraph.com licensing | ☐ | — |
| ArcadeDB (self-hosted) | Apache-2.0 — no benchmark restriction | github.com/ArcadeData/arcadedb | ☐ | Free to publish |

## The Neo4j question

Search results indicate that Neo4j's Acceptable Use Policy prohibits using the Service
"in order to benchmark the Service or to build similar or competitive products or
services", and that the AUP governs Aura Self-Serve — which includes the free tier.

**This has not been verified from the primary document.** `neo4j.com` returns HTTP 403 to
automated fetchers, so every Neo4j figure gathered during research is secondhand. This is
the one item in the whole project where being wrong is legally rather than merely
methodologically embarrassing.

**Action:** open the AUP in a browser, read the relevant section, and either quote what
it says or record that it says something different.

### Three resolutions, pick one and publish which

1. **Ask.** Email each vendor's developer-relations or legal contact, describe the
   methodology, request written permission, and publish the replies — including
   "invited, no response by *date*" for those who do not answer. Costs one email each
   and doubles as visible relationship-building. Realistically, few replies arrive inside
   48 hours; the ask itself is still worth documenting.
2. **Substitute.** Benchmark the self-hosted Community Edition in a container capped to
   the parity envelope. This is explicitly permitted by the assignment brief, sidesteps
   the DBaaS terms entirely, and is arguably the more scientific comparison anyway. The
   cost is that the *managed tier* goes unmeasured, which must be said.
3. **Anonymise.** Report the platform as "Managed Graph DB C", following the long
   research convention of "DBMS-X" — and explain why. The explanation is more interesting
   than the name.

Precedent worth noting: ClickBench handles DeWitt-bound systems by accepting a
methodology description without published numbers.

## Rules for this repository

- No clause is quoted here unless a human opened the source document and read it.
- Every row records the date it was checked. Terms change.
- A platform stays `enabled: false` until its row is resolved.
- If a vendor asks for a correction after publication, publish the correction.
