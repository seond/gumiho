"""Party formation: the supporter FOLLOWS but does NOT auto-assist. 자동지원 defaults to
ON every login and would pull the supporter into combat (where some buff spells can't be
cast), so form_party actively toggles it OFF and keeps it off."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho.access import CharCtx
from gumiho.command import Cmd
from gumiho.reload import get_knowledge, Reloader
from gumiho.state import WorldState

CLOCK = [1000.0]
def now(): return CLOCK[0]


def _ctx(role, name, partner):
    Reloader(lambda: []).load_all()
    from behavior.hooks import party  # noqa: F401
    ctx = CharCtx("b" if role == "supporter" else "a", role, WorldState(),
                  get_knowledge(), set(), name=name, now=now)
    ctx.partner_name = partner
    return ctx


def test_default_assist_on_is_true():
    # A fresh connect assumes 자동지원 ON (the server default) so it gets toggled off once.
    ctx = _ctx("supporter", "돕", "영웅")
    assert ctx.assist_on is True, "assist_on must start True (login default is ON)"
    print("default: assist_on starts True (login default ON) so it will be turned off")


def test_supporter_follows_and_turns_off_autoassist():
    from behavior.hooks.party import form_party
    ctx = _ctx("supporter", "돕", "영웅")
    ctx.assist_on = True
    sent = []
    form_party(ctx, Cmd(sent.append))
    assert "영웅 따라" in sent, f"supporter must follow: {sent}"
    assert "자동지원" in sent, f"supporter must toggle auto-assist OFF: {sent}"
    assert ctx.assist_on is False, "assist_on tracked off after the toggle"

    # already off -> never re-send (that would toggle it back ON and rejoin combat)
    sent.clear()
    form_party(ctx, Cmd(sent.append))
    assert "자동지원" not in sent, f"must NOT re-send once off: {sent}"
    assert "영웅 따라" in sent, "still follows"
    print("supporter: follows + toggles 자동지원 OFF once, never re-sends it")


def test_leader_never_sends_autoassist():
    from behavior.hooks.party import form_party
    ctx = _ctx("leader", "영웅", "돕")
    ctx.assist_on = True
    sent = []
    form_party(ctx, Cmd(sent.append))
    assert sent == ["모두 그룹"], f"leader only groups: {sent}"
    print("leader: only 모두 그룹, never 따라/자동지원")


def _link(a, b):
    a.partner = b; b.partner = a


def test_form_party_includes_then_excludes_by_checkbox():
    """Per-supporter inclusion (2026-09-17): form_party 모두 그룹 (baseline: everyone IN), then a
    '<name> 그룹' toggle for each supporter the operator EXCLUDED (in_group=False). 모두 그룹 resets
    everyone to IN first, so one toggle flips each excluded one OUT — deterministic across regroups."""
    from behavior.hooks.party import form_party
    lead = _ctx("leader", "영웅", "돕")
    s1 = _ctx("supporter", "돕", "영웅"); s1.sid = "b"
    s2 = _ctx("supporter", "둘", "영웅"); s2.sid = "c"
    lead.partners = [s1, s2]
    # all included (default) -> just 모두 그룹
    sent = []; form_party(lead, Cmd(sent.append))
    assert sent == ["모두 그룹"], f"everyone in -> only 모두 그룹: {sent}"
    # exclude s2 -> 모두 그룹 then toggle 둘 OUT
    s2.in_group = False
    sent = []; form_party(lead, Cmd(sent.append))
    assert sent == ["모두 그룹", "둘 그룹"], f"excluded one -> toggle it out: {sent}"
    # exclude both -> toggle each out (in group order)
    s1.in_group = False
    sent = []; form_party(lead, Cmd(sent.append))
    assert sent == ["모두 그룹", "돕 그룹", "둘 그룹"], f"both excluded: {sent}"
    print("form_party: 모두 그룹 baseline, then '<name> 그룹' toggles out each un-checked supporter")


def test_group_gate_permissive_when_level_unknown():
    from behavior.hooks.party import form_party, group_level_ok
    lead = _ctx("leader", "영웅", "돕"); sup = _ctx("supporter", "돕", "영웅")
    _link(lead, sup)
    lead.state.level = None; sup.state.level = 300   # a level not yet read
    assert group_level_ok(lead) is True
    sent = []; form_party(lead, Cmd(sent.append))
    assert sent == ["모두 그룹"], f"unknown level -> allow group (no stall): {sent}"
    print("group gate: unknown levels -> allow (never stalls grouping on a missing 점수 read)")


def test_supporter_follows_even_when_out_leveled():
    # the gate withholds only the LEADER's 그룹; the supporter still 따라s + buffs regardless
    from behavior.hooks.party import form_party
    sup = _ctx("supporter", "돕", "영웅"); sup.assist_on = True
    sent = []; form_party(sup, Cmd(sent.append))
    assert "영웅 따라" in sent, f"supporter follows regardless of the group gate: {sent}"
    print("group gate: supporter still 따라s/buffs even when left out of the group")


def test_multi_supporter_group_is_number_agnostic():
    """The overhaul: a leader with SEVERAL supporters. Togetherness needs EVERY supporter present;
    the level gate blocks 모두 그룹 if ANY out-levels; rest 'recovered'/low-vitals scan them all."""
    from gumiho import events as ev
    from behavior.hooks.party import group_level_ok
    from behavior.hooks.hunting import both_recovered, separated, _low_vitals
    lead = _ctx("leader", "영웅", "돕")
    s1 = _ctx("supporter", "돕", "영웅"); s1.sid = "b"
    s2 = _ctx("supporter", "둘", "영웅"); s2.sid = "c"
    lead.partners = [s1, s2]; s1.partner = lead; s2.partner = lead
    for c in (lead, s1, s2):
        c.state.apply(ev.Status(hp=(100, 100), mp=(100, 100), mv=(100, 100), job="전사"))
        c.state.level = 100
    seen = lambda names: ev.RoomSeen(title="방", description="", exits=["북"],
                                     entities=[f"{n}가 서 있다" for n in names])
    # together needs ALL supporters in the room
    lead.on_event(seen(["돕"]))
    assert lead.together is False, "only one of two supporters present -> NOT together"
    lead.on_event(seen(["돕", "둘"]))
    assert lead.together is True, "both supporters present -> together"
    # a drifted supporter -> separated after the debounce
    lead.on_event(seen(["돕"]))
    CLOCK[0] += 30
    lead.on_event(seen(["돕"]))
    assert separated(lead) is True, "one supporter gone past the debounce -> separated"
    # level gate: ANY higher-level supporter withholds 모두 그룹
    s1.state.level, s2.state.level = 90, 100
    assert group_level_ok(lead) is True
    s2.state.level = 101
    assert group_level_ok(lead) is False, "one higher-level supporter -> withhold 모두 그룹"
    s2.state.level = 100
    # rest: every member must be recovered; the leader rests for ANY supporter's low vitals
    lead.on_event(seen(["돕", "둘"]))
    assert both_recovered(lead) is True, "all full -> group recovered"
    s2.state.apply(ev.Status(hp=(8, 100), mp=(100, 100), mv=(100, 100)))
    assert both_recovered(lead) is False, "one supporter low -> group NOT recovered"
    assert _low_vitals(lead) is True, "leader picks up a supporter's low vitals (any of N)"
    # and from the supporter's view, the group includes its sibling + the leader
    from behavior.hooks.hunting import _group_ctxs
    assert set(id(c) for c in _group_ctxs(s1)) == {id(lead), id(s2)}, "supporter sees leader + sibling"
    print("multi-supporter: together needs ALL; separated on a drifter; level-gate & rest scan every supporter")


if __name__ == "__main__":
    test_default_assist_on_is_true()
    test_supporter_follows_and_turns_off_autoassist()
    test_leader_never_sends_autoassist()
    test_form_party_includes_then_excludes_by_checkbox()
    test_group_gate_permissive_when_level_unknown()
    test_supporter_follows_even_when_out_leveled()
    test_multi_supporter_group_is_number_agnostic()
    print("\nALL PARTY TESTS PASSED")
