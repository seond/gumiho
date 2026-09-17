"""CharCtx — the `s` accessor that guards/steps read.

A thin, readable, *player-safe* view over one character's live WorldState plus
its workflow cursor, buff timers, knowledge, and partner sighting. All mutable
state lives here (data), never in behavior code, so a reload loses nothing.

Buff handling for M1 is request-timed: when the leader requests a buff we stamp
it and treat it active for an estimated duration. Precise expiry detection from
server text is a later-milestone refinement.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

from . import events as ev
from .parser import target_keyword
from .state import WorldState
from .ticktracker import TickTracker


def ironskin_predict(land: float, anchor: float, tick: float, base: float, nominal: float) -> float:
    """Predict the monotonic time 철면 will FADE for a cast that landed at `land`.

    철면 expires on the server's fixed `tick`-second grid (measured 37.5s, stdev 0.10s): the buff
    lasts ~`base` seconds, then fades at the first grid tick at/after (land + base). `anchor` is the
    monotonic time of a PREVIOUSLY observed fade — it pins the grid's phase (all fades are ≡ anchor
    mod tick). With a known anchor the prediction is exact (±~1s); before the first fade of a server
    session we fall back to `land + nominal` (a plain estimate) until a fade teaches us the phase."""
    if anchor <= 0 or tick <= 0:
        return land + nominal
    k = math.ceil((land + base - anchor) / tick)
    return anchor + tick * k

# Entities that are never hunt targets regardless of keyword: corpses/blood, and
# GROUND ITEMS (놓여/떨어져) and aura-wrapped shop display items (오로라). The item
# markers MUST match hunting's _ITEM_MARKERS — otherwise hostiles() counts a
# dropped 동전 as a "mob", so area_barren never fires and the hunt idles forever
# while nothing gets attacked.
_NON_MOB_MARKERS = ("시체", "핏자국", "송장", "놓여", "떨어져", "오로라")


def resolve_alias(aliases: dict, entity: str) -> tuple:
    """First [aliases] entry whose KEY appears as a SUBSTRING of `entity`, resolved
    to (name, kind). This lets a human take the whole row and declare BOTH the name
    and the KIND (mob vs item …) when auto-detection can't tell them apart. Values:
        "카드"                   -> (name="카드", kind="mob")   # keyword only
        ""                       -> (name="",    kind="mob")   # empty name = skip it
        {name="X", kind="item"}  -> (name="X",   kind="item")  # explicit name + kind
        {kind="item"}            -> (name=None,  kind="item")  # kind only (keep name)
    Returns (None, None) when nothing matches. Kinds: mob | item | readable | ambient
    (ambient = ignore). First matching KEY wins, so use SPECIFIC substrings."""
    for key, val in aliases.items():
        if not key or key not in entity:
            continue
        if isinstance(val, dict):
            return (val.get("name") or val.get("kw")), (val.get("kind") or "mob")
        return val, "mob"
    return None, None


class BuffTimer:
    """Request-timed buff tracking. active(name) is true for est. duration after
    the last request; durations are seeded from knowledge and can be learned."""

    def __init__(self, durations: dict[str, float], now: Callable[[], float]):
        self._durations = dict(durations)          # name -> seconds
        self._requested: dict[str, float] = {}     # name -> last ASK ts (re-ask throttle)
        self._confirmed: dict[str, float] = {}     # name -> last LANDED ts (真 active)
        self._now = now
        self.default = 180.0                        # unknown buff: assume 3 min

    def duration(self, name: str) -> float:
        return self._durations.get(name, self.default)

    def requested(self, name: str) -> None:
        """Record that we just ASKED for the buff — throttles re-asks; does NOT mean
        it's active (the old bug: assuming a spoken request always succeeded, so a
        request dropped while the supporter slept looked 'active' for the full 240s)."""
        self._requested[name] = self._now()

    def confirm(self, name: str) -> None:
        """The buff actually LANDED (parsed from the game) — this is what makes it
        active. Requested-but-unconfirmed buffs stay inactive so we keep re-asking."""
        self._confirmed[name] = self._now()

    def expire(self, name: str) -> None:
        """The buff FADED (the server's expiry message) — clear it so the leader
        re-asks immediately, without waiting out the duration estimate."""
        self._confirmed.pop(name, None)
        self._requested.pop(name, None)          # drop the re-ask throttle -> ask now

    def active(self, name: str) -> bool:
        ts = self._confirmed.get(name)             # CONFIRMED landing, never mere request
        if ts is None:
            return False
        # Treat as lapsed a little early so a re-cast overlaps the old one.
        margin = min(15.0, 0.15 * self.duration(name))
        return (self._now() - ts) < (self.duration(name) - margin)

    def should_reask(self, name: str, interval: float) -> bool:
        """Ask again if the buff isn't confirmed-active and we haven't asked within
        `interval` — so a missed request self-heals instead of stalling for 240s."""
        if self.active(name):
            return False
        last = self._requested.get(name)
        return last is None or (self._now() - last) >= interval

    def clear(self) -> None:
        self._requested.clear()
        self._confirmed.clear()


@dataclass
class Cursor:
    """Where the character is in its workflow. Pure data — survives reloads."""
    workflow: str
    sub: str
    entered_at: float = 0.0
    scratch: dict = field(default_factory=dict)


class CharCtx:
    def __init__(
        self,
        sid: str,
        role: str,                       # "leader" | "supporter"
        state: WorldState,
        knowledge: dict,
        known_players: set[str],
        *,
        name: str | None = None,
        now: Callable[[], float] = time.monotonic,
    ):
        self.sid = sid
        self.role = role
        self.state = state
        self.knowledge = knowledge
        self.known_players = known_players
        self.name = name                 # this character's in-game name
        self.partner_name: str | None = None
        self.partner = None              # the leader's FIRST supporter / a supporter's leader (compat)
        self.in_group = True             # supporter: include in the XP 그룹? (operator checkbox).
                                         # form_party groups everyone (모두 그룹) then toggles OUT the
                                         # excluded ones with "<name> 그룹". Default included.
        self.partners: list = []         # the LEADER's supporters (all of them); empty for a supporter.
                                         # Number-agnostic group ops (rest, together, 철면) iterate this;
                                         # a supporter still tracks only the leader via partner/partner_name.
        self._now = now
        self.cursor = Cursor(workflow="", sub="", entered_at=now())
        self.buffs = BuffTimer(knowledge.get("buff_durations", {}), now)
        self.pending_spell: str | None = None   # supporter: a heard "<spell>!" request
        self.pending_chase: str | None = None   # supporter: direction the leader FLED (철면사냥
                                                # regroup) — set from the flee-departure line,
                                                # cleared on reunion; drives chase_partner
        self.flee_failed: bool = False          # leader: last 도망 FAILED ("도망치지 못했습니다") —
                                                # 도망 is stochastic, so retry at once (not after the
                                                # flee grace). Set in webui on the fail line; cleared
                                                # on each new 도망 attempt and when 철면 lands.
        self.recall_fail_at: float = 0.0        # monotonic time of the last "귀환 시도가 실패했습니다".
                                                # BLIND recall can't confirm by room title (blindness
                                                # suppresses rendering), so it confirms by the ABSENCE
                                                # of this line after a 귀환. Set in webui.
        self.last_error: str | None = None       # surfaced to UI on hook failure
        self.equipment: list[dict] | None = None  # last parsed 장비 (None until seen)
        self.shopping_list: list[str] = []        # perishables that rotted away and need
                                                  # re-buying; cleared once re-equipped (장비)
        self.request_stop = False                 # an action can ask the engine to halt
        self._offline_recovery_at = 0.0           # monotonic when the current offline recovery began
                                                  # (grace clock; a stuck one is abandoned after it)
        self.offline_recovery = False             # True while a DEATH/BLIND recovery is being driven
                                                  # with the engine toggled OFF (manual play). Makes
                                                  # the recovery HALT — not resume travel/hunt — when
                                                  # it completes; see Engine._offline_tick.
        self.flow: dict = {}                       # workflow-scoped scratch (survives
                                                   # state changes; reset on workflow entry)
        self.together = False                       # co-located with partner (sighting-
                                                    # based; set on sight, cleared on
                                                    # the partner actually leaving)
        self.assist_on = True                       # 자동지원 is ON by default every login;
                                                     # SERVER-TRUTH thereafter (가능/불가능
                                                     # messages). Starts True so form_party
                                                     # toggles it OFF once on a fresh connect
                                                     # (the supporter must not join combat).
        self.following = False                       # 따라 active (supporter carried by the
                                                     # leader) — SERVER-TRUTH from the 따라
                                                     # start/stop messages; lets travel tell a
                                                     # carried supporter NOT to also walk its
                                                     # own steps (that double-moves it off-route)
        self.need_look = False                       # combat changed the room; 봐 to refresh
        self.combo_ready = False                     # 연타 wind-up cue seen -> re-queue the combo
        self.knocked_down = False                    # a power-bash floored us -> stand up (일어나)
        self.considering = None                      # mob keyword we're 고려-assessing (or None)
        self.consider_at = now()                     # when the 고려 was sent (timeout for 'safe')
        self.consider_danger = False                 # a 고려 danger phrase was seen for it
        self.last_combat_at = now()                 # last combat line (battle freshness)
        self.last_prompt_at = now()                 # last real prompt (quiet detection)
        self.last_kill_at = now()                  # for area-barren (mob regen) detection
        self.barren_moves = 0                       # rooms advanced since the last kill
        self.last_resupply_at = now()               # for the periodic resupply trigger
        self.last_gear_at = now()                    # last 장비 durability check
        self.last_together_at = now()                # last time the partner was seen here
        self.auto_cycle = False                     # True while the hunt cycle drives
                                                    # (resupply then returns; else halts)
        self.hunt_target: str | None = None         # destination/zone parameter for
                                                    # THIS hunt run (travel goes here,
                                                    # coverage confines here)
        self.circuit: list[str] = []                # hunting CIRCUIT: zones to rotate through
        self.circuit_idx: int = 0                   # once one is swept barren, advance to the
                                                    # next (the swept one respawns while we hunt
                                                    # the other). Empty/one -> single-zone behavior.
        self.blacklist: list[str] = []              # PER-ZONE mob names we never attack (nasty to
                                                    # recover from, e.g. 마왕's blind). Loaded from
                                                    # the hunting map by reset_sweep on arrival.
        self.ironskin_zone: bool = False            # 철면사냥: this zone's mobs need the 철면
                                                    # (physical-immunity) buff to fight. Loaded
                                                    # from the map by reset_sweep on arrival.
        self.ironskin_at: float = 0.0               # monotonic when 철면 LANDED on us (>0 = up);
                                                    # cleared to 0 on the fade line. NEVER re-cast
                                                    # while up (re-halves HP without extending).
        self.ironskin_faded_at: float = 0.0         # monotonic of the last 철면 FADE — the emergency
                                                    # flee holds fade_flee_hold secs after it before 도망
        self._iron_req_at: float = -1e9             # monotonic of the last 철면 REQUEST (throttle)
        self._iron_req_pending = False              # a 철면 request is IN FLIGHT (asked, not yet
                                                    # landed) — blocks a re-ask until the land event
                                                    # clears it; the double-cast (HP→1) guard.
        _tk = self.knowledge.get("tick", {})
        self.tick = TickTracker(period=_tk.get("period", 74.5), tol=_tk.get("tol", 3.0),
                                min_confirm=_tk.get("min_confirm", 2))  # master ~74.5s tick layer;
                                                    # synced from 분노/빨리 fade boundaries. 철면 rides
                                                    # its half-grid (see ironskin prediction in webui).
        self.ironskin_grid_anchor: float = 0.0      # monotonic of the LAST observed 철면 fade — the
                                                    # server's 37.5s expiry grid anchor. FALLBACK for
                                                    # 철면 prediction before the master tick is synced.
        self.ironskin_predicted_fade: float = 0.0   # monotonic predicted fade of the CURRENT cast
                                                    # (set at land time from the learned grid).
        self.map_grid = None                        # latest parsed 지도 (MapGrid)
        self.gridsweep = None                       # temporary in-zone 지도 map (per hunt)
        self.survey = None                          # FixedMap for the current hunt zone (a
                                                    # read-only, pre-authored layout); None
                                                    # unless hunting a zone that has a map
        self.director = None                        # shared duo coordinator (lockstep travel)
        # Navigation fallback for resupply legs: (dest_title -> [directions] | None).
        # Unused now (town legs use hand-recorded [routes] macros); kept as a hook.
        self.nav = None

    def reset_activity(self) -> None:
        """Wipe the transient activity accumulators so a (re)booted run starts truly
        FRESH — stale buff timers, 자동지원 state, gear reading, co-location and
        timing left over from a previous run don't leak in. Called by Engine.boot (a
        deliberate (re)start); NOT on a workflow switch, which is a continuation."""
        now = self._now()
        self.buffs.clear()          # buffs must be re-cast for the new run
        # NOTE: assist_on and following are SERVER-TRUTH flags (driven by the server's
        # 자동지원 and 따라 messages) — deliberately NOT reset here. A mid-session
        # (re)boot (사냥 시작 / engine toggle) keeps the SAME connection, so the server
        # still has 자동지원/따라 in effect; forcing the flags False would re-send
        # 자동지원 (toggling it OFF) and make the carried supporter walk its own route
        # (double-move). A genuinely fresh start is a NEW connection with a NEW ctx
        # (both flags default False in __init__), so re-enabling still happens there.
        self.equipment = None       # re-inspect gear before trusting durability
        self.pending_spell = None
        self.pending_chase = None
        self.flee_failed = False
        self.request_stop = False
        self.offline_recovery = False   # a deliberate boot supersedes any offline recovery
        self._iron_req_pending = False   # no 철면 ask is in flight across a fresh (re)boot
        self.last_error = None
        self.need_look = False
        self.combo_ready = False
        self.knocked_down = False
        self.considering = None
        self.consider_danger = False
        self.together = False
        self.barren_moves = 0
        self.last_kill_at = now
        self.last_resupply_at = now
        self.last_gear_at = now
        self.last_combat_at = now
        self.last_prompt_at = now
        self.last_together_at = now

    def alias(self, name: str | None) -> str | None:
        """Map a PARSED name to the name the server actually registers for commands,
        when they differ. The parser reads the full DISPLAYED name (e.g. the 장비
        line '기간테스의바지'), but some are registered under a shorter/other keyword
        ('기간'); the human records the mapping in knowledge [aliases]. ALWAYS run a
        parsed name through this before putting it in a command (repair '<n> 수리',
        attack '<n> 공격', pickup …). Unmapped names pass through unchanged."""
        if not name:
            return name
        val = self.knowledge.get("aliases", {}).get(name, name)
        return val if isinstance(val, str) else name   # table-form entries: exact lookup is name-only

    def entity_alias(self, entity: str) -> tuple:
        """(name, kind) declared for this entity row in [aliases] by substring, or
        (None, None). See resolve_alias — lets a human classify an ambiguous line."""
        return resolve_alias(self.knowledge.get("aliases", {}), entity)

    # --- vitals -------------------------------------------------------------
    @property
    def hp(self) -> int: return self.state.vitals.hp
    @property
    def mp(self) -> int: return self.state.vitals.mp
    @property
    def mv(self) -> int: return self.state.vitals.mv

    def _pct(self, cur: int, mx: int | None) -> float:
        return 100.0 * cur / mx if mx else 100.0

    def hp_pct(self) -> float: return self._pct(self.hp, self.state.vitals.hp_max)
    def mp_pct(self) -> float: return self._pct(self.mp, self.state.vitals.mp_max)
    def mv_pct(self) -> float: return self._pct(self.mv, self.state.vitals.mv_max)

    def ironskin_up(self) -> bool:
        """Is 철면 (physical immunity) currently ON us? True from the landing line until the
        fade line clears it. The duration BACKSTOP is set LONGER than the real ~43s so it never
        pre-empts the real fade (re-casting early re-halves HP without extending — forbidden)."""
        if self.ironskin_at <= 0:
            return False
        backstop = self.knowledge.get("ironskin", {}).get("duration_backstop", 55.0)
        return (self._now() - self.ironskin_at) < backstop

    # --- posture / battle ---------------------------------------------------
    def asleep(self) -> bool: return self.state.posture == "sleeping"
    def standing(self) -> bool: return self.state.posture == "standing"
    def in_battle(self) -> bool: return self.state.in_battle
    def since_combat(self) -> float: return self._now() - self.last_combat_at
    def fighting(self) -> bool:
        # A live battle round: in battle AND a combat line landed recently. The
        # freshness guard releases us if a battle-end event was somehow missed.
        return self.state.in_battle and self.since_combat() < 3.0
    def dead(self) -> bool: return self.state.dead
    def hungry(self) -> bool: return self.state.hungry
    def thirsty(self) -> bool: return self.state.thirsty

    # --- room ---------------------------------------------------------------
    @property
    def room_title(self) -> str | None: return self.state.room_title
    @property
    def zone(self) -> str | None: return self.state.zone
    @property
    def exits(self) -> list[str]: return list(self.state.exits)

    @property
    def group(self) -> list:
        """The LEADER's supporters as a list. Falls back to the single `partner` when the
        `partners` list wasn't populated (a one-supporter setup, or a test that only set
        `partner`), so number-agnostic group ops work whether one or many are wired."""
        if self.partners:
            return self.partners
        if self.role == "leader" and self.partner is not None:
            return [self.partner]
        return []

    def _member_names(self) -> list[str]:
        """Names this character must stay co-located with: the LEADER tracks EVERY supporter;
        a supporter tracks the leader. The basis for group togetherness (any supporter count).
        Leader prefers the live supporter ctxs; falls back to `partner_name` when none are wired
        yet (a leader whose supporter hasn't connected, or a test/solo naming a partner)."""
        if self.role == "leader":
            names = [p.name for p in self.group if p.name]
            return names or ([self.partner_name] if self.partner_name else [])
        return [self.partner_name] if self.partner_name else []

    def has_group(self) -> bool:
        """Is there anyone to group with? (leader: ≥1 supporter; supporter: a leader.)"""
        return bool(self._member_names())

    def present_partners(self) -> list:
        """The LEADER's supporters currently in the room — for 'any supporter can cast'
        decisions (철면) that must not stall on ONE straggler being absent. Group-`together`
        means the last render had EVERYONE present (fast path); otherwise fall back to
        per-name sighting so we still find whoever IS here when someone has drifted off."""
        if self.together:
            return list(self.group)
        ents = self.state.entities
        return [p for p in self.group if p.name and any(p.name in e for e in ents)]

    def _is_own(self, kw: str | None) -> bool:
        return kw is not None and (kw == self.name or kw in self._member_names()
                                   or kw == self.partner_name)

    def _is_player(self, entity: str) -> bool:
        kw = target_keyword(entity)
        if kw is None:
            return False
        return kw in self.known_players or kw.rstrip("님") in self.known_players

    def hostiles(self) -> list[str]:
        """Entities that are safe to attack: not players (safety), not our own
        party, not corpses/blood/items."""
        out = []
        for e in self.state.entities:
            name, kind = self.entity_alias(e)
            if kind is not None:                 # a human alias claims this row
                if kind == "mob" and name:       # explicitly an attackable mob
                    out.append(e)
                continue                          # item / skip / empty-name -> not a hostile
            if any(m in e for m in _NON_MOB_MARKERS):
                continue
            if self._is_player(e):
                continue
            kw = target_keyword(e)
            if self._is_own(kw):
                continue
            out.append(e)
        return out

    def next_target(self) -> str | None:
        """Keyword of the next mob to attack, or None. NEVER a known player."""
        h = self.hostiles()
        if not h:
            return None
        return target_keyword(h[0])

    def partner_seen(self) -> bool:
        """Sighting-based co-location: is the partner in this room right now?"""
        if not self.partner_name:
            return False
        return any(self.partner_name in e for e in self.state.entities)

    # --- knowledge ----------------------------------------------------------
    def required_buffs(self) -> list[str]:
        # Match EACH token of the job — a dual class parses as "전사 검사", which is
        # not a table key, so a whole-string lookup returned [] and the leader never
        # asked for 방비 (same dual-class trap that once broke 연타). Union the buffs
        # of every matching component; fall back to whole-string then default.
        job = self.state.job or ""
        table = self.knowledge.get("required_buffs", {})
        out: list[str] = []
        for tok in job.split():
            for b in table.get(tok, []):
                if b not in out:
                    out.append(b)
        if out:
            return out
        return list(table.get(job) or table.get("default", []))

    def potion_count(self, kind: str) -> int:
        """Approximate: inventory items whose name matches a known potion of kind."""
        items = self.knowledge.get("potions", {}).get(kind, [])
        return sum(1 for inv in self.state.inventory if any(p in inv for p in items))

    def gear_needing_repair(self) -> list[dict]:
        """Equipped items whose durability is below max. Empty if 장비 unseen."""
        return [it for it in (self.equipment or [])
                if it.get("cur") is not None and it["cur"] < it["max"]]

    def since_gear(self) -> float:
        return self._now() - self.last_gear_at

    def gear_worn(self, pct: float) -> bool:
        """Any equipped item worn to <= pct of its max durability — repair before
        it breaks. Uses the last parsed 장비 (None -> False)."""
        for it in (self.equipment or []):
            cur, mx = it.get("cur"), it.get("max")
            if cur is not None and mx and cur / mx <= pct:
                return True
        return False

    def directions_to(self, title: str) -> list[str] | None:
        """A route (list of directions) to a room whose title contains `title`,
        via the graph mapper, else a hand-recorded route macro from knowledge."""
        if self.nav is not None:
            route = self.nav(title)
            if route:
                return list(route)
        macro = self.knowledge.get("routes", {}).get(title)
        return list(macro) if macro else None

    # --- misc ---------------------------------------------------------------
    def since_entered(self) -> float:
        return self._now() - self.cursor.entered_at

    def hunt_zone(self) -> str | None:
        """The zone to hunt/confine to: the per-run target if set, else the
        configured default. This is the 'destination parameter' of the hunt.

        CANONICALIZES to the exact hunting-map key when the target matches one ignoring
        whitespace — so "역사의길" resolves to the map key "역사의 길". This is the single
        resolution point every downstream lookup goes through (map membership, FixedMap
        retrieval in reset_sweep, zone_confine, travel routing), so a one-space difference
        no longer silently falls back to the gridsweep — which has no FixedMap and therefore
        loses the flee map-sync (note_flee), the per-zone blacklist, and the 철면 flag."""
        z = self.hunt_target or self.knowledge.get("hunt", {}).get("zone")
        if not z:
            return z
        maps = self.knowledge.get("hunting_maps", {})
        if z in maps:
            return z
        norm = "".join(z.split())
        for k in maps:
            if "".join(k.split()) == norm:
                return k
        return z

    def since_kill(self) -> float:
        return self._now() - self.last_kill_at

    def since_together(self) -> float:
        return self._now() - self.last_together_at

    def since_resupply(self) -> float:
        return self._now() - self.last_resupply_at

    def mark_resupplied(self) -> None:
        self.last_resupply_at = self._now()

    def _reconcile_shopping(self) -> None:
        """Drop shopping-list items that now appear in the equipped (장비) list — the
        rot name matches the equipped name (e.g. '고블린 머드 장화'). Substring both
        ways so an enchant-suffixed equipped name still clears its base entry."""
        worn = [it.get("name", "") for it in (self.equipment or [])]
        self.shopping_list = [
            want for want in self.shopping_list
            if not any(want == w or want in w or w in want for w in worn if w)
        ]

    def on_event(self, event: ev.Event) -> None:
        """Capture ctx-only signals from the event stream (buffs handled by
        actions; here we catch the supporter's heard spell request and the
        parsed equipment list)."""
        if isinstance(event, ev.Speech):
            self._maybe_spell_request(event)
        elif isinstance(event, ev.Equipment):
            self.equipment = event.items
            self._reconcile_shopping()             # drop anything now re-equipped
        elif isinstance(event, ev.ItemRotted):
            # A perishable decayed away -> remember to re-buy it (kept until 장비
            # shows it equipped again). De-duped, name as the server spells it.
            if event.name and event.name not in self.shopping_list:
                self.shopping_list.append(event.name)
        elif isinstance(event, ev.Prompt):
            self.last_prompt_at = self._now()
        elif isinstance(event, ev.CombatHit):
            self.last_combat_at = self._now()
        elif isinstance(event, (ev.ExpGain, ev.EnemyDown)):
            self.last_kill_at = self._now()        # a kill: area not barren
            self.barren_moves = 0
            self.need_look = True                   # combat changed the room -> re-look
            self.combo_ready = False                # fight over -> never re-queue 연타 now
        # Sighting-based co-location (never coordinate ids). `together` is GROUP-wide: for the
        # LEADER it means EVERY supporter is in the room; for a supporter, the leader is. Reflects
        # the last fresh render (RoomSeen / arrive / leave); last_together_at feeds a debounced
        # "separated" so follow-render lag (a brief absence right after a move) doesn't trip a
        # false regroup. Number-agnostic via _member_names().
        names = self._member_names()
        if names:
            if isinstance(event, ev.RoomSeen):
                self.together = all(any(n in e for e in event.entities) for n in names)
                if self.together:
                    self.last_together_at = self._now()
                    self.pending_chase = None      # reunited -> drop any 철면사냥 chase
            elif isinstance(event, ev.EntityArrived) and any(n in event.text for n in names):
                # One arrival completes the group only when it IS the whole group (a single
                # member). With several supporters the leader confirms the full set on the next
                # RoomSeen (its waiting states 봐 regularly), so don't claim togetherness early.
                if len(names) == 1:
                    self.together = True
                    self.last_together_at = self._now()
                    self.pending_chase = None      # reunited -> drop any 철면사냥 chase
            elif isinstance(event, ev.EntityLeft) and any(n in event.text for n in names):
                self.together = False

    def _maybe_spell_request(self, sp: ev.Speech) -> None:
        # Supporter only, and only the leader's voice. "<spell>!" is the request;
        # the trailing punctuation is the discriminator (not a spell whitelist).
        if self.role != "supporter" or not self.partner_name:
            return
        if self.partner_name not in sp.speaker:
            return
        text = sp.text.strip()
        if not text.endswith(("!", "！")):
            return
        spell = text.rstrip("!！").strip()
        if spell and " " not in spell and len(spell) <= 12:
            self.pending_spell = spell
