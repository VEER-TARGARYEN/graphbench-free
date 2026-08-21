"""Query registry for the cit-HepPh citation graph.

SCHEMA (identical on every platform)

    (:Paper {id: INTEGER, pub_date: STRING 'YYYY-MM-DD', pub_year: INTEGER})
    (:Paper)-[:CITES]->(:Paper)

DESIGN RULES, each of which exists to stop a specific unfair comparison:

1. NO VARIABLE-LENGTH PATHS. 1-, 2- and 3-hop are written as three explicit fixed-length
   patterns. Variable-length paths are the least portable construct in the language:
   openCypher mandates each edge at most once per solution, ISO GQL/SQL-PGQ formalise
   four distinct path modes (WALK / TRAIL / SIMPLE / ACYCLIC), and Gremlin walks with
   repeated vertices unless simplePath() is added. `[:CITES*1..3]` would compare four
   different semantics and call the result a performance difference.

2. EVERY READ ENDS IN AN AGGREGATE OR AN EXPLICIT DISTINCT. Cypher's RETURN is bag
   semantics and DISTINCT operates on the whole row; other engines differ. Counting the
   DISTINCT reachable set is the one definition every engine agrees on, and it makes the
   result checksummable - see harness/equivalence.py.

3. EQUALITY ON A SELECTIVE SCALAR FOR THE INDEXED LOOKUP. Not CONTAINS, not a range:
   those measure which exotic index types an engine happens to ship, not its index.

4. INDEX DDL IS PER-DIALECT AND PUBLISHED. The engines genuinely disagree on syntax.
   Hiding that behind a helper would be dishonest; the README prints exactly what ran
   on each platform, from this file.
"""
from __future__ import annotations

# ── read workloads ─────────────────────────────────────────────────────────────
QUERIES: dict[str, str] = {
    "lookup_point": (
        "MATCH (p:Paper {id: $paper_id}) "
        "RETURN p.id AS id, p.pub_year AS year"
    ),
    "lookup_indexed": (
        "MATCH (p:Paper) WHERE p.pub_date = $pub_date "
        "RETURN count(p) AS n"
    ),
    "traversal_1hop": (
        "MATCH (:Paper {id: $paper_id})-[:CITES]->(c:Paper) "
        "RETURN count(DISTINCT c) AS n"
    ),
    "traversal_2hop": (
        "MATCH (:Paper {id: $paper_id})-[:CITES]->(:Paper)-[:CITES]->(c:Paper) "
        "RETURN count(DISTINCT c) AS n"
    ),
    "traversal_3hop": (
        "MATCH (:Paper {id: $paper_id})-[:CITES]->(:Paper)-[:CITES]->(:Paper)-[:CITES]->(c:Paper) "
        "RETURN count(DISTINCT c) AS n"
    ),
    "aggregation_by_year": (
        "MATCH (p:Paper) "
        "RETURN p.pub_year AS year, count(*) AS n "
        "ORDER BY year"
    ),
    # Write half of the mixed workload. Every written node carries :BenchWrite so the
    # write set can be deleted and the database restored to a byte-identical starting
    # state between runs - without that, run N+1 measures a different graph than run N.
    "write_insert_citation": (
        "MATCH (t:Paper {id: $paper_id}) "
        "CREATE (n:Paper:BenchWrite {id: $new_id, pub_date: $pub_date, pub_year: $pub_year}) "
        "CREATE (n)-[:CITES]->(t) "
        "RETURN n.id AS id"
    ),
}

CLEANUP = "MATCH (n:BenchWrite) DETACH DELETE n"

COUNT_NODES = "MATCH (n:Paper) RETURN count(n) AS n"
COUNT_RELS = "MATCH ()-[r:CITES]->() RETURN count(r) AS n"

# ── ingest ─────────────────────────────────────────────────────────────────────
# Driver-side UNWIND batching on EVERY platform. Managed tiers cannot run a server-side
# bulk importer and cannot read a server-local file, so this is the only load method
# available everywhere. Consequence, stated rather than hidden: the ingest figure
# measures client RTT and PackStream serialization as much as the storage engine, and
# track-A (WAN + TLS) ingest is NOT comparable with track-B (loopback) ingest.
LOAD_NODES = (
    "UNWIND $rows AS r "
    "CREATE (p:Paper {id: r.id, pub_date: r.d, pub_year: r.y})"
)

LOAD_EDGES = (
    "UNWIND $rows AS r "
    "MATCH (a:Paper {id: r.s}) "
    "MATCH (b:Paper {id: r.t}) "
    "CREATE (a)-[:CITES]->(b)"
)

# ── per-dialect index DDL ──────────────────────────────────────────────────────
# Phase order, applied identically everywhere and timed phase by phase:
#   1. load nodes
#   2. build the :Paper(id) index          <- BEFORE edges, or edge lookup is quadratic
#   3. load edges
#   4. build the :Paper(pub_date) index    <- the secondary index under test
# All four phases count toward total wall-clock load time (Raasveldt S 3.7: excluding
# index-build time from ingest is a named pitfall).
DIALECTS: dict[str, dict[str, list[str]]] = {
    "default": {  # Neo4j 5.x, CognoDB, ArcadeDB
        "index_id": ["CREATE INDEX paper_id IF NOT EXISTS FOR (p:Paper) ON (p.id)"],
        "index_secondary": ["CREATE INDEX paper_date IF NOT EXISTS FOR (p:Paper) ON (p.pub_date)"],
    },
    "memgraph": {
        "index_id": ["CREATE INDEX ON :Paper(id)"],
        "index_secondary": ["CREATE INDEX ON :Paper(pub_date)"],
    },
    "falkordb": {
        "index_id": ["CREATE INDEX FOR (p:Paper) ON (p.id)"],
        "index_secondary": ["CREATE INDEX FOR (p:Paper) ON (p.pub_date)"],
    },
}

# Some engines reject `IF NOT EXISTS` or a named index; the adapter falls back through
# these on a syntax error rather than silently running unindexed, which would make the
# platform look catastrophically slow for a reason that is our fault, not theirs.
INDEX_FALLBACKS: dict[str, list[str]] = {
    "index_id": [
        "CREATE INDEX paper_id IF NOT EXISTS FOR (p:Paper) ON (p.id)",
        "CREATE INDEX FOR (p:Paper) ON (p.id)",
        "CREATE INDEX ON :Paper(id)",
    ],
    "index_secondary": [
        "CREATE INDEX paper_date IF NOT EXISTS FOR (p:Paper) ON (p.pub_date)",
        "CREATE INDEX FOR (p:Paper) ON (p.pub_date)",
        "CREATE INDEX ON :Paper(pub_date)",
    ],
}

# ── footprint probes ───────────────────────────────────────────────────────────
# Deliberately per-engine and allowed to fail. Where an engine exposes nothing, the
# results record "not observable" - which the brief explicitly asks for, and which is
# itself a finding: the asymmetry between engines that publish a memory figure and
# engines that do not is worth a sentence.
FOOTPRINT_PROBES: dict[str, list[tuple[str, str]]] = {
    "memgraph": [
        ("storage_info", "SHOW STORAGE INFO"),
    ],
    "default": [
        # Neo4j-family introspection. Unavailable on most managed free tiers, and on
        # CognoDB specifically (no APOC, no dbms.* procedures documented).
        ("store_sizes", "CALL dbms.queryJmx('org.neo4j:name=Store sizes,*')"),
    ],
}


def workload_ids() -> list[str]:
    return list(QUERIES)
