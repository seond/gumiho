"""Fixed, pre-known hunting map — deterministic navigation over a hand-authored layout for a
zone where dead-reckoning fails (identical room titles + descriptions, no usable 지도, and
conditional exits). Instead of GUESSING which room we're in from what we see, we KNOW the
layout and track position purely by COMMANDED MOVES from a known start room.

A hunting-map DEFINITION (see behavior/hunting_maps/*.toml, loaded by gumiho/hunting_maps.py):

    name        the hunting-zone name (also the hunt target)
    route       { name, steps } — the path FROM 중앙 광장 to the start room
    start       the room the route lands at; hunting begins here
    matrix      one or MORE planar grids of WHITESPACE-SEPARATED cells (any width — the column
                is the token's position, so room numbers may be 2, 3 or more digits): a room
                NUMBER, an all-dots token (. / .. / ...) for EMPTY, or an all-X token (XX / XXX)
                for a conditional-exit centre (excluded from edges). Room numbers are GLOBALLY
                UNIQUE across matrices. Pad cells to a common width for readability if you like.
    portal      explicit BOTH-WAYS links for non-adjacent connections (wormholes, stairs
                between matrices): {from, from_dir, to, to_dir}
    door        RESERVED (engine ignores for now): {room, dir, locked, key}

Edges = 4-neighbour grid adjacencies between numbered cells (never to/through an XX centre)
plus the portals. Every edge is therefore a reliable, unconditional passage, which keeps
`pos` in sync with reality without ever identifying a room by its (useless) look.

Interface mirrors ZoneSurvey's hunt surface (cur / observe / note_move / next_hunt_dir /
mark_swept / reset / snapshot …) so it drops straight into the hunt loop and the UI.
"""

from collections import deque

_DELTA = {"북": (-1, 0), "남": (1, 0), "동": (0, 1), "서": (0, -1)}
_DIR_ORDER = ("북", "동", "남", "서")
# The centre/start room is (by convention) the only one with a 아래 exit — a reliable
# landmark to re-sync position after any drift. Made configurable per map via `resync_exit`.
_DEFAULT_RESYNC_EXIT = "아래"


class FixedMap:
    def __init__(self, definition: dict):
        matrices = definition.get("matrix") or definition.get("matrices") or []
        self.coord: dict[int, tuple[int, int, int]] = {}   # rid -> (matrix_index, r, c)
        self.mnames: list[str] = []
        xx: set[tuple[int, int, int]] = set()
        by_coord: dict[tuple[int, int, int], int] = {}
        for mi, mat in enumerate(matrices):
            self.mnames.append(str(mat.get("name", f"m{mi}")))
            for r, row in enumerate(mat.get("grid", [])):
                # Cells are WHITESPACE-SEPARATED tokens of ANY width — the column is the token's
                # POSITION, not its character count — so room numbers may be 2, 3, or more digits.
                # A token of only dots (. / .. / ...) is EMPTY; a token of only X's (XX / XXX,
                # any case) is a conditional-exit centre; anything else is an integer room id.
                for c, tok in enumerate(row.split()):
                    if set(tok) <= {"."}:                 # any run of dots -> empty
                        continue
                    if set(tok.upper()) == {"X"}:         # any run of X's -> conditional centre
                        xx.add((mi, r, c))
                    else:
                        rid = int(tok)
                        self.coord[rid] = (mi, r, c)
                        by_coord[(mi, r, c)] = rid
        # Grid-adjacency edges within each matrix; never to/through an XX centre.
        self.edges: dict[int, dict[str, int]] = {rid: {} for rid in self.coord}
        for rid, (mi, r, c) in self.coord.items():
            for d, (dr, dc) in _DELTA.items():
                nc = (mi, r + dr, c + dc)
                nb = by_coord.get(nc)
                if nb is not None and nc not in xx:
                    self.edges[rid][d] = nb
        # Explicit portals (both ways) — override any grid edge on those directions.
        for p in (definition.get("portal") or definition.get("portals") or []):
            a, ad, b, bd = int(p["from"]), p["from_dir"], int(p["to"]), p["to_dir"]
            if a in self.edges and b in self.edges:
                self.edges[a][ad] = b
                self.edges[b][bd] = a
        self._portals = [(int(p["from"]), p["from_dir"], int(p["to"]), p["to_dir"])
                         for p in (definition.get("portal") or definition.get("portals") or [])]
        # DOORS on an edge: the passage exists on the map but the game gates it — you must OPEN
        # the door ("<dir> 문 열") before stepping through (it auto-closes, so re-open each time).
        # A door on a PORTAL edge blocks BOTH ends, so mirror it onto the portal's other side.
        _portal_rev = {}
        for (a, ad, b, bd) in self._portals:
            _portal_rev[(a, ad)] = (b, bd)
            _portal_rev[(b, bd)] = (a, ad)
        self.doors: dict[tuple[int, str], dict] = {}
        for dr in (definition.get("door") or definition.get("doors") or []):
            key = (int(dr["room"]), str(dr["dir"]))
            # `name` is the door's in-game NAME — the open command is "<dir> <name> 열"
            # (e.g. "위 하늘 열"). Without a name it falls back to the generic "문 열".
            info = {"locked": bool(dr.get("locked", False)), "key": dr.get("key"),
                    "name": dr.get("name")}
            self.doors[key] = info
            if key in _portal_rev:                     # portal door -> gate both traversals
                self.doors.setdefault(_portal_rev[key], dict(info))
        self.arrival = int(definition["start"])
        self.resync_exit = str(definition.get("resync_exit", _DEFAULT_RESYNC_EXIT))
        self.pos = self.arrival
        self.swept: set[int] = set()
        self.pending: str | None = None
        self._prev_pos: int | None = None     # rollback anchor for the optimistic advance
        self.roam_block: set = set()          # for hunt-hook compatibility (unused here)
        self._blocked: set = set()            # (rid, dir) refused = a WALL -> routed around
        self.version = 0
        self.dbg = {"moves": 0, "off_edge": 0, "resync": 0, "refused": 0}

    # ---- hunt-hook / webui compatibility (ZoneSurvey surface) ----------------
    @property
    def cur(self):
        return self.pos

    def _touch(self):
        self.version += 1

    def note_move(self, direction: str) -> None:
        """A move was COMMANDED — advance `pos` along the map edge IMMEDIATELY (optimistic
        dead-reckoning). Position is driven by the COMMAND, never by the room render: renders
        don't correlate 1:1 with moves (combat emits unsolicited re-renders that a render-driven
        scheme mistakes for moves — the source of the 007->017 drift). If the server later
        REFUSES this step, note_refused rolls it back. The hunt gates the next move on `pending`
        so a refusal can roll back before the next optimistic step is taken."""
        self._prev_pos = self.pos
        self.pending = direction
        nb = self.edges.get(self.pos, {}).get(direction)
        if nb is not None:
            self.pos = nb
            self.dbg["moves"] += 1
            self._touch()
        else:
            self.dbg["off_edge"] += 1         # next_hunt_dir only returns edges -> unexpected

    def note_refused(self, direction: str | None = None, hard: bool = True) -> None:
        # The server refused the commanded step ("갈 수 없습니다"): the optimistic advance was
        # wrong -> ROLL BACK to where we stepped from, and BLOCK that (room, dir) as a WALL so
        # next_hunt_dir routes around it (blocks persist for the sweep).
        d = direction if direction is not None else self.pending
        if self._prev_pos is not None:
            if self.pos != self._prev_pos:
                self.dbg["moves"] -= 1        # undo the optimistic move count
            self.pos = self._prev_pos
            self._prev_pos = None
            self._touch()
        if d is not None:
            self._blocked.add((self.pos, d))
            self.dbg["refused"] += 1
        self.pending = None

    def note_teleport(self) -> None:
        self.pending = None                   # relocated off-map; re-seeded on hunt arrival
        self._prev_pos = None

    def note_flee(self, direction: str) -> None:
        """A 도망 threw us out of the room a way WE never learned — but a watcher in the same
        room (the supporter) read the departure direction and reports it here. Unlike note_move
        this is a COMPLETED, already-rendered relocation, not an outstanding optimistic step:
        advance pos along the edge but do NOT arm `pending` (there is no move-render still to
        wait on — arming it would stall the hunt into a spurious 봐). If the fled direction is
        not a known edge, leave pos put and let the landmark re-sync recover."""
        nb = self.edges.get(self.pos, {}).get(direction)
        if nb is not None:
            self.pos = nb
            self.pending = None
            self._prev_pos = None
            self.dbg["moves"] += 1
            self._touch()
        else:
            self.dbg["off_edge"] += 1

    def observe(self, exits, moved: bool, zone=None) -> int:
        """A room render arrived. Position is NOT derived from renders any more (see note_move):
        the render only (a) CONFIRMS the outstanding optimistic move stuck — clears `pending`
        so the hunt may take the next step — and (b) drives the landmark RE-SYNC: seeing the
        arrival room's unique exit pins pos there regardless of any drift. `moved` is ignored
        for position. Crucially, an unsolicited combat re-render can no longer masquerade as a
        move (it never advanced pos to begin with)."""
        exset = {e for e in (exits or []) if e}
        if self.resync_exit and self.resync_exit in exset:
            if self.pos != self.arrival:
                self.dbg["resync"] += 1
            self.pos = self.arrival
            self.pending = None
            self._prev_pos = None
            self._blocked.clear()
            self._touch()
            return self.pos
        # No refusal came with this render -> the outstanding optimistic move stuck. Confirm it
        # (drop the rollback anchor and release the move-gate). A refused move never renders — it
        # answers with "갈 수 없습니다" (note_refused), which rolls back BEFORE any render arrives.
        self.pending = None
        self._prev_pos = None
        return self.pos

    def set_anchor(self) -> None:
        pass                                   # a complete bounded map needs no radius anchor

    def gate_boundary(self, back_dir: str) -> None:
        pass                                   # a complete fixed map has no zone boundary

    def gate(self, direction: str) -> None:
        pass

    def mark_swept(self) -> None:
        if self.pos not in self.swept:
            self.swept.add(self.pos)
            self._touch()

    def reset_swept(self) -> None:
        self.swept = set()
        self._touch()

    def reset(self) -> None:
        """Re-seed at the start room (hunt entry / respawn re-sweep)."""
        self.pos = self.arrival
        self.swept = set()
        self.pending = None
        self._blocked.clear()
        self._touch()

    def next_hunt_dir(self, confine_zone=None, radius=None):
        """First step of the shortest walk from `pos` to the nearest UNSWEPT room (via edges +
        portals). None when every room is swept (barren -> wait for respawns, then reset_swept)."""
        seen = {self.pos}
        q = deque([(self.pos, None)])
        while q:
            rid, first = q.popleft()
            for d in _DIR_ORDER + tuple(k for k in self.edges[rid] if k not in _DIR_ORDER):
                if (rid, d) in self._blocked:
                    continue
                nb = self.edges[rid].get(d)
                if nb is None or nb in seen:
                    continue
                seen.add(nb)
                step = first or d
                if nb not in self.swept:
                    return step
                q.append((nb, step))
        # Everything REACHABLE (via unblocked edges) is swept. If unswept rooms remain, they're
        # SEALED behind a block — a real wall, or a door we couldn't open (wrong/unknown command)
        # — so they're unreachable right now. Report BARREN (the hunt rests/rotates) rather than
        # WANDERING an unblocked edge, which just tight-oscillates between two swept rooms (the
        # 9<->16 loop). A reset (hunt re-arrival) clears the blocks and retries the door.
        if self._blocked and any(r not in self.swept for r in self.coord):
            self.dbg["sealed"] = self.dbg.get("sealed", 0) + 1
        return None

    def next_dir(self, confine_zone=None):
        return self.next_hunt_dir(confine_zone)

    def surveyed(self, confine_zone=None) -> bool:
        return self.next_hunt_dir(confine_zone) is None

    @property
    def rooms(self) -> dict:
        return self.coord                      # truthy + len() for "has a map" checks

    def snapshot(self) -> dict:
        """One frame per matrix (each planar), plus portals — for the UI overlay."""
        frames = []
        for mi, mname in enumerate(self.mnames):
            cin = [(rid, r, c) for rid, (m, r, c) in self.coord.items() if m == mi]
            if not cin:
                continue
            minr = min(r for _, r, _ in cin)
            minc = min(c for _, _, c in cin)
            cells = [{"rid": rid, "r": r - minr, "c": c - minc, "zone": mname,
                      "swept": rid in self.swept, "cur": rid == self.pos}
                     for rid, r, c in cin]
            edges = []
            for rid, r, c in cin:
                for d, nb in self.edges[rid].items():
                    if self.coord[nb][0] == mi and rid < nb:
                        edges.append([rid, nb])
            frames.append({"cells": cells, "edges": edges})
        portals = [{"a": a, "dir": ad, "b": b} for (a, ad, b, bd) in self._portals]
        return {"frames": frames, "portals": portals,
                "rooms": len(self.coord), "version": self.version, "dbg": dict(self.dbg)}
