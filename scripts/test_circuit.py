"""Hunting CIRCUIT: rotate through several zones so a swept zone respawns while we hunt the
next, instead of idling in `wait`. Includes the anti-thrash guard: don't rotate into a zone
that is ALSO freshly empty (both-barren) — fall to the bounded `wait` instead, and rotate only
once the next zone has had a respawn window. This can never get stuck."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho.access import CharCtx
from gumiho.reload import get_knowledge, Reloader
from gumiho.state import WorldState

Reloader(lambda: []).load_all()
from behavior.hooks.hunting import (                       # noqa: E402
    advance_circuit, circuit_ready, mark_zone_cleared, _zone_cleared_at,
)

WINDOW = get_knowledge().get("hunt", {}).get("regen_check", 180)


class Clock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t


def _duo(zones, clk):
    a = CharCtx("a", "leader", WorldState(), get_knowledge(), set(), name="영웅", now=clk)
    b = CharCtx("b", "supporter", WorldState(), get_knowledge(), set(), name="짝꿍", now=clk)
    a.partner, b.partner = b, a
    for c in (a, b):
        c.circuit = list(zones)
        c.circuit_idx = 0
        c.hunt_target = zones[0] if zones else None
    return a, b


def test_advance_rotates_both_and_wraps():
    """The leader drives the rotation but advances BOTH ctxs (the follower is pulled to travel
    and needs the same target for its arrival reset_sweep), and it wraps around."""
    a, b = _duo(["삼국시대", "올림푸스 신전"], Clock())
    advance_circuit(a, None)
    assert a.hunt_target == "올림푸스 신전" and b.hunt_target == "올림푸스 신전"
    assert a.circuit_idx == 1 and b.circuit_idx == 1, "follower rotated in lockstep"
    advance_circuit(a, None)
    assert a.hunt_target == "삼국시대" and b.hunt_target == "삼국시대" and a.circuit_idx == 0
    print("advance_circuit: leader rotates BOTH ctxs, wraps around")


def test_single_zone_is_a_noop():
    """A 1-zone (or empty) circuit never rotates — plain single-zone hunt -> `wait`."""
    a, _ = _duo(["삼국시대"], Clock())
    advance_circuit(a, None)
    assert a.circuit_idx == 0 and a.hunt_target == "삼국시대"
    assert circuit_ready(a) is False, "single-zone circuit never rotates"
    a0, _ = _duo([], Clock())
    assert circuit_ready(a0) is False, "no circuit -> never rotates"
    print("circuit: single/empty is a no-op (falls to wait)")


def test_ready_when_next_zone_unseen():
    """First rotation: the next zone was never cleared this session -> rotate straight in."""
    _zone_cleared_at.clear()
    a, _ = _duo(["삼국시대", "올림푸스 신전"], Clock())   # at 삼국시대, next = 올림푸스 (unseen)
    assert circuit_ready(a) is True, "an unseen next zone is ready to hunt"
    print("circuit_ready: unseen next zone -> rotate")


def test_anti_thrash_and_never_stuck():
    """Both zones freshly empty: do NOT rotate into the empty next zone (thrash) — wait here.
    After a respawn window, the next zone is ready and circuit_ready flips True (never stuck)."""
    _zone_cleared_at.clear()
    clk = Clock()
    a, _ = _duo(["삼국시대", "올림푸스 신전"], clk)        # at 삼국시대, next = 올림푸스
    # we just cleared BOTH zones -> stamp 올림푸스 (the next zone) as empty right now
    _zone_cleared_at["올림푸스 신전"] = clk.t
    assert circuit_ready(a) is False, "next zone just emptied -> DON'T rotate (would thrash); wait"
    clk.t += WINDOW - 1
    assert circuit_ready(a) is False, "still inside the respawn window -> keep waiting"
    clk.t += 2                                            # now past the window
    assert circuit_ready(a) is True, "respawn window elapsed -> next zone ready -> rotate"
    print("anti-thrash: no rotate into an empty zone; rotates once respawned (never stuck)")


def test_mark_zone_cleared_stamps_current():
    """mark_zone_cleared stamps the CURRENT hunt zone at the current time."""
    _zone_cleared_at.clear()
    clk = Clock(5000.0)
    a, _ = _duo(["삼국시대", "올림푸스 신전"], clk)
    mark_zone_cleared(a, None)
    assert _zone_cleared_at.get("삼국시대") == 5000.0, _zone_cleared_at
    print("mark_zone_cleared: stamps the current zone's clear-time")


if __name__ == "__main__":
    test_advance_rotates_both_and_wraps()
    test_single_zone_is_a_noop()
    test_ready_when_next_zone_unseen()
    test_anti_thrash_and_never_stuck()
    test_mark_zone_cleared_stamps_current()
    print("\nALL CIRCUIT TESTS PASSED")
