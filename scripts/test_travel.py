"""Travel / return-to-zone cycle: the robust path from town back into the hunt zone.

Reproduces the failures that made the cycle 'very very broken':
  1. At the recall anchor (중앙 광장) the engine spammed 귀환, each failing with
     "이동력이 없어서" (no move points) — recall needs MV, and we're already home.
  2. Nothing recovered MV or gated travel on it, so route replay burned all 39 steps
     WITHOUT MOVING (every direction failed on empty MV) and dumped the party into
     `hunting` still standing in town -> 게시판 attacks / 자-깨 loop.
  3. `hunting` would run in a town room at all (it must only run IN the zone).
  4. A manually-handled death left `state.dead` latched, so the arbiter force-entered
     the death workflow forever.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import events as ev
from gumiho.access import CharCtx
from gumiho.command import Cmd
from gumiho.director import Director
from gumiho.engine import Engine
from gumiho.reload import get_knowledge, Reloader
from gumiho.state import WorldState

CLOCK = [1000.0]
def now(): return CLOCK[0]
def tick_clock(dt=1.0): CLOCK[0] += dt


ANCHOR = "중앙 광장"
ZONE_ROUTE = ["북", "동", "남"]          # a short recorded route into the zone


def _load():
    Reloader(lambda: []).load_all()
    from behavior.hooks import travel, resupply, party, hunting, common  # noqa: F401
    kn = get_knowledge()
    kn.setdefault("travel", {})
    kn["travel"].update({"min_mv": 150, "min_mv_step": 10,
                         "town_rooms": [ANCHOR, "치료실", "광장 사거리",
                                        "대장간", "한성 떡집", "기증의 방"]})
    kn.setdefault("recall", {})["anchor_room"] = ANCHOR
    kn.setdefault("travel_routes", {})["tz"] = list(ZONE_ROUTE)
    return kn


def _mk(d, sid, role, name, partner, kn, *, hp=(3000, 3000), mp=(100, 100),
        mv=(400, 400), room=None):
    st = WorldState()
    st.apply(ev.Status(hp=hp, mp=mp, mv=mv, job="전사"))
    if room is not None:
        st.room_title = room
    ctx = CharCtx(sid, role, st, kn, set(), name=name, now=now)
    ctx.partner_name = partner
    sent = []
    eng = Engine(ctx, Cmd(sent.append), director=d, now=now)
    d.register(sid, eng)
    return ctx, sent, eng


def _set_mv(ctx, cur, mx=400):
    ctx.state.apply(ev.Status(hp=(3000, 3000), mp=(100, 100), mv=(cur, mx), job="전사"))


def _boot(eng, ctx, workflow, sub, target="tz"):
    eng.enabled = True
    if target is not None:
        ctx.hunt_target = target
    eng.enter_workflow(workflow)
    ctx.cursor.sub = sub
    ctx.cursor.entered_at = now()
    eng._run_on_enter(workflow, sub)


def _run(pairs, n=30, stop=None):
    """Tick each engine n times (advancing the clock), stop early on `stop()`."""
    for _ in range(n):
        tick_clock(2.0)
        for ctx, sent, eng in pairs:
            ctx.state.at_prompt = True
            eng.tick()
        if stop and stop():
            break


# --- 1. no 귀환 spam at the anchor when MV is exhausted ----------------------
def test_no_recall_spam_at_anchor_when_mv_low():
    kn = _load()
    d = Director()
    A = _mk(d, "a", "leader", "A", "B", kn, mv=(20, 400), room=ANCHOR)
    B = _mk(d, "b", "supporter", "B", "A", kn, mv=(20, 400), room=ANCHOR)
    A[0].partner, B[0].partner = B[0], A[0]
    _boot(A[2], A[0], "travel", "to_anchor")
    _boot(B[2], B[0], "travel", "to_anchor")

    _run([A, B], n=8)
    # Already home + no MV: we must NOT fire a single (doomed) 귀환 ...
    assert "귀환" not in A[1], A[1]
    assert "귀환" not in B[1], B[1]
    # ... we drop into MV recovery and sleep instead.
    assert A[0].cursor.sub == "recover_mv", A[0].cursor.sub
    assert "자" in A[1], A[1]
    print("no recall spam: at anchor with MV=20 -> recover_mv (자), zero 귀환")


# --- 2. recover MV, THEN walk the route into the zone (no burning steps) -----
def test_recovers_mv_then_walks_route():
    kn = _load()
    d = Director()
    A = _mk(d, "a", "leader", "A", "B", kn, mv=(20, 400), room=ANCHOR)
    B = _mk(d, "b", "supporter", "B", "A", kn, mv=(20, 400), room=ANCHOR)
    A[0].partner, B[0].partner = B[0], A[0]
    _boot(A[2], A[0], "travel", "to_anchor")
    _boot(B[2], B[0], "travel", "to_anchor")

    # sleep phase: both reach recover_mv without walking anything
    _run([A, B], n=6, stop=lambda: A[0].cursor.sub == "recover_mv")
    assert A[0].cursor.sub == "recover_mv"
    assert not any(s in ZONE_ROUTE for s in A[1]), "walked the route on empty MV!"

    # MV regenerates -> release the sleep, wake, regroup, and NOW walk the route.
    # Follow-based travel: the supporter must have its 따라 active (group_formed) so
    # the leader can lead the walk and drag it along.
    _set_mv(A[0], 400); _set_mv(B[0], 400)
    A[0].together = B[0].together = True
    B[0].following = True
    _run([A, B], n=40, stop=lambda: not A[0].cursor.scratch.get("route", [1]))

    assert "귀환" not in A[1], "recalled from the anchor (pointless)"
    assert [s for s in A[1] if s in ZONE_ROUTE] == ZONE_ROUTE, A[1]
    print("recover-then-walk: slept to MV, woke, walked", ZONE_ROUTE, "— never on empty MV")


# --- 3. a route step never burns on empty MV --------------------------------
def test_route_step_waits_when_mv_below_floor():
    kn = _load()
    d = Director()
    ctx, sent, eng = _mk(d, "a", "leader", "A", None, kn, mv=(5, 400))  # solo, MV=5
    _boot(eng, ctx, "travel", "run_route")
    assert ctx.cursor.scratch["route"] == list(ZONE_ROUTE)

    # (a) run_route as a whole: low MV routes to recovery, never walks a step
    ctx.state.at_prompt = True
    eng.tick()
    assert not any(s in ZONE_ROUTE for s in sent), f"burned a route step on MV=5! {sent}"
    assert ctx.cursor.sub == "recover_mv", ctx.cursor.sub

    # (b) the step-floor itself, in isolation (bypass transitions): route stays intact
    _boot(eng, ctx, "travel", "run_route")
    sent.clear()
    eng.run_action("route_step")
    assert sent == [], f"step burned on MV=5: {sent}"
    assert ctx.cursor.scratch["route"] == list(ZONE_ROUTE), "route consumed on empty MV"

    _set_mv(ctx, 400)
    eng.run_action("route_step")
    assert sent == ["북"], sent
    print("route step: waits on MV=5 (no burn), walks once MV restored")


def test_route_started_at_threshold_completes_without_restart():
    """Regression: a route STARTED at exactly min_mv must run to the end. Moves cost
    ~1 MV, so the first step drops us just under the start-threshold — the mid-route
    bail must NOT trip on that (the old bug would restart the route every step)."""
    kn = _load()
    d = Director()
    # solo, MV exactly at the start threshold; each simulated step spends 1 MV
    ctx, sent, eng = _mk(d, "a", "leader", "A", None, kn, mv=(150, 400))
    _boot(eng, ctx, "travel", "run_route")
    entered = []
    for _ in range(12):
        ctx.state.at_prompt = True
        before = len(sent)
        eng.tick()
        entered.append(ctx.cursor.sub)
        # spend 1 MV per actual move (mirror the server)
        walked = [c for c in sent[before:] if c in ZONE_ROUTE]
        if walked:
            _set_mv(ctx, ctx.mv - len(walked), 400)
        if ctx.cursor.sub == "arrive":
            break
    assert "recover_mv" not in entered, f"restarted mid-route: {entered}"
    assert ctx.cursor.sub == "arrive", ctx.cursor.sub
    assert [s for s in sent if s in ZONE_ROUTE] == ZONE_ROUTE, sent
    print("route-at-threshold: 150 MV start walks the whole route, never restarts")


# --- 3b. CONFIRM-BEFORE-ADVANCE: a refused move must NOT skip the rest of the route --
def _land(ctx, title="어딘가"):
    """Server confirms the last move landed: a fresh room render clears last_move_blocked."""
    ctx.state.apply(ev.RoomSeen(title=title, description="", exits=["북"], entities=[]))

def _refuse(ctx):
    """Server refuses the last move: 그쪽으로는 갈 수 없습니다 -> last_move_blocked latched."""
    ctx.state.apply(ev.CantGo())


def test_refused_step_retries_then_advances_on_confirm():
    """The 주막집 bug: a route step was popped+sent blindly, so a refused move was skipped
    and the rest of the route walked from the WRONG room. Now a refusal RETRIES the same
    step and only a confirmed landing advances the route."""
    kn = _load()
    d = Director()
    ctx, sent, eng = _mk(d, "a", "leader", "A", None, kn, mv=(400, 400))
    _boot(eng, ctx, "travel", "run_route")
    assert ctx.cursor.scratch["route"] == list(ZONE_ROUTE)
    sent.clear()                                                       # drop boot on_enter noise

    eng.run_action("route_step"); assert sent == ["북"], sent          # send step 1
    _refuse(ctx)                                                       # server: can't go north
    eng.run_action("route_step"); assert sent == ["북", "북"], sent    # RETRY the same step
    assert ctx.cursor.scratch["route"] == ["동", "남"], "route advanced past a refusal!"
    _land(ctx)                                                         # now it lands
    eng.run_action("route_step"); assert sent == ["북", "북", "동"], sent  # advance to step 2
    assert ctx.cursor.scratch["route"] == ["남"]
    print("confirm-then-advance: refused move is retried, not skipped; lands -> advances")


def test_persistent_refusal_derails_and_rerecalls():
    """A move refused past max_step_retries marks the walk derailed -> run_route re-recalls
    to the anchor (deterministic restart) instead of finishing the route in the wrong room."""
    kn = _load()
    cap = kn["travel"]["max_step_retries"] = 3
    d = Director()
    ctx, sent, eng = _mk(d, "a", "leader", "A", None, kn, mv=(400, 400), room="어느 길목")
    _boot(eng, ctx, "travel", "run_route")
    sent.clear()

    eng.run_action("route_step")                          # send step 1
    for _ in range(cap):                                  # every retry is refused
        _refuse(ctx); eng.run_action("route_step")
    assert not ctx.cursor.scratch.get("route_derailed"), "derailed before exhausting retries"
    _refuse(ctx); eng.run_action("route_step")            # one past the cap
    assert ctx.cursor.scratch.get("route_derailed"), "past the cap must mark derailed"

    # the guard fires the transition: run_route -> to_anchor (re-recall & re-walk)
    from behavior.hooks.travel import route_derailed
    assert route_derailed(ctx)
    ctx.state.at_prompt = True
    eng.tick()
    assert ctx.cursor.sub == "to_anchor", ctx.cursor.sub
    print(f"derail: >{cap} refusals -> route_derailed -> re-recall to anchor")


def test_open_gate_step_advances_without_a_render():
    """A '<gate> 열' step doesn't move us and never renders a new room — it must be advanced
    past on the next prompt (not stuck waiting for a landing that never comes)."""
    kn = _load()
    kn["travel_routes"]["gz"] = ["북", "남대 열", "남"]
    d = Director()
    ctx, sent, eng = _mk(d, "a", "leader", "A", None, kn, mv=(400, 400))
    _boot(eng, ctx, "travel", "run_route", target="gz")
    sent.clear()

    eng.run_action("route_step"); _land(ctx)              # 북 lands
    eng.run_action("route_step")                          # -> sends '남대 열'
    assert sent[-1] == "남대 열", sent
    eng.run_action("route_step")                          # no render for 열, but advance anyway
    assert sent[-1] == "남", sent
    assert ctx.cursor.scratch["route"] == [], "gate step blocked the walk"
    print("open-gate step: advances without a room render (not treated as a failed move)")


# --- 4. hunting must never run in a town room -> route back to travel --------
def test_hunting_in_town_switches_to_travel():
    kn = _load()
    d = Director()
    ctx, sent, eng = _mk(d, "a", "leader", "A", None, kn, room=ANCHOR)
    ctx.hunt_target = "tz"
    _boot(eng, ctx, "hunting", "regroup")

    ctx.state.at_prompt = True
    eng.tick()
    assert ctx.cursor.workflow == "travel", ctx.cursor.workflow
    assert "자" not in sent, "started the 자-깨 loop in town"
    print("hunting-in-town: 중앙 광장 -> switches to travel (no 자-깨)")


def test_hunting_in_zone_proceeds_normally():
    """Guard against the opposite failure: a real hunt-zone room must NOT be mistaken
    for town (which would make travel<->hunting loop forever)."""
    kn = _load()
    d = Director()
    ctx, sent, eng = _mk(d, "a", "leader", "A", None, kn, room="이상한 나라")
    _boot(eng, ctx, "hunting", "regroup", target="이상한 나라 1층")
    ctx.state.at_prompt = True
    eng.tick()
    assert ctx.cursor.workflow == "hunting", ctx.cursor.workflow
    print("hunting-in-zone: a real zone room is not town -> stays hunting")


# --- 5. a manually-handled death releases the latched dead flag --------------
def test_dead_flag_self_clears_on_life_signal():
    for signal in (ev.ExpGain(amount=5), ev.EnemyDown(),
                   ev.CombatHit(direction="dealt", other="뱀", line="x")):
        st = WorldState()
        st.apply(ev.PlayerDead())
        assert st.dead, "PlayerDead should latch dead"
        st.apply(signal)
        assert not st.dead, f"life signal {type(signal).__name__} must clear dead"
    print("dead flag: self-clears on exp/kill/hit (manual recovery no longer sticks)")


if __name__ == "__main__":
    test_no_recall_spam_at_anchor_when_mv_low()
    test_recovers_mv_then_walks_route()
    test_route_step_waits_when_mv_below_floor()
    test_route_started_at_threshold_completes_without_restart()
    test_refused_step_retries_then_advances_on_confirm()
    test_persistent_refusal_derails_and_rerecalls()
    test_open_gate_step_advances_without_a_render()
    test_hunting_in_town_switches_to_travel()
    test_hunting_in_zone_proceeds_normally()
    test_dead_flag_self_clears_on_life_signal()
    print("\nALL TRAVEL TESTS PASSED")
