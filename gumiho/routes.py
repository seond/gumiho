"""Recorded travel routes (machine-written), kept separate from the hand-edited
knowledge.toml. Merged into knowledge['travel_routes'] by the reloader; edit or
delete entries here freely."""

import json

from .config import ROOT

ROUTES_PATH = ROOT / "behavior" / "routes.json"


def load() -> dict:
    if not ROUTES_PATH.exists():
        return {}
    try:
        data = json.loads(ROUTES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def save_route(name: str, commands: list[str]) -> dict:
    data = load()
    data[name] = list(commands)
    ROUTES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def delete_route(name: str) -> dict:
    data = load()
    data.pop(name, None)
    ROUTES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
