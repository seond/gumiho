"""지도-driven local survey with registration-based positioning.

Room identity here is a cell's POSITION in the server's own 지도 minimap — never
the room title. Identical-looking rooms (a 3x3 of "갈라진 틈") are told apart by
where they sit in the drawn grid, so a sweep can walk EVERY cell and know when it
is done, without dead-reckoning into phantom clones.

WHY REGISTRATION (not dead reckoning): a 지도 is only useful if we know where the
player sits within the accumulated map. The old design advanced a position counter
the instant a move was *commanded*, then anchored every new 지도 to that counter —
so any refused step (blocked exit, closed door, a momentary MV/combat hiccup)
desynced the counter and translated the next 지도's whole neighbourhood, smearing a
clean 3x3 into 3x4 / 4x4 / worse. There was no step that reconciled against ground
truth.

Now each 지도 is REGISTERED against what we already know: we pick the translation
that best fits the overlapping topology (matching connections, contradicting a
known wall is punished) and DERIVE the player's position from that fit. A move is
only a *prior* — a hint for where to look. A refused move is detected for free: the
new 지도 doesn't shift, so registration lands it back where we stood. Cells only
ever merge at a consistent offset, so the drawn shape can no longer balloon.

Pure state machine over parsed MapGrids (gumiho.mapgrid.MapGrid) plus the live
room's exit directions — testable without a live server.
"""

from collections import deque

_DELTA = {"북": (-1, 0), "남": (1, 0), "동": (0, 1), "서": (0, -1)}
_OPP = {"북": "남", "남": "북", "동": "서", "서": "동"}
_ADJ_STEPS = ((1, 0), (0, 1))            # +south / +east — enumerate each pair once


def _dir_of_delta(dr, dc):
    # normalise to a unit step so a 지도's multi-column stride still maps to a dir
    if dr and not dc:
        return "북" if dr < 0 else "남"
    if dc and not dr:
        return "서" if dc < 0 else "동"
    return None


class GridSweep:
    """Accumulated local grid + a cursor. Coordinates are relative to the first
    registered 지도's centre (0, 0). Position is set by registering each 지도 — the
    only source of truth — so loops close exactly and revisits are recognised even
    when moves are refused."""

    SEARCH = 2                            # ± offset window when re-registering a 지도

    def __init__(self):
        self.pos = (0, 0)
        self.anchor_title = None          # the pocket's room title (first 지도) — the
                                          # sweep is CONFINED to same-title cells
        self.cells = {(0, 0): None}       # coord -> title (None = title unknown)
        self.adj = {}                     # coord -> {dir: neighbour coord}
        self.walls = set()                # frozenset{a, b}: an OBSERVED non-connection
        self.visited = set()              # cells we've physically stood in
        self.mapped = set()               # cells a 지도 has been centred on
        self.avoid = set()                # cells the user marked "don't hunt here"
        self.version = 0                  # bumps on any change (UI overlay refresh)
        self.pending_dir = None           # last move COMMANDED, awaiting a 지도
        self._confirmed = False           # the server actually moved us (RoomSeen)
        self._refused = False             # the server refused that move
        self._refused_hard = False        # ...as a WALL (CantGo), not transient (exhaustion)

    def _touch(self):
        self.version += 1

    # ---- move bookkeeping (hints only; pos is authoritative from register) ----
    def note_move(self, direction):
        """Record the move we just sent. It biases where the next 지도 is expected,
        but does NOT move pos — registration only trusts a shift once the server
        CONFIRMS the move actually happened (confirm_move)."""
        self.pending_dir = direction
        self._confirmed = False
        self._refused = False
        self._refused_hard = False

    def confirm_move(self):
        """The server actually moved us (a room render followed the move command).
        Only now will registration trust the commanded shift — an unconfirmed or
        silently-failed move is treated as 'stayed put', so it can't bake a phantom
        offset copy of the pocket into the map."""
        self._confirmed = True

    def note_refused(self, hard=True):
        """The server refused the pending move. hard=True (CantGo / closed / locked)
        means there is genuinely no exit that way — registration drops that phantom
        edge so the sweep stops chasing it. hard=False (transient, e.g. move-point
        exhaustion) just means 'not now' — no shift, but the edge is kept."""
        self._refused = True
        self._refused_hard = hard

    # ---- learning from 지도: register it against what we already know ---------
    def register(self, grid):
        """Fold a parsed 지도 into the world map at the offset that best fits the
        existing topology, and set pos from that fit. Returns True if usable."""
        if grid is None or grid.here is None:
            return False
        hr, hc = grid.here
        gcells = dict(grid.cells)
        gcells.setdefault((hr, hc), None)          # the player's own cell counts
        gedges = {frozenset(tuple(e)) for e in grid.edges}
        gwalls = self._walls_of(gcells, gedges)

        # A hard refusal means there's no exit that way — drop the phantom edge so
        # the sweep can't loop forever trying to walk into it.
        if self.pending_dir is not None and self._refused and self._refused_hard:
            self._invalidate(self.pending_dir)

        # Prior: where we expect `here` to land in world coords. We trust the
        # commanded shift ONLY when the server confirmed the move actually happened;
        # otherwise (refused, or no confirmation yet) we assume we stayed put — so a
        # failed/unconfirmed move can never bake a shifted phantom copy of the map.
        if self.pending_dir is not None and self._confirmed and not self._refused:
            dr, dc = _DELTA[self.pending_dir]
            expect = (self.pos[0] + dr, self.pos[1] + dc)
        else:
            expect = self.pos
        base_off = (expect[0] - hr, expect[1] - hc)

        first = len(self.cells) <= 1 and not self.adj
        if first:
            best_off = base_off                    # nothing to align to yet
        else:
            best_off = self._best_offset(gcells, gedges, gwalls, base_off)

        self._merge(gcells, gedges, gwalls, best_off)
        self.pos = (hr + best_off[0], hc + best_off[1])
        self.mapped.add(self.pos)
        self.cells.setdefault(self.pos, None)
        if self.anchor_title is None:      # the hunt's pocket title — confine to it
            self.anchor_title = self.cells.get(self.pos) or gcells.get((hr, hc))
        self.pending_dir = None
        self._confirmed = self._refused = self._refused_hard = False
        self._touch()
        return True

    def _invalidate(self, direction):
        """Drop a phantom edge out of `pos` (a move that direction was hard-refused):
        remove the adjacency both ways and record a wall, so next_dir won't target
        the now-unreachable cell again."""
        nb = self.adj.get(self.pos, {}).pop(direction, None)
        if nb is not None:
            self.adj.get(nb, {}).pop(_OPP.get(direction), None)
            self.walls.add(frozenset({self.pos, nb}))
            self._touch()

    @staticmethod
    def _walls_of(cells, edges):
        """Pairs of drawn-adjacent cells with NO connector between them — a wall we
        positively observed (as opposed to the map simply ending, which is unknown)."""
        walls = set()
        for (r, c) in cells:
            for dr, dc in _ADJ_STEPS:
                nb = (r + dr, c + dc)
                if nb in cells and frozenset({(r, c), nb}) not in edges:
                    walls.add(frozenset({(r, c), nb}))
        return walls

    def _best_offset(self, gcells, gedges, gwalls, base_off):
        """Choose where to lay the 지도. The MOVE PRIOR (base_off) is trusted unless
        it contradicts known structure — we do NOT chase maximum overlap, or a
        commanded step into fresh territory would be folded back onto known cells
        (a uniform pocket has redundant overlap everywhere). Ranking, best first:
          1) fewest contradictions (a drawn link over a known wall, or vice versa,
             or a title clash) — hard evidence of a wrong fit;
          2) nearest the prior (the move we just made is probably real);
          3) most corroborating overlap — only as a final tie-break."""
        best, best_key = base_off, None
        for dr in range(-self.SEARCH, self.SEARCH + 1):
            for dc in range(-self.SEARCH, self.SEARCH + 1):
                off = (base_off[0] + dr, base_off[1] + dc)
                contra, align = self._score(gcells, gedges, gwalls, off)
                key = (-contra, -(abs(dr) + abs(dc)), align)
                if best_key is None or key > best_key:
                    best, best_key = off, key
        return best

    def _score(self, gcells, gedges, gwalls, off):
        """(contradictions, corroboration) for laying the 지도 at `off`.
        A contradiction is decisive evidence the fit is wrong: a drawn connection
        landing on a KNOWN wall (or a drawn wall landing on a KNOWN connection), or
        a cell whose title clashes with a known one. Corroboration just counts the
        overlap/edges that agree — used only to break ties among equal-contradiction,
        equal-distance fits."""
        oy, ox = off
        contra = 0
        align = 0
        for (r, c), title in gcells.items():
            w = (r + oy, c + ox)
            if w in self.cells:
                align += 1
                wt = self.cells.get(w)
                if title and wt and title != wt:
                    contra += 1
        for e in gedges:
            a, b = tuple(e)
            wa, wb = (a[0] + oy, a[1] + ox), (b[0] + oy, b[1] + ox)
            if wb in self.adj.get(wa, {}).values():
                align += 3
            elif frozenset({wa, wb}) in self.walls:
                contra += 1
        for e in gwalls:
            a, b = tuple(e)
            wa, wb = (a[0] + oy, a[1] + ox), (b[0] + oy, b[1] + ox)
            if wb in self.adj.get(wa, {}).values():
                contra += 1
        return contra, align

    def _merge(self, gcells, gedges, gwalls, off):
        oy, ox = off
        for (r, c), title in gcells.items():
            w = (r + oy, c + ox)
            if title:
                self.cells[w] = title              # a real title wins over a placeholder
            else:
                self.cells.setdefault(w, None)
        for e in gedges:
            a, b = tuple(e)
            wa, wb = (a[0] + oy, a[1] + ox), (b[0] + oy, b[1] + ox)
            d = _dir_of_delta(wb[0] - wa[0], wb[1] - wa[1])
            if d:
                self.cells.setdefault(wa, None)
                self.cells.setdefault(wb, None)
                self.adj.setdefault(wa, {})[d] = wb
                self.adj.setdefault(wb, {})[_OPP[d]] = wa
                self.walls.discard(frozenset({wa, wb}))
        for e in gwalls:
            a, b = tuple(e)
            wa, wb = (a[0] + oy, a[1] + ox), (b[0] + oy, b[1] + ox)
            if wb not in self.adj.get(wa, {}).values():
                self.walls.add(frozenset({wa, wb}))

    def knows_here(self):
        """True if a 지도 has placed our current cell (as a centre or a linked
        neighbour) — so we already know its exits without asking again."""
        return self.pos in self.mapped or self.pos in self.adj

    # ---- user control (runtime only, never persisted) -----------------------
    def set_avoid(self, cell, on):
        cell = tuple(cell)
        if on:
            self.avoid.add(cell)
        else:
            self.avoid.discard(cell)
        self._touch()

    def avoided_here(self):
        return self.pos in self.avoid

    def pocket_cells(self):
        """The cells that belong to the hunt pocket — same title as the anchor.
        Differently-titled neighbours a 지도 happens to draw are excluded (they are
        not part of the zone we hunt), so the map matches the pocket's real shape."""
        return [c for c in self.cells if self._in_pocket(c)]

    def snapshot(self):
        """Serialisable view of the temporary local map for the UI overlay —
        confined to the same-title pocket."""
        pocket = set(self.pocket_cells())
        return {
            "here": list(self.pos),
            "cells": [
                {"rc": list(c), "visited": c in self.visited,
                 "avoid": c in self.avoid, "mapped": c in self.mapped}
                for c in sorted(pocket)
            ],
            "edges": [[list(a), list(b)]
                      for a, nbrs in self.adj.items() for b in nbrs.values()
                      if a < b and a in pocket and b in pocket],
            "version": self.version,
        }

    # ---- the walk -----------------------------------------------------------
    def mark_visited(self):
        if self.pos not in self.visited:
            self.visited.add(self.pos)
            self._touch()

    def move(self, direction):
        """Blind dead-reckoning step — ONLY for the fallback path when 지도 can't be
        parsed in a room. The normal sweep sets pos through register()."""
        dr, dc = _DELTA[direction]
        self.pos = (self.pos[0] + dr, self.pos[1] + dc)
        self.cells.setdefault(self.pos, None)
        self._touch()

    def next_dir(self, exits=None):
        """The direction to send next, or None when the pocket is swept: the first
        step of the shortest path (over known SAME-TITLE cells) to the nearest
        unvisited one. There is NO blind frontier discovery — one 지도 already draws
        the whole local pocket, and a live-room exit the map doesn't show only ever
        leads OUT of it (a different room), so following it would wrongly annex the
        neighbour. New cells are revealed by re-지도-ing each cell we step onto, not
        by walking into the unknown. `exits` is accepted but unused (kept for the
        caller's signature)."""
        return self._toward_unvisited()

    def swept(self, exits=None):
        return self.next_dir() is None

    def _in_pocket(self, cell):
        """A cell counts as part of the hunt pocket only if its title matches the
        anchor (the first cell's). Unknown-title cells are tentatively included."""
        if self.anchor_title is None:
            return True
        t = self.cells.get(cell)
        return t is None or t == self.anchor_title

    def _toward_unvisited(self):
        # Target = a same-title cell not yet stood in AND not user-avoided. We never
        # traverse OUT of the pocket (a differently-titled cell), so an adjacent
        # room can't be swept into the map.
        seen = {self.pos}
        q = deque()
        for d, nb in self.adj.get(self.pos, {}).items():
            if nb not in seen and self._in_pocket(nb):
                seen.add(nb)
                q.append((nb, d))
        while q:
            cell, first = q.popleft()
            if cell not in self.visited and cell not in self.avoid:
                return first
            for d, nb in self.adj.get(cell, {}).items():
                if nb not in seen and self._in_pocket(nb):
                    seen.add(nb)
                    q.append((nb, first))
        return None
