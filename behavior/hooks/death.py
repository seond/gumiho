"""Death recovery (FRAMEWORK line 60).

On death the arbiter force-switches to the `death` workflow. The character lands at
치료실 (resting); the morgue is 아래. There we 시체수습 (retrieve corpse items before they
rot), re-equip, 시체 묻어 (bury -> recover + revive), 보험, then hand back to travel to
regroup and resume hunting.

The recovery is best-effort and time-critical: even if a step is imperfect, running it
PROMPTLY saves the items (the whole failure last time was that NOTHING ran and the
corpse rotted). Commands here queue FIFO in the pacer, so their order holds.
"""
from gumiho.registry import action, guard, step


def _death_cfg(s):
    return s.knowledge.get("death", {})


@guard("partner_dead")
def partner_dead(s):
    # Is the partner ALSO down (in its own death recovery)? Used to decide whether the revived
    # character should SYNC the partner into travel on resume: only pull an ALIVE partner (to
    # regroup at the anchor). A still-dead partner self-switches to travel when ITS recovery
    # finishes — syncing it now would restart its death sequence from 치료실 and misnavigate.
    p = s.partner
    return p is not None and getattr(p.state, "dead", False)


@action("death_wake")
def death_wake(s, cmd):
    # Stand out of the resting/knocked-out state so movement + commands work.
    cmd.send(_death_cfg(s).get("stand", "일어"))


@step("go_morgue")
def go_morgue(s, cmd):
    """Descend 아래 from 치료실 to the morgue. CRITICAL: after death the server does not
    re-render the room, so `room_title` is STALE (wherever we died — e.g. 숲으로 가는 길).
    The old code only looked when the title was EMPTY, so on a stale title it did NOTHING
    (no 봐, no 아래) until stuck:60 forced 시체수습 in the wrong room -> corpse lost. So:
    LOOK until we actually confirm the clinic, then descend."""
    rt = s.room_title or ""
    clinic = _death_cfg(s).get("clinic", "치료실")
    if clinic in rt or "치료소" in rt:
        if not s.cursor.scratch.get("_descended"):
            s.cursor.scratch["_descended"] = True
            cmd.send(_death_cfg(s).get("to_morgue", "아래"))
        return
    # Not (yet) confirmed at the clinic — LOOK to get a fresh render of where we really
    # are (never trust the pre-death title), throttled so it can't spam.
    now = s.since_entered()
    if now - s.cursor.scratch.get("_look_t", -99.0) >= 2.0:
        s.cursor.scratch["_look_t"] = now
        cmd.send("봐")


@guard("at_morgue")
def at_morgue(s):
    # We descended and are no longer at the clinic -> we're at the morgue below.
    rt = s.room_title or ""
    clinic = _death_cfg(s).get("clinic", "치료실")
    return bool(s.cursor.scratch.get("_descended")) and clinic not in rt and "치료소" not in rt


@action("salvage_corpse")
def salvage_corpse(s, cmd):
    # Pull all items off our own corpse. Must be done before it rots.
    cmd.send(_death_cfg(s).get("salvage", "시체수습"))


@action("reequip_gear")
def reequip_gear(s, cmd):
    """모두입어 wears armours (and the light source). The weapon and holds must be named:
    re-arm/re-grip them from the last-known equipment (recovered into inventory). If we
    don't know them, 모두입어 still restores the armours and the rest can be re-equipped
    manually / on the next gear check."""
    r = _death_cfg(s)
    cmd.send(r.get("wear_all", "모두입어"))
    weapon_slots = r.get("weapon_slots", ["무기"])
    hold_slots = r.get("hold_slots", ["손", "쥠", "보조", "왼손", "오른손"])
    for it in (s.equipment or []):
        name = it.get("name")
        slot = it.get("slot", "")
        if not name:
            continue
        if slot in weapon_slots:
            cmd.send(f"{name} {r.get('wield', '무장')}")
        elif slot in hold_slots:
            cmd.send(f"{name} {r.get('hold', '쥐어')}")


@action("bury_corpse")
def bury_corpse(s, cmd):
    # Bury OUR corpse -> recovers HP/MP and revives us. (Never bury another's corpse.)
    cmd.send(_death_cfg(s).get("bury", "시체 묻어"))


@action("reinsure")
def reinsure(s, cmd):
    cmd.send(_death_cfg(s).get("insure", "보험"))


@action("revived_reset")
def revived_reset(s, cmd):
    # Clear the death flag so the arbiter stops force-entering this workflow, and start
    # the resumed hunt from a clean slate.
    s.state.dead = False
    s.reset_activity()
