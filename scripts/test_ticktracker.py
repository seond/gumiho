"""Master-tick tracking layer: learns the ~74.5s tick phase from observed boundary events (buff
fades), reports sync status, and exposes the grid (incl. the 철면 half-tick) for effects on top."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gumiho.ticktracker import TickTracker

P = 74.5


def test_sync_needs_two_consistent_boundaries():
    tk = TickTracker(period=P)
    assert not tk.synced, "not synced at start (phase unknown until observed)"
    tk.observe_boundary(1000.0)
    assert not tk.synced, "one boundary is not enough to confirm the phase"
    tk.observe_boundary(1000.0 + 2 * P)          # 2 ticks later (buffs fade every ~2 ticks)
    assert tk.synced, "two consistent boundaries -> synced"


def test_simultaneous_fades_deduped():
    tk = TickTracker(period=P)
    tk.observe_boundary(1000.0)
    tk.observe_boundary(1000.4)                   # 분노 & 빨리 fade together -> same boundary
    assert tk.confirms == 1 and not tk.synced, "a near-simultaneous dup must not re-anchor/confirm"
    tk.observe_boundary(1000.0 + 2 * P)
    assert tk.synced, "still syncs on the next real boundary"


def test_missed_boundary_absorbed():
    tk = TickTracker(period=P)
    tk.observe_boundary(500.0)
    tk.observe_boundary(500.0 + 2 * P)
    assert tk.synced
    tk.observe_boundary(500.0 + 5 * P)            # a couple of boundaries went unobserved (k jumps)
    assert tk.synced, "a larger whole-tick gap is still consistent -> stays synced"


def test_phase_shift_drops_sync():
    tk = TickTracker(period=P)
    tk.observe_boundary(0.0); tk.observe_boundary(2 * P)
    assert tk.synced
    tk.observe_boundary(2 * P + 30.0)             # off-grid (server reboot shifted the phase)
    assert not tk.synced, "an off-grid boundary drops sync to re-learn"


def test_remaining_and_grid_point():
    tk = TickTracker(period=P)
    tk.observe_boundary(1000.0); tk.observe_boundary(1000.0 + 2 * P)   # anchor now at 1000+2P=1149
    # remaining to next boundary from just after the anchor
    r = tk.remaining(1149.0 + 10.0)
    assert abs(r - (P - 10.0)) < 1e-6, r
    # 철면 half-tick grid (div=2): next 37.25s grid point at/after land+base
    gp = tk.grid_point(1149.0 + 5.0, base=36.75, div=2)
    step = P / 2
    assert abs((gp - tk.anchor) % step) < 1e-6 and gp >= 1149.0 + 5.0 + 36.75, gp
    # before sync -> None
    assert TickTracker(period=P).remaining(123.0) is None
    assert TickTracker(period=P).grid_point(123.0, 36.75, div=2) is None


def test_period_refines_toward_truth():
    tk = TickTracker(period=74.5)                 # hint slightly off the true 75.0
    t = 0.0
    for _ in range(12):
        t += 2 * 75.0                             # real boundaries every 2 ticks of 75s
        tk.observe_boundary(t)
    assert abs(tk.period - 75.0) < 0.3, f"period should refine toward the real 75.0, got {tk.period:.2f}"


if __name__ == "__main__":
    test_sync_needs_two_consistent_boundaries()
    test_simultaneous_fades_deduped()
    test_missed_boundary_absorbed()
    test_phase_shift_drops_sync()
    test_remaining_and_grid_point()
    test_period_refines_toward_truth()
    print("ALL TICKTRACKER TESTS PASSED")
