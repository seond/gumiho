"""Hunting guards and steps.

Role-aware: the leader requests buffs, attacks, and advances the coverage walk;
the supporter FOLLOWS and casts on request (support reflex) but does NOT fight —
자동지원 is left OFF so buff spells that can't be cast mid-combat still work — so its
hunt step is a no-op.
"""

import time

from gumiho import moblore
from gumiho.gridsweep import GridSweep
from gumiho.parser import target_keyword
from gumiho.registry import action, guard, step


def _lore_seeds(s):
    md = s.knowledge.get("mob_danger", {})
    return md.get("known_safe", []), md.get("known_danger", [])


def _lore_class(s, kw):
    safe, danger = _lore_seeds(s)
    return moblore.classify(kw, safe, danger)


def _blacklisted(s, entity_line, kw):
    """A BLACKLISTED mob is one we deliberately never attack for TACTICAL reasons — not
    because it's too strong (that's [mob_danger]) but because fighting it is nasty to recover
    from (e.g. 역사의 길's 마왕 casts a BLIND). The list is PER-ZONE: declared in the hunting
    map's `blacklist = [..]` and loaded onto the ctx by reset_sweep when the hunt arrives, so
    it applies only where it's relevant. A blacklist NAME that appears in the target keyword OR
    anywhere in the entity's rendered line skips it (the hunt walks past, never targets it)."""
    names = getattr(s, "blacklist", None) or []
    return any(n and (n in (kw or "") or n in (entity_line or "")) for n in names)


def _sweep(s):
    """The 지도-driven zone map for this hunt SESSION. Lives on the ctx (not the
    workflow scratch) so it persists across hunt/wait/rest cycles — the learned
    map and the user's 'avoid' marks survive resting — and resets only when a new
    hunt boots (engine.boot)."""
    sw = s.gridsweep
    if sw is None:
        sw = s.gridsweep = GridSweep()
    return sw


def _exit_dirs(s):
    return [d for d in (_dir_of(e) for e in s.exits) if d]


# ---- fixed-map navigation — the SOLE hunt-nav mechanism for a zone that has a hand-authored
#      hunting map (behavior/hunting_maps/<zone>.toml). The old dead-reckoning ZoneSurvey
#      ("auto survey") is GONE: it built a zone graph as it walked and corrupted over time,
#      stranding and killing the duo. A hunting map is READ-ONLY — the engine follows its
#      pre-defined layout and never learns or edits it. A zone WITHOUT a map has no survey
#      (s.survey stays None) and falls back to the 지도 gridsweep. ---------------------------
def _fixedmap_zone(s):
    return s.hunt_zone() in s.knowledge.get("hunting_maps", {})


def _zone_confine(s):
    """Per-zone survey confinement (radius + which dirs the roam won't EXPLORE), from
    [zone_confine].'<zone>'. Falls back to the [hunt] global defaults. Zones differ a
    lot: 이상한 나라 1층 is a huge single floor -> a tight radius(4); 가이아 is one floor of
    same-title 카오스 rooms with OTHER floors above (올림푸스 신전) -> a wide radius but
    block 위/아래 so the roam sweeps the whole 카오스 floor and never wanders off it."""
    return s.knowledge.get("zone_confine", {}).get(s.hunt_zone() or "", {})


def _confine_zone(s):
    """The server ZONE name to confine the roam to — captured once, when first known.
    Also fixes the survey's hunt ANCHOR here (the roam is radius-bounded from it) and
    blocks the boundary directions (per-zone block_dirs) that leave the hunt floor/zone
    — the deterministic containment (e.g. 아래 drops 이상한 나라 onto 호러플랜트; 위 lifts
    가이아 onto the 올림푸스 신전 floor)."""
    sv = getattr(s, "survey", None)
    if sv is not None:
        zc = _zone_confine(s)
        sv.roam_block = set(zc.get("block_dirs",
                            s.knowledge.get("hunt", {}).get("confine_block_dirs", ["아래"])))
    z = s.cursor.scratch.get("_sv_zone")
    if z is None and s.state.zone:
        z = s.cursor.scratch["_sv_zone"] = s.state.zone
        if sv is not None:
            sv.set_anchor()
    return z


def _radius(s):
    return _zone_confine(s).get("radius",
                                s.knowledge.get("hunt", {}).get("survey_radius", 10))


def _survey_move(s, cmd):
    """Roam the survey graph to the nearest unchecked in-zone room. If the last step
    left the zone, gate that boundary and retreat. Falls back to a plain exit-walk if
    the surveyor isn't up yet."""
    sv = getattr(s, "survey", None)
    if sv is None:
        _advance(s, cmd)
        return
    if sv.cur is None:                       # survey doesn't know where we are yet (fresh
        cmd.send("봐")                        # arrival, no combat to seed it) — LOOK so the
        return                               # render feeds observe() and sets the anchor room
    now = s.since_entered()
    # WAIT for the previous move to be CONFIRMED before issuing the next one. Dead-reckoning
    # tracks position by COMMANDED moves, so firing a second move before the first's render
    # lands makes the SERVER walk it twice while the map counts it once (or vice-versa) — the
    # bot walked 7 북 when the map has only 6, overshooting the corridor into a room the map
    # doesn't cover -> instant drift, trapped. `pending` is set by note_move and cleared by
    # the render (observe) or a refusal (note_refused); hold here while it's outstanding.
    hcfg = s.knowledge.get("hunt", {})
    if sv.pending is not None:
        t0 = s.cursor.scratch.get("_sv_move_t", now)
        if now - t0 < hcfg.get("move_confirm_wait", 3.0):
            return                           # still awaiting the render — do NOT double-send
        sv.pending = None                    # render lost/stale -> re-look to re-sync, not re-move
        s.cursor.scratch["_sv_move_t"] = now
        cmd.send("봐")
        return
    # Deliberate roam pace: keep a minimum gap between confirmed moves (human cadence, and
    # extra insurance against back-to-back steps). 0 disables.
    gap = hcfg.get("move_gap", 0.0)
    if gap and now - s.cursor.scratch.get("_sv_move_t", -99.0) < gap:
        return
    zone = _confine_zone(s)
    last = s.cursor.scratch.get("_sv_last")
    if zone and s.state.zone and s.state.zone != zone and last:   # stepped OUT of zone
        back = _OPP.get(last)
        if back:
            sv.gate_boundary(back)
            sv.note_move(back)
            s.cursor.scratch["_sv_last"] = None
            s.cursor.scratch["_sv_move_t"] = now
            cmd.send(back)                       # retreat back into the zone
            return
    sv.mark_swept()                              # this room checked (no mob here)
    d = sv.next_hunt_dir(confine_zone=zone, radius=_radius(s))
    if d is None:
        return                                   # whole pocket checked -> area_barren
    # DOOR on this step? Open it FIRST ("<dir> 문 열"). The server processes commands in order,
    # so the door is open by the time the move lands (the door auto-closes, so re-open each
    # traversal). Only UNLOCKED doors — a locked one needs its key (not implemented; it just
    # stays a wall until then). The open command isn't a direction, so it doesn't touch the
    # move-correlation queue.
    door = getattr(sv, "doors", {}).get((sv.cur, d))
    if door is not None and not door.get("locked"):
        nm = door.get("name")                    # named door -> "<dir> <name> 열" (e.g. 위 하늘 열);
        cmd.send(f"{d} {nm} 열" if nm else "문 열")   # unnamed -> the generic "문 열"
    sv.note_move(d)
    s.cursor.scratch["_sv_last"] = d
    s.cursor.scratch["_sv_move_t"] = now
    s.barren_moves += 1
    cmd.send(d)

_OPP = {"북": "남", "남": "북", "동": "서", "서": "동", "위": "아래", "아래": "위"}
_DIRS = ("북", "남", "동", "서", "위", "아래")
# Ground items read like room entities but are NOT mobs: dropped gear (놓여/
# 떨어져) and the aura-wrapped display items in shops like 기증의 방 (오로라),
# whose descriptions ("...검이 꿈틀거린다") otherwise read like a live mob.
_ITEM_MARKERS = ("놓여", "떨어져", "오로라")


def _pick_target(s):
    """First real mob to attack — skip ground items (놓여/떨어져) and players.
    Normal lines give a keyword via target_keyword. A DESCRIPTION line with no verb
    (e.g. '다이아몬드 킹 카드이다' — the 거울의 방 cards) yields nothing, so we fall
    back to an [aliases] entry whose KEY appears as a SUBSTRING of the line and
    attack with its value. That way odd zones are taught purely by DATA (alias
    entries like '다이아몬드' = '다이아몬드'), never by hardcoded parsing here."""
    for e in s.hostiles():                 # already filters players/corpses/items and
        if any(m in e for m in _ITEM_MARKERS):   # anything a [aliases] row marks non-mob
            continue
        # An [aliases] row can name the mob directly (for description lines the parser
        # mis-reads, like the 거울의 방 cards/cat); else fall back to the parser.
        name, _ = s.entity_alias(e)
        kw = name or target_keyword(e)
        if not kw:
            continue
        if _blacklisted(s, e, kw):          # tactically-avoided (e.g. 마왕's blind) — never target
            continue
        if _lore_class(s, kw) == "danger":  # known-lethal (e.g. 호러플랜트) — never target
            continue
        return kw                           # safe or unknown (unknown is 고려'd first)
    return None


@guard("all_buffs_active")
def all_buffs_active(s):
    return all(s.buffs.active(b) for b in s.required_buffs())


@guard("buff_expired")
def buff_expired(s):
    req = s.required_buffs()
    return bool(req) and any(not s.buffs.active(b) for b in req)


@guard("room_clear")
def room_clear(s):
    return not s.hostiles()


def _group_ctxs(s):
    """Every OTHER member of the group, from this ctx's view — number-agnostic. Leader ->
    all supporters; a supporter -> the leader + its sibling supporters (via the leader's list).
    Used by all group-scoped decisions (rest 'everyone recovered', 'anyone in battle', …)."""
    if s.role == "leader":
        return list(s.group)
    lead = s.partner
    if lead is None:
        return []
    return [lead] + [p for p in lead.group if p is not s]


def _low_vitals(s):
    """Rest threshold: HP or MP below the configured floor. Tunable in [hunt]
    (rest_hp_pct / rest_mp_pct) — a 전사 taking big hits rests earlier so a single
    hard round can't spike it from the floor to death."""
    h = s.knowledge.get("hunt", {})
    hp_floor, mp_floor = h.get("rest_hp_pct", 40), h.get("rest_mp_pct", 20)
    if s.hp_pct() < hp_floor or s.mp_pct() < mp_floor:
        return True
    # The LEADER also rests for ANY supporter's low vitals. Rest is leader-driven (a supporter
    # never self-rests, so it can't be caught sleeping while the leader fights — see rest_ready);
    # the flip side is the leader must pick up EACH supporter's rest need in every zone, not just
    # the 철면-MP case below. Number-agnostic: scan all supporters.
    if s.role == "leader" and any(p.hp_pct() < hp_floor or p.mp_pct() < mp_floor for p in s.group):
        return True
    # 철면사냥: the LEADER also rests when NO supporter can still sustain 철면 (all their MP has
    # dropped below ~two more casts). Resting recovers MP so the next engage is protected — better
    # a proactive rest than a mid-fight cast no supporter can afford.
    if getattr(s, "ironskin_zone", False):
        iron = s.knowledge.get("ironskin", {})
        # BUT never rest while a 철면 fight is COMMITTED — that's the cast-then-sleep bug: the
        # leader asks 철면, the cast spends the supporter below the floor, and rest fires in the
        # cast-to-land window (or right after landing) BEFORE the leader ever attacks, wasting the
        # cast. While 철면 is up, in battle, or the ask is still in flight, USE the 철면 (fight) —
        # only rest to refill once the fight is over and it's down.
        committing = (s._now() - getattr(s, "_iron_req_at", -1e9)) < iron.get("recast_retry", 6.0)
        if s.ironskin_up() or s.state.in_battle or committing:
            return False
        # 철면 is DOWN and we're between fights (e.g. just fled, or it lapsed). Rest to recover if we
        # can't re-protect the NEXT engage — REST rather than hold stuck at the cap or wander into a
        # mob we can't shield. Two reasons to rest:
        #   - our HP is below the 철면 cast floor: 철면 caps HP to 50% (< the 55% request floor) and
        #     potions don't fire at exactly 50%, so only rest-regen lifts us back to castable.
        #   - NO supporter can sustain 철면 (all their MP fell below ~two casts).
        # (If HP is castable AND some supporter has MP, DON'T rest — traverse to the next mob.)
        if s.hp_pct() < iron.get("cast_min_hp_pct", 55):
            return True
        sustain = iron.get("sustain_mp", 1400)
        if s.group and not any(p.mp >= sustain for p in s.group):
            return True
    return False


@guard("hp_or_mp_low_and_safe")
def hp_or_mp_low_and_safe(s):
    # Below the rest floor (potions aren't keeping up, or are gone) -> rest.
    return _low_vitals(s) and not s.state.zone_hostile


# Runtime "rest needed" latch, per character. Survives the clear_room->rest
# workflow switch (which resets scratch/flow) so a low-HP moment DURING a battle
# is remembered and acted on once the battle is over — instead of forcing a 자
# that the server refuses mid-fight (the freeze the user hit).
_REST_PENDING = {}


@guard("vitals_low")
def vitals_low(s):
    return _low_vitals(s)


@guard("needs_rest_flag")
def needs_rest_flag(s):
    # Fire the latch ONCE (the arbiter runs one reflex then returns, so a reflex
    # that matched every low tick would starve the rest_ready transition).
    return _low_vitals(s) and not _REST_PENDING.get(s.sid)


@action("flag_rest")
def flag_rest(s, cmd):
    _REST_PENDING[s.sid] = True          # latch it; cleared only once recovered


@action("clear_rest_flag")
def clear_rest_flag(s, cmd):
    _REST_PENDING[s.sid] = False


@guard("rest_ready")
def rest_ready(s):
    # Rest is LEADER-DRIVEN. The SUPPORTER never switches to rest on its own — it enters rest ONLY
    # via the leader's synced switch. Otherwise, in the gap where the leader's in_battle briefly
    # clears (between mobs / cross-link lag), the supporter would lie down while the leader is about
    # to fight or flee — and an ASLEEP supporter can't chase (movement is refused during 자), so the
    # pair separates (the exact bug). Leader-driven means they only ever sleep together.
    if s.role == "supporter":
        return False
    # Rest is needed AND it's actually safe to sleep now: no active battle for us or our partner
    # (자 is refused mid-fight, and the sync switch would strand a fighting partner). This is the
    # latch being 'picked up after the battle'.
    if not _REST_PENDING.get(s.sid):
        return False
    if s.state.in_battle:
        return False
    return not any(p.state.in_battle for p in s.group)   # no supporter mid-fight


@guard("in_combat")
def in_combat(s):
    return s.state.in_battle


def _is_barren(s):
    # Mobs are exhausted only once we've SWEPT every cell of the local 지도 grid
    # and there's nothing to fight here — never after a blind move count, which
    # used to give up before all rooms were checked. `barren_moves` stays only
    # as a high safety ceiling for when 지도 can't be parsed at all.
    if s.hostiles():
        sv = getattr(s, "survey", None)
        if sv is not None:
            sv._barren_resweeps = 0          # a live mob -> zone is producing; restart the count
        # barren_moves counts CONSECUTIVE empty rooms (it's "rooms advanced since the last
        # mob"), so a mob appearing resets it: we keep sweeping as long as mobs keep turning
        # up within the last few rooms, and only concede barren after min_barren_moves EMPTY
        # rooms in a row. Without this it counted total rooms moved and quit mid-productive-zone.
        s.barren_moves = 0
        return False
    cap = s.knowledge.get("hunt", {}).get("sweep_cap", 40)
    if _fixedmap_zone(s):                    # fixed-map roam: barren when every mapped
        sv = getattr(s, "survey", None)      # room has been checked (or the cap)
        if sv is not None and sv.cur is None:
            return False                     # survey hasn't OBSERVED this room yet — not
                                             # barren, we just haven't looked (a mob-less
                                             # arrival room like 올림푸스 신전's center 20).
                                             # hunt_step's _survey_move 봐s to seed it.
        if sv is None:
            return s.barren_moves >= s.knowledge.get("hunt", {}).get("fixedmap_sweep_cap", 500)
        if sv.next_hunt_dir(_confine_zone(s), radius=_radius(s)) is not None:
            return False                     # unswept rooms remain -> keep sweeping
        # Every mapped room is swept. But a SINGLE pass declares exhaustion a bit early (user
        # report): mobs that respawned / wandered into already-swept rooms DURING the pass are
        # never re-checked, so we drop into `wait` while the zone is still producing. So before
        # conceding barren, RE-SWEEP the whole map `barren_resweeps` more times (reset_swept
        # re-opens it). The counter resets the instant a live hostile is seen (above), so a
        # producing zone never counts down — only fully-empty passes do; then it's truly barren.
        if getattr(sv, "_barren_resweeps", 0) < s.knowledge.get("hunt", {}).get("barren_resweeps", 1):
            sv._barren_resweeps = getattr(sv, "_barren_resweeps", 0) + 1
            sv.reset_swept()                 # re-open the map for another confirming pass
            return False
        return True
    sw = s.gridsweep
    if sw is not None and sw.knows_here() and sw.swept(_exit_dirs(s)):
        # Local 지도 pocket swept. If it was a SMALL pocket AND there are exits to walk, don't
        # concede barren yet — walk a few more rooms (see _sweep_move) so adjacent mobs are found
        # instead of dropping straight into `wait`. A dead-end (no exits) has nowhere to explore,
        # so barren at once. Only bites on small pockets; a big one already exceeds the floor.
        if _exit_dirs(s) and s.barren_moves < s.knowledge.get("hunt", {}).get("min_barren_moves", 6):
            return False
        return True
    return s.barren_moves >= cap


@guard("area_barren")
def area_barren(s):
    return _is_barren(s)


# Per-zone "last swept barren" timestamps (monotonic), shared across the process. Used to
# decide whether rotating to the next zone is worthwhile yet, or whether it too is still empty
# (both-zones-barren) — in which case we DON'T rotate into an empty zone (thrash) but fall to
# the bounded `wait` here. Reset on hot-reload (harmless: at worst one extra empty re-sweep).
_zone_cleared_at: dict[str, float] = {}


@action("mark_zone_cleared")
def mark_zone_cleared(s, cmd):
    # Stamp THIS zone as swept-empty now (both duo members stamp; same monotonic clock).
    z = s.hunt_zone()
    if z:
        _zone_cleared_at[z] = s._now()


@guard("circuit_ready")
def circuit_ready(s):
    # A multi-zone circuit should rotate to the NEXT zone ONLY if that zone has had time to
    # repopulate — i.e. it was never cleared this session, or was cleared more than a respawn
    # window ago. If the next zone is ALSO freshly empty (we just cleared both), do NOT rotate
    # into it (that thrashes between two empty zones + burns MV on recalls); fall through to the
    # bounded `wait` here instead. This can NEVER get stuck: `wait` always exits (regen_elapsed
    # timer or mobs_present), re-sweeps here, and by the next barren the other zone has had a
    # full window to respawn -> circuit_ready is then True -> rotate.
    c = getattr(s, "circuit", []) or []
    if len(c) <= 1:
        return False
    nxt = c[(s.circuit_idx + 1) % len(c)]
    window = s.knowledge.get("hunt", {}).get("regen_check", 180)
    t = _zone_cleared_at.get(nxt)
    return t is None or (s._now() - t) >= window


@action("advance_circuit")
def advance_circuit(s, cmd):
    # Point the hunt at the NEXT zone in the circuit; `travel` (entered next) recalls + walks
    # its route, and reset_sweep on arrival rebuilds the FixedMap for the new zone. The LEADER
    # drives the rotation but sets BOTH ctxs (the follower is pulled to travel by the synced
    # switch and needs the same target so its arrival reset_sweep builds the same zone's map).
    c = getattr(s, "circuit", []) or []
    if len(c) <= 1:
        return
    ni = (s.circuit_idx + 1) % len(c)
    for ctx in (s, getattr(s, "partner", None)):
        cc = getattr(ctx, "circuit", None) if ctx is not None else None
        if cc:
            ctx.circuit_idx = ni % len(cc)
            ctx.hunt_target = cc[ctx.circuit_idx]


def _full(c, pct):
    return c.hp_pct() >= pct and c.mp_pct() >= pct


def _rest_satisfied(c, full_pct, iron_hp_pct, iron_zone):
    # 철면사냥: the LEADER (the 철면 recipient) is hard-capped to ~50% max HP the instant 철면
    # lands, so resting its HP up to 95% only to have the top half discarded is a wasted sleep
    # (~minutes of it). In a 철면 zone, release the leader's HP at the 철면 cap plus a small
    # pre-engage margin. Everyone else's HP is unchanged.
    hp_target = iron_hp_pct if (iron_zone and c.role == "leader") else full_pct
    # HP is the gate. The LEADER does NOT wait for MP to top up before resuming — it's melee-
    # primary (its casts are optional and MP regens while hunting/moving), so sleeping minutes
    # just to refill its MP bar is wasted downtime. Its rest-end MP bar is a low/zero floor
    # (rest.leader_mp_pct, default 0). The SUPPORTER lives on MP (buffs/철면/spells at ~700 a
    # cast), so it still fills to full — each rest banks a long hunting burst.
    rest = c.knowledge.get("rest", {})
    mp_target = rest.get("leader_mp_pct", 0) if c.role == "leader" else full_pct
    return c.hp_pct() >= hp_target and c.mp_pct() >= mp_target


@guard("both_recovered")
def both_recovered(s):
    # Release the sleep only once the WHOLE group is recovered (to their targets) — number-agnostic.
    rest = s.knowledge.get("rest", {})
    full_pct = rest.get("full_pct", 95)
    group = _group_ctxs(s)
    # a 철면 zone if ANY member knows it (a follower may not run reset_sweep to learn the flag).
    iron_zone = bool(getattr(s, "ironskin_zone", False)
                     or any(getattr(p, "ironskin_zone", False) for p in group))
    iron_hp = s.knowledge.get("ironskin", {}).get("rest_hp_pct", 55)
    if not _rest_satisfied(s, full_pct, iron_hp, iron_zone):
        return False
    return all(_rest_satisfied(p, full_pct, iron_hp, iron_zone) for p in group)


@action("wait_doze")
def wait_doze(s, cmd):
    # `wait` (barren-zone regen wait) sleeps to pass the regen window. But if BOTH are ALREADY
    # fully recovered (a cleared zone we just rested through), sending 자 would fire then
    # both_recovered instantly wakes it — a pointless 자→깨 on every wait entry, which becomes the
    # 자-깨 STORM under any rapid re-entry. So doze ONLY when actually not recovered; otherwise skip
    # straight to watch (dozed_and_recovered stays false -> the `both_recovered -> watch` transition).
    if not both_recovered(s):
        cmd.send("자")
        s.cursor.scratch["_dozed"] = True


@guard("dozed_and_recovered")
def dozed_and_recovered(s):
    # We actually slept AND are now recovered -> 깨 (wake). If we never dozed (already full on
    # entry), this stays false and `recover` routes straight to watch with no 자 and no 깨.
    return bool(s.cursor.scratch.get("_dozed")) and both_recovered(s)


@guard("mobs_present")
def mobs_present(s):
    return bool(s.hostiles())


@guard("regen_elapsed")
def regen_elapsed(s):
    # Waited long enough in the regen window -> go re-sweep the pocket (that's how
    # silently-respawned mobs are found; a stationary 봐 never sees them).
    return s.since_entered() >= s.knowledge.get("hunt", {}).get("regen_check", 300)


@guard("gear_worn")
def gear_worn(s):
    # Some armour/weapon is worn to the repair threshold -> go resupply (which
    # repairs everything) before it breaks. Only the leader tracks 장비.
    if s.role != "leader":
        return False
    pct = s.knowledge.get("resupply", {}).get("gear_repair_pct", 0.5)
    return s.gear_worn(pct)


@action("check_gear")
def check_gear(s, cmd):
    """Re-read 장비 so durability is CURRENT before we sleep. Rest is a frequent, natural pause,
    and the periodic gear_check (every gear_check secs) can be too stale between rests — a weapon
    can cross the repair threshold and BREAK before the next scheduled read (a 카타나 was lost
    overnight this way). Force a fresh parse here; gear_worn then decides repair-vs-sleep on live
    data. Leader only (only it tracks 장비); counts as the periodic read too (no double 장비)."""
    if s.role != "leader":
        return
    s.equipment = None            # invalidate the stale parse; gear_worn stays False until it lands
    s.last_gear_at = s._now()     # this IS the periodic check -> don't also fire it right after
    cmd.send("장비")


@guard("pre_rest_ready")
def pre_rest_ready(s):
    """The pre-rest gear read has landed (leader), or there's nothing to read (supporter) — proceed
    to sleep. Paired with gear_worn (checked first), which diverts to repair on a fresh worn read."""
    return s.role != "leader" or s.equipment is not None


@action("watch_poll")
def watch_poll(s, cmd):
    # Awake, mobs exhausted: periodically MOVE to another room in the zone and 봐 —
    # respawns may not appear in the exact room we're standing in. Leader roams;
    # the supporter follows (따라). mobs_present (checked each tick) resumes hunting.
    if s.role != "leader":
        return
    interval = s.knowledge.get("hunt", {}).get("regen_check", 300.0)
    if s.since_entered() - s.cursor.scratch.get("last_watch", -interval) >= interval:
        s.cursor.scratch["last_watch"] = s.since_entered()
        sw = _sweep(s)
        d = sw.next_dir(_exit_dirs(s)) if sw.knows_here() else None
        if d:
            sw.note_move(d)             # NOT a blind pos change — register confirms it
            cmd.send(d)                 # relocate within the pocket
            cmd.send("지도")            # re-map so registration fixes our position
        else:
            cmd.send("봐")               # nowhere to roam — just look for respawns


@action("reset_barren")
def reset_barren(s, cmd):
    s.barren_moves = 0
    # FULL reset, not just visited: the temporary zone map is rebuilt from 지도 each
    # sweep (cheap), and persisting it across the regen wait let ONE stray
    # registration (e.g. a stale position after roaming) permanently graft a phantom
    # cell — the "4th row that isn't there". A fresh sweep can't inherit corruption.
    s.gridsweep = None               # _sweep() re-creates it clean on the next step
    # Survey-nav: KEEP the learned graph (it's the true zone map), just re-open every
    # room for a fresh respawn check.
    if getattr(s, "survey", None) is not None:
        s.survey.reset_swept()


@action("reset_sweep")
def reset_sweep(s, cmd):
    # Drop the temporary in-zone map when we (re)arrive to hunt — after travel or a
    # resupply the player has relocated, so the old map/position is stale and would
    # mis-register the first 지도 back in the pocket.
    s.gridsweep = None
    # A fresh arrival has swept NOTHING yet — start the empty-room counter at 0 so the
    # "move a few rooms before conceding barren" floor actually applies. Without this, a
    # stale high count carried over from the previous barren made area_barren fire on the
    # FIRST tick (gridsweep is None here -> the sw-based floor is skipped and _is_barren
    # falls straight to `barren_moves >= cap`), so it arrived and dropped into `wait`
    # without moving a single room — the recurring idle-on-arrival bug.
    s.barren_moves = 0
    # A zone with a hand-authored FIXED MAP navigates it DETERMINISTICALLY (track position
    # from the known arrival room). This is the SOLE hunt-nav mechanism now — the accumulating
    # dead-reckoning ZoneSurvey has been removed (it corrupted over time and killed the duo).
    # Swap a FRESH read-only FixedMap in on arrival; a zone with no map has no survey at all
    # (s.survey = None) and the 지도 gridsweep takes over.
    zone = s.hunt_zone()
    hmap = s.knowledge.get("hunting_maps", {}).get(zone or "")
    if hmap:
        from gumiho.fixedmap import FixedMap
        s.survey = FixedMap(hmap)
    else:
        s.survey = None
    # PER-ZONE mob blacklist — loaded from THIS zone's hunting map when the hunt arrives, so
    # it applies only where it's declared (e.g. 역사의 길's 마왕). Cleared on a zone with none.
    s.blacklist = list((hmap or {}).get("blacklist", []) or [])
    # 철면사냥 flag — likewise per-zone. When set, the leader only engages under 철면 and the
    # supporter keeps it up (see hooks/ironskin.py). Cleared on a zone that doesn't declare it.
    s.ironskin_zone = bool((hmap or {}).get("ironskin", False))
    if not s.ironskin_zone:
        s.ironskin_at = 0.0     # dropping the flag also drops any stale immunity timer
    s.cursor.scratch.pop("_sv_zone", None)
    s.cursor.scratch.pop("_sv_last", None)


def _maintain_buffs(s, cmd):
    """Leader-only: (re-)ask the supporter for any required buff that isn't
    CONFIRMED active, throttled to [hunt].buff_reask. Confirmation comes from the
    buff's `landed` message ([buff_signals]) — so a request the supporter missed
    (asleep, mid-regroup) keeps being re-asked until it actually lands, instead of
    the old 'assume success for 240s' that left 방비 silently absent."""
    if s.role != "leader":
        return
    # Request buffs ONLY out of combat. A buff cast mid-fight tends not to take (no landed line),
    # so it never confirms and gets re-asked every buff_reask — the "빨리가기/분노 requested so
    # frequently" waste. Between fights the cast lands cleanly and holds for its full duration.
    # (This also keeps 철면 zones from spending the supporter's scarce MP on buffs mid-crisis.)
    if s.in_battle():
        return
    interval = s.knowledge.get("hunt", {}).get("buff_reask", 8.0)
    for b in s.required_buffs():
        if s.buffs.should_reask(b, interval):
            cmd.say(f"{b}!")          # supporter hears (말 protocol) and casts
            s.buffs.requested(b)


@action("request_missing_buffs")
def request_missing_buffs(s, cmd):
    _maintain_buffs(s, cmd)


def _self_buffs(s):
    """The leader's SELF-cast buffs for its job ([self_buffs], e.g. 성직 -> 수호/축복). Union across
    job tokens, like required_buffs."""
    job = s.state.job or ""
    table = s.knowledge.get("self_buffs", {})
    out = []
    for tok in job.split():
        for b in table.get(tok, []):
            if isinstance(b, str) and b not in out:
                out.append(b)
    return out


def _maintain_self_buffs(s, cmd):
    """Leader-only, out of combat: keep the caster's OWN buffs up by casting '<buff> 걸' (NO target).
    Distinct from _maintain_buffs (which 말-requests supporter buffs). No confirmation line is wired
    for these, so re-cast on a BLIND timer — per-buff [buff_durations].<buff> else [hunt].
    self_buff_recast — which is spam-free (one cast per window). One buff per tick to keep the pace."""
    if s.role != "leader" or s.in_battle():
        return
    buffs = _self_buffs(s)
    if not buffs:
        return
    now = s._now()
    at = getattr(s, "_self_buff_at", None)
    if at is None:
        at = {}
        s._self_buff_at = at
    durs = s.knowledge.get("buff_durations", {})
    default = s.knowledge.get("hunt", {}).get("self_buff_recast", 120.0)
    for b in buffs:
        if now - at.get(b, -1e9) >= durs.get(b, default):
            cmd.send(f"{b} 걸")       # SELF-cast, no target (수호 걸 / 축복 걸)
            at[b] = now
            return                    # one per tick (human pace)


@guard("battle_spell_due")
def battle_spell_due(s):
    """Caster leader (성직): while in a fight, cast the battle spell (벼락) at the opponent for extra
    damage — throttled, MP-gated, job-gated. Self-clearing (only in_battle for the right job with MP
    and the cadence elapsed), so it stays silent for melee jobs and out of combat."""
    c = s.knowledge.get("combat", {})
    spell = c.get("battle_spell")
    if not spell or not s.in_battle():
        return False
    job = s.state.job or ""
    if not any(tok in c.get("battle_spell_jobs", []) for tok in job.split()):
        return False
    if s.mp_pct() < c.get("battle_spell_min_mp", 15):
        return False                                  # can't afford it -> don't spam-fail
    gap = c.get("battle_spell_gap", 3.0)
    return (s._now() - getattr(s, "_battle_spell_at", -1e9)) >= gap


@action("cast_battle_spell")
def cast_battle_spell(s, cmd):
    c = s.knowledge.get("combat", {})
    spell = c.get("battle_spell", "벼락")
    verb = c.get("battle_spell_verb", "걸어")
    cmd.send(f"{spell} {verb}")     # a SPELL, NO target — the fight is locked on the opponent: '벼락 걸어'
    s._battle_spell_at = s._now()


@guard("separated")
def separated(s):
    # Debounced: a group member absent for a while (not a one-render follow lag). Group-wide
    # `together` = ALL members present, so this fires if ANY supporter has drifted off too long.
    if not s.has_group():
        return False
    return not s.together and \
        s.since_together() > s.knowledge.get("hunt", {}).get("regroup_after", 15)


@action("look")
def look(s, cmd):
    # THROTTLED: waiting states (regroup, reform, recall_regroup, arrive,
    # resume_hunt) use `look` as their step to refresh co-location. Sending 봐
    # every tick hammers the server when the condition (e.g. a disconnected
    # partner) never clears — re-look at most every few seconds instead.
    now = s.since_entered()
    if now - s.cursor.scratch.get("_look_t", -99.0) >= 3.0:
        s.cursor.scratch["_look_t"] = now
        cmd.send("봐")


def _combat_cfg(s):
    return s.knowledge.get("combat", {})


def _combo_enabled(s):
    """Does the 연타-style multi-strike combo apply to THIS character right now?
    Configurable in [combat]: a combo_skill is set, our class matches combo_jobs, and
    our level is at/above combo_min_level. The class can be a DUAL class ("전사 검사"),
    so match if ANY of its tokens is an eligible job."""
    c = _combat_cfg(s)
    jobs = c.get("combo_jobs", [])
    my_classes = (s.state.job or "").split()
    return bool(c.get("combo_skill")) and any(j in my_classes for j in jobs) \
        and (s.state.level or 0) >= c.get("combo_min_level", 10 ** 9)


def _attack_verb(s):
    """'연타' (combo) when eligible, else plain '공격' — used to INITIATE a fight."""
    return _combat_cfg(s).get("combo_skill") if _combo_enabled(s) else "공격"


@guard("combo_continue")
def combo_continue(s):
    """A wind-up cue set combo_ready mid-fight and we're combo-eligible (전사/검사/장군
    at level) — the multi-strike must continue. This is an ALWAYS-ON reflex (see
    [engine].always_reflexes): the server drives battle rounds no matter which
    workflow we're in, so a combo can come due while resting/travelling too."""
    if not (s.fighting() and s.combo_ready and _combo_enabled(s)):
        return False
    # 철면사냥: NEVER continue the 연타 while UNPROTECTED. In a 철면 zone the mobs are lethal
    # without 철면, so beating on one while immunity is down just soaks full damage (this is what
    # crashed the leader's HP and drove the 연타-철면!-도망 thrash). No offense until 철면 is back up.
    if getattr(s, "ironskin_zone", False) and not s.ironskin_up():
        return False
    return True


@action("continue_combo")
def continue_combo(s, cmd):
    s.combo_ready = False                       # consume the cue
    cmd.send(_combat_cfg(s)["combo_skill"])     # BARE skill (no target) continues the combo


@guard("knocked_down")
def knocked_down(s):
    """A mob's power-bash floored us ("…당신은 땅바닥에 떨썩 주저앉습니다.") — webui set the flag.
    Self-clearing (the stand consumes it), so it stays silent until we're actually knocked down.
    ALWAYS-ON (see [engine].always_reflexes / offline_reflexes): a bash can land in any state and
    even with the engine off, and standing up is always safe and urgent."""
    return bool(getattr(s, "knocked_down", False))


@action("stand_up")
def stand_up(s, cmd):
    s.knocked_down = False                      # consume it (a fresh bash re-sets it)
    cmd.send(_combat_cfg(s).get("stand_cmd", "일어나"))   # get back on our feet


def _consider_gate(s, cmd, target):
    """Assess an UNFAMILIAR mob with `<mob> 고려` BEFORE engaging (FRAMEWORK: only fight
    a fair fight). Returns 'engage' (known/ruled SAFE), 'skip' (ruled DANGER — recorded,
    never fought), or 'wait' (고려 in flight — hold this tick). A mob is 고려'd once; the
    verdict persists in moblore so we never re-assess or re-risk a known-lethal mob."""
    cls = _lore_class(s, target)
    if cls == "safe":
        return "engage"
    if cls == "danger":
        return "skip"                       # (also filtered out in _pick_target)
    wait = s.knowledge.get("mob_danger", {}).get("consider_wait", 2.0)
    if s.considering != target:             # start a fresh assessment
        s.considering = target
        s.consider_at = time.monotonic()
        s.consider_danger = False
        cmd.send(f"{target} 고려")
        return "wait"
    if s.consider_danger:                    # a lethal phrase came back -> never fight it
        moblore.learn(target, "danger")
        s.considering = None
        return "skip"
    if time.monotonic() - s.consider_at >= wait:   # reply settled, no lethal phrase -> fair
        moblore.learn(target, "safe")
        s.considering = None
        return "engage"
    return "wait"


@step("hunt_step")
def hunt_step(s, cmd):
    if s.role != "leader":
        return
    _maintain_buffs(s, cmd)           # keep 방비 up throughout the hunt (non-blocking,
                                      # throttled; re-asks until the cast is confirmed)
    _maintain_self_buffs(s, cmd)      # caster leader (성직): self-cast 수호/축복 ("<buff> 걸")
    if s.in_battle():                 # a battle is live — the server drives the rounds and
        return                        # the always-on `combo` reflex continues 연타. Gate on
                                      # in_battle (NOT fighting()): fighting() goes stale after
                                      # 3s while in_battle holds ~15s, and in that gap hunt_step
                                      # was RE-INITIATING '<mob> 연타' every tick — each a fresh
                                      # multi-strike, piling on incoming hits until it died.
                                      # in_battle clears promptly on the kill (EnemyDown), so
                                      # this still resumes fast; the 15s backstop covers a
                                      # missed end-event.
    if s.need_look:                   # combat changed the room — refresh before deciding
        s.need_look = False
        cmd.send("봐")
        return
    # User marked this cell "don't hunt here" on the overlay — pass through it
    # (no fighting), just keep sweeping toward a huntable room.
    if _sweep(s).avoided_here():
        _sweep_move(s, cmd)
        return
    target = s.alias(_pick_target(s))  # real mob only (never a player/item); parsed
                                       # name -> the server's registered attack keyword
    if target:
        # 철면사냥 ENGAGE GATE: in a 철면 zone, NEVER initiate a fight unprotected — hold until
        # the immunity is up. Stamp "I'm holding to engage a mob RIGHT NOW" so the supporter
        # casts 철면 ONLY for a real, imminent fight — never in town/rest/travel/wait, where an
        # unconditional cast would waste 700 MP and halve HP for nothing (the pre-engage cast;
        # the mid-fight re-cast keys off in_battle instead). This is a brief hold, not a
        # deadlock; a co-location/low-MP issue is handled by the rest gate and the follow logic.
        if getattr(s, "ironskin_zone", False) and not s.ironskin_up():
            s.cursor.scratch["_iron_want_at"] = s._now()
            return
        # 고려 an unfamiliar mob before engaging — a lethal one (호러플랜트) is recorded
        # and skipped, never fought. 'wait' holds while the assessment is in flight.
        if _consider_gate(s, cmd, target) != "engage":
            return
        # Anti-spam: if we just attacked this and it didn't engage (no combat),
        # it isn't a real target — re-look instead of hammering it.
        now = time.monotonic()
        if (s.cursor.scratch.get("atk_kw") == target and not s.in_battle()
                and now - s.cursor.scratch.get("atk_t", 0.0) < 3.0):
            # attacked this recently and combat didn't register -> re-look to refresh the room,
            # but THROTTLE it: without this it set need_look EVERY tick (~0.3s) for the whole 3s
            # window -> a 봐 storm (worst when a mob's combat verb wasn't recognized so in_battle
            # never stuck — the 한성의 하수구 쑤셨/물어뜯 case). One refreshing look per relook_gap.
            gap = s.knowledge.get("hunt", {}).get("relook_gap", 1.5)
            if now - s.cursor.scratch.get("_relook_t", -99.0) >= gap:
                s.cursor.scratch["_relook_t"] = now
                s.need_look = True
            return
        s.cursor.scratch["atk_kw"] = target
        s.cursor.scratch["atk_t"] = now
        cmd.send(f"{target} {_attack_verb(s)}")  # initiate (연타 if eligible, else 공격);
                                                 # server then runs the rounds
        return
    # Never wander off from the supporter: hold until it's back in-room. But the
    # `together` flag can go STALE-False (we last rendered this room before the
    # supporter followed in), and a bare hold with no re-look deadlocks even when
    # it's standing right here — so re-look at most every few seconds to refresh
    # co-location (throttled; not a poll spam). A real separation is still caught
    # by `separated` -> regroup -> travel.
    if s.partner_name and not s.together:
        now = s.since_entered()
        if now - s.cursor.scratch.get("_hold_look", -99.0) >= 3.0:
            s.cursor.scratch["_hold_look"] = now
            cmd.send("봐")
        return
    # No mob here: periodic gear-durability check keeps ctx.equipment fresh so the
    # gear_worn trigger can send us to repair BEFORE the armor breaks.
    if s.since_gear() >= s.knowledge.get("resupply", {}).get("gear_check", 180):
        s.last_gear_at = s._now()
        cmd.send("장비")
        return
    _sweep_move(s, cmd)


def _sweep_move(s, cmd):
    """지도-driven sweep: identify the local grid from the server's minimap and
    walk to the nearest unvisited cell. Room identity is the cell's position in
    the 지도 (registered against what we know), never the title — so a pocket of
    look-alike rooms gets fully swept without minting phantom clones, and a refused
    step can't smear the map (the next 지도 just registers back in place).

    Each step is CONFIRMED by a fresh 지도 before we decide the next move: send 지도,
    let webui.register() set our true position, then step. That's one cheap read per
    room while actively surveying — a careful human glancing at the map — not a blind
    counter that drifts."""
    if _fixedmap_zone(s):                    # this zone has a fixed map -> navigate by it
        _survey_move(s, cmd)
        return
    sw = _sweep(s)
    # 1) Confirm our position with a 지도 whenever a move is still unconfirmed or the
    #    current cell was never drawn. register() clears pending_dir once it lands.
    if sw.pending_dir is not None or not sw.knows_here():
        now = s.since_entered()
        if now - s.cursor.scratch.get("_jido_t", -99.0) < 1.5:
            return                          # already asked; wait for the 지도 to arrive
        tries = s.cursor.scratch.get("jido_tries", 0)
        if tries < 3:
            s.cursor.scratch["jido_tries"] = tries + 1
            s.cursor.scratch["_jido_t"] = now
            cmd.send("지도")
            return
        # 지도 won't parse in this room — accept the pending move blindly, then fall
        # back to plain exit-walking for this one step.
        if sw.pending_dir is not None:
            sw.move(sw.pending_dir)
            sw.pending_dir = None
        s.cursor.scratch["jido_tries"] = 0
        _advance(s, cmd)
        return
    # 2) Position confirmed — decide and take the next step.
    s.cursor.scratch["jido_tries"] = 0
    sw.mark_visited()
    d = sw.next_dir(_exit_dirs(s))
    if d is None:                           # local 지도 pocket swept
        # Explore beyond it for a few rooms before conceding barren (see _is_barren): a plain
        # exit-walk into an adjacent room, which the next 지도 re-maps into a fresh pocket. Only
        # while under the min-rooms floor — past it, area_barren fires and we go to `wait`.
        if s.barren_moves < s.knowledge.get("hunt", {}).get("min_barren_moves", 6):
            _advance(s, cmd)                # walk one room out (increments barren_moves)
        return                              # else: swept + floor reached -> area_barren next tick
    # ANTI-PINGPONG — the one rule: never immediately reverse the last move while another exit
    # exists. That reversal is the 동-서-동-서 bounce between two rooms: in a pocket of look-alike
    # 카오스 rooms the 지도 can't tell cells apart, so next_dir keeps choosing the cell we just came
    # from. _advance already avoids reversal; next_dir did NOT — this closes that gap. A single
    # backtrack is fine; two-in-a-row can't happen because we only ever step to a non-reverse exit.
    last = s.cursor.scratch.get("last_dir")
    if last is not None and d == _OPP.get(last):
        alts = [e for e in _exit_dirs(s) if e != d]
        if alts:
            d = alts[0]
    sw.note_move(d)                         # a prior for the next 지도, not a pos change
    s.cursor.scratch["last_dir"] = d
    s.barren_moves += 1
    cmd.send(d)


def _dir_of(exit_str):
    for d in _DIRS:
        if exit_str.startswith(d):
            return d
    return None


def _advance(s, cmd):
    """Move along an exit, avoiding an immediate reversal (coverage via the
    graph mapper is a later refinement)."""
    dirs = [d for d in (_dir_of(e) for e in s.exits) if d]
    if not dirs:
        return
    back = _OPP.get(s.cursor.scratch.get("last_dir"))
    choices = [d for d in dirs if d != back] or dirs
    d = choices[0]
    s.cursor.scratch["last_dir"] = d
    s.barren_moves += 1                # a move with no mob here — toward "barren"
    cmd.send(d)
