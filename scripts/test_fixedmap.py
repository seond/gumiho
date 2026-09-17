"""FixedMap: deterministic navigation over a hand-authored zone layout (올림푸스 신전),
used where dead-reckoning fails. Verifies the derived topology, deterministic move-tracking,
and a full sweep."""

import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho.fixedmap import FixedMap

# The user's 올림푸스 신전 matrix: 4 3x3 squares joined by a central cross; XX = the four
# conditional-exit centres (excluded); the route lands at room 20 (dead centre).
MATRIX = [
    ".. .. .. .. 00 01 02 .. .. .. ..",
    ".. .. .. .. 03 XX 04 .. .. .. ..",
    ".. .. .. .. 05 06 07 .. .. .. ..",
    ".. .. .. .. .. 08 .. .. .. .. ..",
    "09 10 11 .. .. 12 .. .. 13 14 15",
    "16 XX 17 18 19 20 21 22 23 XX 24",
    "25 26 27 .. .. 28 .. .. 29 30 31",
    ".. .. .. .. .. 32 .. .. .. .. ..",
    ".. .. .. .. 33 34 35 .. .. .. ..",
    ".. .. .. .. 36 XX 37 .. .. .. ..",
    ".. .. .. .. 38 39 40 .. .. .. ..",
]
DEF = {"start": 20, "matrix": [{"name": "main", "grid": MATRIX}]}


def new():
    return FixedMap(DEF)


def _walk(fm, d):
    fm.note_move(d)
    fm.observe(exits=None, moved=True)


def test_topology_all_rooms_connected_no_xx():
    """41 numbered rooms; every edge a grid adjacency that never touches an XX centre; the
    whole floor is one connected component reachable from the arrival room."""
    fm = new()
    assert len(fm.coord) == 41, f"expect rooms 00-40, got {len(fm.coord)}"
    seen = {20}
    q = deque([20])
    while q:
        x = q.popleft()
        for nb in fm.edges[x].values():
            if nb not in seen:
                seen.add(nb); q.append(nb)
    assert seen == set(fm.coord), f"not all connected: missing {set(fm.coord) - seen}"
    assert sorted(fm.edges[20]) == ["남", "동", "북", "서"], "centre has all four arms"
    assert sorted(fm.edges[0]) == ["남", "동"], "corner room 00 has two exits"
    # no edge lands on an XX coordinate (e.g. 01 -남-> XX at (1,5) must NOT exist)
    assert "남" not in fm.edges[1], "01 must not have an edge INTO the XX centre"
    print("topology: 41 rooms, one connected component, XX centres excluded")


def test_deterministic_move_tracking():
    """Position follows known edges exactly; a move with no edge leaves pos put (a refusal)."""
    fm = new()
    assert fm.cur == 20
    _walk(fm, "북"); assert fm.cur == 12, "20 -북-> 12"
    _walk(fm, "북"); assert fm.cur == 8, "12 -북-> 08"
    _walk(fm, "남"); assert fm.cur == 12, "08 -남-> 12 (back)"
    # 동 from 12 is not an edge (empty cell) -> stay put, counted as off_edge
    _walk(fm, "동"); assert fm.cur == 12, "no 동 edge from 12 -> stay"
    assert fm.dbg["off_edge"] == 1
    # a refused move clears pending without moving
    fm.note_move("남"); fm.note_refused(); fm.observe(exits=None, moved=False)
    assert fm.cur == 12
    print("moves: deterministic along known edges; off-edge / refused stay put")


def test_full_sweep_visits_every_room():
    """Following next_hunt_dir + deterministic moves sweeps all 41 rooms, then reports barren;
    reset_swept re-opens it for the respawn cycle."""
    fm = new()
    visited = {fm.pos}
    for _ in range(500):
        fm.mark_swept()
        d = fm.next_hunt_dir()
        if d is None:
            break
        _walk(fm, d)
        visited.add(fm.pos)
    assert visited == set(fm.coord), f"unvisited: {set(fm.coord) - visited}"
    assert fm.swept == set(fm.coord), "every room marked swept"
    assert fm.next_hunt_dir() is None, "all swept -> barren"
    fm.reset_swept()
    assert fm.next_hunt_dir() is not None, "reset_swept re-opens the sweep for respawns"
    print("sweep: visits every one of the 41 rooms, barren when done, reset re-opens")


def test_desync_recovery_landmark_and_block():
    """After a position desync (a map-move refused because we're actually elsewhere), the map
    blocks that step so it tries another direction (unstuck), and RE-SYNCS the instant it sees
    the 아래 landmark (the centre is the only room with a down-stair)."""
    fm = new()
    fm.pos = 7                                         # DESYNCED: map thinks 7 (a 2-exit corner)
    d = fm.next_hunt_dir()
    fm.note_move(d)
    fm.note_refused(direction=d)                       # server refused it -> block (7, d)
    assert (7, d) in fm._blocked
    d2 = fm.next_hunt_dir()
    assert d2 != d, "after a refusal it must try a DIFFERENT direction"
    # a render now shows 아래 -> only the centre has it -> re-sync to 20, blocks cleared
    fm.observe(exits={"북", "동", "남", "서", "아래"}, moved=False)
    assert fm.pos == 20 and not fm._blocked and fm.dbg["resync"] == 1
    print("desync recovery: block+retry on refusal, re-sync at the 아래 landmark")


def test_walls_persist_across_moves():
    """A refused edge (a WALL — the map assumed a passage the server refuses) STAYS blocked
    after an unrelated successful move. Clearing it on every move made the hunt re-try the
    same wall forever, oscillating in a tiny pocket while `refused` climbed without bound
    (the 삼국시대 bug). Only a landmark re-sync or reset clears walls."""
    d = {"start": 0, "matrix": [{"name": "t", "grid": ["00 01", "02 03"]}]}
    fm = FixedMap(d)
    fm.note_move("동"); fm.note_refused(direction="동")     # 0 -동-> 01 refused -> wall
    assert (0, "동") in fm._blocked
    fm.note_move("남"); fm.observe(exits=None, moved=True)  # a REAL move 0 -남-> 02
    assert fm.cur == 2
    assert (0, "동") in fm._blocked, "wall must survive a confirmed move"
    fm.note_move("북"); fm.observe(exits=None, moved=True)  # back 02 -북-> 00
    assert fm.cur == 0 and (0, "동") in fm._blocked, "wall still blocked after returning"
    fm.mark_swept()
    assert fm.next_hunt_dir() != "동", "must not retry the blocked wall (routes around it)"
    # A landmark re-sync (the signal that pos was actually WRONG) is what clears walls.
    fm2 = FixedMap({**d, "resync_exit": "아래"})
    fm2.note_move("동"); fm2.note_refused(direction="동")
    fm2.observe(exits={"아래"}, moved=False)
    assert not fm2._blocked, "landmark re-sync clears walls (treats them as desync artifacts)"
    print("walls persist across moves; only re-sync/reset clears them")


def test_render_never_advances_pos():
    """Position is COMMAND-driven (note_move advances optimistically); renders NEVER move it.
    An unsolicited combat re-render — the server pushing the room again mid-fight — must not
    advance pos. That render-as-move confusion was the 007->017 drift."""
    fm = new()                                       # start=20; 20 -북-> 12
    fm.note_move("북")                               # commanded -> advances immediately
    assert fm.cur == 12, "note_move advances optimistically at send"
    for _ in range(5):                               # a flurry of combat re-renders
        fm.observe(exits={"동", "서"}, moved=False)
    assert fm.cur == 12, "renders never advance pos"
    assert fm.pending is None, "a render (no refusal) confirms the move, releasing the gate"
    print("renders never advance pos; only commands do")


def test_refusal_rolls_back():
    """A refused move rolls pos back to where it stepped FROM and blocks that wall — the
    optimistic advance is undone, so a wall can't drift position forward."""
    fm = new()                                       # start=20; 20 -북-> 12
    fm.note_move("북"); assert fm.cur == 12
    fm.note_refused()                                # server: 갈 수 없습니다 -> roll back
    assert fm.cur == 20, "rolled back to the origin room"
    assert (20, "북") in fm._blocked, "the refused step is blocked as a wall"
    assert fm.dbg["moves"] == 0, "the optimistic move was undone in the count"
    assert fm.next_hunt_dir() != "북" or fm.edges[20].get("북") is None, "won't retry the wall"
    print("a refused move rolls back to origin + blocks the wall")


def test_door_on_portal_gates_both_ends():
    """A [[door]] on a portal edge is parsed and MIRRORED onto the portal's other side, so the
    hunt opens it on BOTH the outbound and the return traversal; locked/key carry through."""
    base = {"start": 0,
            "matrix": [{"name": "a", "grid": ["00 01"]}, {"name": "b", "grid": ["10 11"]}],
            "portal": [{"from": 1, "from_dir": "위", "to": 10, "to_dir": "아래"}]}
    d = dict(base, door=[{"room": 1, "dir": "위", "locked": False, "name": "하늘"}])
    fm = FixedMap(d)
    assert fm.doors.get((1, "위")) == {"locked": False, "key": None, "name": "하늘"}, fm.doors
    assert fm.doors.get((10, "아래")) == {"locked": False, "key": None, "name": "하늘"}, "mirrored w/ name"
    # a locked door carries its flag + key (the hunt won't open it — needs the key, not yet impl)
    d2 = dict(base, door=[{"room": 1, "dir": "위", "locked": True, "key": "황금 열쇠"}])
    fm2 = FixedMap(d2)
    assert fm2.doors[(1, "위")]["locked"] is True and fm2.doors[(1, "위")]["key"] == "황금 열쇠"
    # no door -> empty dict, and getattr on any FixedMap is safe
    assert FixedMap(base).doors == {}
    print("doors: parsed w/ name, mirrored across the portal, locked+key carried")


def test_sealed_region_is_barren_not_oscillating():
    """A block that seals off the only route to unswept rooms -> next_hunt_dir returns None
    (barren), NOT a wander that tight-oscillates between two swept rooms (the 9<->16 loop)."""
    fm = FixedMap({"start": 0, "matrix": [{"name": "t", "grid": ["00 01 02"]}]})   # 0-1-2 line
    fm.swept = {0, 1}                       # 2 unswept, reachable only via (1,동)
    fm.pos = 1
    assert fm.next_hunt_dir() == "동", "reachable unswept -> route to it"
    fm._blocked = {(1, "동")}               # a door/wall seals off room 2
    assert fm.next_hunt_dir() is None, "sealed unswept -> barren, not a wander"
    assert fm.dbg.get("sealed", 0) >= 1, "the sealed-off condition is flagged"
    print("sealed region -> barren (no tight 9<->16 oscillation)")


def test_resync_exit_can_be_disabled():
    """A zone whose arrival room has no UNIQUE landmark exit disables the re-sync with
    resync_exit="". Otherwise a common exit rendered at a non-arrival room (지하동굴's default
    "아래" shows at the traversed portal room 007, not arrival 001) snaps pos back to arrival on
    every such render — the 'jumps back and forth' bug. Disabled: renders never move pos."""
    d = {"start": 0, "resync_exit": "", "matrix": [{"name": "t", "grid": ["00 01 02"]}]}
    fm = FixedMap(d)
    assert fm.resync_exit == "", "empty resync_exit disables the landmark snap"
    fm.note_move("동"); assert fm.cur == 1                 # 00 -동-> 01 (dead-reckoned)
    fm.observe(exits={"동", "서", "아래"}, moved=False)     # a 아래-bearing render
    assert fm.cur == 1, "resync disabled -> a render must NOT snap pos to arrival"
    # and the DEFAULT still re-syncs (other zones unaffected)
    fm2 = FixedMap({"start": 0, "matrix": [{"name": "t", "grid": ["00 01 02"]}]})
    fm2.note_move("동"); fm2.observe(exits={"아래"}, moved=False)
    assert fm2.cur == 0, "default resync_exit still snaps to arrival (unchanged for other zones)"
    print("resync: resync_exit='' disables the snap; the default is unchanged")


def test_note_flee_advances_pos_without_arming_pending():
    """A 도망 relocates the leader a direction it never learns; the supporter reports it via
    note_flee. Pos must advance along that edge (so the map stays in sync) but WITHOUT arming
    `pending` — the flee already rendered, and a stray pending would stall the hunt into a 봐.
    An unknown fled direction leaves pos put for the landmark re-sync to recover."""
    fm = new()                                    # start=20; 20 -북-> 12
    assert fm.cur == 20 and fm.pending is None
    fm.note_flee("북")
    assert fm.cur == 12, "fled 북 -> pos follows the edge to 12"
    assert fm.pending is None, "note_flee must NOT arm pending (no move-render is coming)"
    assert fm._prev_pos is None
    # ordering-independence: note_teleport (from the leader's own landing render) must not
    # clobber the pos note_flee already set, whichever fires first.
    fm.note_teleport()
    assert fm.cur == 12, "note_teleport leaves the flee-synced pos intact"
    # a fled direction with no edge -> stay put (let the 아래 landmark re-sync fix it), off_edge++
    before = fm.dbg["off_edge"]
    fm.note_flee("동")                            # 12 has no 동 edge
    assert fm.cur == 12 and fm.dbg["off_edge"] == before + 1
    print("note_flee: syncs pos along the fled edge, no pending armed, off-edge stays put")


def test_snapshot_shape_for_ui():
    fm = new()
    fm.mark_swept()
    snap = fm.snapshot()
    assert snap["rooms"] == 41 and len(snap["frames"]) == 1
    cells = snap["frames"][0]["cells"]
    cur = [c for c in cells if c["cur"]]
    assert len(cur) == 1 and cur[0]["rid"] == 20, "cur marks the current room"
    assert any(c["swept"] for c in cells), "swept flag present"
    print("snapshot: one planar frame, cur + swept flags, UI-ready")


def test_multi_matrix_and_portals():
    """Several matrices joined by explicit BOTH-WAYS portals form one navigable graph; the
    snapshot yields one frame per matrix and lists the portals."""
    d = {"start": 0,
         "matrix": [{"name": "1층", "grid": ["00 01", "02 03"]},
                    {"name": "2층", "grid": ["10 11"]}],
         "portal": [{"from": 3, "from_dir": "위", "to": 10, "to_dir": "아래"}]}
    fm = FixedMap(d)
    assert set(fm.coord) == {0, 1, 2, 3, 10, 11}
    assert fm.edges[3]["위"] == 10 and fm.edges[10]["아래"] == 3, "portal is bidirectional"
    seen = {0}; q = deque([0])
    while q:
        x = q.popleft()
        for nb in fm.edges[x].values():
            if nb not in seen:
                seen.add(nb); q.append(nb)
    assert seen == set(fm.coord), "the portal connects the two matrices into one graph"
    snap = fm.snapshot()
    assert len(snap["frames"]) == 2 and len(snap["portals"]) == 1
    print("multi-matrix + portals: one graph across floors, one frame each")


def test_wide_room_numbers_and_markers():
    """Room numbers of ANY digit count parse (column = token position), and the empty/centre
    markers work at any width (. / .. / ... and XX / XXX)."""
    d = {"start": 100, "matrix": [{"name": "t", "grid": [
        "099 100 101",
        "102 XXX 103",     # XXX = conditional centre (excluded from edges)
        "104 ... 106",     # ... = empty (any run of dots)
    ]}]}
    fm = FixedMap(d)
    assert sorted(fm.coord) == [99, 100, 101, 102, 103, 104, 106], f"got {sorted(fm.coord)}"
    assert fm.edges[100] == {"서": 99, "동": 101}, f"100 edges wrong: {fm.edges[100]}"
    assert fm.edges[102] == {"북": 99, "남": 104}, f"102 edges wrong: {fm.edges[102]}"
    assert "남" not in fm.edges[100], "no edge INTO the conditional (XXX) centre"
    assert "동" not in fm.edges[104], "no edge from 104 into the empty (...) cell"
    print("wide numbers: 3-digit rooms + ./../... empty + XX/XXX centre all parse")


def test_loader_reads_folder_and_ignores_example():
    """The hunting_maps loader reads real map files and skips _example.toml; the 올림푸스 신전
    map loads with its route and builds a valid FixedMap."""
    from gumiho import hunting_maps
    maps = hunting_maps.load()
    assert "올림푸스 신전" in maps, "the 올림푸스 신전 map file must load"
    assert "예시 사냥터" not in maps, "_example.toml must be ignored"
    d = maps["올림푸스 신전"]
    assert d["route"]["steps"] and d["start"] == 20
    fm = FixedMap(d)
    assert len(fm.coord) == 41 and fm.cur == 20
    print("loader: reads map folder, ignores _example, builds FixedMap from a real file")


if __name__ == "__main__":
    test_topology_all_rooms_connected_no_xx()
    test_deterministic_move_tracking()
    test_full_sweep_visits_every_room()
    test_desync_recovery_landmark_and_block()
    test_walls_persist_across_moves()
    test_render_never_advances_pos()
    test_refusal_rolls_back()
    test_door_on_portal_gates_both_ends()
    test_sealed_region_is_barren_not_oscillating()
    test_resync_exit_can_be_disabled()
    test_note_flee_advances_pos_without_arming_pending()
    test_multi_matrix_and_portals()
    test_wide_room_numbers_and_markers()
    test_loader_reads_folder_and_ignores_example()
    test_snapshot_shape_for_ui()
    print("\nALL FIXEDMAP TESTS PASSED")
