"""Shared guards and actions: survival, posture, spell support."""

from gumiho.registry import action, guard


@action("noop")
def noop(s, cmd):
    pass


@guard("always")
def always(s):
    return True


@guard("is_supporter")
def is_supporter(s):
    return s.role == "supporter"


# --- vitals guards -----------------------------------------------------------

@guard("hp_below")
def hp_below(s, arg):
    return s.hp_pct() < float(arg)


@guard("mp_below")
def mp_below(s, arg):
    return s.mp_pct() < float(arg)


@guard("standing")
def standing(s):
    return s.standing()


# Generic timeout: true once the character has been in the current state longer
# than N seconds. The building block for retries / giving up on stochastic steps.
@guard("stuck")
def stuck(s, arg):
    return s.since_entered() > float(arg)


@guard("recovered")
def recovered(s):
    return s.hp_pct() >= 90 and s.mp_pct() >= 90


# Drink only if we actually have the potion — otherwise fall through to resting.
@guard("should_drink_hp")
def should_drink_hp(s, arg):
    return s.hp_pct() < float(arg) and s.potion_count("hp") > 0


@guard("should_drink_mp")
def should_drink_mp(s, arg):
    return s.mp_pct() < float(arg) and s.potion_count("mp") > 0


# Field drink/eat throttles: wall-clock of the last 버드 마셔 / 시루떡 먹어, per
# character. Module-level so they survive across states/ticks and a hot-reload
# (same idiom as _REST_PENDING).
_LAST_DRINK = {}
_LAST_EAT = {}


@guard("should_field_drink")
def should_field_drink(s):
    """Thirsty in the field -> drink from our own bottle (버드 마셔) instead of
    hauling to town. Only when it's safe and sensible: bottle not dry, standing,
    no hostiles around (thirst isn't urgent — never drink mid-fight or asleep),
    and throttled so we send it once, not every tick until the server confirms."""
    if not s.state.thirsty or s.state.bottle_dry:
        return False
    if not s.standing() or s.hostiles():
        return False
    interval = s.knowledge.get("resupply", {}).get("field_drink_interval", 4.0)
    return s._now() - _LAST_DRINK.get(s.sid, -1e9) >= interval


def _has_food(s):
    """We carry the field food (시루떡) — the eat analog of 'bottle not dry'."""
    item = s.knowledge.get("resupply", {}).get("provision_item", "시루떡")
    return any(item in it for it in s.state.inventory)


@guard("should_field_eat")
def should_field_eat(s):
    """Hungry in the field -> eat our own 시루떡 (시루떡 먹어) instead of a town trip.
    Mirror of should_field_drink: only when we actually carry food, standing, no
    hostiles around, throttled. Hunger clears on the server's 배가 부릅니다 (parsed
    -> hungry=False), so it self-terminates; if we're out of food it falls through
    to needs_resupply, which restocks at 한성 떡집."""
    if not s.state.hungry or not _has_food(s):
        return False
    if not s.standing() or s.hostiles():
        return False
    interval = s.knowledge.get("resupply", {}).get("field_eat_interval", 4.0)
    return s._now() - _LAST_EAT.get(s.sid, -1e9) >= interval


# --- survival actions --------------------------------------------------------

def _held_potion(s, kind):
    """The first CONFIGURED potion of `kind` actually PRESENT in inventory (keyword match), or
    None. drink_* must consume what we HOLD: blindly using items[0] spammed '<missing> 복용' when
    the character held a DIFFERENT listed potion (e.g. has 불고기피자 but not 쑥 -> '쑥 복용' failed
    forever while should_drink_hp kept firing on the real potion it DID have)."""
    for p in s.knowledge.get("potions", {}).get(kind, []):
        if any(p in inv for inv in s.state.inventory):
            return p
    return None


@action("drink_hp_potion")
def drink_hp_potion(s, cmd):
    held = _held_potion(s, "hp")
    if held:
        cmd.send(f"{held} 복용")


@action("drink_mp_potion")
def drink_mp_potion(s, cmd):
    held = _held_potion(s, "mp")
    if held:
        cmd.send(f"{held} 복용")


@action("field_drink")
def field_drink(s, cmd):
    cmd.send(s.knowledge.get("resupply", {}).get("field_drink", "버드 마셔"))
    _LAST_DRINK[s.sid] = s._now()          # throttle; thirst clears on the server's
                                           # 목이 마르지 않습니다 (parsed -> thirsty=False)


@action("field_eat")
def field_eat(s, cmd):
    cmd.send(s.knowledge.get("resupply", {}).get("field_eat", "시루떡 먹어"))
    _LAST_EAT[s.sid] = s._now()            # throttle; hunger clears on 배가 부릅니다


@action("lie_down")
def lie_down(s, cmd):
    cmd.send("자")


@action("wake_up")
def wake_up(s, cmd):
    cmd.send("깨")
    cmd.send("일")


@action("flee")
def flee(s, cmd):
    cmd.send("도망")


# --- spell support (supporter side of the 말 protocol) -----------------------

@guard("has_spell_request")
def has_spell_request(s):
    return s.pending_spell is not None


@action("cast_requested_spell")
def cast_requested_spell(s, cmd):
    spell = s.pending_spell
    if spell and s.partner_name:
        cmd.send(f"{s.partner_name} {spell} 걸어")
    s.pending_spell = None


# --- keep up with a FLEEING leader (GENERAL — any hunt, not just 철면사냥) ----------------
# When the leader 도망s it leaves via a direction the supporter can read off the flee-departure
# line (webui sets ctx.pending_chase). A normal 따라/follow doesn't track a flee, so without this
# the pair separates whenever the leader bolts a fight — in ANY zone.
# NOTE: do NOT also gate on `not s.together`. A 도망 does not emit the standard "leaves the room"
# line, so the supporter's `together` flag stays STALE-TRUE after the leader bolts — that extra
# check silently blocked the chase (detection fired, supporter never moved). pending_chase is a
# reliable trigger on its own: it is set ONLY on a real flee-DEPARTURE and cleared on reunion
# (access.py clears it when `together` flips true on a fresh render with the partner present).
@guard("partner_fled")
def partner_fled(s):
    return s.role == "supporter" and getattr(s, "pending_chase", None) is not None


@action("chase_partner")
def chase_partner(s, cmd):
    d = getattr(s, "pending_chase", None)
    if d:
        cmd.send(d)                 # step the way the leader fled
    s.pending_chase = None          # one step per flee line; a further flee re-sets it, reunion clears it


# --- confirmed leg-walking (shared by resupply's town legs) ------------------
def _is_move(step):
    """A leg step that changes rooms (so a fresh render confirms it and a refusal means we stayed
    put). '<gate> 열' opens neither move nor render us, so they're advanced past, never retried."""
    return "열" not in step and " " not in step


def walk_leg(s, cmd, *, record_trail):
    """One CONFIRMED step of a recorded town leg (resupply). A blind pop+send — the old goto_step —
    walked past a REFUSED move and stranded the leg in the wrong room (the 주막집 resupply hang).
    Here a refused MOVE (last_move_blocked still set: no fresh render cleared it) is RETRIED, not
    skipped; a persistent block RE-ROUTES from the room we're actually in (graph nav), so a macro
    that no longer matches our position can't walk the rest off-course. The confirm-state is keyed
    on `dest`, so it resets cleanly at every new leg. record_trail logs steps for the way back."""
    sc = s.cursor.scratch
    dest = sc.get("dest")
    if sc.get("_leg_for") != dest:                       # a fresh leg -> reset the confirm state
        sc["_leg_for"] = dest
        sc["_leg_sent"] = None
        sc["_leg_tries"] = 0
    # 1) CONFIRM the last step FIRST (before the empty-route check), so even a refused FINAL step is
    #    retried rather than left as a false 'arrived' at the wrong room.
    last = sc.get("_leg_sent")
    if last is not None and _is_move(last) and s.state.last_move_blocked:
        tries = sc.get("_leg_tries", 0) + 1
        sc["_leg_tries"] = tries
        if tries > s.knowledge.get("travel", {}).get("max_step_retries", 4):
            # persistent block: the recorded macro doesn't match where we ACTUALLY are, so walking
            # its remaining steps just strays further. Re-route from the current room via the learned
            # graph (a self-heal); if we can't path, leave the route empty so the workflow's
            # stuck-escape recalls us rather than bouncing here.
            newr = s.directions_to(dest) if dest else None
            sc["route"] = list(newr) if newr else []
            sc["_leg_sent"] = None
            sc["_leg_tries"] = 0
            return
        cmd.send(last)                                   # retry the refused step; do NOT advance
        return
    # 2) last step landed (or none pending) -> advance to the next
    sc["_leg_sent"] = None
    route = sc.get("route")
    if not route:
        return
    d = route.pop(0)
    sc["_leg_sent"] = d
    sc["_leg_tries"] = 0
    cmd.send(d)
    if record_trail:
        s.flow.setdefault("trail", []).append(d)
