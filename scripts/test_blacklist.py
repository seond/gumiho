"""Per-zone mob BLACKLIST: mobs we deliberately never attack in a given zone, loaded from that
zone's hunting map when the hunt arrives (reset_sweep). Distinct from [mob_danger] (too strong
to survive) — these are beatable but nasty to recover from, e.g. 역사의 길's 마왕 (BLIND)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho.access import CharCtx
from gumiho.command import Cmd
from gumiho.reload import get_knowledge, Reloader
from gumiho.state import WorldState

Reloader(lambda: []).load_all()
from behavior.hooks.hunting import _pick_target, reset_sweep   # noqa: E402

now = lambda: 0.0


def _ctx(zone=None):
    c = CharCtx("a", "leader", WorldState(), get_knowledge(), set(), name="영웅", now=now)
    c.hunt_target = zone
    return c


def test_blacklisted_mob_is_never_targeted():
    c = _ctx()
    c.blacklist = ["마왕"]
    # 마왕 present alongside a normal mob -> the normal mob is chosen, 마왕 skipped
    c.state.entities = ["역사의 마왕이 당신을 노려봅니다.", "거미가 기어가고 있다."]
    tgt = _pick_target(c)
    assert tgt and "마왕" not in tgt, f"must not target 마왕, got {tgt!r}"
    assert "거미" in tgt, tgt
    # a LONE blacklisted mob -> no target (we walk past it, never engage)
    c.state.entities = ["역사의 마왕이 당신을 노려봅니다."]
    assert _pick_target(c) is None, "a lone blacklisted mob yields no target"
    print("blacklist: 마왕 never targeted; a lone 마왕 yields no target")


def test_without_blacklist_the_same_mob_would_be_attacked():
    """Proves the blacklist is what skips it — the identical 마왕 is a normal target otherwise."""
    c = _ctx()
    c.blacklist = []                                   # no blacklist for this zone
    c.state.entities = ["역사의 마왕이 당신을 노려봅니다."]
    assert _pick_target(c) is not None, "without the blacklist, 마왕 is a normal target"
    print("blacklist off: 마왕 would be targeted -> the blacklist is the thing skipping it")


def test_reset_sweep_loads_per_zone():
    cmd = Cmd(lambda _l: None)
    c = _ctx("역사의 길")
    reset_sweep(c, cmd)
    # loaded straight from the 역사의 길 map (마왕 blinds; 교황/한웅천황 likewise nasty to fight)
    assert c.blacklist == ["마왕", "교황", "한웅천황"], c.blacklist
    # a different zone (no blacklist declared) clears it — the list never leaks across zones
    c2 = _ctx("삼국시대")
    c2.blacklist = ["마왕"]                             # pretend it carried over
    reset_sweep(c2, cmd)
    assert c2.blacklist == [], f"a zone with no blacklist clears it, got {c2.blacklist}"
    print("reset_sweep: loads the per-zone blacklist on arrival, clears it in zones without one")


if __name__ == "__main__":
    test_blacklisted_mob_is_never_targeted()
    test_without_blacklist_the_same_mob_would_be_attacked()
    test_reset_sweep_loads_per_zone()
    print("\nALL BLACKLIST TESTS PASSED")
