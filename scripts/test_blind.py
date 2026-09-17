"""Blind recovery: on "당신의 눈이 멀었습니다!" the engine force-enters the `blind` workflow
(recall -> 동 to the 치료소 -> 장님치 부탁 -> resume) — the only recovery possible while blind."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import events as ev
from gumiho.access import CharCtx
from gumiho.command import Cmd
from gumiho.engine import Engine
from gumiho.parser import PLAYER_BLIND_RE
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


def test_blind_line_detected_and_sets_flag():
    assert PLAYER_BLIND_RE.search("당신의 눈이 멀었습니다!"), "the blind line must match"
    assert not PLAYER_BLIND_RE.search("당신은 죽었습니다"), "must not match the death line"
    st = WorldState()
    st.apply(ev.PlayerBlinded())
    assert st.blind is True, "PlayerBlinded sets state.blind"


def test_sight_restored_line_clears_blind():
    """The CURE is confirmed by the real line '당신의 시력이 회복되었습니다!' -> state.blind clears.
    Previously nothing detected it: blind was cleared by an 8s optimistic timer after 장님치 부탁,
    and a stray/uncured blind lingered (which also stranded the offline recovery)."""
    from gumiho.parser import SIGHT_RESTORED_RE
    from behavior.hooks.blind import not_blind
    assert SIGHT_RESTORED_RE.search("당신의 시력이 회복되었습니다!"), "the cure line must match"
    assert not SIGHT_RESTORED_RE.search("당신의 눈이 멀었습니다!"), "must not match the blind line"
    st = WorldState()
    st.apply(ev.PlayerBlinded());  assert st.blind is True
    st.apply(ev.SightRestored());  assert st.blind is False, "the cure line clears state.blind"
    # and the workflow's cure->resume guard now fires on the real cure
    ctx = CharCtx("a", "leader", st, get_knowledge(), set(), name="영웅", now=now)
    assert not_blind(ctx) is True, "not_blind is True once the cure line has cleared blind"
    print("blind: '시력이 회복' clears the flag + satisfies not_blind (real cure, not a timer)")


def test_blind_forces_the_blind_workflow():
    Reloader(lambda: []).load_all()
    ctx, sent, eng = _mk()
    eng.boot("hunting")                          # mid-hunt
    ctx.state.blind = True                       # BLINDED
    r = ev.RoomSeen(title="역사의길", description="", exits=["서"], entities=[])
    ctx.state.apply(r); ctx.on_event(r); ctx.state.at_prompt = True
    eng.tick()
    assert ctx.cursor.workflow == "blind", f"blind must force the recovery workflow, got {ctx.cursor.workflow}"
    print("blind: forces the `blind` workflow from mid-hunt")


def test_death_wins_over_blind():
    """If somehow both, DEATH takes priority (corpse rots — that recovery can't wait)."""
    Reloader(lambda: []).load_all()
    ctx, sent, eng = _mk()
    eng.boot("hunting")
    ctx.state.dead = True
    ctx.state.blind = True
    r = ev.RoomSeen(title="어딘가", description="", exits=["위"], entities=[])
    ctx.state.apply(r); ctx.on_event(r); ctx.state.at_prompt = True
    eng.tick()
    assert ctx.cursor.workflow == "death", f"death outranks blind, got {ctx.cursor.workflow}"
    print("blind: death takes priority when both are set")


def test_blind_recovery_actions():
    from behavior.hooks.blind import go_clinic, cure_blind, clear_blind, not_blind
    Reloader(lambda: []).load_all()
    ctx, sent, eng = _mk()
    cmd = Cmd(sent.append)
    ctx.state.blind = True
    assert not_blind(ctx) is False, "blind -> not_blind is False"
    go_clinic(ctx, cmd)
    assert sent[-1] == "동", f"go_clinic walks east to the 치료소: {sent}"
    cure_blind(ctx, cmd)
    assert sent[-1] == "장님치 부탁", f"cure_blind requests the cure: {sent}"
    clear_blind(ctx, cmd)
    assert ctx.state.blind is False, "clear_blind clears the flag"
    assert not_blind(ctx) is True, "cured -> not_blind is True"
    print("blind recovery actions: 동, 장님치 부탁, clear flag")


def test_blind_recall_confirms_by_absence():
    """While blind the room never renders, so a recall can't be confirmed by seeing 중앙 광장. It's
    confirmed by the ABSENCE of the "귀환 시도가 실패했습니다" line after a 귀환 (user's rule); a fail
    line means retry."""
    from behavior.hooks.blind import blind_recall, blind_recall_watch, blind_recalled
    kn = Reloader(lambda: []).load_all() or get_knowledge()
    ctx, sent, eng = _mk()
    ctx.state.apply(ev.Status(hp=(400, 400), mp=(100, 100), mv=(400, 400)))   # MV ok to 귀환
    ctx.state.blind = True
    cmd = Cmd(sent.append)
    confirm = get_knowledge()["recall"].get("blind_confirm_wait", 3.0)
    interval = get_knowledge()["recall"].get("retry_interval", 3.0)

    blind_recall(ctx, cmd)
    assert sent[-1] == "귀환", sent
    assert not blind_recalled(ctx), "not confirmed within the wait window"
    # a fail line arrives after the send -> NOT recalled, and watch re-sends after the interval
    ctx.recall_fail_at = now()
    CLOCK[0] += interval + 0.1
    assert not blind_recalled(ctx), "a fail line after the 귀환 -> not recalled"
    sent.clear(); blind_recall_watch(ctx, cmd)
    assert sent == ["귀환"], f"re-sends 귀환 after a failed attempt: {sent}"
    # no fail after this (new) send + the confirm window passes -> recall landed
    CLOCK[0] += confirm + 0.1
    assert blind_recalled(ctx), "no fail line within the confirm window -> recall landed"
    # and it must NOT keep sending 귀환 once it succeeded (no render to stop a blind re-send)
    sent.clear(); blind_recall_watch(ctx, cmd)
    assert sent == [], f"no re-send once the recall succeeded: {sent}"
    print("blind recall: confirms by ABSENCE of the fail line; retries only on a fail")


if __name__ == "__main__":
    test_blind_line_detected_and_sets_flag()
    test_sight_restored_line_clears_blind()
    test_blind_forces_the_blind_workflow()
    test_death_wins_over_blind()
    test_blind_recovery_actions()
    test_blind_recall_confirms_by_absence()
    print("\nALL BLIND TESTS PASSED")
