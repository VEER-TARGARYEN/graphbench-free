"""Configuration loading. Secrets come from the environment, never from a file in git."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
PARAMS = ROOT / "params"
DATA = ROOT / "data"
RESULTS = ROOT / "results"
CHARTS = ROOT / "charts"


def load_env(path: Path | None = None) -> None:
    """Read .env into os.environ without overwriting anything already set.

    Deliberately hand-rolled and dependency-light so that `probe` works before a full
    install, and so there is no doubt about what it reads.
    """
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_platforms() -> tuple[dict, list[dict]]:
    doc = yaml.safe_load((CONFIG / "platforms.yaml").read_text(encoding="utf-8"))
    defaults = doc.get("defaults", {})
    platforms = doc.get("platforms", [])
    for p in platforms:
        p.setdefault("bolt_defaults", defaults.get("bolt", {}))
    return defaults, platforms


def load_workloads() -> dict:
    return yaml.safe_load((CONFIG / "workloads.yaml").read_text(encoding="utf-8"))


def build_adapter(platform: dict):
    from .adapters.bolt import BoltAdapter

    kind = platform.get("adapter", "bolt")
    if kind == "bolt":
        return BoltAdapter(platform["id"], platform["label"], platform)
    if kind == "mock":
        from .adapters.mock import MockAdapter

        return MockAdapter(platform["id"], platform["label"], platform)
    raise ValueError(f"unknown adapter {kind!r} for platform {platform['id']!r}")


def selected_platforms(only: list[str] | None = None, include_disabled: bool = False) -> list[dict]:
    _, platforms = load_platforms()
    out = []
    for p in platforms:
        if only and p["id"] not in only:
            continue
        if not include_disabled and not only and not p.get("enabled", False):
            continue
        out.append(p)
    return out
