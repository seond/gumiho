"""Cmd — the paced command interface handed to actions/steps.

It delegates to a push function (the existing PacedSender.push), so the
human-pace / anti-flood floor lives in the substrate and no reloaded behavior
can bypass it. `say` speaks in the room via the object-first "<msg> 말" form
(this is how the leader requests buffs: say("방비!") -> "방비! 말").
"""

from __future__ import annotations

from typing import Callable


class Cmd:
    def __init__(self, push: Callable[[str], None]):
        self._push = push
        self.n = 0        # commands pushed — lets the engine detect "did this act?"

    def send(self, line: str) -> None:
        """Queue a raw command (paced by the substrate)."""
        self.n += 1
        self._push(line)

    def say(self, message: str) -> None:
        """Speak `message` in the current room."""
        self.n += 1
        self._push(f"{message} 말")
