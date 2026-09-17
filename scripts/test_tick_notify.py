"""Desktop notification on the master-tick boundary (GameSession._notify_tick_due). Fires the SAME
notifier path as a 대화, LEADER only, once the tick is SYNCED, exactly at each boundary (polled from
the heartbeat), and never twice for one boundary. Arms silently on first sight."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho.access import CharCtx
from gumiho.reload import get_knowledge, Reloader
from gumiho.state import WorldState
from gumiho.webui import GameSession

CLOCK = [1000.0]
def now(): return CLOCK[0]
def adv(dt): CLOCK[0] += dt


class FakeNotifier:
    def __init__(self): self.calls = []
    def notify(self, title, message, dedupe=False): self.calls.append((title, message))


def _mk(sid="a"):
    Reloader(lambda: []).load_all()
    gs = GameSession("h", 0, Path("."), broadcast=lambda d: None,
                     notify=False, sid=sid, notifier=FakeNotifier())
    ctx = CharCtx(sid, "leader" if sid == "a" else "supporter",
                  WorldState(), get_knowledge(), set(), name="플레이어제로", now=now)
    gs.ctx = ctx
    return gs, ctx


def _sync(ctx):
    tk = ctx.tick
    CLOCK[0] = 1000.0; tk.observe_boundary(now())          # anchor
    adv(tk.period);    tk.observe_boundary(now())          # 2nd consistent boundary -> synced
    assert tk.synced, "two consistent boundaries -> synced"
    return tk


def test_fires_once_per_boundary_leader():
    gs, ctx = _mk("a")
    tk = _sync(ctx)
    gs._notify_tick_due()                                   # arms silently, no fire
    assert gs.notifier.calls == [], "must not fire on the arming sight"
    # poll across the cycle a few times before the boundary — still no fire
    adv(tk.period * 0.5); gs._notify_tick_due()
    assert gs.notifier.calls == [], "no fire mid-cycle"
    # cross ONE boundary -> exactly one notification
    adv(tk.period * 0.6); gs._notify_tick_due()            # now > next boundary
    assert len(gs.notifier.calls) == 1, f"one fire at the boundary: {gs.notifier.calls}"
    # polling again in the same cycle must NOT re-fire
    gs._notify_tick_due(); gs._notify_tick_due()
    assert len(gs.notifier.calls) == 1, "no duplicate within the same boundary"
    # next boundary -> one more
    adv(tk.period); gs._notify_tick_due()
    assert len(gs.notifier.calls) == 2, "one fire per boundary"
    print("tick-notify: leader fires exactly once per master-tick boundary")


def test_supporter_never_notifies():
    gs, ctx = _mk("b")
    _sync(ctx)
    gs._notify_tick_due()
    adv(ctx.tick.period * 1.2); gs._notify_tick_due()
    assert gs.notifier.calls == [], "session B (supporter) never notifies on the tick"
    print("tick-notify: supporter (B) never fires")


def test_no_fire_until_synced():
    gs, ctx = _mk("a")
    assert not ctx.tick.synced
    gs._notify_tick_due()
    adv(200.0); gs._notify_tick_due()
    assert gs.notifier.calls == [], "no notification while the tick is unsynced"
    print("tick-notify: silent until the tick is synced")


if __name__ == "__main__":
    test_fires_once_per_boundary_leader()
    test_supporter_never_notifies()
    test_no_fire_until_synced()
    print("\nALL TICK-NOTIFY TESTS PASSED")
