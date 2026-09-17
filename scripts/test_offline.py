"""Offline reflexes (the engine toggle is OFF — manual play). A NARROW set of reactions must
still fire so the duo stays safe and responsive without ever hunting, travelling, roaming, or
resting on its own:
  - 연타 combat continuity (combo bundle)
  - the supporter casts a spoken (말) spell request (support bundle)
  - DEATH / BLIND recovery — a rotting corpse loses every item — which then HALTS (never resumes
    the hunt the user turned off)
Everything else stays silent: an idle manual session sends nothing, and a hunting-cursor never
roams while the engine is off."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import events as ev
from gumiho.access import CharCtx, Cursor
from gumiho.command import Cmd
from gumiho.engine import Engine
from gumiho.reload import get_knowledge, Reloader
from gumiho.state import WorldState

CLOCK = [1000.0]
def now(): return CLOCK[0]


def _mk(role="leader", job="전사", level=70, partner=None):
    Reloader(lambda: []).load_all()
    st = WorldState()
    st.apply(ev.Status(hp=(400, 400), mp=(100, 100), mv=(200, 200), job=job))
    st.level = level
    ctx = CharCtx("a", role, st, get_knowledge(), set(),
                  name="영웅" if role == "leader" else "돕", now=now)
    ctx.partner_name = partner
    sent = []
    eng = Engine(ctx, Cmd(sent.append), now=now)
    eng.enabled = False                                   # ENGINE OFF — the whole point
    ctx.cursor = Cursor(workflow="hunting", sub="clear_room", entered_at=now())
    return ctx, sent, eng


def _in_battle(ctx):
    ctx.state.in_battle = True
    ctx.last_combat_at = now()          # fighting() = in_battle AND a recent combat line
    ctx.combo_ready = True              # the 연타 wind-up cue set this


# --- 연타 continuity fires with the engine OFF -------------------------------
def test_combo_fires_with_engine_off():
    ctx, sent, eng = _mk(job="전사 검사")
    _in_battle(ctx)
    ctx.state.at_prompt = True
    eng.tick()                                            # enabled is False -> offline path
    assert sent == ["연타"], f"연타 must continue with the engine OFF: {sent}"
    assert ctx.combo_ready is False, "cue consumed"
    print("offline: 전사 continues 연타 with the engine OFF")


def test_stand_up_on_knockdown_with_engine_off():
    ctx, sent, eng = _mk(job="전사")                       # engine OFF in _mk
    stand = get_knowledge()["combat"]["stand_cmd"]        # config-driven (user-tunable)
    ctx.knocked_down = True                               # a power-bash floored us
    ctx.state.at_prompt = True
    eng.tick()
    assert sent == [stand], f"must send the stand command on a knockdown with the engine OFF: {sent}"
    assert ctx.knocked_down is False, "cue consumed"
    print(f"offline: stands up ({stand}) on a knockdown with the engine OFF")


def test_combo_only_for_eligible_class_offline():
    ctx, sent, eng = _mk(job="마법", level=99)             # mage never 연타
    _in_battle(ctx)
    ctx.state.at_prompt = True
    eng.tick()
    assert "연타" not in sent, f"mage must not 연타 offline either: {sent}"
    print("offline: 마법 (mage) never fires 연타 — class-gated, offline too")


# --- supporter answers a spoken spell request with the engine OFF ------------
def test_spell_request_answered_with_engine_off():
    ctx, sent, eng = _mk(role="supporter", job="마법", partner="플레이어제로")
    ctx.pending_spell = "방비"                             # heard the 말 "방비!" request
    ctx.state.at_prompt = True
    eng.tick()
    assert sent == ["플레이어제로 방비 걸어"], f"must cast the spoken request offline: {sent}"
    assert ctx.pending_spell is None, "request consumed"
    print("offline: supporter casts a spoken 방비 request with the engine OFF")


def test_fleeing_partner_chased_with_engine_off():
    ctx, sent, eng = _mk(role="supporter", job="마법", partner="플레이어제로")
    ctx.pending_chase = "동"                               # leader fled east (webui set this)
    ctx.state.at_prompt = True
    eng.tick()
    assert sent == ["동"], f"supporter must chase the fled leader offline: {sent}"
    assert ctx.pending_chase is None, "chase consumed"
    print("offline: supporter chases a fleeing leader with the engine OFF")


# --- offline NEVER hunts / roams --------------------------------------------
def test_offline_is_silent_when_idle():
    ctx, sent, eng = _mk(job="전사")                        # not fighting, no request, not dead
    ctx.state.at_prompt = True
    eng.tick()                                            # cursor is hunting/clear_room
    assert sent == [], f"offline must not hunt/roam/act when idle: {sent}"
    print("offline: idle manual play sends nothing (no roam/attack while engine OFF)")


# --- DEATH recovery runs offline, then HALTS --------------------------------
def test_death_enters_recovery_with_engine_off():
    ctx, sent, eng = _mk(job="전사")
    ctx.state.dead = True
    ctx.state.at_prompt = True
    eng.tick()                                            # offline path -> recovery override
    assert ctx.cursor.workflow == "death", f"offline death must enter recovery: {ctx.cursor.workflow}"
    assert ctx.offline_recovery is True, "offline recovery flagged"
    assert eng.enabled is False, "recovery must NOT turn the autonomous engine on"
    assert "일어" in sent, f"death recovery started (death_wake): {sent}"
    print("offline: death drops into recovery even with the engine OFF (일어 sent)")


def test_offline_recovery_halts_instead_of_resuming():
    ctx, sent, eng = _mk(job="전사")
    ctx.offline_recovery = True
    ctx._offline_recovery_at = now()                     # recovery just started (grace not lapsed)
    ctx.cursor = Cursor(workflow="death", sub="done", entered_at=now())
    ctx.state.dead = False                                # revived by the bury stage
    ctx.state.at_prompt = True
    eng.tick()                                            # done would `switch travel` -> intercepted
    assert ctx.cursor.workflow == "death", f"must NOT resume travel offline: {ctx.cursor.workflow}"
    assert ctx.offline_recovery is False, "recovery flag cleared on completion"
    assert eng.enabled is False, "engine stays OFF after recovery"
    print("offline: recovery HALTS at done — no travel/hunt resume, engine stays OFF")


def test_stuck_recovery_does_not_freeze_reflexes_when_active():
    """REGRESSION (2026-09-08): a stuck offline recovery — the ctx stranded in death/blind with
    offline_recovery latched but NO LONGER dead/blind — diverted every _offline_tick into recovery
    and FROZE all reflexes ("all reflexes stopped without the engine"). Now: an active (in-battle)
    character abandons the leftover recovery immediately and the reflexes resume the SAME tick."""
    ctx, sent, eng = _mk(role="supporter", job="마법", partner="플레이어제로")
    ctx.cursor = Cursor(workflow="death", sub="done", entered_at=now())   # stranded in recovery
    ctx.offline_recovery = True; ctx._offline_recovery_at = now()
    ctx.state.dead = False; ctx.state.blind = False; ctx.state.in_battle = True
    ctx.pending_spell = "방비"                             # a spell request is waiting
    ctx.state.at_prompt = True
    eng.tick()
    assert ctx.offline_recovery is False, "active character abandons the stuck recovery"
    assert ctx.cursor.workflow != "death", "recovery cursor cleared (future death re-enters clean)"
    assert sent == ["플레이어제로 방비 걸어"], f"reflex resumed after abandoning stuck recovery: {sent}"
    print("offline: a stuck recovery on an ACTIVE character is abandoned -> reflexes resume")


def test_stuck_recovery_abandoned_on_grace_timeout():
    """Not in battle, but the recovery has been latched longer than offline_recovery_grace without
    completing (can't finish offline / user handled it) -> abandon so reflexes still resume."""
    ctx, sent, eng = _mk(role="supporter", job="마법", partner="플레이어제로")
    ctx.cursor = Cursor(workflow="blind", sub="recall", entered_at=now())
    ctx.offline_recovery = True; ctx._offline_recovery_at = now() - 999.0   # grace long lapsed
    ctx.state.dead = False; ctx.state.blind = False; ctx.state.in_battle = False
    ctx.pending_spell = "방비"
    ctx.state.at_prompt = True
    eng.tick()
    assert ctx.offline_recovery is False, "grace-lapsed stuck recovery abandoned"
    assert sent == ["플레이어제로 방비 걸어"], f"reflex resumed after timeout: {sent}"
    print("offline: a grace-lapsed stuck recovery is abandoned -> reflexes resume")


def test_engine_on_recovery_still_resumes_travel():
    """Regression guard: the offline interception must not change the ENABLED path — an engine-ON
    death recovery still hands off to travel at `done` exactly as before."""
    ctx, sent, eng = _mk(job="전사")
    eng.enabled = True                                    # engine ON
    ctx.cursor = Cursor(workflow="death", sub="done", entered_at=now())
    ctx.state.dead = False
    ctx.state.at_prompt = True
    eng.tick()                                            # full arbiter: done -> switch travel
    assert ctx.cursor.workflow == "travel", f"engine-ON recovery resumes travel: {ctx.cursor.workflow}"
    print("engine ON: death recovery still resumes travel (offline change didn't regress it)")


if __name__ == "__main__":
    test_combo_fires_with_engine_off()
    test_stand_up_on_knockdown_with_engine_off()
    test_combo_only_for_eligible_class_offline()
    test_spell_request_answered_with_engine_off()
    test_fleeing_partner_chased_with_engine_off()
    test_offline_is_silent_when_idle()
    test_death_enters_recovery_with_engine_off()
    test_offline_recovery_halts_instead_of_resuming()
    test_stuck_recovery_does_not_freeze_reflexes_when_active()
    test_stuck_recovery_abandoned_on_grace_timeout()
    test_engine_on_recovery_still_resumes_travel()
    print("\nALL OFFLINE-REFLEX TESTS PASSED")
