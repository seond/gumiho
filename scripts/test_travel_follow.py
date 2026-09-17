"""Follow-based travel: the LEADER walks the recorded route; the SUPPORTER is dragged
along by the server via 따라 and never walks its own route. (This replaced the old
lockstep where both walked their own copy and desynced into different rooms.)"""

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

ROUTE = ["북", "동", "동대문 열", "동", "아래"]      # incl. a gate + a manhole special


def _load():
    Reloader(lambda: []).load_all()
    from behavior.hooks import travel, party, common, hunting  # noqa: F401
    kn = get_knowledge()
    kn.setdefault("travel_routes", {})["tz"] = list(ROUTE)
    return kn


def _mk(d, sid, role, name, partner, kn):
    st = WorldState()
    st.apply(ev.Status(hp=(400, 400), mp=(100, 100), mv=(400, 400), job="전사"))
    ctx = CharCtx(sid, role, st, kn, set(), name=name, now=now)
    ctx.partner_name = partner
    sent = []
    eng = Engine(ctx, Cmd(sent.append), director=d, now=now)
    d.register(sid, eng)
    return ctx, sent, eng


def _enter_run_route(eng, ctx):
    eng.enabled = True
    ctx.hunt_target = "tz"
    eng.enter_workflow("travel")
    ctx.cursor.sub = "run_route"
    ctx.cursor.entered_at = now()
    eng._run_on_enter("travel", "run_route")


# --- leader walks the whole route; supporter is carried (sends nothing) ------
def test_leader_walks_supporter_carried():
    kn = _load()
    d = Director()
    A = _mk(d, "a", "leader", "A", "B", kn)
    B = _mk(d, "b", "supporter", "B", "A", kn)
    A[0].partner, B[0].partner = B[0], A[0]
    B[0].following = True                          # 따라 established at reform
    for ctx, sent, eng in (A, B):
        _enter_run_route(eng, ctx)

    walked, sup_sent = [], []
    for _ in range(20):
        for sid, (ctx, sent, eng) in (("a", A), ("b", B)):
            ctx.state.at_prompt = True
            ctx.together = True                    # carried: co-located every step
            before = len(sent)
            eng.tick()
            new = [c for c in sent[before:] if c != "점수"]
            if sid == "a":
                walked += [c for c in new if c in ROUTE]
            else:
                sup_sent += new
        if not A[0].cursor.scratch["route"]:
            break

    assert walked == ROUTE, walked                 # leader walked EVERY step (incl. specials)
    # supporter is CARRIED: it may emit a throttled 봐 (keeps together/at_anchor honest — the
    # anti-deadlock refresh) but must NEVER navigate a route step itself.
    assert all(c == "봐" for c in sup_sent), sup_sent
    assert not any(c in ROUTE for c in sup_sent), sup_sent
    print("follow travel: leader walked", ROUTE, "— supporter carried, never navigated")


# --- supporter re-affirms 따라 only if it loses co-location -------------------
def test_supporter_regrabs_follow_when_separated():
    kn = _load()
    d = Director()
    A = _mk(d, "a", "leader", "A", "B", kn)
    ctxB, sentB, engB = _mk(d, "b", "supporter", "B", "A", kn)
    ctxB.partner = A[0]
    _enter_run_route(engB, ctxB)
    ctxB.following = True
    ctxB.together = False                          # lost the leader
    ctxB.state.at_prompt = True
    engB.tick()
    assert "A 따라" in sentB, sentB                 # re-grab the follow, never a route step
    assert not any(c in ROUTE for c in sentB), sentB
    assert ctxB.cursor.scratch["route"] == list(ROUTE), "supporter route never consumed"
    print("supporter: re-grabs 따라 when co-location drops, never walks the route")


# --- group_formed: 따라 active + co-located + supporter READY at the anchor ---
def test_group_formed_guard():
    from behavior.hooks.travel import group_formed
    from gumiho.access import Cursor
    kn = _load()
    d = Director()
    A = _mk(d, "a", "leader", "A", "B", kn)
    B = _mk(d, "b", "supporter", "B", "A", kn)
    A[0].partner, B[0].partner = B[0], A[0]
    A[0].cursor = Cursor(workflow="travel", sub="reform")
    B[0].cursor = Cursor(workflow="travel", sub="reform")   # supporter ready at the anchor

    A[0].together = B[0].together = True
    B[0].following = False
    assert group_formed(A[0]) is False, "not ready until the supporter is following"
    B[0].following = True
    assert group_formed(A[0]) is True, "co-located + following + supporter in reform -> ready"
    B[0].cursor.sub = "recover_mv"      # supporter fell into MV recovery
    assert group_formed(A[0]) is False, "leader must WAIT while the supporter recovers MV"
    B[0].cursor.sub = "to_anchor"       # supporter still recalling
    assert group_formed(A[0]) is False, "leader must WAIT while the supporter is still recalling"
    B[0].cursor.sub = "run_route"
    assert group_formed(A[0]) is True, "supporter past the gate (run_route) -> ready"
    A[0].together = False
    assert group_formed(A[0]) is False, "must see the supporter"

    solo = _mk(d, "c", "leader", "C", None, kn)[0]
    assert group_formed(solo) is True, "solo is trivially ready"
    print("group_formed: true only when co-located AND supporter's 따라 is active")


# --- the leader WAITS for a follower that's recovering MV (no walk-off) -------
def test_leader_waits_while_follower_recovers_mv():
    """Repro of the live bug: the follower recalled with MV=133 (< travel floor) and
    slept to recover; the leader (stale partner-MV read) walked the whole route and
    abandoned it. The leader must stay in reform until the follower is READY."""
    kn = _load()
    d = Director()
    A = _mk(d, "a", "leader", "A", "B", kn)
    B = _mk(d, "b", "supporter", "B", "A", kn)
    A[0].partner, B[0].partner = B[0], A[0]
    B[0].following = True

    # leader ready in reform; follower asleep recovering MV, co-located at the anchor
    A[2].enabled = True; A[0].hunt_target = "tz"
    A[2].enter_workflow("travel"); A[0].cursor.sub = "reform"; A[0].cursor.entered_at = now()
    A[2]._run_on_enter("travel", "reform")
    B[2].enabled = True; B[0].hunt_target = "tz"
    B[2].enter_workflow("travel"); B[0].cursor.sub = "recover_mv"

    sentA = A[1]
    for _ in range(10):
        A[0].state.at_prompt = True
        A[0].together = True                       # follower is right here, just asleep
        A[2].tick()
    assert not any(c in ROUTE for c in sentA), f"leader walked off while follower rested: {sentA}"
    assert A[0].cursor.sub != "run_route", "leader must not enter run_route yet"

    # follower finishes recovering and reaches reform -> NOW the leader may walk
    B[0].cursor.sub = "reform"
    A[0].state.at_prompt = True; A[0].together = True
    A[2].tick()
    assert A[0].cursor.sub == "run_route", "once the follower is ready, the leader walks"
    print("leader waits: no walk-off while follower recovers MV; walks once it's ready")


# --- route_done: leader on empty route; supporter never on its own -----------
def test_route_done_roles():
    from behavior.hooks.travel import route_done
    kn = _load()
    d = Director()
    A = _mk(d, "a", "leader", "A", "B", kn)
    B = _mk(d, "b", "supporter", "B", "A", kn)
    for ctx, sent, eng in (A, B):
        _enter_run_route(eng, ctx)
    assert route_done(A[0]) is False, "leader not done with a full route"
    A[0].cursor.scratch["route"] = []
    assert route_done(A[0]) is True, "leader done when route empty"
    B[0].cursor.scratch["route"] = []
    assert route_done(B[0]) is False, "supporter never finishes its own route (pulled by sync)"
    print("route_done: leader on empty route; supporter deferred to the leader's sync-switch")


# --- solo still walks --------------------------------------------------------
def test_solo_walks():
    kn = _load()
    d = Director()
    ctx, sent, eng = _mk(d, "a", "leader", "A", None, kn)   # no partner
    _enter_run_route(eng, ctx)
    for _ in range(12):
        ctx.state.at_prompt = True
        eng.tick()
        if not ctx.cursor.scratch["route"]:
            break
    assert [c for c in sent if c in ROUTE] == ROUTE, sent
    print("solo: leader walks the route with no partner")


if __name__ == "__main__":
    test_leader_walks_supporter_carried()
    test_supporter_regrabs_follow_when_separated()
    test_group_formed_guard()
    test_leader_waits_while_follower_recovers_mv()
    test_route_done_roles()
    test_solo_walks()
    print("\nALL FOLLOW-TRAVEL TESTS PASSED")
