#!/usr/bin/env python3
"""One-command entry point. Works on Windows, macOS and Linux without make.

    python gbf.py selftest      # verify the harness - no network, no credentials
    python gbf.py prepare       # fetch the dataset and curate query parameters
    python gbf.py probe         # prove every platform is reachable, build the capability matrix
    python gbf.py plan          # print the run matrix and a time estimate
    python gbf.py run           # the full suite against every enabled platform
    python gbf.py charts        # render charts from the newest run
    python gbf.py all           # prepare -> probe -> run -> charts

Anything after the command is forwarded, so `python gbf.py run --only cognodb` works.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Prefer the project virtualenv if it exists, so a user who forgot to activate it still
# gets the pinned dependencies rather than a confusing ImportError.
VENV = ROOT / ".venv" / ("Scripts" if sys.platform == "win32" else "bin") / (
    "python.exe" if sys.platform == "win32" else "python"
)
PY = str(VENV) if VENV.exists() else sys.executable

STEPS: dict[str, list[list[str]]] = {
    "selftest": [["scripts/smoke_test.py"]],
    "prepare": [["scripts/fetch_dataset.py"], ["scripts/curate_params.py", "--scale", "S"]],
    "probe": [["scripts/probe_platforms.py"]],
    "plan": [["scripts/run_benchmark.py", "--dry-run"]],
    "run": [["scripts/run_benchmark.py"]],
    "charts": [["scripts/make_charts.py"]],
    "verify": [["scripts/fetch_dataset.py", "--verify"]],
}
STEPS["all"] = STEPS["prepare"] + STEPS["probe"] + STEPS["run"] + STEPS["charts"]

# Only the final script of a composite command receives forwarded arguments; passing
# --only to fetch_dataset.py would just be an error.
FORWARD_TO_LAST = {"run", "probe", "charts", "plan", "all"}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    cmd, extra = argv[1], argv[2:]
    if cmd not in STEPS:
        print(f"unknown command {cmd!r}\n", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 2

    steps = [list(s) for s in STEPS[cmd]]
    if extra and cmd in FORWARD_TO_LAST:
        steps[-1].extend(extra)

    for step in steps:
        print(f"\n$ {Path(PY).name} {' '.join(step)}", flush=True)
        rc = subprocess.call([PY, *step], cwd=ROOT)
        if rc != 0:
            print(f"\n{step[0]} exited {rc} - stopping.", file=sys.stderr)
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
