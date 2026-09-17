"""Director — the thin duo coordinator (only shared decisions).

Per-character reactions live in each Engine's reflexes and workflow. The director
owns only what genuinely spans both characters: sync'd workflow switches (both
enter rest/restock together) and, in later milestones, regroup-after-death.

Its own state is data; its logic is small and hot-reloadable-adjacent (it reads
the same registry the engines do).
"""

from __future__ import annotations


class Director:
    def __init__(self):
        self.engines: dict[str, "object"] = {}   # sid -> Engine

    def register(self, sid: str, engine) -> None:
        self.engines[sid] = engine

    # NOTE: travel is FOLLOW-BASED, not lockstep — the leader walks the route and the
    # server drags the supporter along via 따라. The old per-sid route-progress
    # counters (route_reset/ready/advanced, both_routes_done, partner_lockstep) are
    # gone; the director now only coordinates sync'd workflow switches.

    def request_switch(self, sid: str, kind: str) -> None:
        """A sync'd transition: switch BOTH characters into `kind` together.

        M1 keeps this immediate — the character that hit the condition and its
        partner both re-enter `kind` from the top. (Barrier/handshake refinement
        can come later; for rest/restock, switching together promptly is right.)
        """
        for eng in self.engines.values():
            if eng.enabled:
                eng.enter_workflow(kind)
