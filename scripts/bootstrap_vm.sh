#!/usr/bin/env bash
# Provision a fresh Debian/Ubuntu VM as the benchmark client.
#
# WHY A VM AT ALL. On a managed tier reached over a WAN, the round trip is mostly
# geography. Measured from Varanasi, India, the CognoDB free instance in us-east
# answered in 266 ms - of which roughly 2 ms was the database. Engine differences of
# a few milliseconds are invisible under a 266 ms constant, so the comparison would
# have measured the Atlantic.
#
# Putting the client in the SAME region as the database instances drops the round trip
# to single-digit milliseconds, which is the difference between a benchmark that
# compares engines and one that compares undersea cables. It also satisfies the brief's
# "same client machine and region for every platform" requirement properly, and it
# provides the container runtime that track B needs.
#
# Usage on a fresh VM:
#   curl -fsSL https://raw.githubusercontent.com/VEER-TARGARYEN/graphbench-free/main/scripts/bootstrap_vm.sh | bash
# or, after cloning:
#   bash scripts/bootstrap_vm.sh
#
# Idempotent: safe to re-run.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/VEER-TARGARYEN/graphbench-free.git}"
REPO_DIR="${REPO_DIR:-$HOME/graphbench-free}"
INSTALL_DOCKER="${INSTALL_DOCKER:-yes}"   # set to "no" for a track-A-only client

log() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }

# ── system packages ───────────────────────────────────────────────────────────
log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq \
  python3 python3-venv python3-pip git curl ca-certificates \
  chrony >/dev/null

# Accurate wall clock matters: the scheduler compares intended send times against
# actual ones, and a drifting clock would be recorded as schedule lag.
sudo systemctl enable --now chrony >/dev/null 2>&1 || true

# ── repository ────────────────────────────────────────────────────────────────
if [ -d "$REPO_DIR/.git" ]; then
  log "Repository already present, pulling"
  git -C "$REPO_DIR" pull --ff-only
else
  log "Cloning $REPO_URL"
  git clone --depth 1 "$REPO_URL" "$REPO_DIR"
fi
cd "$REPO_DIR"

# ── python environment ────────────────────────────────────────────────────────
log "Creating virtualenv and installing pinned dependencies"
python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
./.venv/bin/python -c "import neo4j, hdrh.histogram, yaml, matplotlib, numpy; print('dependencies OK')"

# ── docker, for track B ───────────────────────────────────────────────────────
if [ "$INSTALL_DOCKER" = "yes" ]; then
  if command -v docker >/dev/null 2>&1; then
    log "Docker already installed"
  else
    log "Installing Docker (needed only for the track-B parity containers)"
    curl -fsSL https://get.docker.com | sudo sh >/dev/null
    sudo usermod -aG docker "$USER"
    echo "    NOTE: log out and back in (or run 'newgrp docker') before using docker."
  fi
fi

# ── raise the open-file limit ─────────────────────────────────────────────────
# 40 concurrent Bolt clients across five platforms plus pooled connections can exceed
# the default soft limit of 1024 on some images.
if ! grep -q "graphbench-free" /etc/security/limits.conf 2>/dev/null; then
  log "Raising the open-file limit for concurrent Bolt connections"
  printf '* soft nofile 65535\n* hard nofile 65535\n# graphbench-free\n' \
    | sudo tee -a /etc/security/limits.conf >/dev/null
fi

# ── self-test ─────────────────────────────────────────────────────────────────
log "Verifying the harness (no network, no credentials)"
./.venv/bin/python scripts/smoke_test.py

# ── environment report ────────────────────────────────────────────────────────
log "Client environment"
./.venv/bin/python -c "
from harness import environment as e
print(e.summarise(e.capture()))
"

cat <<'NEXT'

────────────────────────────────────────────────────────────────────────────
Next, on this VM:

  cd ~/graphbench-free
  cp .env.example .env
  nano .env                       # paste your connection details

  ./.venv/bin/python scripts/probe_platforms.py --all

CHECK THE BASELINE RTT IN THAT OUTPUT BEFORE GOING FURTHER.
If it is not in single-digit milliseconds, this VM is not in the same region as
your database instances and the whole reason for creating it has been lost.
Destroy it and recreate in the right region rather than running anyway.

Then:
  ./.venv/bin/python gbf.py prepare
  ./.venv/bin/python gbf.py plan
  ./.venv/bin/python gbf.py run
  ./.venv/bin/python gbf.py charts
────────────────────────────────────────────────────────────────────────────
NEXT
