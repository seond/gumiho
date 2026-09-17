"""CharCtx.hunt_zone() canonicalizes a whitespace-variant zone name to the exact hunting-map key,
so a target typed/rendered without the space ("역사의길") still resolves to the FixedMap "역사의 길".
A miss here silently drops the hunt to the gridsweep — losing the flee map-sync (note_flee), the
per-zone blacklist, and the 철면 flag — which is exactly the "nothing happened after fleeing" bug."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import events as ev
from gumiho.access import CharCtx
from gumiho.reload import get_knowledge, Reloader
from gumiho.state import WorldState

CLOCK = [1000.0]
def now(): return CLOCK[0]


def _ctx(target):
    Reloader(lambda: []).load_all()
    st = WorldState()
    st.apply(ev.Status(hp=(400, 400), mp=(100, 100), mv=(200, 200), job="장군"))
    ctx = CharCtx("a", "leader", st, get_knowledge(), set(), name="영웅", now=now)
    ctx.hunt_target = target
    return ctx


def test_exact_key_passes_through():
    ctx = _ctx("역사의 길")
    assert ctx.hunt_zone() == "역사의 길"
    print("hunt_zone: exact map key passes through unchanged")


def test_spaceless_name_canonicalizes_to_map_key():
    ctx = _ctx("역사의길")                                  # NO space — what broke the flee sync
    assert ctx.hunt_zone() == "역사의 길", ctx.hunt_zone()
    # and it now resolves to a real map (reset_sweep would build a FixedMap, not None)
    hmap = ctx.knowledge.get("hunting_maps", {}).get(ctx.hunt_zone() or "")
    assert hmap is not None, "canonical zone must retrieve the hunting map"
    print("hunt_zone: '역사의길' -> map key '역사의 길' (FixedMap loads, not gridsweep)")


def test_unmapped_zone_is_unchanged():
    ctx = _ctx("존재하지않는사냥터")
    assert ctx.hunt_zone() == "존재하지않는사냥터", "a zone with no map is returned verbatim"
    print("hunt_zone: an unmapped zone name is left untouched (gridsweep)")


def test_no_target_falls_back_to_default():
    ctx = _ctx(None)
    assert ctx.hunt_zone() == ctx.knowledge.get("hunt", {}).get("zone")
    print("hunt_zone: no target -> the configured default zone")


if __name__ == "__main__":
    test_exact_key_passes_through()
    test_spaceless_name_canonicalizes_to_map_key()
    test_unmapped_zone_is_unchanged()
    test_no_target_falls_back_to_default()
    print("\nALL HUNT-ZONE TESTS PASSED")
