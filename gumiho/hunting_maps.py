"""Loader for hand-authored HUNTING MAPS (behavior/hunting_maps/*.toml).

Each file defines one map — name, route {name, steps}, start, one or more matrices, portals,
and (reserved) doors. See behavior/hunting_maps/_example.toml for the full format.

Files whose name starts with '_' or '.' are IGNORED (templates/examples), so the engine skips
_example.toml. A file that fails to parse or lacks a name is skipped (never crashes the load).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .config import ROOT

MAPS_DIR = ROOT / "behavior" / "hunting_maps"


def _ignored(path: Path) -> bool:
    return path.name.startswith("_") or path.name.startswith(".")


def load() -> dict:
    """Return {zone_name: definition}. Definitions are the raw parsed TOML (name, route, start,
    matrix, portal, door)."""
    out: dict = {}
    if not MAPS_DIR.exists():
        return out
    for path in sorted(MAPS_DIR.glob("*.toml")):
        if _ignored(path):
            continue
        try:
            d = tomllib.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue                            # a broken map file never breaks the others
        name = d.get("name")
        if name:
            out[str(name)] = d
    return out


def files():
    """Paths the reloader should watch (so an edited/added map hot-reloads)."""
    if not MAPS_DIR.exists():
        return []
    return [p for p in MAPS_DIR.glob("*.toml") if not _ignored(p)]
