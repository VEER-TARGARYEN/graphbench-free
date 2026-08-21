# Client VM setup

## Why

The first measurements were taken from Varanasi, India against a CognoDB free instance
that provisioned in us-east (Washington DC, Google Cloud):

| | |
|---|---|
| ICMP ping to the instance | **264 ms** |
| Bolt round trip, p50 over 20 samples | **266 ms** |
| Implied database time | **~2 ms** |

Over 99% of every latency measurement was the network. The load phase showed the same
thing from a different angle: 169 batched round trips took 50.6s, i.e. 0.30s per batch,
which *is* the round trip — the reported "3,392 relationships/sec" was really
`batch_size ÷ RTT` and told us almost nothing about the storage engine.

Engine differences on these workloads are single-digit milliseconds. Under a 266 ms
constant they are around 1% of the signal, so a five-platform comparison taken from here
would rank undersea cables, not databases.

Moving the client into the same region as the instances fixes three things at once:

1. Round trip drops to single-digit milliseconds, so engine time becomes most of the
   measurement rather than a rounding error.
2. It satisfies the brief's *"same client machine and region for every platform"*
   requirement properly rather than nominally.
3. It provides the container runtime that track B needs, which is unavailable on the
   original Windows client (no Docker, WSL broken).

**Keep the original Varanasi numbers.** Running the identical suite from two client
locations against the same database is a genuinely interesting result and costs nothing
extra — it quantifies how much of a managed-tier benchmark is geography. Very few
published benchmarks state their client's distance from the endpoint at all.

## Where

CognoDB's instance IP (`136.70.132.96`) geolocates to Washington DC and is announced by
Google. GCP's Washington-DC-area region is **`us-east4`** (Ashburn, Virginia), so that is
the first choice.

This is an inference from geolocation, not a vendor statement. **Verify by measuring:**
the bootstrap script prints baseline RTT, and if it is not in single digits, destroy the
VM and try `us-east1` or `us-east5` rather than running anyway.

When you create the Memgraph and FalkorDB instances, **pick a us-east region for those
too.** A benchmark where one platform is 3 ms away and another is 80 ms away is not a
comparison, and no amount of careful percentile reporting fixes it.

## Machine

**`e2-standard-2`** — 2 vCPU, 8 GB, Ubuntu 24.04 LTS. Roughly $0.067/hour, so about
**$3.20 for a 48-hour assignment**; delete it afterwards.

Why not smaller: track B runs a 512 MB engine container *and* the load generator on the
same box. The compose file pins the engine to core 1 and leaves core 0 for the
generator — on a 1-vCPU machine they would fight for the same core and you would publish
client queueing as database latency, which is precisely the failure this whole harness is
built to avoid.

Why not the always-free `e2-micro`: it is not offered in `us-east4`, and 1 GB cannot hold
a 512 MB container plus a 40-thread generator.

## Create it

Console: <https://console.cloud.google.com/compute/instances> → **Create instance** →
Region `us-east4`, Machine type `e2-standard-2`, Boot disk **Ubuntu 24.04 LTS** (20 GB
is plenty). No inbound firewall rules are needed — the harness only makes outbound
connections. Then **SSH** from the console.

Or with the `gcloud` CLI:

```bash
gcloud compute instances create graphbench-client --zone=us-east4-c --machine-type=e2-standard-2 --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud --boot-disk-size=20GB
```

```bash
gcloud compute ssh graphbench-client --zone=us-east4-c
```

AWS equivalent: `t3.small` (2 vCPU, 2 GB) in `us-east-1`, Ubuntu 24.04. The 2 GB is
tighter but workable if you run one parity container at a time.

## Set it up

On the VM:

```bash
curl -fsSL https://raw.githubusercontent.com/VEER-TARGARYEN/graphbench-free/main/scripts/bootstrap_vm.sh | bash
```

That installs Python, git, chrony (an accurate clock matters — the scheduler compares
intended send times against actual ones, and drift would be recorded as schedule lag),
Docker, clones the repo, builds the virtualenv from pinned requirements, raises the
open-file limit for 40 concurrent Bolt connections, and runs the harness self-test.

Then:

```bash
cd ~/graphbench-free && cp .env.example .env && nano .env
```

```bash
./.venv/bin/python scripts/probe_platforms.py --all
```

**Stop and read the baseline RTT.** Single-digit milliseconds means the VM is in the
right place. Anything above ~20 ms means it is not, and the VM should be recreated
elsewhere rather than used.

```bash
./.venv/bin/python gbf.py prepare && ./.venv/bin/python gbf.py plan
```

At single-digit RTT the run matrix collapses from roughly 36 minutes per platform to a
few minutes, because the offered-rate calibration is no longer bounded by geography.

## Track B, once the VM is up

```bash
cd ~/graphbench-free && docker compose -f docker/compose.parity.yml up -d neo4j_ce
```

Run **one engine at a time** and check `docker stats` to confirm the generator is not
the bottleneck. Override the cap with `GBF_PARITY_CPUS`, `GBF_PARITY_MEM` and
`GBF_PARITY_CPUSET` if you move to a larger machine.

Expect Neo4j Community to OOM at 512 MB — its stock image wants 512 MB heap plus 512 MB
page cache before JVM overhead. That is a **result**, not a failure. Record the exact
configuration attempted and bisect the smallest cap at which it survives.

## Afterwards

```bash
gcloud compute instances delete graphbench-client --zone=us-east4-c --quiet
```

Copy `results/` and `charts/` off the VM first — or just commit and push from the VM,
which is simpler.
