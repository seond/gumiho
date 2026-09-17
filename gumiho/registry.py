"""The name registry: the single place the engine resolves behavior.

Hooks register guards/actions/steps by name here via decorators. Workflow and
reflex definitions are stored here after parsing. The engine only ever looks
things up through this module, so hot-reload = re-populate these maps; nothing
else holds references to behavior.

Late binding is the whole point: a hook module does
    from gumiho.registry import guard, action, step
and reloading that module re-runs its decorators, overwriting the entries here.
Because this module is never itself reloaded, the maps persist across reloads.
"""

from __future__ import annotations

from typing import Callable

from .workflow import ReflexBundle, Workflow

# name -> callable. guards: fn(s) or fn(s, arg) -> bool. actions/steps: fn(s, cmd).
# Steps and actions share one namespace (a step is just an action used as the
# calm move); @step is an authoring alias for @action.
_guards: dict[str, Callable] = {}
_actions: dict[str, Callable] = {}
# name -> defining module __name__, so a module reload can drop its stale names.
_source: dict[tuple[str, str], str] = {}   # (kind, name) -> module

_workflows: dict[str, Workflow] = {}       # kind -> Workflow
_bundles: dict[str, ReflexBundle] = {}     # name -> ReflexBundle


def _register(kind: str, table: dict, name: str, fn: Callable) -> Callable:
    table[name] = fn
    _source[(kind, name)] = getattr(fn, "__module__", "?")
    return fn


def guard(name: str) -> Callable[[Callable], Callable]:
    return lambda fn: _register("guard", _guards, name, fn)


def action(name: str) -> Callable[[Callable], Callable]:
    return lambda fn: _register("action", _actions, name, fn)


def step(name: str) -> Callable[[Callable], Callable]:
    # A step is an action used as a state's calm move; same namespace.
    return lambda fn: _register("action", _actions, name, fn)


def forget_module(module_name: str) -> None:
    """Drop every hook a module registered — call before reloading it so renamed
    or deleted names don't linger."""
    for (kind, name), mod in list(_source.items()):
        if mod != module_name:
            continue
        {"guard": _guards, "action": _actions}[kind].pop(name, None)
        del _source[(kind, name)]


# --- lookups (raise KeyError with a clear message if a name is missing) -------

def get_guard(name: str) -> Callable:
    try:
        return _guards[name]
    except KeyError:
        raise KeyError(f"unknown guard {name!r}") from None


def get_action(name: str) -> Callable:
    try:
        return _actions[name]
    except KeyError:
        raise KeyError(f"unknown action {name!r}") from None


# A step is stored as an action.
get_step = get_action


def get_step(name: str) -> Callable:
    try:
        return _steps[name]
    except KeyError:
        raise KeyError(f"unknown step {name!r}") from None


# --- definition storage (replaced wholesale on reload) ------------------------

def put_workflow(wf: Workflow) -> None:
    _workflows[wf.kind] = wf


def get_workflow(kind: str) -> Workflow:
    try:
        return _workflows[kind]
    except KeyError:
        raise KeyError(f"unknown workflow {kind!r}") from None


def has_workflow(kind: str) -> bool:
    return kind in _workflows


def put_bundle(b: ReflexBundle) -> None:
    _bundles[b.name] = b


def get_bundle(name: str) -> ReflexBundle:
    try:
        return _bundles[name]
    except KeyError:
        raise KeyError(f"unknown reflex bundle {name!r}") from None


def all_workflow_kinds() -> list[str]:
    return sorted(_workflows)


# --- introspection (for the UI Behavior Lab) ----------------------------------

def list_guards() -> list[str]:
    return sorted(_guards)


def list_actions() -> list[str]:
    return sorted(_actions)


def list_bundles() -> list[str]:
    return sorted(_bundles)


def workflows_detail() -> dict[str, list[str]]:
    """kind -> [state names], for the state-jump selector."""
    return {k: list(wf.states) for k, wf in sorted(_workflows.items())}
