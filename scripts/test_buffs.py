"""Buff protocol: the leader keeps asking the supporter (말 <buff>!) until the cast is
CONFIRMED landed (the buff's `landed` message), and re-asks the moment it FADES (the
`faded` message) — so a buff is never silently absent. Signals are data-driven in
[buff_signals], so a new buff (방비, 빨리가기, …) is taught with no code change."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import events as ev
from gumiho.access import CharCtx, BuffTimer
from gumiho.command import Cmd
from gumiho.reload import get_knowledge, Reloader
from gumiho.state import WorldState

CLOCK = [1000.0]
def now(): return CLOCK[0]
def adv(dt): CLOCK[0] += dt


def _load():
    Reloader(lambda: []).load_all()
    from behavior.hooks import hunting, common, party  # noqa: F401
    return get_knowledge()


def _leader(kn, job="전사 검사"):
    st = WorldState()
    st.apply(ev.Status(hp=(3000, 3000), mp=(100, 100), mv=(400, 400), job=job))
    ctx = CharCtx("a", "leader", st, kn, set(), name="플레이어제로", now=now)
    ctx.partner_name = "스튀르들뤼손"
    return ctx


def _signals(ctx, text):
    """Replicate webui.on_text's [buff_signals] scan: confirm on `landed`, expire on
    `faded`. (webui does exactly this per prompt.)"""
    for buff, sig in ctx.knowledge.get("buff_signals", {}).items():
        if sig.get("landed") and sig["landed"] in text:
            ctx.buffs.confirm(buff)
        if sig.get("faded") and sig["faded"] in text:
            ctx.buffs.expire(buff)


# --- 1. the server messages confirm / expire the buffs (data-driven) ---------
def test_buff_signals_confirm_and_expire():
    kn = _load()
    ctx = _leader(kn)
    assert not ctx.buffs.active("방비")
    _signals(ctx, "\r\n누군가 당신을 보호함을 느낍니다.\r\n")
    assert ctx.buffs.active("방비"), "방비 landing message -> confirmed active"

    assert not ctx.buffs.active("빨리가기")
    _signals(ctx, "\r\n당신은 더 빨리 움직이기 시작합니다.\r\n")
    assert ctx.buffs.active("빨리가기"), "빨리가기 landing message -> confirmed active"
    _signals(ctx, "\r\n당신은 천천히 다니기 시작합니다.\r\n")
    assert not ctx.buffs.active("빨리가기"), "빨리가기 fade message -> expired"
    print("buff signals: 방비/빨리가기 land -> active; 빨리가기 fade -> expired")


# --- 2. active() needs a CONFIRM; expire clears it ---------------------------
def test_buff_timer_confirmation_semantics():
    bt = BuffTimer({"방비": 240.0, "빨리가기": 300.0}, now)
    bt.requested("방비")                       # asked, but not landed
    assert not bt.active("방비"), "a request alone must NOT count as active"
    assert not bt.should_reask("방비", 8.0), "just asked -> throttled"
    adv(8.0)
    assert bt.should_reask("방비", 8.0), "throttle elapsed, still not active -> re-ask"
    bt.confirm("방비")
    assert bt.active("방비"), "confirmed -> active"
    assert not bt.should_reask("방비", 8.0), "active -> no re-ask"
    adv(240.0)
    assert not bt.active("방비"), "lapsed after its duration"
    assert bt.should_reask("방비", 8.0), "lapsed -> re-ask"

    bt.confirm("빨리가기")
    assert bt.active("빨리가기")
    bt.expire("빨리가기")                       # the fade message
    assert not bt.active("빨리가기"), "expire clears the confirmation"
    assert bt.should_reask("빨리가기", 8.0), "expired -> re-ask immediately (throttle dropped)"
    print("BuffTimer: request≠active; confirm=active; lapse & explicit expire both re-open")


# --- 3. the leader re-asks EACH required buff until confirmed -----------------
def test_leader_reasks_all_required_until_confirmed():
    from behavior.hooks.hunting import _maintain_buffs
    kn = _load()
    ctx = _leader(kn)
    req = ctx.required_buffs()                   # config-driven (방비/빨리가기/분노/허상/…)
    signals = kn["buff_signals"]
    # GUARD: every required buff MUST have a landed signal — else the leader can never confirm it
    # and re-asks it FOREVER (the "허상! 말" spam: 허상 was required with no [buff_signals] entry).
    missing = [b for b in req if b not in signals or not signals[b].get("landed")]
    assert not missing, f"required buffs with no landed signal (re-asked forever): {missing}"
    asks = [f"{b}! 말" for b in req]
    sent = []
    cmd = Cmd(sent.append)

    _maintain_buffs(ctx, cmd)                   # all missing -> ask for EACH required, in order
    assert sent == asks, sent
    _maintain_buffs(ctx, cmd)                   # immediately -> throttled, no dup
    assert sent == asks, sent

    # confirm the FIRST required buff via its landed line -> it drops out; the REST are re-asked
    _signals(ctx, signals[req[0]]["landed"])
    adv(8.0)
    _maintain_buffs(ctx, cmd)
    assert sent == asks + [f"{b}! 말" for b in req[1:]], sent

    # confirm the rest -> ALL active -> silence (no buff re-asked while it's still up — the 허상 fix)
    for b in req[1:]:
        _signals(ctx, signals[b]["landed"])
    adv(8.0)
    before = list(sent)
    _maintain_buffs(ctx, cmd)
    assert sent == before, f"all confirmed -> silent, got extra: {sent[len(before):]}"
    print(f"leader: asks each required buff {req} until its landing confirms; then silent")


# --- 4. a FADE message triggers an immediate re-ask --------------------------
def test_fade_triggers_immediate_reask():
    from behavior.hooks.hunting import _maintain_buffs
    kn = _load()
    ctx = _leader(kn)
    sent = []
    cmd = Cmd(sent.append)
    ctx.buffs.confirm("방비")                   # isolate: only 빨리가기 in play here
    _signals(ctx, "당신은 더 빨리 움직이기 시작합니다")   # 빨리가기 active
    _maintain_buffs(ctx, cmd)
    assert "빨리가기! 말" not in sent, "active -> no ask"

    _signals(ctx, "당신은 천천히 다니기 시작합니다")      # it FADES
    _maintain_buffs(ctx, cmd)
    assert "빨리가기! 말" in sent, "fade -> re-ask at once, not after the duration timer"
    print("fade: 빨리가기 expiry message -> immediate re-ask")


# --- 5. supporter never drives the request -----------------------------------
def test_supporter_never_asks():
    from behavior.hooks.hunting import _maintain_buffs
    kn = _load()
    sup = CharCtx("b", "supporter", WorldState(), kn, set(), name="스튀르들뤼손", now=now)
    sup.state.apply(ev.Status(hp=(900, 900), mp=(1400, 1400), mv=(300, 300), job="마법"))
    sup.partner_name = "플레이어제로"
    ssent = []
    _maintain_buffs(sup, Cmd(ssent.append))
    assert ssent == [], ssent
    print("supporter: never asks for buffs (leader-only)")


def test_every_job_required_buff_has_a_signal():
    """EVERY buff ANY job requires must have a landed [buff_signals] entry — else it never confirms
    and is re-asked forever. That 돌면/허상 gap (성직) drove the supporter to re-cast nonstop, draining
    its MP below full_pct so the duo rested/idled endlessly. This guards all jobs, not just the test
    character's, and would have caught it."""
    kn = _load()
    signals = kn["buff_signals"]
    missing = {}
    for job, buffs in kn.get("required_buffs", {}).items():
        for b in buffs:
            if b not in signals or not signals[b].get("landed"):
                missing.setdefault(job, []).append(b)
    assert not missing, f"required buffs with no landed signal (re-asked forever): {missing}"
    print("every job's required buffs have a landed signal (수호/축복 are self-cast, separate)")


def test_landed_signals_match_real_server_lines():
    """The 허상 spam bug: the configured `landed` text ('여러개의 허상으로 분리…') did NOT match what
    the server ACTUALLY prints when 허상 lands ('당신의 허상이 나타나기 시작합니다') — 0 matches in the
    logs — so 허상 never confirmed and was re-asked 157× a session. The existing 'signal exists' guard
    can't catch a wrong-but-present text; this pins every buff's landed/faded to REAL server lines
    captured from session logs, so a mismatched signal fails HERE, not in production."""
    kn = _load()
    sig = kn["buff_signals"]
    # (buff -> (real land line, real fade line)) — verified verbatim from session-2026*.log leader streams.
    REAL = {
        "방비":     ("누군가 당신을 보호함을 느낍니다.", None),
        "빨리가기": ("당신은 더 빨리 움직이기 시작합니다.", "당신은 천천히 다니기 시작합니다."),
        "분노":     ("당신은 피가 끓어오는 분노를 느낍니다.", None),
        "허상":     ("당신의 허상이 나타나기 시작합니다!", "당신의 허상이 당신의 몸과 합쳐집니다."),
        "돌면":     ("당신의 피부가 돌로 변하는 것을 느낍니다.", "당신은 피부가 부드러워지는것을 느낍니다."),
    }
    for buff, (land, fade) in REAL.items():
        s = sig.get(buff, {})
        assert s.get("landed") and s["landed"] in land, \
            f"{buff}: configured landed {s.get('landed')!r} does NOT appear in the REAL line {land!r}"
        if fade and s.get("faded"):
            assert s["faded"] in fade, \
                f"{buff}: configured faded {s['faded']!r} does NOT appear in the REAL line {fade!r}"
    print("buff signals match REAL server lines (허상 wrong-landed-text regression guard)")


if __name__ == "__main__":
    test_buff_signals_confirm_and_expire()
    test_buff_timer_confirmation_semantics()
    test_leader_reasks_all_required_until_confirmed()
    test_fade_triggers_immediate_reask()
    test_supporter_never_asks()
    test_every_job_required_buff_has_a_signal()
    test_landed_signals_match_real_server_lines()
    print("\nALL BUFF TESTS PASSED")
