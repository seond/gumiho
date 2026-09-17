"""Travel to the hunt zone from town (used to return after a resupply run).

Recall to the 중앙 광장 anchor, then replay a recorded command route to the hunt
zone (directions plus any specials like 맨홀 열 / 아래), regroup, and hand off to
hunting. The route lives in knowledge [travel_routes] keyed by the hunt zone —
record it once; without it, travel fails cleanly instead of wandering.

Recall / party hooks are shared (defined in resupply.py / party.py).
"""

from gumiho.registry import action, guard, step


# --- move points: the resource both walking AND 귀환 spend ---------------------
# Travel used to ignore MV entirely: it would 귀환 on empty (the server refuses with
# "이동력이 없어서" and we'd spam it), then replay the whole route while every step
# silently failed — landing us in `hunting` still standing in town. So MV is now a
# first-class gate: rest to recover it before recalling / walking, and never burn a
# route step we can't actually take.

def _is_move(step: str) -> bool:
    """True for a route step that actually changes rooms (so a fresh render confirms it,
    and a refusal means we stayed put). The '<gate> 열' open-commands don't move us and
    never render a new room, so they're advanced past on the next prompt regardless."""
    return "열" not in step and " " not in step


def _min_mv(s):
    return s.knowledge.get("travel", {}).get("min_mv", 150)


def _recall_min_mv(s):
    return s.knowledge.get("travel", {}).get("recall_min_mv", 30)


@guard("cant_recall")
def cant_recall(s):
    """MV too low to even 귀환 — a short rest is unavoidable (else 귀환 spam-fails with
    '이동력이 없어서 귀환할 수 없습니다'). Below the recall floor, NOT the 150 walk gate."""
    return s.mv < _recall_min_mv(s)


@guard("can_recall")
def can_recall(s):
    """Enough MV to 귀환 again (this character only) — releases a rest that was regenerating
    MV for a recall."""
    return s.mv >= _recall_min_mv(s)


@guard("mv_low")
def mv_low(s):
    """Not enough move points (ours OR the partner's) to travel — rest first."""
    if s.mv < _min_mv(s):
        return True
    return s.partner is not None and s.partner.mv < _min_mv(s)


@guard("mv_ready")
def mv_ready(s):
    """Both characters have the move points to recall + walk the route."""
    if s.mv < _min_mv(s):
        return False
    return s.partner is None or s.partner.mv >= _min_mv(s)


@guard("mv_stuck")
def mv_stuck(s):
    """Genuinely out of move points mid-route — can't take even one more step. This
    is the ONLY mid-route bail: it must NOT reuse `mv_low` (the pre-travel start
    threshold), or the very first step would drop us below it and restart the route."""
    floor = s.knowledge.get("travel", {}).get("min_mv_step", 10)
    if s.mv < floor:
        return True
    return s.partner is not None and s.partner.mv < floor


@guard("at_town")
def at_town(s):
    """In a town / anchor room (never a huntable place). Uses the always-fresh room
    title, not the 지도-only `zone` (which is stale right after a recall)."""
    t = s.room_title or ""
    rooms = s.knowledge.get("travel", {}).get(
        "town_rooms", ["중앙 광장", "치료실"])
    return any(r in t for r in rooms)


@action("travel_recall")
def travel_recall(s, cmd):
    """Recall to the anchor — but skip a 귀환 that is pointless (already home) or
    doomed (no MV: it just fails with "이동력이 없어서"). The travel transitions then
    route us to reform / recover_mv instead of spamming a dead command."""
    anchor = s.knowledge.get("recall", {}).get("anchor_room", "중앙 광장")
    if s.room_title and anchor in s.room_title:
        return                          # genuinely standing at the anchor already
    if s.mv < _recall_min_mv(s):        # too drained to even recall — cant_recall rests us
        return
    # NOTE: no `if s.together: return` — a STALE together (an idle follower never re-renders)
    # used to skip the recall and strand us. If we're not at the anchor, recall; the landing
    # reunites us there. retry_recall keeps trying and 봐s to keep at_anchor honest.
    cmd.send("귀환")
    s.together = False                  # recall teleports — co-location unknown til re-seen
    s.cursor.scratch["tries"] = 1
    s.cursor.scratch["last_try"] = s.since_entered()


@guard("group_formed")
def group_formed(s):
    """Ready to travel as a group, so the leader can start walking and the server will
    drag the supporter along. Requires: co-located, the supporter's 따라 active, AND the
    supporter itself READY at the anchor — meaning it has reached reform/run_route,
    i.e. it recalled AND cleared its OWN MV gate (not still recalling or asleep
    recovering MV). That last check is on the supporter's STATE, deliberately NOT a
    cross-engine MV read: the two engines tick independently, so the partner's MV
    number lags a tick — the bug where the leader saw stale MV≥150, walked, and left
    the follower asleep at 133. A state is only entered once its own guards passed, so
    it can't be stale that way. Solo: trivially ready."""
    if not s.partner_name:
        return True
    if not s.together:
        return False
    sup = s if s.role == "supporter" else s.partner
    if sup is None or not sup.following:
        return False
    return sup.cursor.sub in ("reform", "run_route")


@action("load_hunt_route")
def load_hunt_route(s, cmd):
    zone = s.hunt_zone()               # the destination parameter for this run
    routes = s.knowledge.get("travel_routes", {})
    # A hunting map names its own route; otherwise fall back to a route keyed by the zone
    # (legacy / recorded). Handles specials like 동대문 열 / 아래. Only the LEADER walks it;
    # the supporter is carried by 따라 (loading it for the supporter is harmless).
    hmap = s.knowledge.get("hunting_maps", {}).get(zone or "")
    rname = (hmap.get("route", {}).get("name") if hmap else None) or zone
    route = routes.get(rname) or routes.get(zone or "")
    s.cursor.scratch["route"] = list(route) if route else []
    s.cursor.scratch["no_route"] = not route
    # confirm-before-advance bookkeeping (see route_step) — a fresh walk starts clean
    s.cursor.scratch["_route_sent"] = None
    s.cursor.scratch["_route_retries"] = 0
    s.cursor.scratch["route_derailed"] = False


@step("route_step")
def route_step(s, cmd):
    """FOLLOW-BASED travel. The LEADER walks the recorded route, one step per prompt.
    The SUPPORTER sends NOTHING — a group follower is dragged along by the server
    through EVERY step (plain moves, 동대문 열, 아래 manholes — all of it), so the only
    thing that can strand it is running out of MV, which the reform MV-gate prevents.
    (This replaced the old lockstep where BOTH walked their own route copy and desynced
    into different rooms — the server's 따라 IS the sync.)"""
    if s.role != "leader":
        # Carried by 따라. Re-affirm the follow only if we've lost co-location (a
        # self-heal; the server ignores a redundant 따라). Never walk the route.
        if s.partner_name and not s.together:
            cmd.send(f"{s.partner_name} 따라")
        else:
            # Keep co-location FRESH. A follower that just sits waiting to be dragged never
            # re-renders its room, so a `together` that was true when the leader left goes
            # STALE-true and strands us here forever (the 중앙 광장 deadlock). A throttled 봐
            # re-renders the room: if the leader isn't actually here, `together` flips false
            # and the run_route `separated` transition recalls us to regroup.
            last = s.cursor.scratch.get("_look_at", -1e9)
            if s.since_entered() - last >= 4.0:
                s.cursor.scratch["_look_at"] = s.since_entered()
                cmd.send("봐")
        return
    route = s.cursor.scratch.get("route")
    if not route:
        return
    # Out of MV: don't send a move that will silently fail — the run_route mv_stuck
    # transition rests us. (Pre-gated at reform, so this is a rare safety net.)
    if s.mv < s.knowledge.get("travel", {}).get("min_mv_step", 10):
        return
    # CONFIRM-THEN-ADVANCE. route_step used to pop+send every step blindly, so a single
    # REFUSED move ("그쪽으로는 갈 수 없습니다") was skipped and the rest of the deterministic
    # route walked from the WRONG room — silently derailing the trip and dumping us wherever
    # the leftover steps happened to land (the 주막집 0-kill loop). Now, before advancing, we
    # check the LAST move step we sent: a refusal (last_move_blocked still set — no fresh room
    # render cleared it) means we didn't move, so RETRY the same step instead of walking on.
    # A persistent refusal sets route_derailed -> re-recall to the anchor and re-walk. ('열'
    # gate-opens never move/render us, so they're never retried — see _is_move.)
    sc = s.cursor.scratch
    last = sc.get("_route_sent")
    if last is not None and _is_move(last) and s.state.last_move_blocked:
        tries = sc.get("_route_retries", 0) + 1
        sc["_route_retries"] = tries
        if tries > s.knowledge.get("travel", {}).get("max_step_retries", 4):
            sc["route_derailed"] = True          # off the path -> deterministic restart
            return
        cmd.send(last)                           # retry the refused step; don't advance the route
        return
    d = route.pop(0)                             # landed (or first step / a '열') -> next step
    sc["_route_sent"] = d
    sc["_route_retries"] = 0
    cmd.send(d)                                  # a direction or a special (동대문 열 / 아래 …)
    s.flow.setdefault("trail", []).append(d)


@guard("route_done")
def route_done(s):
    # The LEADER (or a solo char) is done when it has walked the whole route. The
    # carried SUPPORTER never finishes its own route — the leader's arrive→hunting
    # sync-switch pulls it into hunting, so its route_done need never fire.
    if s.role != "leader":
        return False
    return not s.cursor.scratch.get("route")


@guard("route_derailed")
def route_derailed(s):
    """A route MOVE was refused past the retry cap — the walk has fallen off the
    recorded path. Re-recall to the anchor and re-walk from a known position instead
    of finishing the route in the wrong room (the 주막집 dump)."""
    return bool(s.cursor.scratch.get("route_derailed"))


@guard("no_route")
def no_route(s):
    return bool(s.cursor.scratch.get("no_route"))


@action("fail_travel")
def fail_travel(s, cmd):
    zone = s.hunt_zone()
    s.last_error = f"사냥터 이동 경로 미설정: {zone} — [travel_routes] 에 기록 필요"
    s.request_stop = True
