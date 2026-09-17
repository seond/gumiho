"""Mob danger record — what we've fought and what to never touch.

FRAMEWORK: a mob is only worth fighting if it's a FAIR fight. Before engaging an
UNFAMILIAR mob the hunt runs `<mob> 고려` (consider); if the assessment shows a lethal
phrase ("꿈도 꾸지 마세요!" / "많은 운과 좋은 장비가 필요합니다") the mob is recorded as
DANGER and never attacked. Everything else is recorded SAFE. The record persists to
behavior/mob_danger.json so a mob is 고려'd once, not every session — this is exactly
the "record of the mobs we have fought" the framework calls for.

Shared across both characters (danger is global). Seeds (known safe/danger) come from
knowledge [mob_danger]; learned verdicts are written here.
"""
from __future__ import annotations

import json
import os

_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "behavior", "mob_danger.json")
_lore: dict | None = None


def _load() -> dict:
    global _lore
    if _lore is None:
        try:
            data = json.load(open(_PATH, encoding="utf-8"))
        except Exception:
            data = {}
        _lore = {"safe": list(data.get("safe", [])),
                 "danger": list(data.get("danger", []))}
    return _lore


def classify(kw: str, seed_safe=(), seed_danger=()) -> str | None:
    """'danger' | 'safe' | None(unknown). Seeds (from knowledge) win when set — a
    human/known verdict is authoritative and needs no 고려."""
    if not kw:
        return None
    l = _load()
    if kw in seed_danger or kw in l["danger"]:
        return "danger"
    if kw in seed_safe or kw in l["safe"]:
        return "safe"
    return None


def learn(kw: str, verdict: str) -> None:
    """Record a 고려 verdict ('danger'|'safe') and persist it."""
    if not kw or verdict not in ("danger", "safe"):
        return
    l = _load()
    other = "safe" if verdict == "danger" else "danger"
    if kw in l[other]:
        l[other].remove(kw)
    if kw not in l[verdict]:
        l[verdict].append(kw)
    _save()


def snapshot() -> dict:
    l = _load()
    return {"safe": list(l["safe"]), "danger": list(l["danger"])}


def _save() -> None:
    try:
        json.dump(_lore, open(_PATH, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    except Exception:
        pass
