"""Party formation: follow / group / auto-assist, and the co-location gate."""

from gumiho.registry import action, guard


@guard("group_level_ok")
def group_level_ok(s):
    """Group only when EVERY supporter's level is <= the leader's (user rule, 2026-09-11) — so a
    higher-level supporter doesn't drag the party's XP share. `모두 그룹` is all-or-nothing (it groups
    everyone present), so we withhold it whenever ANY supporter is KNOWN to out-level the leader;
    unknown levels -> allow (levels are read at connect via 점수, so this only ever suppresses a
    confirmed higher-level supporter, never stalls on a missing read). Those supporters still FOLLOW
    + buff; only the formal 그룹 (XP-sharing) is withheld."""
    if s.role != "leader" or not s.group:
        return True
    lead = s.state.level
    if lead is None:
        return True
    return not any(p.state.level is not None and p.state.level > lead for p in s.group)


@action("form_party")
def form_party(s, cmd):
    # POLICY (2026-08-31): hunting got too dangerous for the supporter to chime into battles,
    # so 자동지원 is turned OFF — the supporter FOLLOWS (따라) and casts the leader's requested
    # buffs (방비/빨리가기/분노 via the 말 protocol) but does NOT auto-assist in combat. 자동지원
    # defaults ON every login and is a blind toggle with no server feedback, so send it EXACTLY
    # ONCE per login; the per-ctx `_autoassist_off` flag (reset with a fresh ctx on reconnect)
    # stops a re-form (regroup/resupply) from toggling it back ON.
    if s.role == "leader":
        # PER-SUPPORTER INCLUSION (2026-09-17): 모두 그룹 is the baseline — it groups EVERYONE present
        # (resets all to IN). Then toggle OUT the supporters the operator has EXCLUDED (checkbox off,
        # in_group=False) with "<name> 그룹". Because 모두 그룹 first resets everyone to IN, each
        # excluded one needs exactly one toggle (IN->OUT) — deterministic, no drift across regroups.
        # (Replaces the old blanket level-gate withhold; the operator now excludes precisely.)
        cmd.send("모두 그룹")
        for p in s.group:
            if not getattr(p, "in_group", True) and p.name:
                cmd.send(f"{p.name} 그룹")
        return
    if s.partner_name:
        cmd.send(f"{s.partner_name} 따라")
        if s.assist_on:                    # login default ON -> toggle it OFF once (assist_on
            cmd.send("자동지원")            # is server-truth thereafter; a fresh ctx on reconnect
            s.assist_on = False            # resets it True so we re-toggle after a re-login)


@guard("party_ready")
def party_ready(s):
    if not s.has_group():
        return True                    # solo: nothing to pair with
    return s.together                  # group-wide sighting: ALL members present
