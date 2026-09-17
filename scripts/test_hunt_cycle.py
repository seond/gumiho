"""M2: hunting off-cycle wiring — mob-regen wait, and the resupply trigger +
auto-return through travel. Single leader ctx (no director: sync == direct)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import events as ev
from gumiho.access import CharCtx
from gumiho.command import Cmd
from gumiho.director import Director
from gumiho.engine import Engine
from gumiho.gridsweep import GridSweep
from gumiho.mapgrid import MapGrid
from gumiho import moblore
from gumiho.reload import get_knowledge, Reloader
from gumiho.state import WorldState

# The 고려 gate assesses UNFAMILIAR mobs before engaging. These mechanics tests attack
# known mobs directly, so seed them SAFE (and never touch the real json) — the 고려
# behaviour itself is covered by test_consider_gate.
moblore._save = lambda: None
moblore._lore = {"safe": ["개구리", "뱀", "지네", "왕지네", "구렁이", "거미",
                          "다이아몬드", "하트", "스페이드", "클로버", "장미", "시녀"],
                 "danger": []}

CLOCK = [1000.0]
def now(): return CLOCK[0]

_D = {"북": (-1, 0), "남": (1, 0), "동": (0, 1), "서": (0, -1)}


def _grid(dirs):
    """A 지도 centred on the player, with a connected neighbour in each of `dirs`."""
    g = MapGrid(zone="z"); g.here = (3, 3); g.cells[(3, 3)] = "방"
    for d in dirs:
        dr, dc = _D[d]; c = (3 + dr, 3 + dc)
        g.cells[c] = "방"; g.edges.add(frozenset({(3, 3), c}))
    return g


def _feed_jido(ctx, dirs):
    """Mimic webui: fold a 지도 into the hunt's live sweep."""
    sw = ctx.gridsweep
    if sw is None:
        sw = ctx.gridsweep = GridSweep()
    sw.register(_grid(dirs))


def mk():
    st = WorldState()
    st.apply(ev.Status(hp=(400, 400), mp=(100, 100), mv=(200, 200), job="전사"))
    ctx = CharCtx("a", "leader", st, get_knowledge(), set(), name="영웅", now=now)
    ctx.partner_name = None          # solo for these mechanics tests (see test_pairing)
    ctx.nav = lambda t: {"광장 사거리": ["남"], "대장간": ["동"]}.get(t)
    sent = []
    return ctx, sent, Engine(ctx, Cmd(sent.append), now=now)


def test_wait():
    Reloader(lambda: []).load_all()
    ctx, sent, eng = mk()          # solo, full vitals
    # Simulate a small zone that reports 'swept' after a few empty rooms — the
    # real exhaustion signal (coverage.next_move -> None) now drives area_barren.
    eng.boot("hunting")
    # A 1-cell dead-end pocket: the 지도 shows no neighbours, so the sweep is done
    # immediately with no mob -> wait (sleep to recover).
    for _ in range(40):
        _room(ctx, [], exits=())
        _feed_jido(ctx, [])            # 지도 for a neighbourless cell
        eng.tick()
        if ctx.cursor.workflow == "wait":
            break
    assert ctx.cursor.workflow == "wait", ctx.cursor
    # ALREADY FULL on entry -> must NOT sleep: go straight to watch (no pointless 자→깨, which under
    # rapid re-entry was the "자-깨 storm"). Sleeping only happens when actually not recovered.
    for _ in range(8):
        _room(ctx, [])
        eng.tick()
        if ctx.cursor.sub == "watch":
            break
    assert ctx.cursor.sub == "watch", ctx.cursor
    assert "자" not in sent, "already full -> must NOT 자 (no 자-깨 storm)"
    # while watching, a mob respawns -> resume hunting
    for _ in range(4):
        _room(ctx, ["개구리가 돌아다니고 있습니다."])
        eng.tick()
        if ctx.cursor.workflow == "hunting":
            break
    assert ctx.cursor.workflow == "hunting", ctx.cursor
    assert ctx.barren_moves == 0, "barren reset on resume"
    print("wait: FULL -> straight to watch (no 자-깨) -> hunt on respawn")


def test_rest_is_leader_driven():
    """The SUPPORTER never self-triggers rest — it enters rest only via the leader's synced switch,
    so it can't be left asleep while the leader is still fighting/fleeing (an asleep supporter can't
    chase). The corollary: the LEADER now rests for the PARTNER's low vitals in any zone (the
    supporter lost its self-rest, so the leader must pick up its need)."""
    Reloader(lambda: []).load_all()
    from behavior.hooks.hunting import rest_ready, _low_vitals, flag_rest
    cmd = Cmd(lambda _l: None)
    lc = CharCtx("a", "leader", WorldState(), get_knowledge(), set(), name="L", now=now); lc.partner_name = "S"
    sc = CharCtx("b", "supporter", WorldState(), get_knowledge(), set(), name="S", now=now); sc.partner_name = "L"
    lc.partner = sc; sc.partner = lc
    lc.state.apply(ev.Status(hp=(400, 400), mp=(100, 100), mv=(400, 400), job="전사"))
    sc.state.apply(ev.Status(hp=(400, 400), mp=(100, 100), mv=(400, 400), job="마법사"))
    lc.state.in_battle = sc.state.in_battle = False
    flag_rest(lc, cmd); flag_rest(sc, cmd)          # latch rest-needed on BOTH
    # SUPPORTER: even latched + everyone safe -> rest_ready stays FALSE (leader-driven only)
    assert not rest_ready(sc), "supporter must NEVER self-trigger rest"
    # LEADER: latched + safe -> rest_ready True (it drives the synced rest)
    assert rest_ready(lc), "leader triggers rest when safe + latched"
    # LEADER rests for the PARTNER's low vitals — here the supporter's MP is low, non-철면 zone
    sc.state.apply(ev.Status(hp=(400, 400), mp=(5, 100), mv=(400, 400)))     # supporter MP ~5%
    assert _low_vitals(lc), "leader's rest-need picks up the partner's low MP (any zone)"
    print("rest: leader-driven (supporter never self-rests); leader rests for a low partner")


def test_leader_rest_ends_on_hp_not_mp():
    """When resting, the LEADER wakes on HP alone — it does NOT wait for MP to top up (melee-primary;
    MP regens while hunting). The SUPPORTER still needs MP (buffs/spells at ~700 a cast), so its low
    MP keeps the pair resting. Config: [rest].leader_mp_pct (default 0 -> leader MP ignored)."""
    from behavior.hooks.hunting import both_recovered
    Reloader(lambda: []).load_all()
    kn = get_knowledge()
    full = kn["rest"]["full_pct"]
    lc = CharCtx("a", "leader", WorldState(), kn, set(), name="L", now=now)
    # full HP, MP nearly empty -> recovered (MP is NOT the leader's gate anymore)
    lc.state.apply(ev.Status(hp=(400, 400), mp=(5, 100), mv=(400, 400), job="성직"))
    assert both_recovered(lc), "leader: full HP + low MP -> wake (MP not required)"
    # HP still gates the leader
    lc.state.apply(ev.Status(hp=(40, 400), mp=(100, 100), mv=(400, 400)))
    assert not both_recovered(lc), "leader: low HP -> keep resting (HP is what matters)"
    # SUPPORTER low MP still holds the pair — its casts are the real bottleneck
    lc.state.apply(ev.Status(hp=(400, 400), mp=(100, 100), mv=(400, 400)))          # leader fine
    sc = CharCtx("b", "supporter", WorldState(), kn, set(), name="S", now=now)
    lc.partner = sc; sc.partner = lc
    sc.state.apply(ev.Status(hp=(400, 400), mp=(int(400 * full / 100), 400), mv=(400, 400), job="마법"))
    assert both_recovered(lc), "leader fine + supporter MP banked -> wake"
    sc.state.apply(ev.Status(hp=(400, 400), mp=(5, 400), mv=(400, 400)))            # supporter MP crashes
    assert not both_recovered(lc), "supporter low MP still holds the rest (it lives on MP)"
    print("rest: leader wakes on HP (MP not required); supporter MP still gates")


def test_wait_doze_only_sleeps_when_low():
    """wait_doze sends 자 ONLY when not already recovered — the fix for the 자-깨 storm (a cleared
    zone where both are full would otherwise 자→both_recovered→깨 on every wait entry)."""
    from behavior.hooks.hunting import wait_doze, dozed_and_recovered, both_recovered
    Reloader(lambda: []).load_all()
    ctx, sent, eng = mk()                      # solo, full vitals
    cmd = Cmd(sent.append)
    assert both_recovered(ctx), "full + solo -> recovered"
    wait_doze(ctx, cmd)
    assert sent == [], f"already full -> NO 자 (no storm): {sent}"
    assert not dozed_and_recovered(ctx), "never dozed -> routes straight to watch"
    # genuinely low -> it DOES sleep, then wakes once recovered
    ctx.state.apply(ev.Status(hp=(40, 400), mp=(100, 100), mv=(200, 200)))
    assert not both_recovered(ctx)
    sent.clear(); wait_doze(ctx, cmd)
    assert sent == ["자"], f"low vitals -> sleep: {sent}"
    ctx.state.apply(ev.Status(hp=(400, 400), mp=(100, 100), mv=(200, 200)))   # recovered
    assert dozed_and_recovered(ctx), "slept then recovered -> wake (깨)"
    print("wait_doze: no 자 when full; sleeps when low; wakes after a real sleep")


def _sim(ctx):
    """Supply the external conditions each state is waiting on."""
    sub = ctx.cursor.sub
    if sub in ("reform", "arrive"):
        ctx.state.entities = [f"{ctx.partner_name}가 서 있습니다."]   # co-located
    else:
        ctx.state.entities = []
    if sub in ("recall", "sup_recall"):
        ctx.state.room_title = "중앙 광장"                           # recall landed
    if sub == "inspect_gear" and ctx.equipment is None:
        ctx.on_event(ev.Equipment([{"slot": "몸", "name": "갑옷", "cur": 50, "max": 50}]))
    if sub == "refill":
        ctx.state.hungry = ctx.state.thirsty = False        # the fountain fed/quenched us


def test_resupply_trigger_and_return():
    Reloader(lambda: []).load_all()
    kn = get_knowledge()
    kn["travel_routes"] = {"한성의 하수구": ["남", "동"]}       # recorded return route
    ctx, sent, eng = mk()
    eng.boot("hunting")
    ctx.state.hungry = True                                     # a real need only town clears

    seq = []
    for _ in range(200):
        CLOCK[0] += 1.5
        _sim(ctx)
        ctx.state.apply(ev.Prompt(hp=400, mp=100, mv=200))
        eng.tick()
        if not seq or seq[-1] != ctx.cursor.workflow:
            seq.append(ctx.cursor.workflow)
        if seq[-2:] == ["resupply", "hunting"] and len(seq) >= 3:
            break

    assert "resupply" in seq, f"hunger should trigger resupply: {seq}"
    assert seq[-1] == "hunting", f"auto resupply should return to hunting: {seq}"
    assert ctx.cursor.workflow == "hunting", f"cycle should end back hunting: {seq}"
    assert ctx.auto_cycle is True
    assert eng.enabled is True, "auto cycle must NOT halt"
    assert ctx.state.hungry is False, "the fountain resolved hunger"
    print("resupply cycle workflow path:", seq)


def test_manual_resupply_halts():
    Reloader(lambda: []).load_all()
    ctx, sent, eng = mk()
    eng.boot("resupply")                              # manual: auto_cycle stays False
    for _ in range(120):
        CLOCK[0] += 1.5
        _sim(ctx)
        ctx.state.apply(ev.Prompt(hp=400, mp=100, mv=200))
        eng.tick()
        if not eng.enabled:
            break
    assert eng.enabled is False, "manual resupply should halt at 치료실"
    assert ctx.cursor.sub == "done", ctx.cursor
    assert ctx.auto_cycle is False
    print("manual resupply halts at 치료실 OK")


def test_hunt_target_parameter():
    Reloader(lambda: []).load_all()
    ctx, sent, eng = mk()
    eng.boot("hunting", target="특정존")
    assert ctx.hunt_target == "특정존" and ctx.hunt_zone() == "특정존"
    # Movement is driven off the 지도 grid, not the zone name: walk to a neighbour.
    for _ in range(8):
        _room(ctx, [], exits=["북"])
        _feed_jido(ctx, ["북"])
        eng.tick()
        if "북" in sent:
            break
    assert "북" in sent, f"sweep should walk the grid: {sent}"
    # a TARGETLESS re-boot (engine toggle / workflow reboot / auto-reconnect re-arm)
    # must PRESERVE the standing target, so travel still heads to the deep pocket
    # instead of the shallow default zone.
    eng.boot("hunting")
    assert ctx.hunt_target == "특정존", "targetless re-boot must keep the target"
    assert ctx.hunt_zone() == "특정존"
    # only a genuinely never-targeted engine falls back to the default zone.
    ctx2, _, eng2 = mk()
    eng2.boot("hunting")
    assert ctx2.hunt_target is None
    assert ctx2.hunt_zone() == get_knowledge()["hunt"]["zone"]
    print("hunt target: set carries through, targetless re-boot preserves it")


def test_pick_target_substring_alias():
    """Target picking is data-driven: an [aliases] KEY found as a substring of a mob
    line overrides the parser (non-empty VALUE = attack kw, empty = skip), while
    normal verb lines still parse directly. No zone logic hardcoded."""
    from behavior.hooks.hunting import _pick_target
    Reloader(lambda: []).load_all()
    ctx, _, _ = mk()
    ctx.knowledge["aliases"]["고양이"] = "고양이"   # override the mangled "고양"
    ctx.knowledge["aliases"]["진열장"] = ""          # a described prop -> skip
    # a card (description) is attacked by suit via substring
    ctx.state.entities = ["다이아몬드 킹 카드이다", "거미가 기어가고 있다."]
    assert _pick_target(ctx) == "다이아몬드", "card -> suit via substring override"
    # the mangled-cat line is overridden to the real keyword, not "고양"
    ctx.state.entities = ["금빛눈의 고양이 체사네코다."]
    assert _pick_target(ctx) == "고양이", "고양이 substring overrides parser's 고양"
    # a 'skip' entity (empty value) is passed over -> the card behind it is picked
    ctx.state.entities = ["먼지 쌓인 진열장이다.", "하트 킹 카드이다"]
    assert _pick_target(ctx) == "하트", "진열장='' skips the prop, card picked"
    # a row declared kind='item' is NOT a hostile (never attacked); the card is
    ctx.knowledge["aliases"]["목걸이"] = {"name": "목걸이", "kind": "item"}
    ctx.state.entities = ["체사네코가 아끼는 목걸이이다.", "스페이드 퀸 카드이다"]
    assert "체사네코가 아끼는 목걸이이다." not in ctx.hostiles(), "item-kind row not a hostile"
    assert _pick_target(ctx) == "스페이드", "item skipped, card picked"
    # a normal verb line with no alias still parses directly
    ctx.state.entities = ["거미가 기어가고 있다."]
    assert _pick_target(ctx) == "거미", "normal verb line still parses"
    # WHOLE-SENTENCE rule must beat a BROAD one (first-match-wins order): the handmaiden
    # line LEADS with 장미 flavor but the real mob is 시녀, while a genuine rose line still
    # targets 장미. The specific key is listed FIRST so it resolves before broad "장미".
    ctx.knowledge["aliases"] = {
        "빨간 페인트를 칠하고 있는 시녀": {"name": "시녀", "kind": "mob"},   # specific, first
        "하나씩 입고 있던 앞치마": {"name": "앞치마", "kind": "item"},        # whole-sentence item
        "장미": {"name": "장미", "kind": "mob"},                            # broad, after
    }
    ctx.state.entities = ["하얀 장미에 여왕님의 명령에 따라 빨간 페인트를 칠하고 있는 시녀이다."]
    assert _pick_target(ctx) == "시녀", "whole-sentence rule wins over broad 장미"
    ctx.state.entities = ["붉은 장미가 탐스럽게 피어 있다."]
    assert _pick_target(ctx) == "장미", "a genuine 장미 line still targets 장미"
    # the apron line is a whole-sentence ITEM -> excluded from hostiles, never attacked
    ctx.state.entities = ["페인트칠을 열심히 하던 하녀들이 하나씩 입고 있던 앞치마이다."]
    assert "페인트칠을 열심히 하던 하녀들이 하나씩 입고 있던 앞치마이다." not in ctx.hostiles(), \
        "whole-sentence item-kind row is not a hostile"
    assert _pick_target(ctx) is None, "apron item is never a target"
    print("pick_target: suit/mangle, skip, kind=item, whole-sentence>broad + item, normal")


def test_shopping_list():
    """A rotted perishable is remembered until re-equipped: ItemRotted -> shopping_list
    (de-duped); a 장비 that lists it equipped clears it; unrelated gear leaves it."""
    Reloader(lambda: []).load_all()
    ctx, _, _ = mk()
    ctx.on_event(ev.ItemRotted("고블린 머드 장화"))
    ctx.on_event(ev.ItemRotted("청동갑완장"))
    ctx.on_event(ev.ItemRotted("고블린 머드 장화"))          # dup ignored
    assert ctx.shopping_list == ["고블린 머드 장화", "청동갑완장"], ctx.shopping_list
    # a 장비 without either item leaves the list untouched
    ctx.on_event(ev.Equipment([{"slot": "몸", "name": "은갑옷", "cur": 40, "max": 50}]))
    assert ctx.shopping_list == ["고블린 머드 장화", "청동갑완장"], ctx.shopping_list
    # re-equipping one clears just that entry (name matches the 장비 form)
    ctx.on_event(ev.Equipment([{"slot": "발", "name": "고블린 머드 장화", "cur": 30, "max": 30}]))
    assert ctx.shopping_list == ["청동갑완장"], ctx.shopping_list
    print("shopping list: rot adds (deduped), unrelated 장비 keeps, re-equip clears")


def test_shop_remove_dispatch():
    """The UI's ✕ button releases an item from the shopping list: the webui `shop_remove`
    dispatch drops it from ctx.shopping_list and re-emits state; a missing item is a
    harmless no-op."""
    import asyncio
    from gumiho.webui import WebUI

    class _Sess:
        def __init__(self):
            self.ctx = mk()[0]
            self.ctx.shopping_list = ["고블린 머드 장화", "청동갑완장"]
            self.emitted = 0
        def _emit_state(self): self.emitted += 1

    srv = WebUI.__new__(WebUI)            # bypass __init__ — dispatch only needs .sessions
    sess = _Sess()
    srv.sessions = {"a": sess}
    asyncio.run(srv.dispatch({"type": "shop_remove", "sid": "a", "item": "청동갑완장"}))
    assert sess.ctx.shopping_list == ["고블린 머드 장화"], sess.ctx.shopping_list
    assert sess.emitted == 1, "state re-emitted so the sidebar updates"
    asyncio.run(srv.dispatch({"type": "shop_remove", "sid": "a", "item": "없는아이템"}))
    assert sess.ctx.shopping_list == ["고블린 머드 장화"], "missing item -> no-op"
    assert sess.emitted == 1, "no re-emit when nothing removed"
    print("shop_remove: ✕ releases the item + re-emits; missing item is a no-op")


def test_fixture_not_targeted():
    """Town/room fixtures like 게시판 (bulletin board) are never targeted."""
    from behavior.hooks.hunting import _pick_target
    Reloader(lambda: []).load_all()
    ctx, _, _ = mk()
    ctx.state.entities = ["커다란 공지 게시판이 벽에 걸려있다.", "거미가 기어간다."]
    assert _pick_target(ctx) == "거미", "게시판 (fixture) skipped, the real mob picked"
    print("fixture skip: 게시판 never targeted")


def test_consider_gate():
    """Unfamiliar mobs are 고려'd before engaging; a lethal verdict is recorded and the
    mob never fought; a fair one is engaged and remembered; a known-lethal seed
    (호러플랜트) is filtered from targeting entirely."""
    from behavior.hooks.hunting import _consider_gate, _pick_target
    saved_lore = moblore._lore
    moblore._lore = {"safe": [], "danger": []}   # isolated: restored in finally
    try:
        Reloader(lambda: []).load_all()
        ctx, sent, _ = mk()
        cmd = Cmd(sent.append)
        ctx.knowledge["mob_danger"] = {"danger_phrases": ["꿈도 꾸지 마세요"],
                                       "known_danger": ["호러플랜트"], "known_safe": [],
                                       "consider_wait": 2.0}
        # (1) unknown -> 고려 sent, NOT attacked
        sent.clear()
        assert _consider_gate(ctx, cmd, "왕지네") == "wait"
        assert sent == ["왕지네 고려"], sent
        # (2) still waiting -> no re-send
        sent.clear()
        assert _consider_gate(ctx, cmd, "왕지네") == "wait" and sent == [], sent
        # (3) a lethal phrase came back -> recorded danger, skip (and stays skipped)
        ctx.consider_danger = True
        assert _consider_gate(ctx, cmd, "왕지네") == "skip"
        assert moblore.classify("왕지네") == "danger"
        sent.clear()
        assert _consider_gate(ctx, cmd, "왕지네") == "skip" and sent == [], "no re-고려"
        # (4) a fair mob: 고려, then the wait elapses with no phrase -> engage + safe
        assert _consider_gate(ctx, cmd, "지네") == "wait"
        ctx.consider_at -= 5                      # simulate the consider_wait elapsing
        assert _consider_gate(ctx, cmd, "지네") == "engage"
        assert moblore.classify("지네") == "safe"
        # (5) a known-lethal seed is filtered from _pick_target entirely
        moblore.learn("개구리", "safe")
        ctx.state.entities = ["징그러운 호러플랜트가 촉수를 흔든다.", "개구리가 운다."]
        assert _pick_target(ctx) == "개구리", "호러플랜트 filtered, the fair mob picked"
        print("고려 gate: unknown→고려, danger→skip+record, fair→engage+record, lethal filtered")
    finally:
        moblore._lore = saved_lore


def test_boot_resets_stale_activity():
    """A (re)boot must start FRESH: stale buffs / gear / co-location / timers from a
    prior run must not leak in (else 방비 won't re-cast, an old 장비 triggers a false
    repair, etc.). BUT assist_on and following are SERVER-TRUTH flags (a boot keeps the
    SAME connection, so the server still has 자동지원/따라 in effect) — they must be
    PRESERVED, not reset, or the boot would re-toggle 자동지원 off and make a carried
    supporter walk its own route."""
    Reloader(lambda: []).load_all()
    ctx, sent, eng = mk()
    ctx.assist_on = True
    ctx.following = True
    ctx.buffs.requested("방비")
    ctx.equipment = [{"slot": "몸", "name": "갑옷", "cur": 10, "max": 50}]
    ctx.barren_moves = 9
    ctx.together = True
    ctx.request_stop = True
    ctx.last_error = "old"
    ctx.pending_spell = "방비"
    CLOCK[0] += 500                                    # let the timers go stale
    eng.boot("hunting", target="X")
    assert ctx.assist_on is True, "자동지원 is server-truth — a same-connection boot keeps it"
    assert ctx.following is True, "따라 is server-truth — a boot must not clear it"
    assert not ctx.buffs.active("방비"), "buffs must be cleared so they re-cast"
    assert ctx.equipment is None, "gear must be re-inspected"
    assert ctx.barren_moves == 0 and ctx.together is False
    assert ctx.request_stop is False and ctx.last_error is None
    assert ctx.pending_spell is None
    assert ctx.since_resupply() == 0 and ctx.since_kill() == 0, "timers reset to now"
    print("boot resets stale activity: buffs/gear/timers fresh; 자동지원/따라 server-truth kept")


def _room(ctx, entities, exits=("북",)):
    for e in (ev.RoomSeen(title="방", description="", exits=list(exits),
                          entities=list(entities)),
              ev.Prompt(hp=400, mp=100, mv=200)):
        ctx.state.apply(e)
        ctx.on_event(e)          # webui feeds both; together/need_look live in on_event


def test_pairing():
    Reloader(lambda: []).load_all()
    ctx, sent, eng = mk()
    ctx.partner_name = "짝꿍"                       # duo
    eng.boot("hunting")
    # starts in regroup; not co-located -> can't proceed, tries to form the party
    _room(ctx, []); eng.tick()
    assert ctx.cursor.sub == "regroup", ctx.cursor
    assert "모두 그룹" in sent, sent
    # co-located -> proceeds past regroup
    for _ in range(5):
        _room(ctx, ["짝꿍가 서 있습니다."]); eng.tick()
        if ctx.cursor.sub != "regroup":
            break
    assert ctx.cursor.sub != "regroup", "should pair up and proceed"
    for _ in range(5):
        _room(ctx, ["짝꿍가 서 있습니다."]); eng.tick()
        if ctx.cursor.sub == "clear_room":
            break
    assert ctx.cursor.sub == "clear_room", ctx.cursor
    # partner not in room -> leader HOLDS (does not wander off; a refresh 봐 is ok)
    sent.clear()
    _room(ctx, []); eng.tick()
    assert all(c not in ("북", "남", "동", "서", "위", "아래") for c in sent), \
        f"leader should hold for the partner, not move: {sent}"
    # partner gone long enough -> separated -> back to regroup
    CLOCK[0] += 30
    _room(ctx, []); eng.tick()
    assert ctx.cursor.sub == "regroup", ctx.cursor
    print("pairing: waits to pair, holds for a lagging partner, regroups when separated")


def test_rest_poll_and_wake():
    Reloader(lambda: []).load_all()
    ctx, sent, eng = mk()
    ctx.state.apply(ev.Status(hp=(50, 400), mp=(20, 100), mv=(200, 200)))  # low
    eng.boot("rest")
    # rest now re-reads 장비 first (pre-rest gear check); feed a healthy reply so it proceeds to sleep
    for _ in range(4):
        if ctx.equipment is None:
            ctx.on_event(ev.Equipment([{"slot": "무기", "name": "칼", "cur": 90, "max": 100}]))
        ctx.state.at_prompt = True; eng.tick()
        if "자" in sent: break
    assert "자" in sent, sent
    # server goes quiet (stale prompt, no fresh one) -> watchdog polls
    sent.clear()
    CLOCK[0] += 15
    ctx.state.at_prompt = True
    eng.tick()
    assert "점수" in sent, f"quiet -> should poll: {sent}"
    # poll reply still low -> stay asleep
    for e in (ev.Status(hp=(60, 400), mp=(30, 100), mv=(200, 200)),
              ev.Prompt(hp=60, mp=30, mv=200)):
        ctx.state.apply(e); ctx.on_event(e)
    eng.tick()
    assert ctx.cursor.sub == "sleeping", ctx.cursor
    # poll reply shows recovered -> wake
    for e in (ev.Status(hp=(400, 400), mp=(100, 100), mv=(200, 200)),
              ev.Prompt(hp=400, mp=100, mv=200)):
        ctx.state.apply(e); ctx.on_event(e)
    eng.tick()
    assert ctx.cursor.sub == "waking" and "깨" in sent, (ctx.cursor, sent)
    print("rest: polls while quiet, wakes when recovered")


def _mk2(sid, role, name, partner, director):
    st = WorldState()
    st.apply(ev.Status(hp=(400, 400), mp=(100, 100), mv=(200, 200), job="전사"))
    ctx = CharCtx(sid, role, st, get_knowledge(), set(), name=name, now=now)
    ctx.partner_name = partner
    ctx.nav = lambda t: {"광장 사거리": ["남"], "대장간": ["동"]}.get(t)
    sent = []
    return ctx, sent, Engine(ctx, Cmd(sent.append), director=director, now=now)


def test_duo_resupply_roles():
    Reloader(lambda: []).load_all()
    d = Director()
    lc, ls, le = _mk2("a", "leader", "영웅", "짝꿍", d)
    sc, ss, se = _mk2("b", "supporter", "짝꿍", "영웅", d)
    lc.partner = sc; sc.partner = lc
    d.register("a", le); d.register("b", se)
    le.boot("resupply"); se.boot("resupply")
    for eng, ctx in ((le, lc), (se, sc)):          # tick past the role branch
        p = ev.Prompt(hp=400, mp=100, mv=200)
        ctx.state.apply(p); ctx.on_event(p); eng.tick()
    # leader drives; supporter recalls itself to the anchor then follows
    assert lc.cursor.sub == "recall", lc.cursor
    assert sc.cursor.sub == "sup_recall", sc.cursor
    assert "귀환" in ss, "supporter should recall to the anchor too"
    # supporter never NAVIGATES (leader isn't in-room; only 따라 may be sent)
    for _ in range(6):
        p = ev.Prompt(hp=400, mp=100, mv=200)
        sc.state.apply(p); sc.on_event(p)
        se.tick()
    assert not any(x in ("남", "북", "동", "서", "아래", "위") for x in ss), \
        f"supporter must not navigate in resupply: {ss}"
    # once the supporter reaches the anchor it switches to following
    for _ in range(4):
        e = ev.RoomSeen(title="중앙 광장", description="", exits=["남"], entities=[])
        sc.state.apply(e); sc.on_event(e)
        p = ev.Prompt(hp=400, mp=100, mv=200); sc.state.apply(p); sc.on_event(p)
        se.tick()
        if sc.cursor.sub == "sup_follow":
            break
    assert sc.cursor.sub == "sup_follow", sc.cursor
    # the leader's sync-switch back to hunting pulls the supporter out of follow
    d.request_switch("a", "hunting")
    assert lc.cursor.workflow == "hunting" and sc.cursor.workflow == "hunting"
    print("duo resupply: leader drives, supporter follows (no nav), sync ends together")


def test_no_item_attack_and_no_spam():
    Reloader(lambda: []).load_all()
    ctx, sent, eng = mk()
    eng.boot("hunting")
    for _ in range(5):
        _room(ctx, [], exits=["북"]); eng.tick()
        if ctx.cursor.sub == "clear_room":
            break
    # a ground item must NOT be attacked
    sent.clear()
    _room(ctx, ["훈련생바지가 떨어져 있습니다."], exits=["북"]); eng.tick()
    assert not any("공격" in c for c in sent), f"must not attack a ground item: {sent}"
    # a real mob -> attack once
    sent.clear()
    _room(ctx, ["파란 개구리가 돌아다니고 있습니다."], exits=["북"]); eng.tick()
    assert any("개구리 공격" in c for c in sent), sent
    # same mob still not engaged (no CombatHit) -> re-look, don't re-attack (anti-spam)
    sent.clear()
    _room(ctx, ["파란 개구리가 돌아다니고 있습니다."], exits=["북"]); eng.tick()
    assert not any("공격" in c for c in sent), f"anti-spam: re-look not re-attack: {sent}"
    print("no item-attack + anti-spam OK")


def test_combo_skill():
    """전사/검사/장군 at/above combo_min_level INITIATE with 연타 (not 공격), then
    RE-QUEUE it bare — but ONLY mid-fight when the wind-up cue set combo_ready, so it
    can never engage a 2nd enemy or mistime. Ineligible chars stay on plain 공격."""
    from behavior.hooks.hunting import hunt_step, _combo_enabled, _attack_verb
    Reloader(lambda: []).load_all()
    ctx, sent, eng = mk()                 # job 전사 (from mk's Status)
    cmd = Cmd(sent.append)
    ctx.state.level = 70                  # >= combo_min_level (60) -> eligible
    assert _combo_enabled(ctx) and _attack_verb(ctx) == "연타"
    # DUAL class ("전사 검사") — the real warrior's 점수 shows both; ANY token counts
    ctx.state.job = "전사 검사"
    assert _combo_enabled(ctx), "dual class 전사 검사 is eligible"
    ctx.state.job = "마법"; assert not _combo_enabled(ctx), "mage not eligible"
    ctx.state.job = "전사"                 # back to eligible for the rest

    # INITIATE: a mob present, not fighting -> "<mob> 연타" (the ONLY targeted send)
    for b in ctx.required_buffs():        # buffs already up -> hunt_step won't re-ask them
        ctx.buffs.confirm(b)
    sent.clear(); ctx.need_look = False
    ctx.state.entities = ["파란 개구리가 돌아다니고 있습니다."]
    hunt_step(ctx, cmd)
    assert sent == ["개구리 연타"], f"initiate with combo verb: {sent}"

    # MID-FIGHT + cue seen (combo_ready): the ALWAYS-ON `combo` reflex re-queues a
    # BARE 연타 and clears the flag. (Moved out of hunt_step so it fires in any mode.)
    from behavior.hooks.hunting import combo_continue, continue_combo
    sent.clear()
    ctx.state.in_battle = True; ctx.last_combat_at = now()   # fighting()
    ctx.combo_ready = True
    assert combo_continue(ctx), "combo_continue guard true mid-fight with the cue"
    continue_combo(ctx, cmd)
    assert sent == ["연타"], f"bare re-queue on cue: {sent}"
    assert not ctx.combo_ready, "combo_ready consumed after re-queue"

    # MID-FIGHT, no cue: guard false -> nothing (server drives the rounds; no stray 연타)
    assert not combo_continue(ctx), "no combo without the cue"
    # and hunt_step itself sends nothing while fighting
    sent.clear()
    hunt_step(ctx, cmd)
    assert sent == [], f"hunt_step silent while fighting: {sent}"

    # A kill clears a stale combo_ready so a bare 연타 can't leak onto the next enemy
    ctx.combo_ready = True
    ctx.on_event(ev.EnemyDown())
    assert not ctx.combo_ready, "kill clears combo_ready (no stray engage)"

    # INELIGIBLE (below level): plain 공격, and the cue is ignored
    sent.clear()
    ctx.state.in_battle = False; ctx.state.level = 50
    ctx.cursor.scratch.pop("atk_kw", None)          # clear anti-spam memory
    assert not _combo_enabled(ctx) and _attack_verb(ctx) == "공격"
    ctx.need_look = False
    ctx.state.entities = ["파란 개구리가 돌아다니고 있습니다."]
    hunt_step(ctx, cmd)
    assert sent == ["개구리 공격"], f"ineligible -> plain 공격: {sent}"
    print("combo 연타: initiate verb, bare cue re-queue, kill-clear, ineligible=공격")


def test_resupply_is_need_based_not_timed():
    """A cleared + recovered zone must NOT go to town just because time passed.
    Resupply is need-based: only hunger/thirst/bottle-dry/worn-gear sends us. This
    guards against the old timer loop (resupply<->hunt drained MV and stranded us)."""
    Reloader(lambda: []).load_all()
    ctx, sent, eng = mk()
    eng.boot("hunting")
    # clear a 1-cell dead-end -> wait -> recover (full) -> wake -> watch
    for _ in range(40):
        _room(ctx, [], exits=())
        _feed_jido(ctx, [])
        eng.tick()
        if ctx.cursor.sub == "watch":
            break
    assert ctx.cursor.sub == "watch", ctx.cursor

    # (1) Downtime alone (no real need) must NOT trigger resupply — it stays in the
    # watch/wait cycle and eventually re-sweeps (regen_elapsed -> hunting).
    ctx.state.hungry = ctx.state.thirsty = ctx.state.bottle_dry = False
    CLOCK[0] += 130
    for _ in range(4):
        _room(ctx, [], exits=())
        _feed_jido(ctx, [])
        eng.tick()
    assert ctx.cursor.workflow != "resupply", \
        f"downtime alone must NOT go to town: {ctx.cursor}"

    # (2) A real need (thirst) DOES trigger resupply, and it auto-returns to hunting.
    # Bounce back to watch first, then get thirsty.
    for _ in range(40):
        _room(ctx, [], exits=())
        _feed_jido(ctx, [])
        eng.tick()
        if ctx.cursor.sub == "watch":
            break
    ctx.state.thirsty = True
    for _ in range(6):
        _room(ctx, [], exits=())
        _feed_jido(ctx, [])
        eng.tick()
        if ctx.cursor.workflow == "resupply":
            break
    assert ctx.cursor.workflow == "resupply", f"thirst should send us to town: {ctx.cursor}"
    assert ctx.auto_cycle is True, "should auto-return to hunting after resupply"
    print("resupply is need-based: downtime stays local, thirst -> town")


def test_field_drink_thirst():
    """Thirsty in the field -> 버드 마셔 from the carried bottle (need-based, throttled),
    not a town trip. Never with a hostile present, never when the bottle is dry.
    Probes the survival reflex guard/action directly (deterministic)."""
    Reloader(lambda: []).load_all()
    ctx, sent, eng = mk()
    ctx.state.entities = []                                   # no hostiles, upright (default)
    ctx.state.thirsty = True
    CLOCK[0] += 5

    ok, err = eng.eval_guard("should_field_drink")
    assert ok and not err, f"thirst should arm the field-drink: {ok} {err}"
    eng.run_action("field_drink")
    assert "버드 마셔" in sent, f"field-drink sends the bottle command: {sent}"

    ok, _ = eng.eval_guard("should_field_drink")
    assert not ok, "must be throttled right after drinking"
    CLOCK[0] += 5
    ok, _ = eng.eval_guard("should_field_drink")
    assert ok, "re-arms after the throttle interval (still thirsty)"

    ctx.state.thirsty = False
    ok, _ = eng.eval_guard("should_field_drink")
    assert not ok, "no drink once the server quenches us"

    ctx.state.thirsty = True; ctx.state.bottle_dry = True; CLOCK[0] += 5
    ok, _ = eng.eval_guard("should_field_drink")
    assert not ok, "a dry bottle can't field-drink (needs a town refill)"

    ctx.state.bottle_dry = False; ctx.state.entities = ["뱀이 서 있습니다."]
    ok, _ = eng.eval_guard("should_field_drink")
    assert not ok, "never drink with a hostile present (defer past the fight)"

    ctx.state.entities = []; ctx.state.posture = "sleeping"
    ok, _ = eng.eval_guard("should_field_drink")
    assert not ok, "never drink while asleep (must be standing)"
    print("field-drink: thirst -> 버드 마셔, throttled, need-based, safe-gated")


def test_field_eat_hunger():
    """Hungry in the field -> 시루떡 먹어 from carried food (need-based, throttled),
    not a town trip. Only when we carry 시루떡, upright, no hostile present."""
    Reloader(lambda: []).load_all()
    ctx, sent, eng = mk()
    ctx.state.entities = []                                   # no hostiles, upright
    ctx.state.inventory = ["시루떡 (5)"]                      # we carry food
    ctx.state.hungry = True
    CLOCK[0] += 5

    ok, err = eng.eval_guard("should_field_eat")
    assert ok and not err, f"hunger + food should arm the field-eat: {ok} {err}"
    eng.run_action("field_eat")
    assert "시루떡 먹어" in sent, f"field-eat sends the eat command: {sent}"

    ok, _ = eng.eval_guard("should_field_eat")
    assert not ok, "must be throttled right after eating"
    CLOCK[0] += 5
    ok, _ = eng.eval_guard("should_field_eat")
    assert ok, "re-arms after the throttle interval (still hungry)"

    ctx.state.hungry = False
    ok, _ = eng.eval_guard("should_field_eat")
    assert not ok, "no eat once the server fills our belly"

    ctx.state.hungry = True; ctx.state.inventory = ["성수 (2)"]   # out of 시루떡
    CLOCK[0] += 5
    ok, _ = eng.eval_guard("should_field_eat")
    assert not ok, "no field-eat without carried food (falls through to resupply)"

    ctx.state.inventory = ["시루떡 (3)"]; ctx.state.entities = ["뱀이 서 있습니다."]
    ok, _ = eng.eval_guard("should_field_eat")
    assert not ok, "never eat with a hostile present"

    ctx.state.entities = []; ctx.state.posture = "sleeping"
    ok, _ = eng.eval_guard("should_field_eat")
    assert not ok, "never eat while asleep (must be standing)"
    print("field-eat: hunger -> 시루떡 먹어, throttled, food-gated, safe-gated")


def test_gridsweep_explores_before_barren():
    """Gridsweep zone (no fixed map): don't concede barren the instant a SMALL 지도 pocket is swept
    — keep moving until min_barren_moves rooms so adjacent mobs are found before dropping to `wait`.
    Only bites on small pockets; a big one passes the floor while sweeping."""
    from behavior.hooks.hunting import _is_barren
    Reloader(lambda: []).load_all()
    kn = get_knowledge()
    n = kn["hunt"]["min_barren_moves"]
    ctx = CharCtx("a", "leader", WorldState(), kn, set(), name="영웅", now=now)
    ctx.hunt_target = "카오스"                     # NOT a hunting_maps key -> gridsweep path
    class _SweptSw:                               # a fully-swept local pocket
        def knows_here(self): return True
        def swept(self, exits=None): return True
    ctx.gridsweep = _SweptSw()
    ctx.state.entities = []                       # no hostiles
    ctx.state.exits = ["북", "동"]                 # HAS exits -> exploring is possible
    ctx.barren_moves = n - 1
    assert _is_barren(ctx) is False, "swept small pocket, has exits, under the floor -> explore"
    ctx.barren_moves = n
    assert _is_barren(ctx) is True, "floor reached -> now barren"
    # a dead-end (no exits) has nowhere to explore -> barren at once, even under the floor
    ctx.state.exits = []
    ctx.barren_moves = 0
    assert _is_barren(ctx) is True, "no exits -> barren immediately (don't get stuck exploring)"
    print(f"gridsweep: walks up to {n} rooms before barren; dead-end barrens at once")


def test_arrival_resets_barren_so_it_sweeps():
    """The recurring idle-on-arrival bug: after a resupply round-trip the leader arrives with a
    STALE-high barren_moves (never reset). On the first tick gridsweep is None, so _is_barren skips
    the explore floor and hits `barren_moves >= cap` -> barren AT ONCE, dropping into `wait` without
    moving a room. reset_sweep (run on travel-arrive) must zero barren_moves so the sweep runs."""
    from behavior.hooks.hunting import _is_barren, reset_sweep
    Reloader(lambda: []).load_all()
    kn = get_knowledge()
    ctx = CharCtx("a", "leader", WorldState(), kn, set(), name="영웅", now=now)
    ctx.hunt_target = "카오스"                     # gridsweep zone (no fixed map)
    ctx.state.entities = []
    ctx.state.exits = ["북", "동", "남", "서"]
    ctx.barren_moves = 50                          # stale count carried from the last barren
    # BEFORE reset: gridsweep None + stale count -> _is_barren would fire immediately
    assert _is_barren(ctx) is True, "precondition: stale count makes it look barren on arrival"
    reset_sweep(ctx, Cmd(lambda c: None))          # what travel-arrive runs
    assert ctx.barren_moves == 0, "arrival must zero the empty-room counter"
    assert _is_barren(ctx) is False, "fresh arrival -> NOT barren -> it sweeps instead of waiting"
    print("arrival: reset_sweep zeroes barren_moves -> sweeps on arrival, no instant wait")


def test_hostile_resets_barren_moves():
    """`barren_moves` counts CONSECUTIVE empty rooms ('rooms since the last mob'): a mob appearing
    resets it, so the duo keeps sweeping as long as mobs turn up within the last few rooms and only
    concedes barren after min_barren_moves EMPTY rooms in a row (the user's spec)."""
    from behavior.hooks.hunting import _is_barren
    Reloader(lambda: []).load_all()
    kn = get_knowledge()
    ctx = CharCtx("a", "leader", WorldState(), kn, set(), name="영웅", now=now)
    ctx.hunt_target = "카오스"
    ctx.state.exits = ["북", "동"]
    ctx.barren_moves = kn["hunt"]["min_barren_moves"] + 3     # would be barren if left alone
    ctx.state.entities = ["쥐가 찍찍거리고 있다"]              # but a mob is right here
    assert _is_barren(ctx) is False, "a live mob -> not barren"
    assert ctx.barren_moves == 0, "a mob appearing resets the empty-room counter -> keep sweeping"
    print("mobs-keep-appearing: a hostile resets barren_moves -> sweep continues, no premature wait")


def test_sweep_never_pingpongs():
    """The one rule: the sweep must NEVER immediately reverse its last move while another exit
    exists — that's the 동-서-동-서 bounce between two rooms. 지도 next_dir can keep picking the cell
    we just came from in a pocket of look-alike 카오스 rooms; _sweep_move overrides a reversal to a
    non-reversing exit. A single-exit dead-end may reverse (nowhere else to go)."""
    from behavior.hooks import hunting as H
    Reloader(lambda: []).load_all()
    kn = get_knowledge()
    class _Sw:
        pending_dir = None
        def knows_here(self): return True
        def mark_visited(self): pass
        def note_move(self, d): self.noted = d
        def next_dir(self, exits): return self._nd
    def run(last_dir, nd, exits):
        ctx = CharCtx("a", "leader", WorldState(), kn, set(), name="영웅", now=now)
        ctx.hunt_target = "카오스"                          # gridsweep zone (no fixed map)
        sw = _Sw(); sw._nd = nd; ctx.gridsweep = sw
        ctx.state.exits = exits
        ctx.cursor.scratch["last_dir"] = last_dir
        ctx.cursor.scratch["jido_tries"] = 0
        sent = []
        H._sweep_move(ctx, Cmd(sent.append))
        return sent, ctx.cursor.scratch.get("last_dir")

    # next_dir wants to reverse (came 동, wants 서) + other exits -> step to a NON-reversing exit
    sent, ld = run("동", "서", ["북", "동", "남", "서"])
    assert sent and sent[-1] != "서", f"must not reverse into a pingpong; sent {sent}"
    assert sent[-1] == ld == "북", f"stepped forward to a non-reverse exit, got {sent}"
    # a non-reversing next_dir choice passes through untouched (legit exploration)
    sent, _ = run("동", "남", ["북", "동", "남", "서"])
    assert sent[-1] == "남", f"non-reverse choice must pass through, got {sent}"
    # the reverse IS allowed when it is the ONLY exit (dead-end corridor -> nowhere else)
    sent, _ = run("동", "서", ["서"])
    assert sent[-1] == "서", f"only exit is the reverse -> allowed, got {sent}"
    print("anti-pingpong: never reverses while another exit exists; single-exit reverse allowed")


def test_barren_resweeps_before_wait():
    """A fully-swept fixed map does NOT immediately concede barren -> it RE-SWEEPS
    `barren_resweeps` more passes first (catching mobs that respawned/wandered in during the
    pass), then barrens. A live hostile resets the counter so a producing zone keeps hunting."""
    from behavior.hooks.hunting import _is_barren
    from gumiho.fixedmap import FixedMap
    Reloader(lambda: []).load_all()
    kn = get_knowledge()
    n = kn["hunt"]["barren_resweeps"]
    ctx = CharCtx("a", "leader", WorldState(), kn, set(), name="영웅", now=now)
    ctx.hunt_target = "역사의 길"
    fm = FixedMap(kn["hunting_maps"]["역사의 길"])
    ctx.survey = fm
    ctx.state.entities = []                                  # no hostiles anywhere
    fm.swept = set(fm.coord)                                 # a full pass just completed

    for i in range(n):                                       # each empty pass -> re-sweep, not barren
        assert _is_barren(ctx) is False, f"pass {i}: must re-sweep, not barren yet"
        assert fm.swept == set(), "reset_swept re-opened the map for another pass"
        fm.swept = set(fm.coord)                             # simulate the empty re-sweep completing
    assert _is_barren(ctx) is True, f"after {n} empty re-sweep(s) -> truly barren"

    # a live hostile mid-sweep resets the counter (zone is producing -> keep hunting)
    fm._barren_resweeps = n
    ctx.state.entities = ["쥐가 찍찍거리고 있다"]              # an attackable mob is here
    assert _is_barren(ctx) is False, "a live hostile -> not barren"
    assert fm._barren_resweeps == 0, "the re-sweep counter reset on a live hostile"
    print(f"barren: re-sweeps {n}x before waiting; a live mob resets the count")


def test_gear_checked_before_rest_diverts_when_worn():
    """A worn weapon must be caught before it breaks (a 카타나 was lost overnight): entering rest
    re-reads 장비 first, and a fresh worn reading diverts to REPAIR instead of sleeping on it."""
    from behavior.hooks.hunting import gear_worn, pre_rest_ready
    Reloader(lambda: []).load_all()
    ctx, sent, eng = mk()
    ctx.state.apply(ev.Status(hp=(40, 400), mp=(100, 100), mv=(200, 200), job="전사"))  # low -> genuine rest
    eng.boot("rest")
    assert ctx.cursor.sub == "gear_check", ctx.cursor
    for _ in range(3):
        ctx.state.at_prompt = True; eng.tick()
        if "장비" in sent: break
    assert "장비" in sent, f"rest must re-read 장비 before sleeping: {sent}"
    assert ctx.equipment is None and not pre_rest_ready(ctx), "waits for the fresh read to land"
    ctx.on_event(ev.Equipment([{"slot": "무기", "name": "카타나", "cur": 20, "max": 100}]))  # 20% worn
    assert gear_worn(ctx) is True
    ctx.state.at_prompt = True; eng.tick()
    assert ctx.cursor.workflow == "resupply", f"worn gear -> repair, not sleep: {ctx.cursor}"
    print("pre-rest gear: 장비 re-read before rest; a worn weapon diverts to repair")


def test_gear_ok_before_rest_sleeps():
    from behavior.hooks.hunting import pre_rest_ready
    Reloader(lambda: []).load_all()
    ctx, sent, eng = mk()
    ctx.state.apply(ev.Status(hp=(40, 400), mp=(100, 100), mv=(200, 200), job="전사"))
    eng.boot("rest")
    for _ in range(3):
        ctx.state.at_prompt = True; eng.tick()
        if "장비" in sent: break
    ctx.on_event(ev.Equipment([{"slot": "무기", "name": "카타나", "cur": 95, "max": 100}]))  # healthy
    assert pre_rest_ready(ctx)
    for _ in range(3):
        ctx.state.at_prompt = True; eng.tick()
        if ctx.cursor.sub == "sleeping": break
    assert ctx.cursor.sub == "sleeping", ctx.cursor
    assert "자" in sent, f"healthy gear -> proceed to sleep: {sent}"
    print("pre-rest gear: healthy gear proceeds to sleep")


if __name__ == "__main__":
    test_barren_resweeps_before_wait()
    test_sweep_never_pingpongs()
    test_arrival_resets_barren_so_it_sweeps()
    test_hostile_resets_barren_moves()
    test_gridsweep_explores_before_barren()
    test_wait()
    test_rest_is_leader_driven()
    test_leader_rest_ends_on_hp_not_mp()
    test_wait_doze_only_sleeps_when_low()
    test_resupply_is_need_based_not_timed()
    test_field_drink_thirst()
    test_field_eat_hunger()
    test_no_item_attack_and_no_spam()
    test_combo_skill()
    test_resupply_trigger_and_return()
    test_manual_resupply_halts()
    test_hunt_target_parameter()
    test_pick_target_substring_alias()
    test_boot_resets_stale_activity()
    test_fixture_not_targeted()
    test_consider_gate()
    test_shopping_list()
    test_shop_remove_dispatch()
    test_pairing()
    test_rest_poll_and_wake()
    test_duo_resupply_roles()
    test_gear_checked_before_rest_diverts_when_worn()
    test_gear_ok_before_rest_sleeps()
    print("\nALL HUNT-CYCLE TESTS PASSED")
