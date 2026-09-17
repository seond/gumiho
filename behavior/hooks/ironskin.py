"""철면사냥 (iron-skin hunting). In a zone flagged `ironskin = true`, the mobs deal too much
physical damage to fight normally, so the 마법사 supporter keeps 철면 (physical immunity) on the
leader. Tracked as the leader's SEPARATE `ironskin_at` status (set/cleared by the landed/fade
lines in webui). HARD RULE: never re-cast while it is up (re-halves HP for free).

WHO DECIDES: the LEADER. It asks the supporter to (re)cast via the 말 protocol — it says
"철면!" and the supporter casts on hearing it (the generic has_spell_request/cast_requested_spell
path, same as 방비 등). The decision is the leader's because only the leader sees its OWN 철면
land/fade lines immediately on its own connection; a supporter reading the leader's status across
the director link lagged, and that lag let it re-cast an already-up 철면 (the double-cast). Now the
supporter never inspects the leader's 철면 status at all — it only reacts to the spoken request."""

from gumiho.registry import action, guard


def _iron(s):
    return s.knowledge.get("ironskin", {})


# The leader's last 철면-REQUEST time is stamped on the ctx as `s._iron_req_at` (a plain runtime
# attribute), NOT in cursor.scratch. scratch RESETS on every workflow/state hop — on waking the
# leader crosses rest -> hunting -> ensure_buffs -> clear_room, so a scratch throttle got wiped
# between two asks and the leader re-asked before its first cast landed (the double-cast). A ctx
# attribute survives all those hops, AND hunting._low_vitals can read the same stamp to suppress
# resting while a 철면 fight is still being committed (the cast-then-sleep fix).


# --- leader: ASK the supporter (말: "철면!") to (re)cast when 철면 is DOWN and a fight is real ---
@guard("ironskin_request_due")
def ironskin_request_due(s):
    if s.role != "leader" or not getattr(s, "ironskin_zone", False):
        return False
    if s.ironskin_up():                          # OWN status, immediate -> stop the instant it
        return False                             #   lands (this is what kills the double-cast)
    iron = _iron(s)
    # 철면 CANNOT land on a target below 50% max HP. Below that, asking is futile (the cast fails) —
    # hold off and let should_drink_hp:50 heal us back to castable first, then ask.
    if s.hp_pct() < iron.get("cast_min_hp_pct", 50):
        return False
    # ASK ONLY FOR A REAL FIGHT — otherwise 철면 gets requested in town / before sleeping / while
    # waiting, each cast halving HP and burning 700 MP for nothing. Two legitimate triggers:
    #   - mid-fight re-cast: already in battle (s.in_battle()).
    #   - pre-engage: holding at the engage gate on a mob RIGHT NOW — hunt_step stamps _iron_want_at
    #     each such tick (own scratch; auto-clears on a workflow switch, so rest/travel/town don't
    #     look "wanting").
    in_battle = s.in_battle()
    want = iron.get("want_window", 2.0)
    engaging = (s._now() - s.cursor.scratch.get("_iron_want_at", -99.0)) < want
    if not (in_battle or engaging):
        return False
    # MP gate on the SUPPORTER (p.mp is slow-changing, so cross-link lag is harmless here), split
    # by trigger so a fight is never STARTED that can't be seen through:
    #   - mid-fight re-cast: fight already committed -> only need mp_cost to protect it.
    #   - pre-engage: need sustain_mp (== the rest floor), so the party EITHER affords a whole
    #     fight (>= sustain -> ask + engage) OR rests first (< sustain) — never ask then sleep.
    need = iron.get("mp_cost", 700) if in_battle else iron.get("sustain_mp", 1400)
    # ANY present supporter with enough MP can cast 철면 (number-agnostic) — don't stall the
    # request on one straggler being momentarily absent while another can protect us.
    if not any(p.mp >= need for p in s.present_partners()):
        return False
    # IN-FLIGHT LATCH (the double-cast killer). 철면 has a LONG, variable cast->land latency
    # (~5.5s measured) and the say is queued behind the buff burst in the pacer, so a plain
    # `recast_retry` timer is unsafe: the throttle can expire BEFORE 철면 lands (ironskin_up still
    # False in that gap), and a 2nd ask slips through -> a double-cast that crashes HP to 1 -> a
    # death (seen live 2026-09-07, twice). So once we ask, we do NOT ask again until we KNOW the
    # outcome: the LAND event clears `_iron_req_pending` (webui), and a generous timeout is the
    # only fallback (a genuinely lost cast -> one retry). Event-cleared, not just timed, so it is
    # immune to the latency/pacer jitter that defeated the timer.
    if getattr(s, "_iron_req_pending", False):
        if (s._now() - getattr(s, "_iron_req_at", -1e9)) < iron.get("request_timeout", 12.0):
            return False                         # a request is in flight -> wait for it to land
        # timed out: the cast was lost (never landed) -> fall through to allow ONE retry
    # Secondary floor: even without a pending latch, never ask twice within recast_retry.
    last = getattr(s, "_iron_req_at", -1e9)
    return (s._now() - last) >= iron.get("recast_retry", 6.0)


@action("request_ironskin")
def request_ironskin(s, cmd):
    cmd.say(f"{_iron(s).get('spell', '철면')}!")   # 말: the supporter hears "철면!" and casts (걸어)
    s._iron_req_at = s._now()
    s._iron_req_pending = True                     # in flight until the LAND line clears it (webui)


# --- leader: bail out if 철면 lapses mid-fight and can't be renewed -------------------------
@guard("ironskin_emergency")
def ironskin_emergency(s):
    """In battle in a 철면 zone with 철면 DOWN and no way to be re-protected in time -> FLEE ONCE
    before the leader takes lethal physical damage. Losing the mob is fine; a death is not."""
    if not getattr(s, "ironskin_zone", False) or not s.state.in_battle:
        return False
    if s.ironskin_up():
        return False
    iron = _iron(s)
    # THROTTLE: after a 도망 hold for flee_grace secs — do NOT 도망 every tick. The supporter sees
    # which way the leader fled and CHASES in to re-cast 철면; a per-tick flee instead just bolts
    # room-to-room into more mobs and widens the separation (the 도망-spam doom loop). During the
    # grace the leader is covered by HP potions (survival reflex) while the supporter regroups.
    # FLEE THE INSTANT 철면 is down mid-fight and can't be RENEWED — do NOT soak lethal physical
    # damage waiting for a re-cast that won't come (that got the leader killed). Renewal needs BOTH:
    #   - the supporter able to cast (co-located + MP), AND
    #   - our HP ≥ the 철면 cast floor — a re-cast can't LAND below cast_min_hp_pct.
    # Since 철면 caps HP to 50% (below the 55% request floor), a mid-fight fade almost always fails
    # the HP test and flees at once — which is exactly what we want in 철면사냥: bail, regroup, heal
    # out of combat, re-engage with fresh 철면, rather than tanking to death.
    supporter_can_recast = any(p.mp >= iron.get("mp_cost", 700) for p in s.present_partners())
    castable = s.hp_pct() >= iron.get("cast_min_hp_pct", 55)
    if supporter_can_recast and castable:
        return False                             # renewable -> a re-cast is coming, hold
    # FADE HOLD: right after 철면 fades mid-fight, do NOT bolt on the very first tick. The leader now
    # has plenty of HP to absorb a couple of hits, so hold `fade_flee_hold` secs — a brief beat that
    # avoids a jumpy flee on a fade the moment it happens. Only on the FIRST decision: once a flee is
    # already in flight (flee_failed retry / within grace), escaping fast is what matters, so skip it.
    if not getattr(s, "flee_failed", False):
        faded = getattr(s, "ironskin_faded_at", 0.0)
        if faded > 0 and (s._now() - faded) < iron.get("fade_flee_hold", 2.0):
            return False                         # hold: tank briefly before committing to the flee
    # We must flee. 도망 is stochastic — REPEAT it until one lands. A FAILED 도망 (webui set
    # flee_failed) leaves us still trapped, so retry NOW, ignoring the grace. Only a SUCCESSFUL
    # 도망 gets the grace (it moved us -> let the supporter chase before we bolt further).
    if getattr(s, "flee_failed", False):
        return True
    if (s._now() - getattr(s, "_iron_flee_at", -1e9)) < iron.get("flee_grace", 4.0):
        return False
    return True


@action("ironskin_flee")
def ironskin_flee(s, cmd):
    cmd.send("도망")            # bolt to an adjacent room; the supporter chases the flee direction
    s._iron_flee_at = s._now()  # stamp so a SUCCESSFUL flee holds (grace) instead of bolting on
    s.flee_failed = False       # fresh attempt — webui re-sets this if THIS 도망 also fails, and the
                                # guard then retries at once; a success leaves it false and combat ends


# --- leader: is 철면 up so we may engage? (used by the engage gate in hunt_step) ------------
@guard("ironskin_ready_to_engage")
def ironskin_ready_to_engage(s):
    # Not an ironskin zone -> always fine. Ironskin zone -> only engage while immune.
    return (not getattr(s, "ironskin_zone", False)) or s.ironskin_up()
