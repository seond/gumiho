"""Test equipment parsing (real 장비 sample) + the resupply workflow flow."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import events as ev
from gumiho.access import CharCtx, Cursor
from gumiho.command import Cmd
from gumiho.engine import Engine
from gumiho.parser import StreamParser
from gumiho.reload import get_knowledge, Reloader
from gumiho.state import WorldState

CLOCK = [1000.0]
def now(): return CLOCK[0]

SAMPLE = (
    "당신이 사용하고 있는 물건:\r\n"
    "[횃불]       은십자가  ....뿌연 오로라에 둘러싸여 있습니다!\r\n"
    "[몸]         은갑옷 [48/50]\r\n"
    "[머리]       청동갑투구 [43/50]\r\n"
    "[방패]       새갑 방패 [47/50] ....뿌연 오로라에 둘러싸여 있습니다!\r\n"
    "[두름]       코카트리스의 가죽 [40/40]\r\n"
    "[무기]       미스릴 칼 ...웅웅 대는 소리가 납니다!\r\n"
    "[손]         박쥐 가죽 장갑\r\n"
    "\r\n457:100:169> "
)


def test_parse_dual_class_status():
    """The warrior's 점수 shows a DUAL class '전사 검사'; the job regex must capture
    the whole multi-word class AND the level (a single-token capture failed the whole
    match, leaving job/level None -> 연타 eligibility could never see the class)."""
    got = []
    p = StreamParser(lambda e: got.append(e) if isinstance(e, ev.Status) else None)
    p._mode = "none"
    p.feed("* 직  업 : 전사 검사  (레벨 : 81)   * 종  족 : 거인\r\n"
           "\r\n457:100:169> ")
    st = got[-1]
    assert st.job == "전사 검사", repr(st.job)
    assert st.level == 81, st.level
    print("parse 점수 dual class -> job:", st.job, "level:", st.level)


def test_parse_item_rotted():
    """A perishable decaying in hand -> ItemRotted(name). The name matches the 장비
    equipped form, and BOTH particle forms parse: spaced ('장화 가') and attached
    ('완장이'). A leaked prompt prefix must not bleed into the name."""
    got = []
    p = StreamParser(lambda e: got.append(e) if isinstance(e, ev.ItemRotted) else None)
    p._mode = "none"
    p.feed("고블린 머드 장화 가 당신의 손안에서 썩어 없어집니다.\r\n")
    p.feed("청동갑완장이 당신의 손안에서 썩어 없어집니다.\r\n")
    p.feed("2373:100:387> 삼지창이 당신의 손안에서 썩어 없어집니다.\r\n")
    names = [e.name for e in got]
    assert names == ["고블린 머드 장화", "청동갑완장", "삼지창"], names
    print("parse rot -> names:", names)


def test_supporter_repairs_at_smith():
    """The supporter self-inspects its gear during resupply and repairs its OWN worn
    items at 대장간 — it never did before, so its armour slowly broke and the party
    couldn't stay strong. The smith detour also fires when only the supporter is worn."""
    from behavior.hooks.resupply import tag_along, repair_needed
    Reloader(lambda: []).load_all()
    ctx = CharCtx("b", "supporter", WorldState(), get_knowledge(), set(), name="짝꿍", now=now)
    ctx.partner_name = "영웅"
    ctx.together = True
    ctx.cursor = Cursor(workflow="resupply", sub="sup_follow")
    sent = []
    cmd = Cmd(sent.append)
    # (1) first tag_along tick -> self gear inspect (may be preceded by a throttled 봐 refresh
    # that keeps together/at_anchor honest — the anti-deadlock look)
    tag_along(ctx, cmd)
    assert [c for c in sent if c != "봐"] == ["장비"], sent
    ctx.equipment = [{"slot": "몸", "name": "은갑옷", "cur": 20, "max": 50},
                     {"slot": "머리", "name": "청동투구", "cur": 50, "max": 50}]
    # (2) at 대장간 -> repair the worn item (one per tick)
    sent.clear(); ctx.state.room_title = "대장간"
    tag_along(ctx, cmd)
    assert sent == ["은갑옷 수리"], sent
    # (3) worn list exhausted -> no more repair sends
    sent.clear(); tag_along(ctx, cmd)
    assert sent == [], "repairs done, no re-send"
    # (Part B) the leader's smith detour fires when only the PARTNER's gear is worn
    leader = CharCtx("a", "leader", WorldState(), get_knowledge(), set(), name="영웅", now=now)
    leader.equipment = [{"slot": "몸", "name": "갑옷", "cur": 50, "max": 50}]  # leader fine
    leader.partner = ctx                                                       # supporter worn
    assert repair_needed(leader), "detour to the smith when only the supporter is worn"
    print("supporter repair: self-inspect 장비 + '<item> 수리' at 대장간; detour on partner wear")


def test_parse():
    got = []
    p = StreamParser(lambda e: got.append(e) if isinstance(e, ev.Equipment) else None)
    p._mode = "none"          # simulate post-login (real sessions exit login first)
    p.feed(SAMPLE)
    eq = got[-1]
    by = {it["name"]: it for it in eq.items}
    assert by["은갑옷"]["cur"] == 48 and by["은갑옷"]["max"] == 50
    assert by["새갑 방패"]["cur"] == 47                    # multi-word name kept
    assert by["코카트리스의 가죽"]["cur"] == 40            # full
    assert by["박쥐 가죽 장갑"]["cur"] is None             # no durability shown
    worn = [it["name"] for it in eq.items
            if it["cur"] is not None and it["cur"] < it["max"]]
    assert worn == ["은갑옷", "청동갑투구", "새갑 방패"], worn
    print("parse 장비 -> worn:", worn)


def _mk(nav, name="영웅"):
    st = WorldState()
    st.apply(ev.Status(hp=(400, 400), mp=(100, 100), mv=(200, 200), job="전사"))
    ctx = CharCtx("a", "leader", st, get_knowledge(), set(), name=name, now=now)
    ctx.nav = nav
    sent = []
    return ctx, sent, Engine(ctx, Cmd(sent.append), now=now)


def _run(equipment, siru=5, name="영웅"):
    """Run resupply to completion, injecting `equipment` when 장비 is issued and an
    inventory holding `siru` 시루떡 when 소지품 is issued (siru>=5 skips the 떡집 leg)."""
    routes = {"광장 사거리": ["남", "남"], "대장간": ["동"]}
    ctx, sent, eng = _mk(lambda t: list(routes[t]) if t in routes else None, name=name)
    eng.boot("resupply")
    injected = inv_injected = False
    for _ in range(80):
        ctx.state.apply(ev.Prompt(hp=400, mp=100, mv=200))
        CLOCK[0] += 1.0
        if ctx.cursor.sub in ("recall", "sup_recall"):
            ctx.state.room_title = "중앙 광장"                   # simulate recall landing
        elif ctx.cursor.scratch.get("route") == [] and ctx.cursor.scratch.get("dest"):
            ctx.state.room_title = ctx.cursor.scratch["dest"]   # arrived: route walked out
        if ctx.cursor.sub == "check_provisions" and not inv_injected:
            ctx.state.apply(ev.Inventory([f"시루떡 ({siru})"] if siru else ["성수 (2)"]))
            inv_injected = True
        if ctx.cursor.sub == "inspect_gear" and not injected:
            ctx.on_event(ev.Equipment(equipment))
            injected = True
        eng.tick()
        if not eng.enabled:
            break
    return ctx, sent, eng


def test_flow_repair():
    Reloader(lambda: []).load_all()
    ctx, sent, eng = _run([
        {"slot": "몸", "name": "은갑옷", "cur": 48, "max": 50},
        {"slot": "머리", "name": "청동갑투구", "cur": 43, "max": 50},
        {"slot": "두름", "name": "코카트리스의 가죽", "cur": 40, "max": 40},
    ])
    assert eng.enabled is False and ctx.cursor.sub == "done", ctx.cursor
    for c in ["귀환", "남", "버드 물 채워", "장비", "은갑옷 수리", "청동갑투구 수리"]:
        assert c in sent, f"missing {c!r} in {sent}"
    assert sent.count("귀환") == 1, f"no recall after repair: {sent}"
    assert "코카트리스의 가죽 수리" not in sent, "repaired a full item!"
    assert "서" in sent, f"reverse walk-back present: {sent}"       # detoured + reversed
    assert not any(";" in x for x in sent), f"no ';' chaining: {sent}"
    print("repair branch:", sent)


def test_flow_no_repair():
    Reloader(lambda: []).load_all()
    ctx, sent, eng = _run([
        {"slot": "몸", "name": "은갑옷", "cur": 50, "max": 50},          # all full
        {"slot": "두름", "name": "코카트리스의 가죽", "cur": 40, "max": 40},
    ])
    assert eng.enabled is False and ctx.cursor.sub == "done", ctx.cursor
    assert "수리" not in " | ".join(sent), f"should not repair anything: {sent}"
    assert "장비" in sent and "버드 물 채워" in sent
    # no 대장간 detour: trail is just the fountain 남남 -> reversed 북북, no 서
    assert sent.count("북") >= 2 and "서" not in sent, f"should skip 대장간: {sent}"
    assert sent.count("귀환") == 1
    assert not any(";" in x for x in sent), f"no ';' chaining: {sent}"
    print("no-repair branch:", sent)


def test_bakery_buys_when_short():
    """< target 시루떡 -> detour to 한성 떡집, buy the shortfall (object-first '시루떡 사'),
    round-trip back to the fountain, then continue. The 떡집 leg must NOT pollute the
    walk-back trail (it's a side-branch round-trip, not on the linear path home)."""
    Reloader(lambda: []).load_all()
    target = get_knowledge()["resupply"]["provision_target"]   # user-tunable; don't hardcode
    held = 0                                      # hold none -> buy the full target
    ctx, sent, eng = _run([                      # gear all full -> no smith detour
        {"slot": "몸", "name": "은갑옷", "cur": 50, "max": 50},
    ], siru=held)
    assert eng.enabled is False and ctx.cursor.sub == "done", ctx.cursor
    assert sent.count("시루떡 사") == target - held, f"should buy exactly the shortfall ({target}-{held}): {sent}"
    assert "소지품" in sent, "checks the bag with 소지품"
    # bought at the bakery, before inspecting gear
    assert sent.index("시루떡 사") < sent.index("장비"), f"buy before gear inspect: {sent}"
    print("bakery short -> bought:", sent.count("시루떡 사"), "시루떡")


def test_bakery_skipped_when_stocked():
    """>= target 시루떡 already -> never stop by 떡집 (no 시루떡 사, no bakery detour)."""
    Reloader(lambda: []).load_all()
    ctx, sent, eng = _run([
        {"slot": "몸", "name": "은갑옷", "cur": 50, "max": 50},
    ], siru=5)
    assert eng.enabled is False and ctx.cursor.sub == "done", ctx.cursor
    assert "시루떡 사" not in sent, f"already stocked -> must not buy: {sent}"
    assert "소지품" in sent, "still checks the bag to decide"
    print("bakery stocked -> skipped, no buy")


def test_repair_uses_alias():
    """A parsed gear name that isn't the server's registered name must be looked up
    in [aliases] before the repair command (기간테스의바지 -> 기간). Unmapped names
    pass through unchanged."""
    Reloader(lambda: []).load_all()
    ctx, sent, eng = _run([
        {"slot": "다리", "name": "기간테스의바지", "cur": 56, "max": 60},   # worn, aliased
        {"slot": "몸", "name": "은갑옷", "cur": 30, "max": 50},              # worn, no alias
    ])
    assert eng.enabled is False and ctx.cursor.sub == "done", ctx.cursor
    assert "기간 수리" in sent, f"should repair via the alias '기간': {sent}"
    assert "기간테스의바지 수리" not in sent, f"must NOT use the raw parsed name: {sent}"
    assert "은갑옷 수리" in sent, f"unmapped names pass through: {sent}"
    print("repair uses alias: 기간테스의바지 -> 기간")


def test_strip_engraving():
    """An item engraved with the character's OWN name loses the '<name>의 ' prefix;
    anyone ELSE's engraving, and plain names that merely CONTAIN 의, are untouched."""
    from gumiho.access import strip_engraving as se
    me = "플레이어제로"
    assert se("플레이어제로의 소드브레이커", me) == "소드브레이커"
    assert se("플레이어제로의 불사의 갑옷", me) == "불사의 갑옷"      # only the LEADING 의
    assert se("현실의자각의 블랙 레네게이드", "현실의자각") == "블랙 레네게이드"  # 의 inside the NAME
    assert se("플레이어제로의카타나", me) == "카타나"                 # engraved without a space
    assert se("기간테스의바지", me) == "기간테스의바지"               # plain name containing 의
    assert se("스튀르들뤼손의 스파이드", me) == "스튀르들뤼손의 스파이드"  # someone else's
    assert se("플레이어제로의", me) == "플레이어제로의"               # never strip to nothing
    assert se("소드브레이커", None) == "소드브레이커"                 # unknown owner: no-op
    print("strip_engraving: own engraving dropped, others kept")


def test_repair_de_engraves():
    """Engraved gear needs NO [aliases] entry — the repair command uses the bare name.
    Another player's engraving still goes through [aliases] unchanged."""
    Reloader(lambda: []).load_all()
    ctx, sent, eng = _run([
        {"slot": "무기", "name": "플레이어제로의 새칼", "cur": 20, "max": 60},   # ours, unaliased
        {"slot": "몸", "name": "객현국의 거불탄방패", "cur": 30, "max": 50},      # not ours, aliased
    ], name="플레이어제로")
    assert eng.enabled is False and ctx.cursor.sub == "done", ctx.cursor
    assert "새칼 수리" in sent, f"engraved name must reduce to the bare one: {sent}"
    assert "플레이어제로의 새칼 수리" not in sent, f"raw engraved name sent: {sent}"
    assert "거불탄 수리" in sent, f"another player's engraving keeps its alias: {sent}"
    print("repair de-engraves: 플레이어제로의 새칼 -> 새칼")


def test_recall_persists():
    """Recall never lands -> NEVER give up (no dead-end halt). Keeps retrying 귀환
    indefinitely and stays enabled, so a later manual recall can still rescue it."""
    ctx, sent, eng = _mk(lambda t: None)          # never reaches the anchor
    eng.boot("resupply")
    for _ in range(60):
        ctx.state.apply(ev.Prompt(hp=400, mp=100, mv=200))
        ctx.state.room_title = "어딘가"            # NOT 중앙 광장 -> at_anchor stays False
        CLOCK[0] += 5.0
        eng.tick()
    assert eng.enabled is True, "must NOT halt on recall failure (dead end)"
    assert ctx.cursor.sub in ("recall", "sup_recall"), ctx.cursor
    assert sent.count("귀환") >= 5, f"should keep retrying 귀환: {sent.count('귀환')}"
    print("recall-fail: kept retrying 귀환 x", sent.count("귀환"), "— never halted")


if __name__ == "__main__":
    test_parse()
    test_parse_dual_class_status()
    test_parse_item_rotted()
    test_supporter_repairs_at_smith()
    test_flow_repair()
    test_flow_no_repair()
    test_bakery_buys_when_short()
    test_bakery_skipped_when_stocked()
    test_repair_uses_alias()
    test_strip_engraving()
    test_repair_de_engraves()
    test_recall_persists()
    print("\nALL RESUPPLY TESTS PASSED")
