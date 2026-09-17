"""Death recovery (FRAMEWORK line 60): on death the engine force-enters the `death`
workflow and runs 시체수습 -> re-equip -> 시체 묻어 -> 보험 -> resume, so a death no longer
leaves the corpse to rot with all items lost."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import events as ev
from gumiho.access import CharCtx
from gumiho.command import Cmd
from gumiho.engine import Engine
from gumiho.reload import get_knowledge, Reloader
from gumiho.state import WorldState

CLOCK = [1000.0]
def now(): return CLOCK[0]


def _mk():
    ctx = CharCtx("a", "leader", WorldState(), get_knowledge(), set(), name="영웅", now=now)
    ctx.partner_name = None
    sent = []
    eng = Engine(ctx, Cmd(sent.append), now=now)
    eng.enabled = True
    return ctx, sent, eng


def _tick(eng, ctx, room):
    r = ev.RoomSeen(title=room, description="", exits=["위"], entities=[])
    ctx.state.apply(r); ctx.on_event(r)
    ctx.state.at_prompt = True
    eng.tick()


def test_death_forces_recovery():
    Reloader(lambda: []).load_all()
    ctx, sent, eng = _mk()
    eng.boot("hunting")                          # mid-hunt
    ctx.equipment = [{"slot": "무기", "name": "담금질한 칼"},
                     {"slot": "손", "name": "박쥐 가죽 장갑"},
                     {"slot": "몸", "name": "은갑옷"}]
    ctx.state.dead = True                        # DIE

    got = []
    # feed room renders: land at 치료실, then (after 아래) the 장의사 morgue
    for i in range(16):
        room = "치료실" if i < 2 else "장의사 작업실"
        before = len(sent)
        _tick(eng, ctx, room)
        got += sent[before:]
        if ctx.cursor.workflow != "death":       # recovered -> handed back to travel
            break

    assert "일어" in got, f"stand out of resting: {got}"
    assert "아래" in got, f"descend to the morgue: {got}"
    assert "시체수습" in got, f"retrieve corpse items: {got}"
    assert "모두입어" in got, f"wear armours: {got}"
    assert "담금질한 칼 무장" in got, f"re-arm the weapon: {got}"
    assert "박쥐 가죽 장갑 쥐어" in got, f"re-grip the hold: {got}"
    assert "시체 묻어" in got, f"bury -> revive: {got}"
    assert "보험" in got, f"re-insure: {got}"
    # order: salvage BEFORE bury (never bury before pulling items off the corpse)
    assert got.index("시체수습") < got.index("시체 묻어"), got
    assert not ctx.state.dead, "dead flag cleared after recovery"
    assert ctx.cursor.workflow == "travel", f"resumes via travel, got {ctx.cursor.workflow}"
    print("death recovery: 일어->아래->시체수습->re-equip->시체 묻어->보험->travel; dead cleared")


def test_go_morgue_looks_when_title_is_stale():
    """The real bug that lost the gear: after death the server doesn't re-render, so the
    room title is STALE (the room we died in, e.g. 숲으로 가는 길). go_morgue must LOOK to
    find out where it actually is — the old code did NOTHING on a non-empty non-clinic
    title, so it never descended and 시체수습 ran in the wrong place -> corpse rotted."""
    from behavior.hooks.death import go_morgue
    from gumiho.access import Cursor
    Reloader(lambda: []).load_all()
    ctx, sent, eng = _mk()
    ctx.state.room_title = "숲으로 가는 길"       # STALE death-location title, not the clinic
    ctx.cursor = Cursor(workflow="death", sub="to_morgue", entered_at=now() - 10)

    go_morgue(ctx, Cmd(sent.append))
    assert sent == ["봐"], f"must LOOK to find its location, not do nothing: {sent}"

    ctx.state.room_title = "치료실"              # the look reveals the clinic
    sent.clear()
    go_morgue(ctx, Cmd(sent.append))
    assert sent == ["아래"], f"descend to the morgue once the clinic is confirmed: {sent}"
    print("go_morgue: looks on a stale post-death title, descends once 치료실 confirmed")


def test_death_resume_syncs_alive_partner():
    """After recovery the revived character switches to `travel` to regroup + return. If it does
    NOT sync an ALIVE partner along, the partner stays hunting, never comes to 중앙 광장, and reform
    can't form the duo -> the revived one loops reform<->to_anchor forever ('stuck at 중앙 광장 after
    death'). Fix: sync the alive partner into travel; a still-dead partner is NOT synced (it
    self-switches when its own recovery ends)."""
    from gumiho.director import Director
    from behavior.hooks.death import partner_dead
    Reloader(lambda: []).load_all()
    d = Director()

    def mk(sid, role, name, partner):
        st = WorldState(); st.apply(ev.Status(hp=(400, 400), mp=(100, 100), mv=(400, 400), job="전사"))
        c = CharCtx(sid, role, st, get_knowledge(), set(), name=name, now=now)
        c.partner_name = partner; c.nav = lambda t: None
        sent = []
        e = Engine(c, Cmd(sent.append), director=d, now=now); e.enabled = True
        d.register(sid, e)
        return c, sent, e

    lc, ls, le = mk("a", "leader", "영웅", "짝꿍")
    sc, ss, se = mk("b", "supporter", "짝꿍", "영웅")
    lc.partner = sc; sc.partner = lc

    se.boot("hunting")                      # supporter ALIVE, hunting
    lc.state.dead = True; le.boot("death")  # leader DEAD, recovering
    assert not partner_dead(lc), "supporter alive -> partner_dead False (so leader will sync it)"

    def feed(eng, ctx, room):
        r = ev.RoomSeen(title=room, description="", exits=["위"], entities=[])
        ctx.state.apply(r); ctx.on_event(r); ctx.state.at_prompt = True; eng.tick()

    feed(le, lc, "치료실")                  # go_morgue: confirm clinic -> 아래 (_descended)
    for _ in range(12):                     # morgue -> salvage -> reequip -> bury -> insure -> done
        feed(le, lc, "장의사")
        if lc.cursor.workflow == "travel":
            break
    assert lc.cursor.workflow == "travel", f"revived leader resumes via travel: {lc.cursor}"
    # THE FIX: the alive supporter was SYNCED into travel too, so it recalls to the anchor & regroups
    assert sc.cursor.workflow == "travel", f"alive partner must be pulled into travel to regroup: {sc.cursor}"
    print("death resume: revived leader syncs the ALIVE partner into travel (no 중앙 광장 stranding)")


if __name__ == "__main__":
    test_go_morgue_looks_when_title_is_stale()
    test_death_forces_recovery()
    test_death_resume_syncs_alive_partner()
    print("\nALL DEATH TESTS PASSED")
