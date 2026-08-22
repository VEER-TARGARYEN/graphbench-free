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

## Machine — free options, ranked

**Oracle Cloud Always Free, region `us-ashburn-1` (Ashburn, VA) — recommended.**
Free forever, no expiry, and Ashburn is the exact city the CognoDB instance geolocates
to — likely the lowest RTT of any option here, paid or free. Sign up at
[cloud.oracle.com](https://signup.cloud.oracle.com) and pick home region
**US East (Ashburn)** at signup — this cannot be changed later. A card is required for
identity verification but the Always Free tier is never charged.

Shape: **`VM.Standard.A1.Flex`** (Ampere/Arm, up to 4 OCPU / 24 GB, Always Free) if
your tenancy has Ampere capacity, otherwise **`VM.Standard.E2.1.Micro`** (AMD, 1 GB,
Always Free — tight, but workable for track A; run one track-B container at a time).
Image: **Ubuntu 24.04**. Add your SSH key at creation — Oracle doesn't enable password
auth by default. Console → Compute → Instances → Create Instance.

**AWS Free Tier, region `us-east-1` (N. Virginia) — alternative.** 12 months free,
`t2.micro`/`t3.micro`, 750 hrs/month. `us-east-1` is the same metro as Ashburn. Requires
a card; stays free under the monthly hour limit.

```bash
aws ec2 run-instances --image-id ami-0c101f26f147fa7fd --instance-type t2.micro --region us-east-1 --key-name <your-key>
```

**GCP Always Free, region `us-east1` (South Carolina) — fallback.** Forever-free
`e2-micro`, no card charge, but `us-east1` is ~500 km from Ashburn rather than in it —
still worth measuring, just expect a few ms more than the other two.

```bash
gcloud compute instances create graphbench-client --zone=us-east1-b --machine-type=e2-micro --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud --boot-disk-size=20GB
```

Whichever you pick, **the region choice is a hypothesis, not a guarantee** — the
bootstrap script prints baseline RTT precisely so you can check it before running
anything real. If the free tier's smallest shape (1 GB RAM) can't hold a parity
container and the load generator at once, run track B one container at a time, or skip
track B on the free VM and note that constraint in the README rather than force it.

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
