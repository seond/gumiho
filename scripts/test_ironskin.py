"""철면사냥 (iron-skin hunting). In a zone flagged `ironskin = true`, the 마법사 supporter keeps
철면 (physical immunity) on the leader, tracked SEPARATELY as ctx.ironskin_at. The rules that
matter and are proven here:
  - detection: the landed line raises the status, the fade line drops it, and a missed fade line
    is caught by duration_backstop (so it's never wrongly believed up forever).
  - the CRITICAL rule: the supporter NEVER re-casts while 철면 is still up (a re-cast re-halves
    the leader's HP without extending the timer). It casts only when it's DOWN.
  - the leader NEVER engages a mob unprotected (engage gate), and FLEES if 철면 lapses mid-fight
    with no way to renew it in time.
  - the party rests when the supporter can no longer sustain 철면 (its MP fell below ~2 casts)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import events as ev
from gumiho.access import CharCtx
from gumiho.command import Cmd
from gumiho.reload import get_knowledge, Reloader
from gumiho.state import WorldState

CLOCK = [1000.0]
def now(): return CLOCK[0]
def adv(dt): CLOCK[0] += dt


def _load():
    Reloader(lambda: []).load_all()
    from behavior.hooks import hunting, common, party, ironskin  # noqa: F401
    return get_knowledge()          # throttle now lives on each ctx (_iron_req_at), so fresh per test


def _leader(kn, hp=(3000, 3000), mp=(200, 200), job="전사 검사"):
    st = WorldState()
    st.apply(ev.Status(hp=hp, mp=mp, mv=(400, 400), job=job))
    c = CharCtx("a", "leader", st, kn, set(), name="플레이어제로", now=now)
    c.partner_name = "스튀르들뤼손"
    return c


def _supporter(kn, mp=(2900, 2900)):
    st = WorldState()
    st.apply(ev.Status(hp=(1200, 1200), mp=mp, mv=(400, 400), job="마법사"))
    c = CharCtx("b", "supporter", st, kn, set(), name="스튀르들뤼손", now=now)
    c.partner_name = "플레이어제로"
    return c


def _detect(ctx, text):
    """Replicate webui's 철면 landed/fade scan (set/clear ironskin_at)."""
    iron = ctx.knowledge.get("ironskin", {})
    if iron.get("landed") and iron["landed"] in text:
        ctx.ironskin_at = ctx._now()
    if iron.get("faded") and iron["faded"] in text:
        ctx.ironskin_at = 0.0


# --- 1. detection: landed raises, fade drops, backstop catches a missed fade ---------------
def test_detection_and_backstop():
    kn = _load()
    c = _leader(kn)
    assert not c.ironskin_up(), "starts down"
    _detect(c, "당신은 피부가 쇠로 변하는 것을 느낍니다.")
    assert c.ironskin_up(), "landed line raises 철면"
    adv(20.0)
    assert c.ironskin_up(), "still up 20s in"
    _detect(c, "당신은 피부가 부드러워지는것을 느낍니다.")
    assert not c.ironskin_up(), "fade line drops 철면"
    # a MISSED fade line must not leave it believed-up forever -> backstop
    _detect(c, "당신은 피부가 쇠로 변하는 것을 느낍니다.")
    assert c.ironskin_up()
    backstop = kn["ironskin"]["duration_backstop"]
    adv(backstop + 1.0)
    assert not c.ironskin_up(), "backstop expires a 철면 whose fade line was missed"
    print("detection: landed raises, fade drops, backstop catches a missed fade")


# --- 2. LEADER asks (말 "철면!") ONLY when it's down; NEVER while up (the critical rule) -----
def test_leader_requests_only_when_down():
    from behavior.hooks.ironskin import ironskin_request_due, request_ironskin
    kn = _load()
    lead = _leader(kn); sup = _supporter(kn)
    lead.partner = sup; sup.partner = lead
    lead.ironskin_zone = True                       # leader is in a 철면 zone
    lead.together = True                            # co-located with the supporter
    lead.cursor.scratch["_iron_want_at"] = now()   # leader holding to engage a mob RIGHT NOW

    # 철면 DOWN + engaging -> leader should ASK via 말 ("철면!" -> "철면! 말")
    lead.ironskin_at = 0.0
    assert ironskin_request_due(lead), "asks when the leader's OWN 철면 is down and it's engaging"
    sent = []
    request_ironskin(lead, Cmd(sent.append))
    assert sent == ["철면! 말"], sent

    # THE CRITICAL RULE, now judged by the leader's OWN immediate status -> no double-cast:
    # 철면 UP -> must NOT ask again (an extra cast re-halves HP for nothing)
    lead.ironskin_at = now()
    assert not ironskin_request_due(lead), "NEVER asks again while the leader's own 철면 is up"

    # up, then fades -> asks again, but throttled right after a request so an in-flight cast
    # isn't re-requested every tick (request_ironskin above already stamped the throttle)
    lead.ironskin_at = 0.0
    lead.cursor.scratch["_iron_want_at"] = now()   # still engaging
    assert not ironskin_request_due(lead), "throttled immediately after a request"

    # THE DOUBLE-ASK FIX: the throttle must survive a workflow/state switch. On waking, the leader
    # crosses rest -> hunting -> ensure_buffs -> clear_room and cursor.scratch RESETS each hop — a
    # scratch-based throttle got wiped and the leader re-asked before its first cast landed. The
    # throttle now lives on the ctx (_iron_req_at), so even a full cursor.scratch reset can't wipe it.
    assert getattr(lead, "_iron_req_at", None) is not None, "request stamped the ctx throttle"
    lead.cursor = type(lead.cursor)(workflow="hunting", sub="clear_room")   # simulate a state switch
    lead.cursor.scratch["_iron_want_at"] = now()                            # re-stamped by hunt_step
    assert not ironskin_request_due(lead), "still throttled after a state switch wiped scratch"

    # IN-FLIGHT LATCH: a plain recast_retry wait is NOT enough to re-ask while a cast is still in
    # flight — 철면's ~5.5s cast->land latency means the 6s timer expired before the land, letting a
    # 2nd ask through -> double-cast -> HP→1 -> death. The latch blocks until the LAND clears it.
    adv(kn["ironskin"].get("recast_retry", 6.0) + 0.1)
    lead.cursor.scratch["_iron_want_at"] = now()
    assert not ironskin_request_due(lead), "in-flight latch blocks a re-ask past recast_retry (no double-cast)"
    # the LAND event (webui) clears the latch; while 철면 is then UP no ask; once it FADES, ask again.
    lead._iron_req_pending = False                  # webui does this on the land line
    lead.ironskin_at = now()                        # 철면 landed -> up
    assert not ironskin_request_due(lead), "no ask while 철면 is up"
    lead.ironskin_at = 0.0                          # faded
    lead.cursor.scratch["_iron_want_at"] = now()
    assert ironskin_request_due(lead), "asks again after 철면 fades (latch cleared by the prior land)"
    print("leader: asks 철면! when down+engaging, NEVER while up, in-flight latch blocks re-ask")


def test_no_double_cast_race():
    """THE DEATH BUG (2026-09-07): the leader asked 철면 twice ~5.7s apart — the recast_retry(6s)
    timer expired before 철면's ~5.5s cast->land latency completed, so a 2nd ask slipped through
    while the first was still in flight -> double-cast -> HP crashed to 1 -> death. The in-flight
    latch (event-cleared by the LAND line, not a timer) closes the race."""
    from behavior.hooks.ironskin import ironskin_request_due, request_ironskin
    kn = _load()
    lead = _leader(kn); sup = _supporter(kn)
    lead.partner = sup; sup.partner = lead
    lead.ironskin_zone = True; lead.together = True
    lead.ironskin_at = 0.0
    lead.cursor.scratch["_iron_want_at"] = now()
    # 1) first ask goes out (in flight)
    assert ironskin_request_due(lead)
    request_ironskin(lead, Cmd([].append))
    assert lead._iron_req_pending is True, "request marks the cast IN FLIGHT"
    # 2) the killer window: throttle has expired but 철면 has NOT landed yet (still in flight)
    adv(kn["ironskin"].get("recast_retry", 6.0) + 1.0)     # 7s > recast_retry, < request_timeout
    lead.cursor.scratch["_iron_want_at"] = now()
    assert lead.ironskin_at == 0.0, "still not landed (the ~5.5s latency window)"
    assert not ironskin_request_due(lead), "MUST NOT ask again while the first cast is in flight (no double-cast)"
    # 3) a genuinely LOST cast (never lands): after request_timeout, allow ONE retry
    adv(kn["ironskin"].get("request_timeout", 12.0))
    lead.cursor.scratch["_iron_want_at"] = now()
    assert ironskin_request_due(lead), "a truly missed cast retries once request_timeout passes"
    print("no double-cast: in-flight latch blocks the re-ask race; a lost cast still retries")


def test_full_protocol_supporter_casts_on_hearing():
    """End-to-end: the leader speaks '철면!' and the supporter casts via the GENERIC 말 path
    (has_spell_request/cast_requested_spell) — it never inspects the leader's 철면 status."""
    from behavior.hooks.common import has_spell_request, cast_requested_spell
    kn = _load()
    sup = _supporter(kn); sup.partner_name = "플레이어제로"
    assert not has_spell_request(sup)
    sup.on_event(ev.Speech(speaker="플레이어제로", text="철면!"))   # hears the leader
    assert has_spell_request(sup), "the spoken '철면!' becomes a pending request"
    sent = []
    cast_requested_spell(sup, Cmd(sent.append))
    assert sent == ["플레이어제로 철면 걸어"], sent
    print("protocol: leader says 철면! -> supporter casts 플레이어제로 철면 걸어")


def test_no_request_below_castable_hp():
    """철면 can't land on a target below 50% max HP, so the leader must not ASK there (the cast would
    fail). It heals back to ≥50% first (should_drink_hp:50), then asks."""
    from behavior.hooks.ironskin import ironskin_request_due
    kn = _load()
    floor = kn["ironskin"].get("cast_min_hp_pct", 50)
    sup = _supporter(kn)
    lead = _leader(kn, hp=(int(8816 * (floor - 6) / 100), 8816), mp=(100, 100))  # below castable
    lead.partner = sup; sup.partner = lead
    lead.ironskin_zone = True; lead.ironskin_at = 0.0; lead.together = True
    lead.state.in_battle = True                          # a real fight, would otherwise ask
    assert not ironskin_request_due(lead), "no 철면 ask while below castable HP (cast would fail)"
    # healed back to ≥50% -> now it asks
    lead.state.apply(ev.Status(hp=(int(8816 * (floor + 4) / 100), 8816), mp=(100, 100), mv=(400, 400)))
    assert ironskin_request_due(lead), "asks once HP is back to castable (≥50%)"
    print("cast-min-HP: no 철면 ask below 50% (cast would fail); asks once healed to castable")


def test_no_request_when_idle_the_bug():
    """The regression: 철면 was cast in town and right before sleeping. Now the LEADER only asks
    for a REAL fight — mid-battle, or actively holding to engage — never idle."""
    from behavior.hooks.ironskin import ironskin_request_due
    kn = _load()
    lead = _leader(kn); sup = _supporter(kn)
    lead.partner = sup; sup.partner = lead
    lead.ironskin_zone = True; lead.ironskin_at = 0.0     # in a 철면 zone, 철면 down
    lead.together = True                                  # co-located; supporter has plenty of MP
    # IDLE: not in battle, not holding to engage -> MUST NOT ask
    assert not ironskin_request_due(lead), "no ask while idle in town/rest (the wasteful bug)"
    # a STALE want stamp (older than the window) must not count as engaging
    lead.cursor.scratch["_iron_want_at"] = now()
    adv(kn["ironskin"].get("want_window", 2.0) + 0.5)
    assert not ironskin_request_due(lead), "a stale engage stamp doesn't trigger an ask"
    # mid-battle (ambush during rest, 철면 down) -> DOES ask to protect
    lead.state.in_battle = True
    assert ironskin_request_due(lead), "asks mid-battle to protect even without a want stamp"
    print("no-idle-ask: no 철면 in town/rest; asks only mid-fight or when engaging")


def test_pre_engage_needs_sustain_mid_fight_needs_mp_cost():
    """The cast-then-sleep race: at supporter MP above mp_cost but below sustain_mp, a PRE-ENGAGE
    ask would fire (cast spends to ~mp_cost) and the rest gate (< sustain_mp) fires the same tick
    -> 철면 then straight to sleep. Fix: a pre-engage ask needs sustain_mp (the rest floor), so the
    party either affords a whole fight or rests first. A mid-fight re-cast still only needs mp_cost."""
    from behavior.hooks.ironskin import ironskin_request_due
    kn = _load()
    mp_cost = kn["ironskin"]["mp_cost"]; sustain = kn["ironskin"]["sustain_mp"]
    marginal = (mp_cost + sustain) // 2                   # affords the cast, but not sustain
    lead = _leader(kn); sup = _supporter(kn, mp=(marginal, 3109))
    lead.partner = sup; sup.partner = lead
    lead.ironskin_zone = True; lead.ironskin_at = 0.0
    lead.together = True
    # PRE-ENGAGE at marginal supporter MP -> must NOT ask (would start an unsustainable fight)
    lead.cursor.scratch["_iron_want_at"] = now()
    assert not ironskin_request_due(lead), "pre-engage ask blocked below sustain_mp (no ask-then-sleep)"
    # MID-FIGHT at the same marginal MP -> DOES ask (fight already committed; protect it)
    lead.cursor.scratch.pop("_iron_want_at", None)
    lead.state.in_battle = True
    assert ironskin_request_due(lead), "mid-fight re-cast asked down to mp_cost"
    # pre-engage WITH sustain-level supporter MP -> asks (can see a whole fight through)
    lead.state.in_battle = False
    lead.cursor.scratch["_iron_want_at"] = now()
    sup.state.apply(ev.Status(hp=(1200, 1200), mp=(sustain + 50, 3109), mv=(400, 400)))
    assert ironskin_request_due(lead), "pre-engage ask fires once supporter MP >= sustain_mp"
    print("MP gate: pre-engage needs sustain_mp, mid-fight needs only mp_cost (no ask-then-sleep)")


def test_leader_wont_ask_without_mp_or_separated():
    from behavior.hooks.ironskin import ironskin_request_due
    kn = _load()
    lead = _leader(kn); lead.ironskin_zone = True; lead.ironskin_at = 0.0
    lead.state.in_battle = True                     # mid-fight -> isolate the tested cause
    # supporter too low even for a mid-fight re-cast (< mp_cost) -> don't ask (leave it to flee)
    sup = _supporter(kn, mp=(500, 2900)); sup.partner = lead; lead.partner = sup
    lead.together = True
    assert not ironskin_request_due(lead), "won't ask when the supporter can't afford it"
    # supporter has MP but we're NOT co-located -> it couldn't land the cast anyway
    sup2 = _supporter(kn); sup2.partner = lead; lead.partner = sup2
    lead.together = False
    assert not ironskin_request_due(lead), "won't ask when not co-located"
    # not a 철면 zone -> never ask
    lead.together = True; lead.ironskin_zone = False
    assert not ironskin_request_due(lead), "no ask outside a 철면 zone"
    print("supporter: no cast without MP, when separated, or outside a 철면 zone")


# --- 3. leader engage gate: never initiate a fight unprotected -----------------------------
def test_engage_gate():
    from behavior.hooks.hunting import hunt_step
    from gumiho import moblore
    kn = _load()
    c = _leader(kn); c.ironskin_zone = True
    for b in c.required_buffs():              # buffs up -> hunt_step won't re-ask them
        c.buffs.confirm(b)
    moblore.learn("개구리", "safe")            # known-safe -> no 고려 detour, straight to engage
    c.need_look = False
    c.state.entities = ["파란 개구리가 돌아다니고 있습니다."]
    # 철면 DOWN -> HOLD: not even a 고려, let alone an attack
    c.ironskin_at = 0.0
    sent = []
    hunt_step(c, Cmd(sent.append))
    assert not any(("개구리" in x) for x in sent), f"must not engage/consider unprotected: {sent}"
    # 철면 UP -> now it engages
    c.ironskin_at = now()
    sent = []
    hunt_step(c, Cmd(sent.append))
    assert any("개구리" in x and ("공격" in x or "연타" in x) for x in sent), \
        f"engages once 철면 is up: {sent}"
    print("engage gate: holds while 철면 is down, engages once it's up")


# --- 4. emergency flee: 철면 lapsed mid-fight, unrenewable -> flee --------------------------
def test_emergency_flee():
    from behavior.hooks.ironskin import ironskin_emergency, ironskin_flee
    kn = _load()
    lead = _leader(kn); lead.ironskin_zone = True; lead.state.in_battle = True
    lead.ironskin_at = 0.0                    # 철면 down mid-fight
    lead.ironskin_faded_at = now() - 100      # old fade -> the 2s hold doesn't apply here; this test
                                              # isolates the RENEWABILITY logic (see test_fade_flee_hold)
    sup = _supporter(kn); lead.partner = sup; sup.partner = lead
    lead.together = True

    # supporter CAN re-cast (MP ok, together) AND HP ≥ the cast floor -> renewable, hold
    lead.state.apply(ev.Status(hp=(3000, 3000), mp=(200, 200), mv=(400, 400)))   # 100% ≥ 55
    assert not ironskin_emergency(lead), "no flee while a re-cast can actually land"
    # THE DEATH SCENARIO: supporter has MP + co-located, but HP is at the 철면 cap (50%, below the
    # 55% cast floor) -> the re-cast CAN'T land -> FLEE at once (don't tank to death waiting).
    floor = kn["ironskin"].get("cast_min_hp_pct", 55)
    lead.state.apply(ev.Status(hp=(int(3000 * (floor - 5) / 100), 3000), mp=(200, 200), mv=(400, 400)))
    assert ironskin_emergency(lead), "flees the instant 철면 lapses below the castable floor (the kill)"
    # supporter OUT of MP -> also unrenewable -> FLEE
    lead.state.apply(ev.Status(hp=(3000, 3000), mp=(200, 200), mv=(400, 400)))   # HP fine again
    sup.state.apply(ev.Status(hp=(1200, 1200), mp=(100, 2900), mv=(400, 400)))
    assert ironskin_emergency(lead), "flees when the supporter can't re-cast"
    # 철면 up again -> no emergency (we're protected)
    sup.state.apply(ev.Status(hp=(1200, 1200), mp=(2900, 2900), mv=(400, 400)))
    lead.ironskin_at = now()
    assert not ironskin_emergency(lead), "no emergency while 철면 is up"
    print("emergency flee: bails the moment 철면 can't be renewed (HP-below-floor or no MP)")


def test_fade_flee_hold():
    """After 철면 FADES mid-fight, the emergency HOLDS fade_flee_hold secs before it 도망s (the
    leader now has the HP to tank a beat), then flees. A flee already in flight (flee_failed) skips
    the hold and retries at once."""
    from behavior.hooks.ironskin import ironskin_emergency
    kn = _load(); hold = kn["ironskin"].get("fade_flee_hold", 2.0)
    lead = _leader(kn); lead.ironskin_zone = True; lead.state.in_battle = True
    sup = _supporter(kn); lead.partner = sup; sup.partner = lead; lead.together = True
    # unrenewable (HP at the 철면 50% cap, below the 55% cast floor) so ONLY the hold gates the flee
    lead.state.apply(ev.Status(hp=(1500, 3000), mp=(200, 200), mv=(400, 400)))
    lead.ironskin_at = 0.0
    lead.ironskin_faded_at = now()               # 철면 JUST faded
    assert not ironskin_emergency(lead), "HOLD right after the fade — tank briefly, don't bolt"
    adv(hold + 0.1)
    assert ironskin_emergency(lead), "flees once the fade hold passes"
    # a FAILED 도망 must retry at once, even inside a fresh hold window (never trap a flee in flight)
    lead.ironskin_faded_at = now()
    lead.flee_failed = True
    assert ironskin_emergency(lead), "a failed 도망 skips the hold and retries immediately"
    print(f"fade hold: holds {hold}s after a mid-fight 철면 fade, then flees; fail-retry skips it")


def test_rest_to_recover_castable_hp_after_flee():
    """After a successful flee the leader sits at the 철면 cap (~50%, below the 55% cast floor) and
    can't re-cast 철면 or drink (potions fire below 50%). In a 철면 zone it must REST to regen back to
    castable HP — not hold stuck or wander into mobs it can't shield. When HP IS castable + supporter
    has MP, it must NOT rest (traverse to find the next target instead)."""
    from behavior.hooks.hunting import _low_vitals
    kn = _load()
    floor = kn["ironskin"].get("cast_min_hp_pct", 55)
    lead = _leader(kn, hp=(int(8816 * (floor - 5) / 100), 8816), mp=(100, 100))   # ~50%, below floor
    lead.ironskin_zone = True; lead.ironskin_at = 0.0; lead.state.in_battle = False
    sup = _supporter(kn, mp=(2900, 2900)); lead.partner = sup; sup.partner = lead
    # 철면 down, out of combat, HP below the cast floor -> REST to recover to castable
    assert _low_vitals(lead), "rest to recover when HP is below the 철면 cast floor (post-flee)"
    # HP back above the floor + supporter has MP -> do NOT rest (go traverse for the next target)
    lead.state.apply(ev.Status(hp=(int(8816 * (floor + 5) / 100), 8816), mp=(100, 100), mv=(400, 400)))
    assert not _low_vitals(lead), "castable HP + MP -> keep hunting (traverse), don't rest"
    # while 철면 is UP we fight at the 50% cap — must NOT rest even though 50% < the floor
    lead.state.apply(ev.Status(hp=(int(8816 * (floor - 5) / 100), 8816), mp=(100, 100), mv=(400, 400)))
    lead.ironskin_at = now()
    assert not _low_vitals(lead), "no rest while 철면 is up (fight at the cap, protected)"
    print("post-flee rest: recovers to castable HP when 철면 down & low; traverses when castable")


def _detect_flee(ctx, text):
    """Replicate webui's flee scan: gate on the FLEE-specific marker ([hunt].flee_mark), then read
    the direction. GENERAL (any hunt). The departure line alone ('…바람을 가르며 떠났…') is NOT the
    trigger — it prints on ordinary walking too (during which the game already auto-follows)."""
    import re
    mark = ctx.knowledge.get("hunt", {}).get("flee_mark")
    if (mark and ctx.role == "supporter" and ctx.partner_name
            and ctx.partner_name in text and mark in text):
        m = re.search(re.escape(ctx.partner_name) + r"[이가]\s*(동|서|남|북|위|아래)쪽?으로", text)
        if m:
            ctx.pending_chase = m.group(1)


def test_supporter_chases_fled_leader():
    """GENERAL regroup (any hunt): the supporter reads which way the leader fled and steps that way
    to catch up — so the pair never separates when the leader 도망s a fight (fixes 'could not follow')."""
    from behavior.hooks.common import partner_fled, chase_partner
    kn = _load()
    sup = _supporter(kn); sup.partner_name = "플레이어제로"
    # a NORMAL walk (departure line, NO flee marker) must NOT trigger a chase — the game auto-follows
    # ordinary moves, and false-firing here spammed a chase on every travel step
    _detect_flee(sup, "플레이어제로가 북쪽으로 바람을 가르며 떠났습니다.  당신은 플레이어제로를 따라갑니다.")
    assert sup.pending_chase is None, "a normal walk (no 도망치려) must not trigger a chase"
    # the exact two-line FLEE message (line 1 has the 도망치려 marker; line 2 the direction)
    msg = ("플레이어제로가 공포에 질려 도망치려 합니다!\n"
           "플레이어제로가 북쪽으로 바람을 가르며 떠났습니다.")
    _detect_flee(sup, msg)
    assert sup.pending_chase == "북", f"parsed the flee direction: {sup.pending_chase!r}"
    # THE FIX: a 도망 doesn't flip `together` false, so it stays STALE-TRUE — the chase must fire
    # ANYWAY (the old `not together` gate blocked it: detection fired, supporter never moved).
    sup.together = True
    assert partner_fled(sup), "chases even with stale-true `together` (a flee doesn't clear it)"
    sent = []
    chase_partner(sup, Cmd(sent.append))
    assert sent == ["북"], sent
    assert sup.pending_chase is None, "chase consumed (one step per flee line)"
    # REUNION clears pending_chase (access.py does this when `together` flips true on a fresh render
    # showing the partner) -> no more chasing. Model that clear:
    sup.pending_chase = "동"
    sup.on_event(ev.RoomSeen(title="어느 방", description="", exits=["동"],
                             entities=[f"{sup.partner_name}가 서 있습니다."]))
    assert sup.pending_chase is None, "reunion (partner re-sighted) clears the pending chase"
    assert not partner_fled(sup), "no chase once reunited (pending_chase cleared)"
    print("chase: fires on a flee even with stale-true together; reunion clears it")


def test_flee_retries_on_fail_but_graces_on_success():
    """도망 is stochastic. A SUCCESSFUL flee gets the grace (don't bolt room-to-room away from the
    supporter). A FAILED flee ("도망치지 못했습니다" -> flee_failed) leaves the leader trapped, so it
    must RETRY at once — the bug that got the leader killed was waiting out the grace on a failed flee."""
    from behavior.hooks.ironskin import ironskin_emergency, ironskin_flee
    kn = _load()
    lead = _leader(kn); lead.ironskin_zone = True; lead.state.in_battle = True
    lead.ironskin_at = 0.0
    sup = _supporter(kn, mp=(100, 2900)); lead.partner = sup; sup.partner = lead
    lead.together = True                      # unrenewable (supporter out of MP) -> emergency
    assert ironskin_emergency(lead), "emergency active (can't re-cast)"
    sent = []
    ironskin_flee(lead, Cmd(sent.append))
    assert sent == ["도망"] and lead.flee_failed is False, (sent, lead.flee_failed)
    # IN-FLIGHT: result not known yet -> held by the grace (don't re-send 도망 before we know)
    assert not ironskin_emergency(lead), "held while the 도망 result is in flight (no re-send)"
    # SUCCESS line ("당신은 간신히 도망쳤습니다") clears the grace -> if the NEW room is also lethal,
    # re-flee at once (don't tank out a stale grace). webui sets _iron_flee_at = 0.0 on that line:
    lead._iron_flee_at = 0.0
    assert ironskin_emergency(lead), "success clears the grace -> can re-flee a lethal new room now"
    # FAIL line ("도망치지 못했습니다") -> flee_failed -> RETRY immediately, ignoring the grace
    ironskin_flee(lead, Cmd([].append))          # re-arm the grace
    lead.flee_failed = True
    assert ironskin_emergency(lead), "failed 도망 retries at once (does NOT wait out the grace)"
    ironskin_flee(lead, Cmd([].append))
    assert lead.flee_failed is False, "each retry clears the flag; webui re-sets it if it fails again"
    print("flee: in-flight grace, cleared on success, retries at once on a fail")


# --- 5. rest gate: rest when the supporter can no longer sustain 철면 -----------------------
def test_rest_on_supporter_mp():
    from behavior.hooks.hunting import _low_vitals
    kn = _load()
    lead = _leader(kn, hp=(3000, 3000), mp=(200, 200))   # leader vitals fine
    lead.ironskin_zone = True
    sup = _supporter(kn, mp=(2900, 2900)); lead.partner = sup; sup.partner = lead
    assert not _low_vitals(lead), "healthy leader + well-stocked supporter -> no rest"
    # supporter MP falls below sustain_mp (~2 casts of headroom) -> rest to recover it
    sustain = kn["ironskin"]["sustain_mp"]
    sup.state.apply(ev.Status(hp=(1200, 1200), mp=(int(sustain) - 100, 2900), mv=(400, 400)))
    assert _low_vitals(lead), "rest when the supporter can't sustain 철면"

    # CAST-THEN-SLEEP FIX: while a 철면 fight is being COMMITTED, do NOT rest for supporter MP,
    # even though it's below sustain — otherwise the leader casts 철면 then sleeps before attacking.
    lead._iron_req_at = now()                          # just asked 철면 (cast in flight)
    assert not _low_vitals(lead), "no rest while a 철면 ask is still in flight (committing)"
    lead._iron_req_at = now() - (kn["ironskin"]["recast_retry"] + 1)   # ask resolved long ago
    lead.ironskin_at = now()                           # 철면 is UP -> use it, don't sleep on it
    assert not _low_vitals(lead), "no rest while 철면 is up (USE the cast, don't waste it)"
    lead.ironskin_at = 0.0; lead.state.in_battle = True
    assert not _low_vitals(lead), "no supporter-MP rest mid-battle (finish the fight)"
    lead.state.in_battle = False                        # fight over, 철면 down, not committing
    assert _low_vitals(lead), "NOW rest to refill (fight done, 철면 down, low MP)"

    # the same low supporter MP is irrelevant OUTSIDE a 철면 zone
    lead.ironskin_zone = False
    assert not _low_vitals(lead), "supporter MP doesn't force a rest outside a 철면 zone"
    print("rest gate: refills supporter MP, but NEVER mid-commit/up/battle (no cast-then-sleep)")


def test_combo_suppressed_while_unprotected():
    """The HP-crash + thrash root: 연타 kept firing while 철면 was down, soaking lethal damage. In a
    철면 zone the combo continues ONLY while 철면 is up; down = no offense. Non-철면 zones unaffected."""
    from behavior.hooks.hunting import combo_continue
    kn = _load()
    lead = _leader(kn, job="장군"); lead.state.level = 70      # combo-eligible
    lead.state.in_battle = True; lead.last_combat_at = now()   # fighting()
    lead.combo_ready = True
    # non-철면 zone: combo continues regardless (existing behavior preserved)
    lead.ironskin_zone = False
    assert combo_continue(lead), "combo continues normally outside a 철면 zone"
    # 철면 zone, 철면 UP -> combo continues (we're protected, keep hitting)
    lead.ironskin_zone = True; lead.ironskin_at = now()
    assert combo_continue(lead), "combo continues while 철면 is up"
    # 철면 zone, 철면 DOWN -> NO combo (don't beat on a mob while unprotected)
    lead.ironskin_at = 0.0
    assert not combo_continue(lead), "combo suppressed while 철면 is down (no unprotected offense)"
    print("combo gate: 연타 only while protected in a 철면 zone (stops the HP crash)")


def test_buffs_maintained_only_out_of_combat():
    """Buffs (방비/빨리가기/분노) ARE maintained — including in 철면 zones (user wants them) — but the
    leader requests them ONLY out of combat. A mid-fight cast doesn't take (no landed line) so it
    never confirms and gets re-asked every 8s (the '빨리가기/분노 requested so frequently' waste).
    Between fights the cast lands and holds for its full duration."""
    from behavior.hooks.hunting import _maintain_buffs
    kn = _load()
    assert _leader(kn, job="장군").required_buffs(), "sanity: 장군 has buffs to maintain"
    # OUT of combat -> it requests the missing buffs (말 "<buff>!") — in ANY zone, 철면 or not
    for zone in (False, True):
        lead = _leader(kn, job="장군")                    # fresh ctx (buff-reask throttle is per-ctx)
        lead.ironskin_zone = zone; lead.state.in_battle = False
        sent = []
        _maintain_buffs(lead, Cmd(sent.append))
        assert any(x.endswith("! 말") for x in sent), f"maintains buffs out of combat (iron={zone}): {sent}"
    # IN combat -> NOTHING (no mid-fight re-request spam; the cast wouldn't confirm anyway)
    lead = _leader(kn, job="장군"); lead.ironskin_zone = True; lead.state.in_battle = True
    sent = []
    _maintain_buffs(lead, Cmd(sent.append))
    assert sent == [], f"no buff requests mid-combat: {sent}"
    print("buffs: maintained out of combat (any zone), never re-requested mid-fight")


# --- 6. reset_sweep loads/clears the per-zone flag -----------------------------------------
def test_reset_sweep_loads_flag():
    from behavior.hooks.hunting import reset_sweep
    kn = _load()
    cmd = Cmd(lambda _l: None)
    c = _leader(kn); c.hunt_target = "템플 1층"
    reset_sweep(c, cmd)
    assert c.ironskin_zone is True, "템플 1층 is a 철면 zone"
    # a different zone with no flag clears it (and any stale timer)
    c.ironskin_at = now()
    c.hunt_target = "삼국시대"
    reset_sweep(c, cmd)
    assert c.ironskin_zone is False, "a non-철면 zone clears the flag"
    assert c.ironskin_at == 0.0, "dropping the flag also drops the stale immunity timer"
    print("reset_sweep: loads 철면 flag in 템플 1층, clears it (and the timer) elsewhere")


def _set(c, hp=None, mp=None):
    v = c.state.vitals
    hp = hp or (v.hp, v.hp_max)
    mp = mp or (v.mp, v.mp_max)
    c.state.apply(ev.Status(hp=hp, mp=mp, mv=(400, 400)))


# --- 7. rest-release: in a 철면 zone the leader stops resting HP at the cap, not 95% ---------
def test_ironskin_rest_release():
    """The leader is capped to ~50% max HP the instant 철면 lands, so resting its HP to 95% is a
    wasted sleep — the top half is discarded on the next cast. In a 철면 zone both_recovered lets
    the leader wake at ~55% HP, while the supporter's MP (the real bottleneck) still banks to full."""
    from behavior.hooks.hunting import both_recovered
    kn = _load()
    floor = kn["ironskin"]["rest_hp_pct"]                    # the 철면-zone leader HP wake floor
    full = kn.get("rest", {}).get("full_pct", 95)
    above = int(8816 * (floor + 8) / 100)                    # comfortably above the floor
    below = int(8816 * (floor - 8) / 100)                    # comfortably below it
    lead = _leader(kn, hp=(above, 8816), mp=(100, 100))      # above the 철면 floor, full MP
    sup = _supporter(kn, mp=(int(3109 * (full + 1) / 100), 3109))   # MP banked past full_pct
    lead.partner = sup; sup.partner = lead
    lead.ironskin_zone = True                                # supporter learns via partner OR-check

    assert both_recovered(lead), "철면 zone: leader above the HP floor + supporter MP banked -> wake"
    assert both_recovered(sup), "symmetric from the supporter's side"

    # leader HP below the 철면 floor -> keep resting (still recovers the safe floor)
    _set(lead, hp=(below, 8816))
    assert not both_recovered(lead), "below the 철면 rest floor -> keep sleeping"

    # supporter MP not yet banked -> keep resting (MP is the real gate)
    _set(lead, hp=(above, 8816))                             # HP fine again
    _set(sup, mp=(int(3109 * (full - 15) / 100), 3109))     # below full_pct
    assert not both_recovered(lead), "supporter MP still low -> keep sleeping to bank casts"

    # OUTSIDE a 철면 zone the leader's 60% HP is NOT enough -> the cap only applies in 철면 zones
    _set(sup, mp=(2985, 3109))                               # supporter fine
    lead.ironskin_zone = False; sup.ironskin_zone = False
    assert not both_recovered(lead), "non-철면 zone: leader still needs full HP (no cap)"
    print("rest release: 철면 zone wakes the leader at the HP cap; supporter MP banks to full")


def test_ironskin_fade_prediction():
    """철면 fades on the server's fixed 37.5s expiry grid (measured: fade mod 37.5 stdev 0.10s). Given
    a grid anchor (a prior fade) the fade is predicted EXACTLY = first grid tick at/after land+base;
    before any fade is observed it falls back to a nominal estimate."""
    from gumiho.access import ironskin_predict
    tick, base, nom = 37.5, 36.75, 56.0
    # no anchor yet -> nominal fallback
    assert ironskin_predict(1000.0, 0.0, tick, base, nom) == 1000.0 + nom
    anchor = 1000.0                       # a prior fade -> grid at 1000, 1037.5, 1075, 1112.5, ...
    assert abs(ironskin_predict(1010.0, anchor, tick, base, nom) - 1075.0) < 1e-6   # land+base=1046.75
    assert abs(ironskin_predict(1005.0, anchor, tick, base, nom) - 1075.0) < 1e-6   # 1041.75 -> 1075
    assert abs(ironskin_predict(1000.1, anchor, tick, base, nom) - 1037.5) < 1e-6   # 1036.85 -> 1037.5
    # the prediction ALWAYS lands on the grid (== anchor mod tick), whatever the cast time
    for L in (1234.5, 1300.0, 1355.2, 1400.9):
        pf = ironskin_predict(L, anchor, tick, base, nom)
        assert abs((pf - anchor) % tick) < 1e-6 and pf >= L + base
    print("ironskin_predict: fades land on the learned 37.5s grid; nominal before an anchor")


def test_ironskin_predict_matches_real_logs():
    """Sanity vs real data: anchor on one observed fade, predict the next casts, expect ~0 error."""
    from gumiho.access import ironskin_predict
    tick, base, nom = 37.5, 36.75, 56.0
    # (land, fade) monotonic-equivalent samples from a real session (seconds-of-day), consecutive:
    samples = [(55341.7, 55412.3), (55497.5, 55562.3), (55653.6, 55712.3), (55821.8, 55862.4)]
    anchor = samples[0][1]                # learn the grid phase from the first observed fade
    errs = [abs(ironskin_predict(L, anchor, tick, base, nom) - F) for L, F in samples[1:]]
    assert max(errs) < 1.5, f"grid prediction should match real fades within ~1s: {errs}"
    print(f"ironskin_predict vs real logs: max error {max(errs):.2f}s")


if __name__ == "__main__":
    test_detection_and_backstop()
    test_leader_requests_only_when_down()
    test_no_double_cast_race()
    test_full_protocol_supporter_casts_on_hearing()
    test_no_request_below_castable_hp()
    test_no_request_when_idle_the_bug()
    test_pre_engage_needs_sustain_mid_fight_needs_mp_cost()
    test_leader_wont_ask_without_mp_or_separated()
    test_engage_gate()
    test_emergency_flee()
    test_fade_flee_hold()
    test_rest_to_recover_castable_hp_after_flee()
    test_supporter_chases_fled_leader()
    test_flee_retries_on_fail_but_graces_on_success()
    test_rest_on_supporter_mp()
    test_combo_suppressed_while_unprotected()
    test_buffs_maintained_only_out_of_combat()
    test_reset_sweep_loads_flag()
    test_ironskin_rest_release()
    test_ironskin_fade_prediction()
    test_ironskin_predict_matches_real_logs()
    print("\nALL 철면사냥 TESTS PASSED")
