"""Combat-verb detection: attack lines must register as combat (-> in_battle) so hunt_step knows a
fight is live and doesn't fall into the anti-spam 봐 loop. REGRESSION (2026-09-11): the 한성의 하수구
sewer verbs 쑤셨 (stab, the leader's hit) / 물어뜯 (rip-bite, the mob's) were missing from _HIT_VERBS
(built only from a 훈련장 fight), so in_battle never stuck and the hunt spammed 봐 and drifted."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import events as ev
from gumiho.parser import COMBAT_DEALT_RE, COMBAT_TAKEN_RE
from gumiho.state import WorldState


def test_sewer_hit_verbs_detected():
    # the leader DEALS a stab (쑤시다) — the sewer mobs' hit verb
    assert COMBAT_DEALT_RE.match("당신은 개구리를 무지막지하게 쑤셨습니다"), "쑤셨 must register as dealt"
    assert COMBAT_DEALT_RE.match("당신은 쥐를 쌍코피가 터지도록 쑤셨습니다"), "쑤셨 variant"
    assert COMBAT_DEALT_RE.match("당신은 개구리를 찌르고 있습니다"), "찌르 must register as dealt"
    # a mob's rip-bite (even a miss means a fight is on)
    assert COMBAT_TAKEN_RE.match("개구리가 당신을 물어뜯으려 했지만 실패했습니다"), "물어뜯 must register as taken"
    # the original 훈련장 verbs still work
    assert COMBAT_TAKEN_RE.match("쥐가 당신을 간신히 물었습니다"), "물었 still detected"
    assert COMBAT_DEALT_RE.match("당신은 허수아비를 힘껏 때렸습니다"), "때렸 still detected"
    print("combat verbs: 쑤셨/찌르/물어뜯 now register (plus the original set)")


def test_combat_hit_sets_in_battle():
    st = WorldState()
    assert st.in_battle is False
    st.apply(ev.CombatHit("dealt", "개구리", "당신은 개구리를 쑤셨습니다"))
    assert st.in_battle is True, "a recognized hit sets in_battle (so hunt_step won't 봐-loop)"
    print("combat: a recognized hit sets in_battle")


def test_non_combat_line_not_matched():
    # plain 당신은 sentences must NOT be mistaken for combat (the verb whitelist's whole point)
    assert not COMBAT_DEALT_RE.match("당신은 최선을 다하고 있습니다"), "non-combat 당신은 line is not a hit"
    assert not COMBAT_DEALT_RE.match("당신은 피부가 쇠로 변하는 것을 느낍니다"), "the 철면 land line is not a hit"
    print("combat: non-combat 당신은 lines are not false-matched")


if __name__ == "__main__":
    test_sewer_hit_verbs_detected()
    test_combat_hit_sets_in_battle()
    test_non_combat_line_not_matched()
    print("\nALL COMBAT-VERB TESTS PASSED")
