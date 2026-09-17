"""지도-driven survey: registration-based positioning must draw the EXACT zone
shape and cover every cell — even when moves are refused (the real-world drift
cause) and when the zone is bigger than one 지도 screen (stitching).

The old suite only ever exercised a world where every move succeeds, so the
command-time dead-reckoning drift never showed up. These tests fail a move on
purpose and assert the drawn shape never balloons.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho.gridsweep import GridSweep
from gumiho.mapgrid import MapGrid

_DELTA = {"북": (-1, 0), "남": (1, 0), "동": (0, 1), "서": (0, -1)}


class World:
    """A W x H grid pocket. The 지도 drawn around the player is a window of it,
    re-centred each step (as the real server does). `radius` (Chebyshev) limits how
    far the minimap reaches — None means the whole zone fits on one screen."""

    def __init__(self, w, h, start, radius=None):
        self.w, self.h = w, h
        self.abs = start
        self.radius = radius

    def _in(self, r, c):
        return 0 <= r < self.h and 0 <= c < self.w

    def exits(self):
        r, c = self.abs
        return [d for d, (dr, dc) in _DELTA.items() if self._in(r + dr, c + dc)]

    def try_move(self, direction, blocked):
        dr, dc = _DELTA[direction]
        r, c = self.abs
        nb = (r + dr, c + dc)
        if not self._in(*nb) or (self.abs, direction) in blocked:
            return False
        self.abs = nb
        return True

    def _vis(self, r, c):
        pr, pc = self.abs
        return self.radius is None or (abs(r - pr) <= self.radius and abs(c - pc) <= self.radius)

    def jido(self):
        g = MapGrid(zone="pocket")
        HR, HC = 5, 5                        # player's display cell (arbitrary centre)
        pr, pc = self.abs
        for r in range(self.h):
            for c in range(self.w):
                if not self._vis(r, c):
                    continue
                cell = (HR + (r - pr), HC + (c - pc))
                g.cells[cell] = "갈라진 틈"        # every room shares one title
                if (r, c) == self.abs:
                    g.here = cell
        for r in range(self.h):
            for c in range(self.w):
                if not self._vis(r, c):
                    continue
                cell = (HR + (r - pr), HC + (c - pc))
                if self._in(r, c + 1) and self._vis(r, c + 1):
                    g.edges.add(frozenset({cell, (cell[0], cell[1] + 1)}))
                if self._in(r + 1, c) and self._vis(r + 1, c):
                    g.edges.add(frozenset({cell, (cell[0] + 1, cell[1])}))
        return g


def run(world, blocked=frozenset(), fail_once=None):
    """Drive a full survey. `blocked` = permanently refused (cell,dir) pairs (a wall
    -> HARD refusal); `fail_once` = {(cell,dir): n} transient refusals (MV hiccup /
    combat) that clear after n attempts (SOFT refusal). Mirrors the engine: 지도 each
    step, and pos advances only on a CONFIRMED move."""
    sweep = GridSweep()
    fails = dict(fail_once or {})
    for _ in range(600):
        sweep.register(world.jido())            # ground-truth positioning
        sweep.mark_visited()
        d = sweep.next_dir(world.exits())
        if d is None:
            break
        sweep.note_move(d)
        key = (world.abs, d)
        if fails.get(key, 0) > 0:
            fails[key] -= 1
            sweep.note_refused(hard=False)      # transient — keep the edge
        elif world.try_move(d, blocked):
            sweep.confirm_move()                # server moved us -> trust the shift
        else:
            sweep.note_refused(hard=True)       # a wall -> drop the phantom edge
    else:
        raise AssertionError("sweep did not converge")
    return world, sweep


def _shape(sweep):
    """Known cells, normalised so the top-left is (0,0) — the drawn shape."""
    cs = set(sweep.cells)
    r0 = min(r for r, c in cs)
    c0 = min(c for r, c in cs)
    return {(r - r0, c - c0) for r, c in cs}


def _rect(w, h):
    return {(r, c) for r in range(h) for c in range(w)}


def test_3x3_from_center():
    world, sweep = run(World(3, 3, (1, 1)))
    assert len(sweep.visited) == 9, sweep.visited
    assert _shape(sweep) == _rect(3, 3), _shape(sweep)      # EXACT shape, no balloon
    assert sweep.swept(world.exits())
    print("3x3 from center: exact 3x3, all 9 cells")


def test_3x3_from_corner():
    world, sweep = run(World(3, 3, (0, 0)))
    assert len(sweep.visited) == 9, sweep.visited
    assert _shape(sweep) == _rect(3, 3), _shape(sweep)
    print("3x3 from corner: exact 3x3, all 9 cells")


def test_refused_move_does_not_balloon():
    """The old bug: a refused step drifted the counter and smeared 3x3 -> 3x4/4x4.
    Here the first two 동 attempts from the start are refused (transient); the shape
    must STILL be an exact 3x3 with no phantom cells."""
    world, sweep = run(World(3, 3, (0, 0)),
                       fail_once={((0, 0), "동"): 2, ((1, 1), "남"): 1})
    assert _shape(sweep) == _rect(3, 3), f"refused move ballooned the map: {_shape(sweep)}"
    assert len(sweep.cells) == 9, f"phantom cells minted: {sorted(sweep.cells)}"
    assert len(sweep.visited) == 9, sweep.visited
    print("refused moves: shape stayed an exact 3x3 (no drift, no phantoms)")


def test_silent_failure_does_not_balloon():
    """Worst case: a move fails but NOTHING signals it (no CantGo/exhaustion hook) —
    the sweep's prior wrongly assumes a step. On a bounded pocket the known walls
    contradict the bogus shift, so registration still lands the shape as an exact
    3x3 with no phantom cells."""
    world = World(3, 3, (0, 0))
    sweep = GridSweep()
    silent = {((0, 0), "동"): 2, ((0, 1), "남"): 1}
    for _ in range(120):
        sweep.register(world.jido())
        sweep.mark_visited()
        d = sweep.next_dir(world.exits())
        if d is None:
            break
        sweep.note_move(d)
        key = (world.abs, d)
        if silent.get(key, 0) > 0:
            silent[key] -= 1                # fails, and NOTHING is signalled (no confirm/refuse)
        elif world.try_move(d, frozenset()):
            sweep.confirm_move()            # real moves are confirmed; silent fails are not
    assert _shape(sweep) == _rect(3, 3), f"silent failure ballooned: {_shape(sweep)}"
    assert len(sweep.cells) == 9 and len(sweep.visited) == 9, sorted(sweep.cells)
    print("silent failure: bounded pocket self-corrects, exact 3x3")


def test_2x5_corridor():
    world, sweep = run(World(2, 5, (0, 0)))
    assert len(sweep.visited) == 10, sweep.visited
    assert _shape(sweep) == _rect(2, 5), _shape(sweep)
    print("2x5 corridor: exact shape, all 10 cells")


def test_stitch_beyond_one_screen():
    """Zone bigger than one 지도 (radius-1 window): the survey must STITCH multiple
    minimaps into one exact 3x5 rectangle — the case single-지도 authority can't do."""
    world, sweep = run(World(5, 3, (0, 0), radius=1))
    assert _shape(sweep) == _rect(5, 3), f"stitching got the shape wrong: {_shape(sweep)}"
    assert len(sweep.cells) == 15, f"stitched to wrong cell count: {len(sweep.cells)}"
    assert len(sweep.visited) == 15, sweep.visited
    print("stitching: 5x3 across radius-1 screens -> exact shape, all 15 cells")


def test_stitch_with_refused_moves():
    """Stitching AND transient refusals together — still one exact rectangle."""
    world, sweep = run(World(4, 3, (0, 0), radius=1),
                       fail_once={((0, 0), "동"): 1, ((0, 2), "동"): 2, ((1, 1), "남"): 1})
    assert _shape(sweep) == _rect(4, 3), f"shape wrong: {_shape(sweep)}"
    assert len(sweep.cells) == 12, f"cell count wrong: {len(sweep.cells)}"
    assert len(sweep.visited) == 12, sweep.visited
    print("stitch + refused: exact 4x3, all 12 cells")


def test_hard_refusal_breaks_phantom_loop():
    """The overnight hang: a phantom cell (left by an earlier drift) that the sweep
    keeps trying to reach via an exit the server refuses (지도/북/지도/북…). A HARD
    refusal (CantGo) must drop that edge so next_dir stops looping on it."""
    from gumiho.mapgrid import MapGrid
    sweep = GridSweep()
    g = MapGrid(zone="p")
    g.here = (5, 5)
    g.cells[(5, 5)] = "X"
    g.cells[(4, 5)] = "X"                       # a north neighbour...
    g.edges.add(frozenset({(5, 5), (4, 5)}))    # ...linked (will prove to be phantom)
    sweep.register(g)
    sweep.mark_visited()
    assert sweep.next_dir([]) == "북", "sweep should target the unvisited north cell"

    sweep.note_move("북")
    sweep.note_refused(hard=True)               # server: 그쪽으로는 갈 수 없습니다
    g2 = MapGrid(zone="p")                       # re-map: north isn't really there
    g2.here = (5, 5)
    g2.cells[(5, 5)] = "X"
    sweep.register(g2)                           # invalidate drops the phantom edge
    assert sweep.next_dir([]) is None, f"phantom edge should be gone: adj={sweep.adj}"
    print("hard refusal: phantom edge dropped, loop broken")


def test_confined_to_same_title_pocket():
    """A same-title 3x3 pocket ('갈라진 틈') with a DIFFERENT-title neighbour ('외곽
    지역 하수관') drawn + connected on the 지도. The sweep must confine to the pocket
    and NEVER annex the neighbour — the live bug where it walked out a boundary exit
    and grew a phantom 4th row (12 cells instead of 9)."""
    from gumiho.mapgrid import MapGrid
    POCKET = {(r, c) for r in range(3) for c in range(3)}
    BOUNDARY = (3, 1)                       # one room south of the pocket, different title
    ALL = POCKET | {BOUNDARY}

    def title(cell):
        return "갈라진 틈" if cell in POCKET else "외곽지역 하수관"

    def jido(abs_pos):
        g = MapGrid(zone="p")
        HR, HC = 5, 5
        for cell in ALL:                    # small area — the 지도 draws it all
            d = (HR + (cell[0] - abs_pos[0]), HC + (cell[1] - abs_pos[1]))
            g.cells[d] = title(cell)
            if cell == abs_pos:
                g.here = d
        for cell in ALL:
            for dr, dc in ((1, 0), (0, 1)):
                nb = (cell[0] + dr, cell[1] + dc)
                if nb in ALL:
                    a = (HR + (cell[0] - abs_pos[0]), HC + (cell[1] - abs_pos[1]))
                    b = (HR + (nb[0] - abs_pos[0]), HC + (nb[1] - abs_pos[1]))
                    g.edges.add(frozenset({a, b}))
        return g

    _D = {"북": (-1, 0), "남": (1, 0), "동": (0, 1), "서": (0, -1)}
    abs_pos = (0, 0)
    sweep = GridSweep()
    for _ in range(200):
        sweep.register(jido(abs_pos))
        sweep.mark_visited()
        d = sweep.next_dir()
        if d is None:
            break
        sweep.note_move(d)
        dr, dc = _D[d]
        nb = (abs_pos[0] + dr, abs_pos[1] + dc)
        if nb in ALL:                       # a real move
            abs_pos = nb
            sweep.confirm_move()
        else:
            sweep.note_refused(hard=True)
    else:
        raise AssertionError("did not converge")
    assert len(sweep.visited) == 9, f"should visit only the 9 pocket cells: {len(sweep.visited)}"
    assert len(sweep.pocket_cells()) == 9, f"pocket view wrong shape: {len(sweep.pocket_cells())}"
    assert len(sweep.snapshot()["cells"]) == 9, "overlay must show only the 3x3 pocket"
    assert abs_pos in POCKET, f"ended outside the pocket: {abs_pos}"
    print("confinement: swept the 3x3 pocket, never annexed the 하수관 neighbour")


def test_avoid_skips_room():
    # A 1x3 row P0-P1-P2 from P0; mark the middle+far cells avoid -> the sweep
    # never targets them, so it's done after visiting only P0.
    world = World(3, 1, (0, 0))            # w=3, h=1: cols 0,1,2 in row 0
    sweep = GridSweep()
    sweep.register(world.jido())
    sweep.set_avoid((0, 1), True)          # relative coords: start is (0,0)
    sweep.set_avoid((0, 2), True)
    sweep.mark_visited()
    assert sweep.next_dir(world.exits()) is None, "avoided cells must not be swept"
    snap = sweep.snapshot()
    avoided = {tuple(c["rc"]) for c in snap["cells"] if c["avoid"]}
    assert (0, 1) in avoided and (0, 2) in avoided, snap
    sweep.set_avoid((0, 1), False)         # un-avoid one -> it becomes a target again
    assert sweep.next_dir(world.exits()) == "동", "un-avoided cell is huntable again"
    print("avoid: marked rooms are skipped; snapshot carries flags; reversible")


if __name__ == "__main__":
    test_3x3_from_center()
    test_3x3_from_corner()
    test_refused_move_does_not_balloon()
    test_silent_failure_does_not_balloon()
    test_2x5_corridor()
    test_stitch_beyond_one_screen()
    test_stitch_with_refused_moves()
    test_hard_refusal_breaks_phantom_loop()
    test_confined_to_same_title_pocket()
    test_avoid_skips_room()
    print("\nALL GRIDSWEEP TESTS PASSED")
