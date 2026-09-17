"""Always-on reflexes ([engine].always_reflexes): certain combat-continuity reflexes
must fire in EVERY workflow state, not just hunting —
  - `combo`   : a 전사/검사/장군 continues its 연타 multi-strike when a cue comes due
  - `support` : the supporter casts a spoken (말) spell request
Both are role/situation-gated, so an idle character never spuriously fires them; and
they're appended AFTER a state's own bundles, so survival (potions) still wins a tie."""

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
    return ctx, sent, eng


def _enter(eng, ctx, workflow, state):
    eng.enabled = True
    eng.enter_workflow(workflow)
    ctx.cursor.sub = state
    ctx.cursor.entered_at = now()
    eng._run_on_enter(workflow, state)


def _in_battle(ctx):
    ctx.state.in_battle = True
    ctx.last_combat_at = now()          # fighting() = in_battle AND recent combat line
    ctx.combo_ready = True              # the wind-up cue set this


# --- 연타 continues in a NON-hunting state -----------------------------------
def test_combo_continues_while_resting():
    ctx, sent, eng = _mk(job="전사 검사", level=70)
    _enter(eng, ctx, "rest", "sleeping")   # arm = ["survival"] — no combat bundle
    _in_battle(ctx)
    sent.clear()
    ctx.state.at_prompt = True
    eng.tick()
    assert sent == ["연타"], f"combo must continue even while resting: {sent}"
    assert ctx.combo_ready is False, "cue consumed"
    print("combo: 전사 continues 연타 mid-fight while in rest.sleeping")


def test_combo_continues_in_travel():
    ctx, sent, eng = _mk(job="전사", level=95)
    _enter(eng, ctx, "travel", "recover_mv")   # a travel state, not hunting
    _in_battle(ctx)
    sent.clear()
    ctx.state.at_prompt = True
    eng.tick()
    assert sent == ["연타"], f"combo must continue during travel: {sent}"
    print("combo: 전사 continues 연타 mid-fight while in travel.recover_mv")


def test_combo_only_for_eligible_class():
    ctx, sent, eng = _mk(job="마법", level=99)    # mage — never 연타
    _enter(eng, ctx, "rest", "sleeping")
    _in_battle(ctx)
    sent.clear()
    ctx.state.at_prompt = True
    eng.tick()
    assert "연타" not in sent, f"mage must not 연타: {sent}"
    print("combo: 마법 (mage) never fires 연타 — class-gated")


def test_combo_silent_when_not_fighting():
    ctx, sent, eng = _mk(job="전사", level=70)
    _enter(eng, ctx, "rest", "sleeping")
    ctx.combo_ready = True              # cue set but NOT in battle
    sent.clear()
    ctx.state.at_prompt = True
    eng.tick()
    assert "연타" not in sent, f"no combo outside a live fight: {sent}"
    print("combo: no 연타 when not fighting — self-gated, never blocks idle states")


# --- stand up on a knockdown, in any state -----------------------------------
def test_knockdown_cue_matches_the_real_line():
    kn = get_knowledge()["combat"]
    line = "누소드가 힘을 모아 당신을 강타하자 당신은 땅바닥에 떨썩 주저앉습니다."
    assert kn["knockdown_cue"] in line, f"cue {kn['knockdown_cue']!r} must be in the real line"
    assert kn.get("stand_cmd"), "a stand command must be configured"   # value is user-tunable
    print("knockdown: config cue matches the real server line")


def test_stand_up_on_knockdown_while_resting():
    ctx, sent, eng = _mk(job="전사")
    stand = get_knowledge()["combat"]["stand_cmd"]   # config-driven (user-tunable)
    _enter(eng, ctx, "rest", "sleeping")   # a NON-combat state — combat bundle is always-on
    ctx.knocked_down = True                 # webui sets this on "…땅바닥에 떨썩 주저앉습니다."
    sent.clear()
    ctx.state.at_prompt = True
    eng.tick()
    assert sent == [stand], f"must send the stand command on a knockdown in any state: {sent}"
    assert ctx.knocked_down is False, "cue consumed"
    print(f"knockdown: stands up ({stand}) even while resting — always-on")


def test_no_stand_when_not_knocked_down():
    ctx, sent, eng = _mk(job="전사")
    _enter(eng, ctx, "rest", "sleeping")
    sent.clear()
    ctx.state.at_prompt = True
    eng.tick()
    assert "일어나" not in sent, f"no 일어나 when upright: {sent}"
    print("knockdown: no 일어나 when not knocked down — self-gated")


# --- supporter answers a spell request in a NON-support state ----------------
def test_spell_request_answered_while_resting():
    ctx, sent, eng = _mk(role="supporter", job="마법", partner="플레이어제로")
    _enter(eng, ctx, "rest", "sleeping")   # arm = ["survival"] — no support bundle
    ctx.pending_spell = "방비"              # a request was heard (말 protocol)
    sent.clear()
    ctx.state.at_prompt = True
    eng.tick()
    assert sent == ["플레이어제로 방비 걸어"], f"must cast the request while resting: {sent}"
    assert ctx.pending_spell is None, "request consumed"
    print("support: supporter casts a pending 방비 request even in rest.sleeping")


def test_no_spell_when_none_pending():
    ctx, sent, eng = _mk(role="supporter", job="마법", partner="플레이어제로")
    _enter(eng, ctx, "wait", "recover")
    sent.clear()
    ctx.state.at_prompt = True
    eng.tick()
    assert not any("걸어" in c for c in sent), f"no cast without a request: {sent}"
    print("support: no cast when nothing is pending — self-gated")


# --- survival still wins a same-tick tie -------------------------------------
def test_survival_outranks_combo_on_tie():
    """The always-on bundles are appended AFTER the state's own arm, so a survival
    reflex (armed by the state) is checked first. Here a low-HP 전사 mid-combo with a
    potion drinks BEFORE continuing the combo."""
    ctx, sent, eng = _mk(job="전사", level=70)
    _enter(eng, ctx, "hunting", "clear_room")   # arms survival + combat + support
    # low HP with a potion available -> survival's should_drink_hp should fire
    kn = get_knowledge()
    hp_pct = kn.get("survival", {})               # config presence not required for the point
    ctx.state.vitals.hp = 40                       # 10% of 400 -> below any drink threshold
    ctx.state.inventory = list(kn.get("potions", {}).get("hp", []))[:1] or ["체력물약"]
    # make the potion name actually count
    if not ctx.potion_count("hp"):
        # seed a matching potion name into knowledge for the test
        kn.setdefault("potions", {}).setdefault("hp", []).append("체력물약")
        ctx.state.inventory = ["체력물약"]
    _in_battle(ctx)
    sent.clear()
    ctx.state.at_prompt = True
    eng.tick()
    assert sent, "something should fire"
    assert sent[0] != "연타", f"survival must win the tie, not combo: {sent}"
    print(f"priority: survival outranks the always-on combo on a tie ({sent[0]!r} first)")


if __name__ == "__main__":
    test_combo_continues_while_resting()
    test_combo_continues_in_travel()
    test_combo_only_for_eligible_class()
    test_combo_silent_when_not_fighting()
    test_knockdown_cue_matches_the_real_line()
    test_stand_up_on_knockdown_while_resting()
    test_no_stand_when_not_knocked_down()
    test_spell_request_answered_while_resting()
    test_no_spell_when_none_pending()
    test_survival_outranks_combo_on_tie()
    print("\nALL ALWAYS-REFLEX TESTS PASSED")
