"""Standalone smoke test for the M1 engine core — no server, no asyncio.

Drives a CharCtx + Engine through scripted events and asserts the arbiter's
command output: travel->hunt handoff, buff request, attack, PLAYER SAFETY,
drink reflex, rest transition + recovery, and reload reconciliation.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import events as ev
from gumiho import moblore
from gumiho import registry
from gumiho.access import CharCtx
from gumiho.command import Cmd
from gumiho.director import Director
from gumiho.engine import Engine
from gumiho.reload import get_knowledge, Reloader
from gumiho.state import WorldState

# 고려 gate: seed the mobs these tests attack as SAFE (real json untouched).
moblore._save = lambda: None
moblore._lore = {"safe": ["개구리", "뱀", "지네", "거미"], "danger": []}

CLOCK = [1000.0]
def now():
    return CLOCK[0]

def prompt(ctx, hp, mp, mv):
    ctx_apply(ctx, ev.Prompt(hp=hp, mp=mp, mv=mv))

def ctx_apply(ctx, event):
    ctx.state.apply(event)
    ctx.on_event(event)

def feed_jido(ctx, dirs):
    """Mimic webui folding a 지도 into the hunt's sweep so movement can proceed."""
    from gumiho.gridsweep import GridSweep
    from gumiho.mapgrid import MapGrid
    D = {"북": (-1, 0), "남": (1, 0), "동": (0, 1), "서": (0, -1)}
    g = MapGrid(zone="z"); g.here = (3, 3); g.cells[(3, 3)] = "방"
    for d in dirs:
        dr, dc = D[d]; c = (3 + dr, 3 + dc)
        g.cells[c] = "방"; g.edges.add(frozenset({(3, 3), c}))
    sw = ctx.gridsweep
    if sw is None:
        sw = ctx.gridsweep = GridSweep()
    sw.register(g)

def make_leader():
    st = WorldState()
    st.apply(ev.Status(hp=(400, 400), mp=(100, 100), mv=(220, 220),
                       level=33, job="전사"))
    kn = get_knowledge()
    ctx = CharCtx("a", "leader", st, kn,
                  known_players={"플레이어제로", "스튀르들뤼손", "담화린"},
                  name="플레이어제로", now=now)
    ctx.partner_name = None          # solo for the mechanics test (no pairing wait)
    return ctx

def main():
    Reloader(lambda: []).load_all()
    assert registry.has_workflow("hunting"), "workflows didn't load"
    print("loaded workflows:", registry.all_workflow_kinds())

    ctx = make_leader()
    sent = []
    eng = Engine(ctx, Cmd(sent.append), now=now)

    # boot hunting -> regroup (solo: passes) -> ensure_buffs requests 방비
    eng.boot("hunting")
    prompt(ctx, 400, 100, 220)
    eng.tick()
    assert ctx.cursor.workflow == "hunting", ctx.cursor
    assert any("방비" in c for c in sent), sent
    print("boot hunting, buff requested:", [c for c in sent if "방비" in c])

    # all_buffs_active -> clear_room
    prompt(ctx, 400, 100, 220)
    eng.tick()
    assert ctx.cursor.sub == "clear_room", ctx.cursor

    # a mob present -> attack it
    sent.clear()
    ctx_apply(ctx, ev.RoomSeen(title="개미굴", description="",
                               exits=["북", "동", "서"],
                               entities=["파란 개구리가 돌아다니고 있습니다."]))
    prompt(ctx, 400, 100, 220)
    eng.tick()
    assert any("공격" in c for c in sent), f"expected an attack, got {sent}"
    print("attack:", sent)

    # PLAYER SAFETY: a known player in the room must NEVER be attacked
    sent.clear()
    ctx_apply(ctx, ev.RoomSeen(title="개미굴", description="",
                               exits=["북", "동", "서"],
                               entities=["담화린님이 서 있습니다."]))
    feed_jido(ctx, ["북", "동", "서"])       # 지도 known -> sweep walks the grid
    prompt(ctx, 400, 100, 220)
    eng.tick()
    assert not any("공격" in c for c in sent), f"SAFETY VIOLATION: {sent}"
    assert any(c in ("북", "동", "서") for c in sent), f"expected a move, got {sent}"
    print("player-safe (moved instead of attacking):", sent)

    # low HP with potions in inventory -> drink reflex fires (not rest). The character holds
    # 불고기피자 but NOT 쑥 (the FIRST-listed hp potion) -> it must drink what it HOLDS, not blindly
    # send '쑥 복용' (that failed forever while HP stayed low -> the "쑥 복용" spam bug).
    inv = ["불고기피자 (3)"]
    ctx.state.apply(ev.Inventory(items=inv))
    sent.clear()
    ctx_apply(ctx, ev.RoomSeen(title="개미굴", description="", exits=["북"],
                               entities=[]))
    prompt(ctx, 100, 100, 220)              # hp 25%
    eng.tick()
    hp_kws = get_knowledge()["potions"]["hp"]               # user-tunable keyword list
    held = next((p for p in hp_kws if any(p in it for it in inv)), None)   # the one we HOLD
    assert held, f"test inv should match a configured hp potion: {hp_kws}"
    assert f"{held} 복용" in sent, f"must drink the HELD potion ({held}), not a first-listed one: {sent}"
    for other in (p for p in hp_kws if p != held):          # never drink one we don't have
        assert f"{other} 복용" not in sent, f"must NOT drink {other} it doesn't have: {sent}"
    assert ctx.cursor.workflow == "hunting", "should not rest while potions last"
    print(f"drink reflex (drinks the HELD potion {held!r}, not an absent one):", sent)

    # out of potions + low HP -> latch 'rest needed', then (battle over) rest. HP must be BELOW
    # the configured rest floor (user-tunable), on a 400-max character.
    rest_pct = get_knowledge()["hunt"]["rest_hp_pct"]
    low_hp = max(1, int(400 * rest_pct / 100) - 20)         # clearly under the rest floor
    ctx.state.apply(ev.Inventory(items=[]))
    for _ in range(4):
        prompt(ctx, low_hp, 100, 220)
        eng.tick()
        if ctx.cursor.workflow == "rest":
            break
    assert ctx.cursor.workflow == "rest", f"expected rest, got {ctx.cursor}"
    print("-> rest (potions gone; latch picked up after battle)")

    # pre-rest gear check: rest re-reads 장비 first; feed a healthy reply so it reaches 'sleeping'
    for _ in range(4):
        if ctx.equipment is None:
            ctx.on_event(ev.Equipment([{"slot": "무기", "name": "칼", "cur": 90, "max": 100}]))
        prompt(ctx, low_hp, 100, 220); eng.tick()
        if ctx.cursor.sub == "sleeping":
            break
    assert ctx.cursor.sub == "sleeping", ctx.cursor

    # recover -> waking -> back to hunting
    prompt(ctx, 400, 100, 220)             # recovered
    eng.tick()
    assert ctx.cursor.sub == "waking", ctx.cursor
    ctx.state.apply(ev.Posture(kind="standing"))
    prompt(ctx, 400, 100, 220)
    eng.tick()
    assert ctx.cursor.workflow == "hunting", f"expected hunting, got {ctx.cursor}"
    print("recovered -> hunting")

    # reload reconciliation: re-enter current workflow from the top
    ctx.cursor.sub = "clear_room"
    ctx.cursor.scratch["last_dir"] = "북"
    eng.reenter_current()
    assert ctx.cursor.sub == "regroup", "reload should re-enter from initial"
    assert ctx.cursor.scratch == {}, "reload should reset in-workflow scratch"
    print("reload re-enters from top, scratch reset")

    # director: a sync switch moves BOTH engines together
    d = Director()
    e1 = Engine(make_leader(), Cmd([].append), director=d, now=now)
    sup = CharCtx("b", "supporter", WorldState(), get_knowledge(),
                  known_players=set(), name="스튀르들뤼손", now=now)
    sup.partner_name = "플레이어제로"
    e2 = Engine(sup, Cmd([].append), director=d, now=now)
    d.register("a", e1); d.register("b", e2)
    e1.boot("hunting"); e2.boot("hunting")
    d.request_switch("a", "rest")
    assert e1.ctx.cursor.workflow == "rest" and e2.ctx.cursor.workflow == "rest"
    print("director sync-switch moved both to rest")

    print("\nALL ENGINE TESTS PASSED")

if __name__ == "__main__":
    main()
