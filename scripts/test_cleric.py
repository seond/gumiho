"""성직 (cleric) leader: self-casts 수호/축복 ('<buff> 걸', NO target) out of combat, and nukes the
opponent with 벼락 ('<opponent> 벼락') during battle for extra damage."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import events as ev
from gumiho.access import CharCtx
from gumiho.command import Cmd
from gumiho.reload import get_knowledge, Reloader
from gumiho.state import WorldState

CLOCK = [1000.0]
def now(): return CLOCK[0]
def adv(dt): CLOCK[0] += dt


def _cleric(job="성직", mp=(200, 200)):
    Reloader(lambda: []).load_all()
    st = WorldState()
    st.apply(ev.Status(hp=(300, 300), mp=mp, mv=(200, 200), job=job))
    return CharCtx("a", "leader", st, get_knowledge(), set(), name="사제", now=now)


def test_self_buffs_cast_no_target():
    # CONFIG-DRIVEN: whatever buffs [self_buffs]."성직" lists (user-editable — e.g. 뜨기
    # was added later), each is cast as "<buff> 걸" (no target), one per tick, timer-gated.
    from behavior.hooks.hunting import _maintain_self_buffs, _self_buffs
    ctx = _cleric()
    buffs = get_knowledge()["self_buffs"]["성직"]
    assert _self_buffs(ctx) == buffs, _self_buffs(ctx)
    assert len(buffs) >= 2, "test needs at least two configured self-buffs"
    casts = [f"{b} 걸" for b in buffs]
    sent = []; cmd = Cmd(sent.append)
    ctx.state.in_battle = False
    for _ in buffs:
        _maintain_self_buffs(ctx, cmd)             # one buff per tick
    assert sent == casts, f"self-cast, NO target: {sent}"
    # immediately again -> timer-gated, no re-cast (spam-free)
    _maintain_self_buffs(ctx, cmd); _maintain_self_buffs(ctx, cmd)
    assert sent == casts, f"blind timer -> no re-cast within the window: {sent}"
    # after the re-cast window -> casts again
    adv(get_knowledge()["hunt"]["self_buff_recast"] + 1)
    _maintain_self_buffs(ctx, cmd)
    assert sent == casts + casts[:1], sent
    print(f"성직 self-buffs: {' / '.join(casts)} (no target), timer-gated, no spam")


def test_self_buffs_not_mid_combat():
    from behavior.hooks.hunting import _maintain_self_buffs
    ctx = _cleric(); ctx.state.in_battle = True
    sent = []; _maintain_self_buffs(ctx, Cmd(sent.append))
    assert sent == [], f"no self-buff cast mid-combat: {sent}"
    print("성직 self-buffs: silent mid-combat")


def test_battle_spell_casts_no_target():
    from behavior.hooks.hunting import battle_spell_due, cast_battle_spell
    ctx = _cleric()
    ctx.state.in_battle = True                       # locked on the opponent; no target needed
    assert battle_spell_due(ctx) is True
    sent = []; cast_battle_spell(ctx, Cmd(sent.append))
    assert sent == ["벼락 걸어"], f"spell cast, NO target: {sent}"
    assert battle_spell_due(ctx) is False, "throttled right after by battle_spell_gap"
    adv(get_knowledge()["combat"]["battle_spell_gap"] + 0.1)
    assert battle_spell_due(ctx) is True, "re-castable after the gap"
    print("성직 battle spell: '벼락 걸어' (no target), throttled by the gap")


def test_battle_spell_gated():
    from behavior.hooks.hunting import battle_spell_due
    out = _cleric(); out.state.in_battle = False; out.state.battle_opponent = "쥐"
    assert battle_spell_due(out) is False, "no cast out of combat"
    melee = _cleric(job="전사"); melee.state.in_battle = True; melee.state.battle_opponent = "쥐"
    assert battle_spell_due(melee) is False, "melee job (전사) never casts 벼락"
    low = _cleric(mp=(10, 200)); low.state.in_battle = True; low.state.battle_opponent = "쥐"
    assert battle_spell_due(low) is False, "skip when MP% is below battle_spell_min_mp"
    print("성직 battle spell: gated by in_battle + job + MP")


if __name__ == "__main__":
    test_self_buffs_cast_no_target()
    test_self_buffs_not_mid_combat()
    test_battle_spell_casts_no_target()
    test_battle_spell_gated()
    print("\nALL 성직 (CLERIC) TESTS PASSED")
