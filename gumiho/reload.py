"""Hot-reload of the behavior tree.

Watches ROOT/behavior (workflows/*.toml, reflexes/*.toml, hooks/*.py,
knowledge.toml) by polling mtimes. On a change it re-parses / re-imports that
file into the registry, guarded only by "does it parse?" — on a syntax/parse
error the last-good version stays and the error is surfaced.

After any successful reload, every active engine re-enters its current workflow
from the top (the agreed reconciliation rule). Live game state in A is untouched.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import tomllib
from pathlib import Path
from typing import Callable

from . import registry
from .config import ROOT
from .workflow import load_reflex_bundle, load_workflow

BEHAVIOR = ROOT / "behavior"

# Knowledge is mutated in place so every CharCtx that holds a reference sees edits.
_knowledge: dict = {}


def get_knowledge() -> dict:
    return _knowledge


def _load_knowledge() -> None:
    from . import routes, hunting_maps
    path = BEHAVIOR / "knowledge.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    _knowledge.clear()
    _knowledge.update(data)
    # Merge machine-recorded routes over the hand-edited [travel_routes].
    _knowledge.setdefault("travel_routes", {}).update(routes.load())
    # Hunting maps: expose the definitions (zone dropdown + FixedMap) and register each map's
    # route by its ROUTE NAME (so it shows in the routes list and load_hunt_route finds it).
    # Merged last, so a map's route is authoritative over a stale recorded one.
    hmaps = hunting_maps.load()
    _knowledge["hunting_maps"] = hmaps
    for zone, d in hmaps.items():
        route = d.get("route") or {}
        steps = route.get("steps")
        if steps:
            _knowledge["travel_routes"][str(route.get("name") or zone)] = list(steps)


def save_route(name: str, commands: list[str]) -> None:
    """Persist a recorded route and update the live knowledge immediately."""
    from . import routes
    routes.save_route(name, commands)
    _knowledge.setdefault("travel_routes", {})[name] = list(commands)


class Reloader:
    def __init__(
        self,
        engines_provider: Callable[[], list],
        on_status: Callable[[str, str], None] | None = None,
        interval: float = 1.0,
    ):
        self._engines = engines_provider
        self._on_status = on_status
        self._interval = interval
        self._mtimes: dict[Path, float] = {}

    # --- initial load -------------------------------------------------------
    def load_all(self) -> None:
        _load_knowledge()
        for path in sorted((BEHAVIOR / "workflows").glob("*.toml")):
            self._load(path, silent=True)
        for path in sorted((BEHAVIOR / "reflexes").glob("*.toml")):
            self._load(path, silent=True)
        for path in sorted((BEHAVIOR / "hooks").glob("*.py")):
            if path.stem != "__init__":
                self._load(path, silent=True)
        self._snapshot_mtimes()

    def _snapshot_mtimes(self) -> None:
        for path in self._all_files():
            try:
                self._mtimes[path] = path.stat().st_mtime
            except OSError:
                pass

    def _all_files(self):
        yield from (BEHAVIOR / "workflows").glob("*.toml")
        yield from (BEHAVIOR / "reflexes").glob("*.toml")
        yield from (p for p in (BEHAVIOR / "hooks").glob("*.py") if p.stem != "__init__")
        kn = BEHAVIOR / "knowledge.toml"
        if kn.exists():
            yield kn
        rt = BEHAVIOR / "routes.json"
        if rt.exists():
            yield rt
        from . import hunting_maps
        yield from hunting_maps.files()

    # --- watch loop ---------------------------------------------------------
    async def watch(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            changed = False
            for path in self._all_files():
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if self._mtimes.get(path) != mtime:
                    self._mtimes[path] = mtime
                    if self._load(path):
                        changed = True
            if changed:
                self._reenter_engines()

    # --- one file -----------------------------------------------------------
    def _load(self, path: Path, silent: bool = False) -> bool:
        """Return True on a successful reload, False on parse error."""
        try:
            if path.name in ("knowledge.toml", "routes.json") or path.parent.name == "hunting_maps":
                _load_knowledge()
            elif path.parent.name == "workflows":
                registry.put_workflow(load_workflow(path))
            elif path.parent.name == "reflexes":
                registry.put_bundle(load_reflex_bundle(path))
            elif path.parent.name == "hooks":
                mod = f"behavior.hooks.{path.stem}"
                registry.forget_module(mod)
                # Drop any cached bytecode so a rapid edit (same second, same
                # length) can't make reload reuse stale .pyc.
                try:
                    Path(importlib.util.cache_from_source(str(path))).unlink()
                except OSError:
                    pass
                importlib.invalidate_caches()
                if mod in sys.modules:
                    importlib.reload(sys.modules[mod])
                else:
                    importlib.import_module(mod)
        except Exception as e:                     # the sole guard: does it parse/import?
            self._status(path.name, f"reload FAILED (kept last good): {e}")
            return False
        if not silent:
            self._status(path.name, "reloaded")
        return True

    def _reenter_engines(self) -> None:
        for eng in self._engines():
            try:
                eng.reenter_current()
            except Exception as e:
                self._status("engine", f"re-enter failed: {e}")

    def _status(self, name: str, msg: str) -> None:
        if self._on_status is not None:
            self._on_status(name, msg)
