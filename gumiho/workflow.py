"""Workflow / reflex definitions and their TOML loaders.

These are pure data. The engine (gumiho.engine) interprets them; the registry
(gumiho.registry) holds the parsed instances and hot-swaps them on reload.

A guard spec is a name, optionally with one argument: "hp_below:40". The engine
resolves the name in the registry and passes the arg (a string) if present.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Transition:
    when: str                    # guard spec, e.g. "room_clear" or "hp_below:40"
    goto: str | None = None      # a state name in THIS workflow
    switch: str | None = None    # another workflow kind (hand-off)
    sync: bool = False           # route the switch through the director (duo)


@dataclass(frozen=True)
class WFState:
    name: str
    arm: tuple[str, ...] = ()            # reflex bundle names armed here
    on_enter: tuple[str, ...] = ()       # action names, run once on entry
    step: str | None = None              # the calm step (action name)
    transitions: tuple[Transition, ...] = ()


@dataclass(frozen=True)
class Workflow:
    kind: str
    initial: str
    states: dict[str, WFState]

    def state(self, name: str) -> WFState | None:
        return self.states.get(name)


@dataclass(frozen=True)
class Reflex:
    when: str                    # guard spec
    do: str                      # action name
    priority: int = 0


@dataclass(frozen=True)
class ReflexBundle:
    name: str
    reflexes: tuple[Reflex, ...]  # kept sorted by priority, descending


# --- loaders -----------------------------------------------------------------

def load_workflow(path: Path) -> Workflow:
    """Parse a workflow TOML. Raises on malformed input (the reload syntax guard)."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    kind = data["kind"]
    initial = data["initial"]
    states: dict[str, WFState] = {}
    for name, sd in (data.get("state") or {}).items():
        transitions = tuple(
            Transition(
                when=t["when"],
                goto=t.get("goto"),
                switch=t.get("switch"),
                sync=bool(t.get("sync", False)),
            )
            for t in (sd.get("transition") or [])
        )
        states[name] = WFState(
            name=name,
            arm=tuple(sd.get("arm", [])),
            on_enter=tuple(sd.get("on_enter", [])),
            step=sd.get("step"),
            transitions=transitions,
        )
    if initial not in states:
        raise ValueError(f"{path.name}: initial state {initial!r} not defined")
    return Workflow(kind=kind, initial=initial, states=states)


def load_reflex_bundle(path: Path) -> ReflexBundle:
    """Parse a reflex-bundle TOML. Raises on malformed input."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    name = data["name"]
    reflexes = [
        Reflex(when=r["when"], do=r["do"], priority=int(r.get("priority", 0)))
        for r in (data.get("reflex") or [])
    ]
    reflexes.sort(key=lambda r: r.priority, reverse=True)
    return ReflexBundle(name=name, reflexes=tuple(reflexes))
