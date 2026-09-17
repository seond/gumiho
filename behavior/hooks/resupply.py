"""Resupply: recall home, refill the bottle, repair worn gear, end at 치료실.

Demonstrates the two requested capabilities:
  1. real-time info collection — `장비` is issued, parsed, and the repair loop
     acts on which gear is actually worn (cur < max).
  2. combining behaviors — recall + navigate + refill + inspect + repair + navigate
     are chained as one workflow.

Navigation uses the graph mapper (s.directions_to), falling back to a hand-
recorded route macro in knowledge.toml [routes] when a destination isn't mapped.
"""

from gumiho.registry import action, guard, step

# Reverse a walked route (undo our own steps) — reliable even where the graph is
# incomplete: reverse the order and invert each direction.
_REV = {"북": "남", "남": "북", "동": "서", "서": "동", "위": "아래", "아래": "위"}


def _reverse(route):
    return [_REV[d] for d in reversed(route or []) if d in _REV]


# --- recall ------------------------------------------------------------------

@action("do_recall")
def do_recall(s, cmd):
    anchor = s.knowledge.get("recall", {}).get("anchor_room", "중앙 광장")
    if s.room_title and anchor in s.room_title:
        return                         # already AT the anchor — don't recall pointlessly
    if s.mv < s.knowledge.get("travel", {}).get("recall_min_mv", 30):
        return                         # too drained to recall — a rest state must regen first
    cmd.send("귀환")
    s.together = False                 # recall teleports — co-location unknown til re-seen
    s.cursor.scratch["tries"] = 1
    s.cursor.scratch["last_try"] = s.since_entered()


@guard("at_anchor")
def at_anchor(s):
    # "At the anchor" means physically at the anchor TOWN — a room-title fact only. It must
    # NOT fall back to `s.together` (co-location): after a stochastic 귀환 FAILS, both chars
    # are still co-located IN THE HUNT ZONE, so `together` flips back true on the next 봐 and
    # the recall state would advance to the fountain leg as if town had been reached (recall
    # "succeeded" while it hadn't). retry_recall/do_recall 봐 on each try to keep the title
    # current, so the title check flips true the instant the recall actually lands.
    anchor = s.knowledge.get("recall", {}).get("anchor_room", "중앙 광장")
    return bool(s.room_title and anchor in s.room_title)


@guard("recalled")
def recalled(s):
    # Recall is stochastic — don't assume it worked. Success = we can now reach
    # town (a route to the fountain exists from where we ended up).
    fountain = s.knowledge.get("places", {}).get("fountain", "광장 사거리")
    return s.directions_to(fountain) is not None


@step("retry_recall")
def retry_recall(s, cmd):
    """Re-cast 귀환 until it lands. Recall is stochastic ("귀환시도가 실패했습니다"),
    so NEVER give up — a dead-end halt can't be recovered, even by a manual recall
    afterward. Retry quickly at first, then back off to a slow interval so we keep
    trying indefinitely without hammering 귀환."""
    anchor = s.knowledge.get("recall", {}).get("anchor_room", "중앙 광장")
    if s.room_title and anchor in s.room_title:
        return                        # already at the anchor — nothing to recall (was the
                                      # source of the 47× "이동력이 없어서 귀환할 수 없습니다" spam)
    if s.mv < s.knowledge.get("travel", {}).get("recall_min_mv", 30):
        return                        # too drained to 귀환 — the cant_recall transition rests us
    rc = s.knowledge.get("recall", {})
    tries = s.cursor.scratch.get("tries", 1)
    interval = (rc.get("retry_interval", 3.0) if tries < rc.get("quick_tries", 6)
                else rc.get("slow_interval", 20.0))
    if s.since_entered() - s.cursor.scratch.get("last_try", 0.0) >= interval:
        s.cursor.scratch["tries"] = tries + 1
        s.cursor.scratch["last_try"] = s.since_entered()
        cmd.send("귀환")
        cmd.send("봐")                # refresh room_title so at_anchor flips true the instant
                                      # we land (a stale title otherwise routes us to reform
                                      # without ever recalling — part of the deadlock)


@guard("recall_exhausted")
def recall_exhausted(s):
    rc = s.knowledge.get("recall", {})
    interval = rc.get("retry_interval", 6.0)
    waited = (s.since_entered() - s.cursor.scratch.get("last_try", 0.0)) >= interval
    return s.cursor.scratch.get("tries", 0) >= rc.get("max_tries", 3) and waited


@action("fail_resupply")
def fail_resupply(s, cmd):
    s.last_error = "재보급 중단: 귀환이 계속 실패 (도달 불가)"
    s.request_stop = True


# --- navigation (shared step + guard; per-destination on_enter) --------------

def _setup_goto(s, place_key, prefer_macro=False):
    """Compute the leg's route at entry. prefer_macro uses the anchor-relative
    [routes] macro over graph pathfinding — for legs that start from a known
    anchor after a recall (the clinic), so a half-mapped room can't misroute us."""
    title = s.knowledge.get("places", {}).get(place_key, place_key)
    s.cursor.scratch["dest"] = title
    macro = s.knowledge.get("routes", {}).get(title)
    route = list(macro) if (prefer_macro and macro) else s.directions_to(title)
    s.cursor.scratch["route"] = list(route) if route else []
    if not s.cursor.scratch["route"]:
        s.last_error = f"경로 없음: {title} (지도 미등록 — 한 번 걸어가 보거나 [routes] 매크로 추가)"


@action("enter_goto_fountain")
def enter_goto_fountain(s, cmd):
    s.flow["trail"] = []                             # start the outbound trail here
    _setup_goto(s, "fountain", prefer_macro=True)    # fixed 남,남 — never graph-pathed


def _strip_prefix(route, prefix):
    """route with a leading `prefix` removed (if it starts with it)."""
    route, prefix = list(route or []), list(prefix or [])
    return route[len(prefix):] if route[:len(prefix)] == prefix else route


@action("enter_goto_smith")
def enter_goto_smith(s, cmd):
    # We're AT the fountain (광장 사거리). Both the fountain and smith macros are
    # recorded FROM 중앙 광장, and 중앙 광장 -> 대장간 runs THROUGH the fountain
    # (the smith macro starts with the fountain macro), so the fountain -> smith
    # leg is the smith macro with the fountain prefix stripped. The accumulated
    # trail (fountain + this leg) then equals the full 중앙 광장 -> 대장간 path, so
    # walk_back reverses it straight home.
    pl = s.knowledge.get("places", {})
    routes = s.knowledge.get("routes", {})
    fountain_r = routes.get(pl.get("fountain", "광장 사거리"), [])
    smith_r = routes.get(pl.get("smith", "대장간"), [])
    seg = _strip_prefix(smith_r, fountain_r)
    s.cursor.scratch["dest"] = pl.get("smith", "대장간")
    s.cursor.scratch["route"] = list(seg)
    if not seg:
        s.last_error = (f"대장간 경로 추론 실패 (fountain={fountain_r}, "
                        f"smith={smith_r})")


@action("enter_walk_back")
def enter_walk_back(s, cmd):
    # Return to the anchor by REVERSING the whole outbound trail — never navigate
    # out of the half-mapped 대장간, and no recall.
    s.cursor.scratch["dest"] = s.knowledge.get("recall", {}).get("anchor_room", "중앙 광장")
    s.cursor.scratch["route"] = _reverse(s.flow.get("trail"))


@action("enter_goto_clinic")
def enter_goto_clinic(s, cmd):
    _setup_goto(s, "clinic", prefer_macro=True)      # 동 from the anchor


@step("tag_along")
def tag_along(s, cmd):
    # Supporter's whole resupply: never navigate — just follow the leader (the
    # leader drives every move) and top up at the fountain when we land there.
    # The leader's sync-switch back to hunting pulls us out.
    if s.partner_name and not s.together:
        cmd.send(f"{s.partner_name} 따라")            # re-establish follow if dropped
        return
    sc = s.cursor.scratch
    # Keep co-location + room_title FRESH: a follower that just waits to be dragged never
    # re-renders, so a `together` that was true when the leader left goes stale-true and
    # strands us (the 중앙 광장 deadlock). A throttled 봐 refreshes both; if the leader isn't
    # actually here, `together` flips false and sup_follow's `separated` transition recalls us.
    if s.partner_name:
        _look = sc.get("_look_at", -1e9)
        if s.since_entered() - _look >= 4.0:
            sc["_look_at"] = s.since_entered()
            cmd.send("봐")
    r = s.knowledge.get("resupply", {})
    pl = s.knowledge.get("places", {})
    # One-shot self gear inspect: the leader's smith detour is decided on BOTH chars'
    # gear (see repair_needed), so make OUR durability known — the supporter's gear was
    # never repaired before, which slowly broke the party down.
    if not sc.get("_sup_geared"):
        sc["_sup_geared"] = True
        s.equipment = None
        cmd.send("장비")
        return
    fountain = pl.get("fountain", "광장 사거리")
    bakery = pl.get("bakery", "한성 떡집")
    smith = pl.get("smith", "대장간")
    if s.room_title and smith in s.room_title:
        _sup_repair(s, cmd)                           # repair OUR OWN worn gear at the smith
    elif s.room_title and fountain in s.room_title:
        if s.state.bottle_dry:
            cmd.send(r.get("refill_command", "버드 물 채워"))
        else:
            cmd.send(r.get("fountain_drink", "분수 마셔"))   # drink -> resolves thirst+hunger
    elif s.room_title and bakery in s.room_title:
        # Followed the leader into the bakery — stock our own 시루떡 too (for field_eat),
        # one per tick until we hit the target, then stop (re-checked via 소지품).
        target = r.get("provision_target", 5)
        if _siru_count(s) < target:
            item = r.get("provision_item", "시루떡")
            cmd.send(f"{item} {r.get('provision_buy_verb', '사')}")
            cmd.send("소지품")                             # refresh count so we stop at target


def _sup_repair(s, cmd):
    """Supporter repairs its OWN worn gear at 대장간 (one item per tick), the same way
    the leader's repair_step does — using the durability from its self-inspect above."""
    rp = s.knowledge.get("repair", {})
    sc = s.cursor.scratch
    worn = sc.get("_sup_worn")
    if worn is None:
        worn = sc["_sup_worn"] = [it["name"] for it in s.gear_needing_repair()]
        sc["_sup_ri"] = 0
    i = sc.get("_sup_ri", 0)
    if rp.get("mode", "each") == "all":
        if i == 0 and worn:
            cmd.send(rp.get("all_command", "수리"))
        sc["_sup_ri"] = 1
        return
    if i < len(worn):                                 # one '<item> 수리' per tick
        cmd.send(f"{s.alias(worn[i])} {rp.get('verb', '수리')}")
        sc["_sup_ri"] = i + 1


@step("drink_fountain")
def drink_fountain(s, cmd):
    # Drink from the fountain repeatedly — fills the belly (resolves hunger) as
    # well as thirst. Command is [resupply].fountain_drink ("물 마셔").
    r = s.knowledge.get("resupply", {})
    cmd.send(r.get("fountain_drink", "물 마셔"))
    s.cursor.scratch["drinks"] = s.cursor.scratch.get("drinks", 0) + 1


@guard("drank_enough")
def drank_enough(s):
    # Stop the moment the server says we're full (배가 불러… / 부릅니다 clear the
    # flags), else after a generous cap as a safety net.
    if not s.state.hungry and not s.state.thirsty and s.cursor.scratch.get("drinks", 0):
        return True
    n = s.knowledge.get("resupply", {}).get("fountain_drinks", 25)
    return s.cursor.scratch.get("drinks", 0) >= n


@guard("ready_to_walk")
def ready_to_walk(s):
    # Co-located AND both have enough move points for the whole round-trip — so a
    # walking leg can't strand us with exhausted MV.
    if s.partner_name and not s.together:
        return False
    m = s.knowledge.get("resupply", {}).get("min_mv", 100)
    if s.mv < m:
        return False
    return s.partner is None or s.partner.mv >= m


@step("goto_step")
def goto_step(s, cmd):
    if s.role != "leader":
        return                                        # only the leader navigates
    dest = s.cursor.scratch.get("dest")
    if dest and s.room_title and dest in s.room_title:
        return                                       # already there
    from behavior.hooks.common import walk_leg
    walk_leg(s, cmd, record_trail=True)              # confirm each step (no blind derail into 주막집)


@guard("arrived")
def arrived(s):
    dest = s.cursor.scratch.get("dest")
    if dest and s.room_title and dest in s.room_title:
        return True
    sc = s.cursor.scratch
    if sc.get("route") != []:                        # still steps to walk
        return False
    # route empty — but only 'arrived' once the FINAL step actually landed (not while it's a
    # refused move waiting to be retried, which would falsely end the leg at the wrong room).
    return not (sc.get("_leg_sent") and s.state.last_move_blocked)


# --- provisions: stock 시루떡 at 한성 떡집 (a side-branch off the fountain) --------
# 떡집 hangs off the fountain junction on a DIFFERENT branch than the smith, so it
# can't extend the linear outbound trail. It's a self-contained ROUND-TRIP from the
# fountain (out, buy, reverse back) that never touches s.flow["trail"] — so the
# existing fountain -> smith -> walk_back chain is unchanged.

def _siru_count(s):
    """How many 시루떡 we currently hold (stacked as '시루떡 (N)' in 소지품)."""
    import re
    item = s.knowledge.get("resupply", {}).get("provision_item", "시루떡")
    for it in s.state.inventory:
        if item in it:
            m = re.search(r"\((\d+)\)", it)
            return int(m.group(1)) if m else 1
    return 0


@action("inspect_bag")
def inspect_bag(s, cmd):
    cmd.send("소지품")                                 # 소 — refreshes s.state.inventory


def _provisions_seen(s):
    # The 소지품 reply lands within ~1s; gate the decision on that (same idiom as
    # `refilled`) so we don't branch on a stale inventory before the parse.
    return s.since_entered() > 1.2


@guard("have_provisions")
def have_provisions(s):
    target = s.knowledge.get("resupply", {}).get("provision_target", 5)
    return _provisions_seen(s) and _siru_count(s) >= target


@guard("need_provisions")
def need_provisions(s):
    target = s.knowledge.get("resupply", {}).get("provision_target", 5)
    return _provisions_seen(s) and _siru_count(s) < target


@action("enter_goto_bakery")
def enter_goto_bakery(s, cmd):
    # From the fountain (광장 사거리): the 떡집 leg is the anchor-relative 떡집 macro
    # with the fountain-macro prefix stripped (both recorded FROM 중앙 광장, and the
    # 떡집 path runs through the fountain). Remember the reverse for the way back.
    pl = s.knowledge.get("places", {})
    routes = s.knowledge.get("routes", {})
    fountain_r = routes.get(pl.get("fountain", "광장 사거리"), [])
    bakery_r = routes.get(pl.get("bakery", "한성 떡집"), [])
    seg = _strip_prefix(bakery_r, fountain_r)
    s.cursor.scratch["dest"] = pl.get("bakery", "한성 떡집")
    s.cursor.scratch["route"] = list(seg)
    s.flow["bakery_ret"] = _reverse(seg)              # fountain-return leg (round-trip)
    if not seg:
        s.last_error = (f"떡집 경로 추론 실패 (fountain={fountain_r}, bakery={bakery_r})")


@action("enter_bakery_back")
def enter_bakery_back(s, cmd):
    # Reverse the 떡집 leg to return to the fountain (never touched the main trail).
    s.cursor.scratch["dest"] = s.knowledge.get("places", {}).get("fountain", "광장 사거리")
    s.cursor.scratch["route"] = list(s.flow.get("bakery_ret") or [])


@step("shop_goto_step")
def shop_goto_step(s, cmd):
    # Like goto_step but for a round-trip shop leg: walk the route WITHOUT recording
    # it into s.flow["trail"] (the leg cancels itself out, so walk_back must not see it).
    if s.role != "leader":
        return
    dest = s.cursor.scratch.get("dest")
    if dest and s.room_title and dest in s.room_title:
        return
    from behavior.hooks.common import walk_leg
    walk_leg(s, cmd, record_trail=False)             # round-trip leg: confirm steps, don't log trail


@step("buy_provisions")
def buy_provisions(s, cmd):
    # Buy up to provision_target, one per tick. If the cargo-weight limit refuses a
    # buy, the server just ignores it — that's fine (user: overflow is OK), we don't
    # need to detect it. Count the shortfall ONCE (inventory only refreshes on 소).
    r = s.knowledge.get("resupply", {})
    if "to_buy" not in s.cursor.scratch:
        target = r.get("provision_target", 5)
        s.cursor.scratch["to_buy"] = max(0, target - _siru_count(s))
    if s.cursor.scratch["to_buy"] > 0:
        item = r.get("provision_item", "시루떡")
        cmd.send(f"{item} {r.get('provision_buy_verb', '사')}")   # object-first: '시루떡 사'
        s.cursor.scratch["to_buy"] -= 1
        if s.cursor.scratch["to_buy"] == 0:
            cmd.send("소지품")                        # refresh so field_eat sees the new count


@guard("bought_enough")
def bought_enough(s):
    return s.cursor.scratch.get("to_buy", 1) <= 0


# --- refill ------------------------------------------------------------------

@action("refill_bottle")
def refill_bottle(s, cmd):
    cmd.send(s.knowledge.get("resupply", {}).get("refill_command", "버드 물 채워"))


@guard("refilled")
def refilled(s):
    return s.since_entered() > 1.0


# --- inspect + repair (the real-time info-collection loop) --------------------

@action("inspect_gear")
def inspect_gear(s, cmd):
    s.equipment = None            # clear stale; the parse of 장비 repopulates it
    cmd.send("장비")


@guard("equipment_ready")
def equipment_ready(s):
    return s.equipment is not None


# Conditional branch after 장비: detour to 대장간 if EITHER character has worn gear.
# The supporter's gear counts too (it repairs its own there) — else its armour slowly
# breaks and the party can't stay strong. Its durability comes from the self-inspect
# in tag_along.
def _partner_worn(s):
    p = getattr(s, "partner", None)
    return p is not None and p.equipment is not None and len(p.gear_needing_repair()) > 0


@guard("repair_needed")
def repair_needed(s):
    return s.equipment is not None and (len(s.gear_needing_repair()) > 0 or _partner_worn(s))


@guard("no_repair_needed")
def no_repair_needed(s):
    return s.equipment is not None and not s.gear_needing_repair() and not _partner_worn(s)


@step("repair_step")
def repair_step(s, cmd):
    rp = s.knowledge.get("repair", {})
    if "names" not in s.cursor.scratch:              # snapshot the worn list once
        s.cursor.scratch["names"] = [it["name"] for it in s.gear_needing_repair()]
        s.cursor.scratch["ri"] = 0
    names = s.cursor.scratch["names"]
    i = s.cursor.scratch["ri"]
    if rp.get("mode", "each") == "all":
        if i == 0 and names:
            cmd.send(rp.get("all_command", "수리"))
        s.cursor.scratch["ri"] = 1
        return
    if i < len(names):                              # one item per tick
        cmd.send(f"{s.alias(names[i])} {rp.get('verb', '수리')}")   # parsed name -> registered
        s.cursor.scratch["ri"] = i + 1


@guard("repairs_done")
def repairs_done(s):
    names = s.cursor.scratch.get("names")
    if names is None:
        return False
    if s.knowledge.get("repair", {}).get("mode", "each") == "all":
        return s.cursor.scratch.get("ri", 0) >= 1 or not names
    return s.cursor.scratch.get("ri", 0) >= len(names)


# --- done --------------------------------------------------------------------

@action("halt")
def halt(s, cmd):
    s.request_stop = True


# --- hunt-cycle wiring -------------------------------------------------------

@guard("needs_resupply")
def needs_resupply(s):
    """Real NEED to leave and resupply — never a bare timer. Fires when:
      - EITHER character is hungry or thirsty (only the town fountain clears these), or
      - gear is worn to/below the repair threshold (lowest endurance <= gear_repair_pct).
    Both characters are inspected so the supporter's hunger/thirst counts too.
    NOT a dry bottle on its own: a dry bottle only matters once we're thirsty, and
    thirst already covers that — an empty bottle with no thirst is no reason for town."""
    for c in (s, s.partner):
        if c is None:
            continue
        if c.state.hungry or c.state.thirsty:
            return True
    pct = s.knowledge.get("resupply", {}).get("gear_repair_pct", 0.5)
    return s.gear_worn(pct)


@guard("resupply_due")
def resupply_due(s):
    """Zone cleared and fully recovered: use the downtime to resupply (refill +
    repairs) and return, so we come back to a regenerated zone topped-up instead
    of idling. Throttled by clear_interval so a fast-regen zone doesn't send us
    to town on every single cycle."""
    if s.state.bottle_dry:
        return True
    interval = s.knowledge.get("resupply", {}).get("clear_interval", 120)
    return s.since_resupply() > interval


@action("mark_cycle")
def mark_cycle(s, cmd):
    s.auto_cycle = True                 # hunting is driving -> resupply should return


@guard("auto_cycle")
def auto_cycle(s):
    return s.auto_cycle


@action("mark_resupplied")
def mark_resupplied(s, cmd):
    s.mark_resupplied()
    s.state.bottle_dry = False          # assume the refill cleared it
    s.equipment = None                  # gear just repaired; drop stale 장비 so the
    s.last_gear_at = s._now()           # gear_worn trigger can't re-fire immediately


@action("finish")
def finish(s, cmd):
    # Manual resupply run: end at 치료실. (The auto_cycle branch switches to travel
    # before this step ever runs.)
    if not s.auto_cycle:
        s.request_stop = True
