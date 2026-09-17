"""Local web GUI: an aiohttp server embedded in the client process.

One shared GameSession per character (the server allows two per IP — the
leader + supporter duo), any number of browser tabs. The browser gets the
decoded stream, structured state snapshots, and comm events over a WebSocket;
it sends commands and connect/quit/disconnect controls back.

The characters are driven entirely by the structured ENGINE (engine.py over
behavior/*). The old LLM planner, ad-hoc walker, persistent graph mapper, and
duo keeper have been removed; player-safety (누구 roster) and 보험 are kept up by
a small safety keeper here, and in-zone navigation is the engine's own 지도
gridsweep plus hand-recorded routes.
"""

import asyncio
import json
import math
import re
import socket
import time
import webbrowser
from collections import deque
from pathlib import Path

from aiohttp import WSMsgType, web

from . import events as ev
from . import registry
from .access import CharCtx, ironskin_predict, resolve_alias
from .client import MudConnection
from .command import Cmd
from .config import ROOT, load_config, load_env
from .director import Director
from .engine import Engine
from .expect import TextWatcher
from .login import LoginError, auto_login
from .notify import Notifier
from .pacing import PacedSender
from .parser import StreamParser, target_keyword
from .reload import Reloader, get_knowledge
from .session_log import SessionLogger
from .state import WorldState

STATIC = Path(__file__).parent / "static"
SERVER_BOOT = str(int(time.time()))   # stale-page detection token


def lan_ip() -> str:
    """This machine's LAN address (so other computers in the house can reach the UI).
    Opens a throwaway UDP socket toward a public IP to see which local interface would
    route out — no packet is actually sent. Falls back to localhost if offline."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"

# Bare directional commands — used to attribute an in-flight move so the sweep
# can confirm it (a room render must follow, or it was refused).
DIRECTIONS = {"북", "남", "동", "서", "위", "아래"}

_ITEM_RE = re.compile(r"떨어져\s*있|놓여\s*있")
_SCENERY = {"핏자국"}  # decorations that read like entities but are neither

# Our own characters + other connected players: never mobs, never attack targets.
KNOWN_PLAYERS: set[str] = set()
_TAKE_HINT_RE = re.compile(r"\(\s*(\S+)\s+가져\s*\)")
_LOOK_HINT_RE = re.compile(r"<\s*(\S+)\s+봐\s*>")

# Route recording: capture movement/door commands, skip look/status/combat/speech.
_REC_SKIP_EXACT = {"봐", "지도", "장비", "점수", "누구", "인벤", "소지품", "상태",
                   "보험", "쉬어", "자", "깨", "일", "일어나", "도망", "끝", "귀환"}
_REC_SKIP_SUFFIX = (" 말", " 공격", " 고려", " 복용", " 마셔", " 걸어")


def _route_worthy(line: str) -> bool:
    line = line.strip()
    if not line or line in _REC_SKIP_EXACT:
        return False
    return not any(line.endswith(suf) for suf in _REC_SKIP_SUFFIX)


def entity_info(entity: str) -> dict:
    """Classify a room-entity line for the UI: creature, ground item,
    readable object, or plain ambience."""
    if entity.startswith("["):
        return {"text": entity, "kind": "ambient", "kw": None}
    m = _LOOK_HINT_RE.search(entity)
    if m:
        return {"text": entity, "kind": "readable", "kw": m.group(1)}
    if _ITEM_RE.search(entity):
        m = _TAKE_HINT_RE.search(entity)
        kw = m.group(1) if m else target_keyword(entity)
        return {"text": entity, "kind": "item", "kw": kw}
    kw = target_keyword(entity)
    if kw in _SCENERY:
        return {"text": entity, "kind": "ambient", "kw": None}
    # A connected player — never a target. Match the roster name even when the
    # room shows it with a trailing honorific ("북해빙궁주님" → "북해빙궁주").
    if kw and (kw in KNOWN_PLAYERS or kw.rstrip("님") in KNOWN_PLAYERS):
        return {"text": entity, "kind": "player", "kw": kw}
    return {"text": entity, "kind": "mob", "kw": kw}


_PROMPT_PREFIX = re.compile(r"^\d+:\d+:\d+>\s*")


def _ignored_speaker(name: str | None) -> bool:
    """A parsed talk-sender we must NOT treat as human interaction: an NPC, our own
    character, the anonymous 누군가, or parse garbage (knowledge [talk_filter].ignore).
    Talk-recognition exists to spot a real person testing if we're a bot, so these
    non-human senders are filtered out. A leaked prompt prefix ('460:821:156> 이름')
    and a trailing 님 are stripped before matching."""
    if not name:
        return True
    n = _PROMPT_PREFIX.sub("", name).strip()
    if n.endswith("님"):
        n = n[:-1].strip()
    return n in get_knowledge().get("talk_filter", {}).get("ignore", [])


def _apply_alias(info: dict) -> dict:
    """Reflect [aliases] in a UI entity, same as the autonomous engine sees it: a
    ROW-substring entry can rename it AND set its KIND (mob/item/…) when the parser
    can't tell them apart; else an exact-keyword alias remaps the parser's keyword.
    Players are never aliased."""
    if info.get("kind") == "player":
        return info
    aliases = get_knowledge().get("aliases", {})
    name, kind = resolve_alias(aliases, info.get("text", ""))
    if kind is not None:                       # a row alias claims this entity
        if kind == "mob":
            if name:
                info["kw"] = name
            else:                              # empty name = skip: show no action buttons
                info["kind"] = "ambient"; info["kw"] = None
        else:                                  # item / readable / ambient …
            info["kind"] = kind
            if name:
                info["kw"] = name
        return info
    kw = info.get("kw")                         # else: exact keyword alias
    if kw:
        alias = aliases.get(kw)
        if isinstance(alias, str) and alias:
            info["kw"] = alias
    return info


class GameSession:
    def __init__(self, host: str, port: int, log_dir: Path,
                 broadcast, notify: bool = True,
                 sid: str = "a", name: str | None = None,
                 password: str | None = None,
                 notifier: Notifier | None = None) -> None:
        self.host, self.port, self.log_dir = host, port, log_dir
        self.sid = sid
        self.name, self.password = name, password
        self.broadcast = broadcast          # sync fn(dict) fanning out to tabs
        self.notifier = notifier or Notifier(enabled=notify)
        self.state = WorldState()
        self.status = "disconnected"        # connecting|logging-in|online|manual|disconnected
        self.conn: MudConnection | None = None
        self.logger: SessionLogger | None = None
        self.parser: StreamParser | None = None
        self._read_task: asyncio.Task | None = None
        self.sender = PacedSender(self.send)
        self._last_line = ""
        self._following = False   # server moves us while following: not a warp
        # Structured engine — the sole driver. Created in start().
        self.partner_name: str | None = None   # the other character's name
        self.director: Director | None = None   # shared, set in serve()
        self.on_group_change = None             # callback (set by serve) rebuilding the group
                                                # graph on connect/disconnect — see WebUI.relink_group
        self.ctx: CharCtx | None = None
        self.engine: Engine | None = None
        self.engine_enabled = False
        self._engine_task: asyncio.Task | None = None
        self._resume_hunt_target: str | None = None   # for engine auto-reconnect
        self._resume_circuit: list[str] = []          # circuit zones to restore on reconnect
        self._reconnect_task: asyncio.Task | None = None
        self._gridmap_ver: int | None = None          # last-broadcast sweep version
        self._survey_ver: int | None = None           # last-broadcast survey version
        self._last_tick_k: int | None = None           # last master-tick boundary index notified
        self.recording: dict | None = None    # {start, commands} while recording a route
        self._pending_move: str | None = None  # a bare direction awaiting its room render
        # FIFO of room-render EXPECTATIONS for the survey, one per room-rendering command
        # sent (move / look / relocate). The server answers commands IN ORDER, so each
        # RoomSeen is matched to the command that actually caused it — a 봐 re-render can
        # never be mistaken for a move just because a direction was queued right after it
        # (the phantom-room-in-a-corridor bug). Bounded so a rare unanswered command can't
        # desync it forever; an empty queue defaults a render to a harmless re-look.
        self._survey_q: deque = deque(maxlen=8)
        self._score_pending = False
        self._waking = False      # debounce for the 꿈속 auto-wake
        self._last_insure = 0.0   # safety keeper: last 보험 re-affirm (monotonic)
        # connection-scoped flags (re-init in start())
        self._throttled = False
        self.bottle_dry = False
        self.exhausted = False
        self._exhausted_mv = 0

    # ---- outgoing to browser --------------------------------------------

    def snapshot(self) -> dict:
        v = self.state.vitals
        # 철면 (iron-skin) countdown for the UI bar. Seconds left = predicted fade - now, where the
        # fade was PREDICTED at land time from the server's learned 37.5s expiry grid (exact once a
        # fade has anchored the phase). `dur` = this cast's predicted total, so the bar is full at
        # cast and empties at the real fade. 0 => not in 철면 (UI hides the bar). The client animates
        # at 0.1s between these (irregular) broadcasts and re-syncs to `ironskin_remaining` on each.
        iron_rem, iron_dur = 0.0, 56.0
        # master-tick meter: whether the ~74.5s tick is synced yet, its period, and seconds to the
        # next boundary (the client sweeps the meter over the period and re-syncs on each broadcast).
        tick_synced, tick_period, tick_rem = False, 74.5, None
        if self.ctx is not None:
            iron_dur = float(self.ctx.knowledge.get("ironskin", {}).get("nominal_secs", 56.0))
            at = getattr(self.ctx, "ironskin_at", 0.0)
            if at > 0:
                pf = getattr(self.ctx, "ironskin_predicted_fade", 0.0) or (at + iron_dur)
                iron_rem = max(0.0, pf - self.ctx._now())
                iron_dur = max(0.1, pf - at)
            tk = getattr(self.ctx, "tick", None)
            if tk is not None:
                tick_synced = tk.synced
                tick_period = round(tk.period, 2)
                r = tk.remaining(self.ctx._now())
                tick_rem = None if r is None else round(r, 1)
        return {
            "type": "state", "sid": self.sid, "name": self.name,
            "server_ver": SERVER_BOOT,
            "status": self.status,
            "room": self.state.room_title, "exits": self.state.exits,
            "entities": [_apply_alias(entity_info(e)) for e in self.state.entities],
            "inventory": self.state.inventory,
            "hp": v.hp, "hp_max": v.hp_max, "mp": v.mp, "mp_max": v.mp_max,
            "mv": v.mv, "mv_max": v.mv_max,
            "level": self.state.level, "job": self.state.job,
            "coins": self.state.coins,
            "exp_remaining": self.state.exp_remaining,
            "zone": self.state.zone,
            "engine": self.engine_enabled,
            "workflow": (self.ctx.cursor.workflow if self.ctx else None),
            "in_battle": self.state.in_battle,
            "opponent": self.state.battle_opponent,
            "in_group": (getattr(self.ctx, "in_group", True) if self.ctx else True),
            "exp_gained": self.state.exp_gained,
            "coins_looted": self.state.coins_looted,
            "hungry": self.state.hungry, "thirsty": self.state.thirsty,
            "shopping": (self.ctx.shopping_list if self.ctx else []),
            "ironskin_remaining": round(iron_rem, 1), "ironskin_dur": iron_dur,
            "tick_synced": tick_synced, "tick_period": tick_period, "tick_remaining": tick_rem,
        }

    def _emit_state(self) -> None:
        self.broadcast(self.snapshot())

    # ---- engine heartbeat + zone-map overlay ----------------------------

    def _start_engine_loop(self) -> None:
        """Periodic self-driver: the engine also ticks on prompts (fast reaction),
        but a behavior that must act while the server is IDLE (settle timers,
        retries) needs a heartbeat too. One command per ready-window is enforced
        by the engine (see Engine._act)."""
        if self._engine_task is not None and not self._engine_task.done():
            return
        self._engine_task = asyncio.create_task(self._engine_loop())

    async def _engine_loop(self) -> None:
        import random
        # Heartbeat runs whenever CONNECTED — not only when the engine is enabled — so the OFFLINE
        # reflexes (spell-on-request, 연타 continuity, death/blind recovery) and the tick-meter
        # sync refresh keep working with the autonomous driver toggled off. tick() dispatches
        # enabled -> full arbiter, disabled -> the offline reflex subset.
        while self.engine is not None and self.status == "online":
            await asyncio.sleep(random.uniform(0.5, 0.9))   # idle-progress heartbeat
            try:
                if self.engine is not None and self.status == "online":
                    self.engine.tick()
                    self._broadcast_gridmap()
                    self._notify_tick_due()     # desktop-notify the leader's master-tick boundary
                    if not self.engine.enabled:
                        self._emit_state()      # engine-off: keep the tick meter / sync status live
            except Exception as e:
                self.broadcast({"type": "error", "data": f"engine: {e}"})

    def _broadcast_gridmap(self) -> None:
        """Push the temporary in-zone map (the 지도-learned sweep grid) to the UI
        overlay whenever it changes — cells learned, current position, and the
        user's per-room 'avoid' flags. Shown ONLY during an active hunt."""
        sw = self.ctx.gridsweep if self.ctx else None
        hunting = (self.engine is not None and self.engine.enabled
                   and self.ctx is not None
                   and self.ctx.cursor.workflow in ("hunting", "wait", "rest"))
        if sw is None or not hunting:
            if self._gridmap_ver is not None:
                self._gridmap_ver = None
                self.broadcast({"type": "gridmap", "sid": self.sid, "grid": None})
            return
        if sw.version != self._gridmap_ver:
            self._gridmap_ver = sw.version
            self.broadcast({"type": "gridmap", "sid": self.sid,
                            "grid": sw.snapshot()})

    def _notify_tick_due(self) -> None:
        """Desktop-notify (the SAME path as a 대화) the instant the master server tick fires —
        LEADER only, and only once the tick is SYNCED (the phase isn't known at connect). Polled
        from the heartbeat (~0.7s), so it lands within a beat of the true boundary. Silently arms
        on first sight so it doesn't fire mid-cycle right after sync."""
        if self.sid != "a" or self.ctx is None:            # leader (세션 A) only
            return
        tk = self.ctx.tick
        if not tk.synced or tk.anchor is None:
            self._last_tick_k = None                       # dropped sync -> re-arm on next boundary
            return
        k = math.floor((self.ctx._now() - tk.anchor) / tk.period)
        if self._last_tick_k is None:
            self._last_tick_k = k                          # arm silently (don't fire on arm)
            return
        if k > self._last_tick_k:
            self._last_tick_k = k
            self.notifier.notify("서버 틱", f"마스터 틱 ({tk.period:.1f}s) 도래", dedupe=False)

    def _broadcast_survey(self) -> None:
        """Push the reconstructed zone graph (multi-frame layout + portals) to the UI
        whenever it changes. Unlike the hunt overlay this is a PERSISTENT survey — it
        accumulates the true structure as the character walks, always on."""
        sv = self.ctx.survey if self.ctx else None
        if sv is None or not sv.rooms:
            return
        if sv.version != self._survey_ver:
            self._survey_ver = sv.version
            self.broadcast({"type": "survey", "sid": self.sid,
                            "nav": self._survey_is_nav(), **sv.snapshot()})

    def _survey_is_nav(self) -> bool:
        """Is a fixed HUNTING MAP the active nav mechanism for the current hunt (vs the 지도
        gridsweep)? — so the UI shows the map overlay in the floating zone box only for a
        zone that has a hand-authored map, where the gridsweep never fills it."""
        if self.ctx is None:
            return False
        return self.ctx.hunt_zone() in get_knowledge().get("hunting_maps", {})

    # ---- event handling -------------------------------------------------

    def on_event(self, event: ev.Event) -> None:
        # Register connected players (from 누구) so the hunter NEVER attacks one
        # — a player kill is instant death (the accident that killed the leader).
        if isinstance(event, ev.Roster):
            KNOWN_PLAYERS.update(event.names)
        self.state.apply(event)
        # New engine: feed ctx-only signals (heard spell requests) and tick the
        # interpreter on each prompt (the server-ready gate). Gated by the toggle.
        if self.ctx is not None:
            self.ctx.on_event(event)
        # Tick on every prompt while connected. The engine gates itself: enabled -> the full
        # arbiter; disabled -> the offline reflex subset (spell-on-request, 연타, recovery).
        if (self.engine is not None and self.status == "online"
                and isinstance(event, ev.Prompt)):
            self.engine.tick()

        match event:
            case ev.RoomSeen() as r:
                # A room render following a move command confirms the move landed
                # -> tell the sweep so registration may trust the shift (an
                # unconfirmed move is treated as 'stayed put' — no phantom cells).
                moved = self._pending_move is not None
                if self._pending_move is not None:
                    self._pending_move = None
                    if self.ctx is not None and self.ctx.gridsweep is not None:
                        self.ctx.gridsweep.confirm_move()
                # Persistent surveyor: build the TRUE graph from exits + this render.
                # Which command caused THIS render is read from the FIFO expectation queue
                # (not the single _pending_move, which a raced 봐/direction pair corrupts).
                # ONLY while we're actually IN the hunt zone (hunting/wait/rest) — never
                # during travel/resupply, or the survey accumulates 중앙 광장 / 떡집 /
                # 광장 사거리 town rooms as disconnected nodes that fragment the zone map.
                if (self.ctx is not None and self.ctx.survey is not None
                        and self.ctx.cursor.workflow in ("hunting", "wait", "rest")):
                    sv = self.ctx.survey
                    # Position is tracked at SEND time now (note_move advances optimistically),
                    # NOT from renders — so we NEVER note_move here. A render only confirms the
                    # move stuck (observe clears pending) and drives the landmark re-sync. This
                    # is what makes it immune to combat's unsolicited re-renders (which used to
                    # pop a "move" off this queue and masquerade as a step -> the 007->017 drift).
                    # We still drain the queue to spot a 귀환/도망 RELOCATE landing.
                    kind, _sdir = self._survey_q.popleft() if self._survey_q else ("look", None)
                    if kind == "relocate":
                        sv.note_teleport()
                    sv.observe(r.exits, False, zone=self.state.zone)
                    self._broadcast_survey()
                self._record_sightings(r.entities)
            case ev.MapGrid(grid=grid):
                # Feed the 지도 to the engine's live sweep: room identity here is
                # the cell's position in the server's own minimap, never the
                # (identical) room title — so the hunt can walk every cell.
                if self.ctx is not None:
                    self.ctx.map_grid = grid
                    if self.ctx.gridsweep is not None:
                        self.ctx.gridsweep.register(grid)   # ground-truth positioning
            case ev.ZoneInfo(name=name):
                self.state.zone = name
                self._record_sightings(self.state.entities)
            case ev.EntityArrived(text=text):
                self._record_sightings([text])
            case ev.CantGo():
                self._pending_move = None
                # A hard refusal (no exit that way) — tell the sweep so it drops
                # the phantom edge and its next 지도 registers with no shift.
                if self.ctx is not None and self.ctx.gridsweep is not None:
                    self.ctx.gridsweep.note_refused()
                if self.ctx is not None and self.ctx.survey is not None:
                    # The refused move produced NO render — drop its queue expectation so a later
                    # render isn't consumed as this move. The direction to roll back + block comes
                    # from the map's OWN `pending` (set by the optimistic note_move at send), not
                    # this queue, which combat re-renders can misalign.
                    if self._survey_q and self._survey_q[0][0] == "move":
                        self._survey_q.popleft()
                    self.ctx.survey.note_refused(hard=True)
            case ev.MissingItem():
                # We used an item we no longer carry (e.g. drank the last 쑥). Our tracked
                # inventory is STALE, so potion_count still reads >0 and the drink reflex
                # loops instead of transitioning to rest. Refresh 소지품 (throttled) to zero
                # the count — then should_drink_hp goes false and rest/sleep takes over.
                now = time.monotonic()
                if now - getattr(self, "_last_inv_refresh", 0.0) > 5:
                    self._last_inv_refresh = now
                    self.sender.push("소지품")
            case ev.ExpGain():
                # Totals only change on level-up (or items); when tracked XP says
                # we leveled, refresh the sheet once.
                if self.state.exp_remaining == 0 and not self._score_pending:
                    self._score_pending = True
                    self.sender.push("점수")
            case ev.Status():
                self._score_pending = False
            case ev.TargetGone():
                now = time.monotonic()
                if now - getattr(self, "_last_refresh", 0.0) > 4:
                    self._last_refresh = now
                    self.sender.push("봐")
            case ev.Prompt(mv=mv):
                # Exhaustion clears once rest has rebuilt a movement budget.
                if self.exhausted and mv >= self._exhausted_mv + 15:
                    self.exhausted = False
                # Staleness backstop: combat runs one round per prompt, so if we
                # think we're in battle but no hit has landed for a while, the
                # fight is over (fled / mob wandered off) — clear it so the hunt/
                # watchdog don't freeze on a stuck in_battle flag.
                if (self.state.in_battle
                        and time.monotonic() - getattr(self, "_last_combat", 0.0) > 15):
                    self.state.in_battle = False
                    self.state.battle_opponent = None
            case ev.CombatHit():
                self._last_combat = time.monotonic()
            case _:
                pass

        # Talk-recognition: surface (notify + UI comm) ONLY talk from a potential
        # human — a real person testing if the characters are bots. Non-human senders
        # (NPCs, our own 방비! requests, 누군가, parse garbage) are filtered out.
        match event:
            case ev.Tell(speaker=who, text=text) if not _ignored_speaker(who):
                self.notifier.notify(f"{who} → {self.name or self.sid}", text)
                self.broadcast({"type": "comm", "kind": "tell", "who": who, "text": text})
            case ev.ChannelMessage(channel=ch, speaker=who, text=text) if not _ignored_speaker(who):
                self.notifier.notify(f"{who} [{ch}]", text, dedupe=True)
                self.broadcast({"type": "comm", "kind": ch, "who": who, "text": text})
            case ev.Speech(speaker=who, text=text) if not _ignored_speaker(who):
                self.notifier.notify(who, text, dedupe=True)
                self.broadcast({"type": "comm", "kind": "말", "who": who, "text": text})
            case _:
                pass
        self._emit_state()

    def _record_sightings(self, entities: list[str]) -> None:
        # No persistent map any more — sightings only matter live (co-location is
        # tracked in CharCtx.on_event). Kept as a hook point; currently a no-op.
        return

    # ---- lifecycle -------------------------------------------------------

    async def start(self, _retries: int = 2) -> None:
        if self.status != "disconnected":
            return
        self._throttled = False
        self.bottle_dry = False
        self.exhausted = False
        self._exhausted_mv = 0
        self.status = "connecting"
        self._emit_state()
        self.logger = SessionLogger(self.log_dir)
        self.state = WorldState()
        self.parser = StreamParser(self.on_event)
        self.sender.start()

        # Structured engine: a CharCtx over this fresh WorldState plus a paced Cmd.
        # Behavior is resolved by name from the registry each tick, so it is created
        # once here and survives hot-reloads.
        role = "leader" if self.sid == "a" else "supporter"
        self.ctx = CharCtx(self.sid, role, self.state, get_knowledge(),
                           KNOWN_PLAYERS, name=self.name)
        self.ctx.partner_name = self.partner_name
        # No dead-reckoning surveyor: navigation is by a read-only FIXED MAP only. On arrival
        # to hunt a mapped zone, reset_sweep swaps a FixedMap into s.survey; elsewhere it's None.
        self.ctx.survey = None
        self._survey_ver = None

        def on_engine_status(info: dict) -> None:
            self.broadcast({"type": "engine", **info})

        self.engine = Engine(self.ctx, Cmd(self.sender.push),
                             director=self.director, on_status=on_engine_status)
        if self.director is not None:
            self.director.register(self.sid, self.engine)
        # Rebuild the GROUP graph (number-agnostic) now that this ctx exists — and again on
        # disconnect (see stop()), so the leader's `partners` always reflects the currently
        # CONNECTED supporters. Never a blind "pair with the last-connected" that would rebind
        # the leader with 3+ characters, and a dropped supporter falls out instead of being
        # waited on forever.
        if self.on_group_change is not None:
            self.on_group_change()
        watcher = TextWatcher()

        def on_text(t: str) -> None:
            watcher.on_text(t)
            if "입력이 없어서" in t:      # server-side rapid-reconnect throttle
                self._throttled = True
            # Bottle state: dry when a drink fails, wet again after a refill.
            if ("비어있습니다" in t or "병이 비" in t or "더 마실 수 없" in t
                    or "물이 없" in t or "말라" in t):
                self.bottle_dry = True
                self.state.bottle_dry = True
            if "물을 채" in t or "가득" in t:
                self.bottle_dry = False
                self.state.bottle_dry = False
            # Move-point exhaustion: the server refuses movement until rested.
            if "완전히 늘어져" in t or "너무 지쳐" in t:
                # A move bounced off exhaustion (no CantGo for this) — tell the
                # sweep so its next 지도 registers with no shift. SOFT refusal:
                # transient ('not now'), so keep the edge (don't wall it off).
                if self.ctx is not None and self.ctx.gridsweep is not None:
                    self.ctx.gridsweep.note_refused(hard=False)
                if self.ctx is not None and self.ctx.survey is not None:
                    self.ctx.survey.note_refused(hard=False)
                if not self.exhausted:
                    self.exhausted = True
                    self._exhausted_mv = self.state.vitals.mv
            # Peaceful (no-combat) room: the server refuses attacks here. Nothing
            # to record without the map — the engine just won't land a fight.
            # A support buff (방비, +20 def) wore off. As the LEADER, immediately
            # SPEAK the request again ("방비 말") so the supporter recasts it.
            if "보호가 덜해지" in t and not self._following:
                now = time.monotonic()
                if now - getattr(self, "_last_bangbi_say", 0.0) > 8:
                    self._last_bangbi_say = now
                    self.sender.push("방비! 말")   # "!" = spell-request discriminator
            # Multi-strike combo (연타) wind-up cue -> flag a re-queue. The engine's
            # hunt_step consumes the flag on the next prompt, and ONLY while genuinely
            # mid-fight (so a bare re-queue can't engage a second enemy or mistime).
            if self.ctx is not None:
                cue = get_knowledge().get("combat", {}).get("combo_cue")
                if cue and cue in t:
                    self.ctx.combo_ready = True
                # KNOCKDOWN: a power-bash floored us -> flag it; the `stand_up` reflex sends 일어나
                # on the next tick (works engine-on AND offline). Self-clearing on the stand.
                kd = get_knowledge().get("combat", {}).get("knockdown_cue")
                if kd and kd in t:
                    self.ctx.knocked_down = True
                # Buff confirmation (the RECEIVER — the leader — sees these): a `landed`
                # message CONFIRMS the cast (leader stops re-asking until it lapses); a
                # `faded` message EXPIRES it (leader re-asks now). Knowledge-driven so a
                # new buff is taught by DATA, no code change (e.g. 빨리가기 land/fade).
                boundary_buffs = get_knowledge().get("tick", {}).get("boundary_buffs", ["분노", "빨리가기"])
                for buff, sig in get_knowledge().get("buff_signals", {}).items():
                    landed, faded = sig.get("landed"), sig.get("faded")
                    if landed and landed in t:
                        self.ctx.buffs.confirm(buff)
                    if faded and faded in t:
                        self.ctx.buffs.expire(buff)
                        # 분노/빨리가기 fade ON the master tick boundary — feed the tick layer so it
                        # learns the phase (dedup handles the two fading together to the same instant).
                        if buff in boundary_buffs:
                            self.ctx.tick.observe_boundary(self.ctx._now())
                # 철면 (iron-skin immunity) — tracked SEPARATELY as the driver of 철면사냥. The
                # RECEIVER (leader) sees these. Landing sets the timestamp (the game halves HP);
                # the fade line clears it, and the supporter's ironskin reflex RE-CASTS the moment
                # it sees `ironskin_at == 0` (never before — early re-cast re-halves HP for free).
                iron = get_knowledge().get("ironskin", {})
                if iron.get("landed") and iron["landed"] in t:
                    now = self.ctx._now()
                    self.ctx.ironskin_at = now
                    self.ctx.flee_failed = False        # protected now -> no flee pending
                    self.ctx._iron_req_pending = False  # the in-flight request LANDED -> clear the
                                                        # re-ask latch (see ironskin_request_due):
                                                        # a 2nd ask now would be a double-cast (HP→1)
                    # PREDICT this cast's fade. 철면 rides the master tick's HALF-grid — prefer the
                    # tick layer once it's synced (unified with 분노/빨리); else fall back to 철면's own
                    # self-learned 37.5s anchor, and to a nominal estimate before any fade is seen.
                    base = iron.get("base_secs", 36.75)
                    gp = self.ctx.tick.grid_point(now, base, div=2)
                    self.ctx.ironskin_predicted_fade = gp if gp is not None else ironskin_predict(
                        now, self.ctx.ironskin_grid_anchor,
                        iron.get("tick_secs", 37.5), base, iron.get("nominal_secs", 56.0))
                if iron.get("faded") and iron["faded"] in t:
                    now = self.ctx._now()
                    self.ctx.ironskin_at = 0.0
                    self.ctx.ironskin_faded_at = now     # stamp the fade -> ironskin_emergency holds
                                                         # `fade_flee_hold` secs before it starts to flee
                    self.ctx._iron_req_pending = False   # not protected + no ask in flight -> a fresh
                                                         # mid-fight re-ask is allowed again
                    self.ctx.ironskin_grid_anchor = now  # this fade anchors the grid phase
                # 도망 is stochastic; on the fail line the leader is STILL in the fight and must
                # RETRY at once (see ironskin_emergency) rather than wait out the flee grace.
                if iron.get("flee_fail") and iron["flee_fail"] in t:
                    self.ctx.flee_failed = True
                # A SUCCESSFUL 도망: clear the flee grace so the leader re-assesses immediately in
                # the room it fled to (re-flee if it's also lethal, else rest/hunt) — no stale hold.
                if iron.get("flee_ok") and iron["flee_ok"] in t:
                    self.ctx._iron_flee_at = 0.0        # 0 => grace inactive (now - 0 ≫ flee_grace)
                    self.ctx.flee_failed = False
                # 귀환 fail line. BLIND recovery can't confirm a recall by room title (blindness
                # hides the render), so it confirms by the ABSENCE of this line after a 귀환.
                rfail = get_knowledge().get("recall", {}).get("fail_mark")
                if rfail and rfail in t:
                    self.ctx.recall_fail_at = self.ctx._now()
                # REGROUP (GENERAL, any hunt): when the leader FLEES (도망) the game does NOT
                # auto-pull the follower along the way a normal walk does, so the pair separates.
                # The supporter reads the flee direction and CHASES (chase_partner). CRUCIAL: gate on
                # the FLEE-SPECIFIC marker ([hunt].flee_mark = "도망치려"), NOT the departure line
                # "…바람을 가르며 떠났…" — that fires on ordinary walking too (during which the game
                # already auto-follows), so anchoring on it false-fired a chase on every travel step.
                hcfg = get_knowledge().get("hunt", {})
                mark = hcfg.get("flee_mark")
                if (mark and self.ctx.role == "supporter" and self.ctx.partner_name
                        and self.ctx.partner_name in t and mark in t):
                    m = re.search(
                        re.escape(self.ctx.partner_name) + r"[이가]\s*(동|서|남|북|위|아래)쪽?으로", t)
                    if m:
                        d = m.group(1)
                        self.ctx.pending_chase = d
                        # KEEP THE LEADER'S MAP IN SYNC: the flee'er is never told which way it
                        # was thrown, so its FixedMap would freeze one room behind after every
                        # 도망 (the whole hunt then dead-reckons from the wrong room). We watched
                        # it go — feed that same direction into the leader's survey so its pos
                        # advances exactly as the chase advances ours. (note_flee, not note_move:
                        # the relocation already rendered — see fixedmap.note_flee.)
                        peer = getattr(self.ctx, "partner", None)   # the leader
                        psv = getattr(peer, "survey", None) if peer is not None else None
                        # DEDUPE across supporters: with several supporters, ALL of them see the
                        # same flee-departure line and would each note_flee the LEADER's ONE survey,
                        # advancing its dead-reckoned position N times for a single flee. Apply it
                        # exactly once — whichever supporter reports first wins; the rest, seeing the
                        # same (direction, ~instant) on the shared leader ctx, skip. (The chase above
                        # is per-supporter and always runs.)
                        now = self.ctx._now()
                        dup = (d == getattr(peer, "_flee_sync_dir", None)
                               and now - getattr(peer, "_flee_sync_at", -1e9) < 2.0)
                        if psv is not None and not dup:
                            peer._flee_sync_at = now
                            peer._flee_sync_dir = d
                            before = psv.cur
                            psv.note_flee(d)
                            # visible confirmation: show the leader's dead-reckoned pos advance so
                            # the sync isn't a silent no-op (it silently did nothing when the leader
                            # was on the gridsweep — psv was None — not the FixedMap).
                            self.broadcast({"type": "info",
                                            "data": f"🧭 map-sync {self.ctx.partner_name} fled {d}: "
                                                    f"{before}→{psv.cur}"})
                        elif psv is not None and dup:
                            pass                            # another supporter already synced this flee
                        else:
                            self.broadcast({"type": "info",
                                            "data": f"🧭 map-sync skipped ({self.ctx.partner_name} "
                                                    f"not on a FixedMap — no dead-reckoning to fix)"})
                        self.broadcast({"type": "info",       # debug-visible: confirm detection
                                        "data": f"🏃 chase {self.ctx.partner_name}->{d}"})
                # 고려 verdict: while assessing an unfamiliar mob, a lethal phrase in
                # the reply marks it DANGER (recorded + never attacked).
                if self.ctx.considering:
                    for ph in get_knowledge().get("mob_danger", {}).get("danger_phrases", []):
                        if ph and ph in t:
                            self.ctx.consider_danger = True
                            break
                # 따라 (follow) is a STANDING server relationship — track it from the
                # server's own FIRST-PERSON messages (only the follower sees "당신은 …").
                # Lets lockstep travel tell whether the supporter is already being
                # carried (and must NOT also walk its own route — that double-moves it).
                # LINE-ANCHORED: `t` is a raw multi-line packet, so a plain
                # `"당신은" in t` leaks from OTHER lines — e.g. the 성의 일층 room desc
                # ("당신은 무서움에 몸을 떨며…") — and, combined with the LEADER's
                # third-person "…이 당신을 따라다니기 시작했습니다" in the same packet,
                # wrongly set the LEADER's following=True (→ both_routes_done short-
                # circuits → travel never walks → the 모두그룹/따라 ping-pong at town).
                # Match ONLY the follower's own first-person line ("당신은 …를 따라…").
                for _ln in t.splitlines():
                    if re.match(r"\s*당신은 .+[를을] 따라다니(기 시작|고 있습니다)", _ln):
                        self.ctx.following = True
                    elif re.match(r"\s*당신은 .+따라다니는 것을 그만", _ln):
                        self.ctx.following = False
                # 자동지원 also TOGGLES — track its true state so form_party never
                # re-sends it (which would flip it OFF) when it's already enabled.
                if "자동지원 가능 상태" in t:
                    self.ctx.assist_on = True
                elif "자동지원 불가능 상태" in t:
                    self.ctx.assist_on = False
            # Tried to act while asleep ("꿈속이라서 아무 것도 할 수 없습니다"). Wake
            # (깨 → 일) UNLESS we're intentionally napping (rest/wait workflow).
            if "꿈속이라서" in t and not self._resting():
                self.state.posture = "sleeping"
                asyncio.ensure_future(self._wake_up())
            # Raw text incl. ANSI codes: the browser renders the colors.
            self.broadcast({"type": "text", "data": t})
            try:
                self.parser.feed(t)
            except Exception as e:
                # A handler bug must never kill the game stream.
                self.broadcast({"type": "error",
                                "data": f"parser: {type(e).__name__}: {e}"})
                if self.logger:
                    self.logger.note(f"PARSER ERROR {type(e).__name__}: {e}")

        self.conn = MudConnection(self.host, self.port, self.logger, on_text=on_text)
        try:
            await self.conn.connect()
        except OSError as e:
            self.broadcast({"type": "error", "data": f"connect failed: {e}"})
            self.status = "disconnected"
            self._emit_state()
            return
        self._read_task = asyncio.create_task(self._read())

        name, password = self.name, self.password
        if name and password:
            self.status = "logging-in"
            self._emit_state()
            try:
                result = await auto_login(self.conn, watcher, name, password)
                self.status = "online"
                self.broadcast({"type": "info", "data": f"auto-login ok ({result})"})
                # The first room scrolls by while the parser is still in login
                # mode — re-look so the sidebar has room/exits/inventory, and 점수
                # for the full stat sheet, and 누구/보험 for player-safety.
                await asyncio.sleep(0.9)
                await self.send("봐")
                await asyncio.sleep(1.2)
                await self.send("소지품")
                await asyncio.sleep(1.2)
                await self.send("점수")
                await asyncio.sleep(1.2)
                await self.send("누구")          # seed the player-safety roster
                await asyncio.sleep(1.2)
                await self.send("보험")          # enroll insurance (keeps gear on death)
            except LoginError as e:
                if self._throttled and _retries > 0:
                    self.broadcast({"type": "info",
                                    "data": "서버가 재접속을 제한했습니다 — 75초 후 재시도"})
                    await self.stop()
                    await asyncio.sleep(75)
                    await self.start(_retries=_retries - 1)
                    return
                self.status = "manual"
                self.broadcast({"type": "error",
                                "data": f"auto-login failed: {e} — continue manually"})
        else:
            self.status = "manual"
            self.broadcast({"type": "info", "data": "no credentials in .env — manual login"})
        self._emit_state()
        if self.status == "online":
            # Run the heartbeat from the moment we're connected — even with the engine OFF — so the
            # offline reflexes and the tick-meter refresh are live under manual play (idempotent).
            self._start_engine_loop()

    def _resting(self) -> bool:
        return (self.engine is not None and self.ctx is not None
                and self.ctx.cursor.workflow in ("rest", "wait"))

    async def _read(self) -> None:
        try:
            await self.conn.read_loop()
        except asyncio.CancelledError:
            raise
        finally:
            if self.status != "disconnected":
                self.status = "disconnected"
                self._emit_state()
                self.broadcast({"type": "info", "data": "connection closed"})
                # Engine-driven run: auto-reconnect and resume the hunt. Throttled
                # so a flapping socket can't hammer the login (server bot policy).
                if self.engine_enabled and self._resume_hunt_target is not None:
                    if self._reconnect_task is None or self._reconnect_task.done():
                        self._reconnect_task = asyncio.create_task(
                            self._engine_reconnect())

    async def _engine_reconnect(self) -> None:
        target = self._resume_hunt_target
        for attempt in range(3):
            await asyncio.sleep(30 if attempt == 0 else 120)   # reconnect cooldown
            if self.status != "disconnected" or not self.engine_enabled:
                return                        # recovered or turned off meanwhile
            self.broadcast({"type": "info",
                            "data": f"🔌 엔진 자동 재접속 시도 ({attempt + 1}/3)"})
            try:
                await self.start()
            except Exception as e:
                self.broadcast({"type": "error", "data": f"재접속 오류: {e}"})
                continue
            await asyncio.sleep(4)            # let login + first renders settle
            if self.status == "online":
                # Re-arm from the top: travel recalls to the anchor and reunites
                # the duo before walking back to the zone — safe after a drop.
                self.engine_enabled = True
                self.engine.boot("travel", target=target)
                if self.ctx is not None:                 # restore the circuit after the fresh ctx
                    self.ctx.circuit = list(self._resume_circuit)
                    self.ctx.circuit_idx = 0
                self._start_engine_loop()
                self._emit_state()
                self.broadcast({"type": "info",
                                "data": f"🔌 재접속 완료 — 사냥 재개 → {target}"})
                return
        self.broadcast({"type": "error",
                        "data": "🔌 자동 재접속 실패 — 수동 개입 필요"})

    async def send(self, line: str) -> None:
        if self.conn is None or self.status in ("disconnected", "connecting"):
            self.broadcast({"type": "error", "data": "not connected"})
            return
        self._last_line = line
        if self.recording is not None and _route_worthy(line):
            self.recording["commands"].append(line)
            self.broadcast({"type": "record", "recording": True,
                            "start": self.recording["start"],
                            "commands": self.recording["commands"]})
        if line.endswith(" 따라"):
            self._following = True
        # Record what RENDER this command should produce, matched to it in order when the
        # render arrives (see _survey_q). The survey's note_move/note_teleport are applied
        # THEN — never at send time — so a render can't be attributed to a later command.
        # Only queue while IN-ZONE (the same gate the observe uses): otherwise a travel /
        # resupply route — with its specials (동대문 열, 맨홀 열) that render without a queue
        # entry — would leave the queue misaligned when the hunt resumes.
        in_zone = (self.ctx is not None
                   and self.ctx.cursor.workflow in ("hunting", "wait", "rest"))
        if line in DIRECTIONS:
            self._pending_move = line                # gridsweep move correlation (unchanged)
            if in_zone:
                self._survey_q.append(("move", line))
        elif line == "봐":
            if in_zone:
                self._survey_q.append(("look", None))
        elif line == "귀환" or line == "도망":
            # RELOCATE with no direction — drop any in-flight gridsweep move; the survey
            # gets a fresh disconnected landing when this render arrives.
            self._pending_move = None
            if in_zone:
                self._survey_q.append(("relocate", None))
        elif line.endswith(" 따라"):
            # 따라 renders only later, when the leader drags us — no expectation to queue.
            self._pending_move = None
        try:
            await self.conn.send_line(line)
        except (ConnectionError, OSError):
            self.broadcast({"type": "error",
                            "data": "connection lost — 접속 to reconnect"})
            await self.stop()
            return
        self.broadcast({"type": "sent", "data": line})

    async def quit_game(self) -> None:
        """Graceful quit: 끝, give the server a moment, then close."""
        if self.conn is not None and self.status in ("online", "manual", "logging-in"):
            try:
                await self.conn.send_line("끝")
                await asyncio.sleep(2.0)
            except (ConnectionError, OSError):
                pass
        await self.stop()

    async def stop(self) -> None:
        self.engine_enabled = False
        if self.engine is not None:
            self.engine.stop()
        self.sender.stop()
        if self._read_task is not None:
            self._read_task.cancel()
            self._read_task = None
        if self.conn is not None:
            await self.conn.close()
            self.conn = None
        if self.logger is not None:
            self.logger.close()
            self.logger = None
        self.status = "disconnected"
        self._emit_state()
        if self.on_group_change is not None:   # drop this char out of the leader's group
            self.on_group_change()

    async def _wake_up(self) -> None:
        """Stand back up after being caught asleep (깨 → 일). Debounced so a burst
        of 꿈속 rejections triggers only one wake sequence."""
        if self._waking:
            return
        self._waking = True
        try:
            self.broadcast({"type": "info", "data": "💤 꿈속 감지 — 깨어남(깨→일)"})
            await self.send("깨")
            await asyncio.sleep(1.0)
            await self.send("일")
            await asyncio.sleep(0.5)
            self.state.posture = "standing"
        finally:
            self._waking = False


class WebUI:
    def __init__(self, sessions: dict[str, GameSession], ui_port: int = 8642) -> None:
        self.sessions = sessions
        # Character roster for the leader/supporter picker (set by serve()).
        # Each entry {"name", "password"} — passwords are NEVER broadcast.
        self.roster: list[dict] = []
        # Factory creating an EXTRA supporter session (sid c, d, …) — set by serve(),
        # which owns the host/log/notifier plumbing a GameSession needs.
        self.new_supporter = None
        self.ui_port = ui_port
        self.clients: set[web.WebSocketResponse] = set()
        self.app = web.Application()
        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/m", self.mobile)      # mobile: leader console + mode only
        self.app.router.add_get("/ws", self.ws_handler)
        self.app.router.add_get("/lan", self.lan)

    def broadcast(self, msg: dict) -> None:
        if not self.clients:
            return
        data = json.dumps(msg, ensure_ascii=False)
        for ws in list(self.clients):
            asyncio.ensure_future(self._safe_send(ws, data))

    async def _safe_send(self, ws: web.WebSocketResponse, data: str) -> None:
        try:
            await ws.send_str(data)
        except (ConnectionError, RuntimeError):
            self.clients.discard(ws)

    async def index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html")

    async def mobile(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "mobile.html")

    async def lan(self, request: web.Request) -> web.Response:
        # The address other machines on the LAN use to reach this UI — shown in the header.
        return web.json_response({"ip": lan_ip(), "port": self.ui_port})

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.clients.add(ws)
        # Character picker data: roster NAMES + the current slot assignment. A fresh tab
        # uses this (with the per-session `status` in the state dumps below) to offer the
        # leader/supporter selection modal when nothing is connected.
        await ws.send_str(json.dumps(self._roster_msg(), ensure_ascii=False))
        await ws.send_str(json.dumps(self._sessions_msg(), ensure_ascii=False))
        for session in self.sessions.values():
            await ws.send_str(json.dumps(session.snapshot(), ensure_ascii=False))
            # Current temporary zone-map, so a freshly-opened page shows the
            # overlay immediately instead of waiting for the next sweep step.
            sw = session.ctx.gridsweep if session.ctx else None
            hunting = (session.engine is not None and session.engine.enabled
                       and session.ctx is not None
                       and session.ctx.cursor.workflow in ("hunting", "wait", "rest"))
            if sw is not None and hunting:
                await ws.send_str(json.dumps(
                    {"type": "gridmap", "sid": session.sid, "grid": sw.snapshot()},
                    ensure_ascii=False))
            sv = session.ctx.survey if session.ctx else None
            if sv is not None and sv.rooms:
                await ws.send_str(json.dumps(
                    {"type": "survey", "sid": session.sid,
                     "nav": session._survey_is_nav(), **sv.snapshot()},
                    ensure_ascii=False))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                try:
                    await self.dispatch(data, ws)
                except Exception as e:  # one bad message must not kill the tab
                    self.broadcast({"type": "error", "data": f"{type(e).__name__}: {e}"})
        finally:
            self.clients.discard(ws)
        return ws

    def _roster_msg(self) -> dict:
        """Roster NAMES + current leader/supporter assignment (no passwords, ever)."""
        a, b = self.sessions.get("a"), self.sessions.get("b")
        return {"type": "roster",
                "names": [r["name"] for r in self.roster],
                "leader": a.name if a else None,
                "supporter": b.name if b else None}

    def relink_group(self) -> None:
        """Rebuild the leader↔supporters graph from the CONNECTED sessions (number-agnostic).
        Leader gets `partners` = every connected supporter's ctx (+ `partner` = the first, for
        compat); each connected supporter's `partner` = the leader. A disconnected session is
        excluded, so the leader never waits on a dropped supporter. Called on connect/disconnect."""
        live = [s for s in self.sessions.values()
                if s.status != "disconnected" and s.ctx is not None]
        leader = next((s.ctx for s in live if s.sid == "a"), None)
        sups = [s.ctx for s in live if s.sid != "a"]
        for s in live:
            if s.sid == "a":
                s.ctx.partners = sups
                s.ctx.partner = sups[0] if sups else None
            else:
                s.ctx.partner = leader
                s.ctx.partners = []

    def _sessions_msg(self) -> dict:
        """The session LAYOUT: which sids exist, each one's role/name/status. The UI
        builds the leader pane + one supporter section per (named) supporter from this."""
        return {"type": "sessions",
                "list": [{"sid": s.sid,
                          "role": "leader" if s.sid == "a" else "supporter",
                          "name": s.name, "status": s.status}
                         for s in self.sessions.values()]}

    async def dispatch(self, data: dict, ws: web.WebSocketResponse | None = None) -> None:
        s = self.sessions.get(str(data.get("sid", "a")))
        if s is None:
            return
        match data.get("type"):
            case "command":
                await s.send(str(data.get("line", "")).rstrip())
            case "engine":
                # The structured engine drives the whole duo; enabling it boots a
                # workflow on every connected session.
                on = bool(data.get("enabled", False))
                workflow = str(data.get("workflow") or "travel")
                for sess in self.sessions.values():
                    if sess.engine is None:
                        continue
                    sess.engine_enabled = on
                    if on:
                        sess.engine.boot(workflow)
                        sess._start_engine_loop()
                    else:
                        sess.engine.stop()
                    sess._emit_state()
                self.broadcast({"type": "info",
                                "data": f"⚙️ 엔진: "
                                        f"{'ON ('+workflow+')' if on else 'OFF'}"})
            case "engine_list":
                # Behavior-lab catalog (registry is global; both panes populate).
                self.broadcast({"type": "engine_list",
                                "actions": registry.list_actions(),
                                "guards": registry.list_guards(),
                                "workflows": registry.workflows_detail(),
                                # routes = every defined path from 중앙 광장; zones = the
                                # hand-authored fixed-map hunting zones (automated hunting).
                                "destinations": sorted(get_knowledge()
                                                       .get("travel_routes", {})),
                                "zones": sorted(get_knowledge()
                                                .get("hunting_maps", {}))})
            case "hunt_start":
                # Parameterized hunt: travel to `target` (a zone / recorded route), then start
                # hunting there on arrival. Applies to the whole duo. `targets` (a list) instead
                # sets up a CIRCUIT — rotate zones as each is swept barren (see advance_circuit).
                raw = data.get("targets")
                circuit = ([str(t).strip() for t in raw if str(t).strip()]
                           if isinstance(raw, list) else [])
                target = circuit[0] if circuit else (str(data.get("target") or "").strip() or None)
                circuit = circuit if len(circuit) > 1 else []   # a 1-zone "circuit" is a normal hunt
                for sess in self.sessions.values():
                    if sess.engine is None:
                        continue
                    sess.engine_enabled = True
                    sess._resume_hunt_target = target          # remember for auto-reconnect
                    sess._resume_circuit = list(circuit)
                    sess.engine.boot("travel", target=target)  # sets ctx.hunt_target
                    if sess.ctx is not None:
                        sess.ctx.circuit = list(circuit)       # set AFTER boot (boot doesn't touch it)
                        sess.ctx.circuit_idx = 0
                    sess._start_engine_loop()
                    sess._emit_state()
                label = " ⇄ ".join(circuit) if circuit else (target or "(기본 사냥터)")
                self.broadcast({"type": "info", "data": f"🎯 사냥 시작 → {label}"})
            case "engine_run":
                if s.engine is None:
                    s.broadcast({"type": "probe", "sid": s.sid, "ok": False,
                                 "text": "엔진 없음 — 먼저 접속하세요"})
                else:
                    name = str(data.get("name", ""))
                    err = s.engine.run_action(name)
                    s.broadcast({"type": "probe", "sid": s.sid, "ok": err is None,
                                 "text": f"실행 {name}" + (f" ✗ {err}" if err else " ✓")})
            case "engine_guard":
                if s.engine is None:
                    s.broadcast({"type": "probe", "sid": s.sid, "ok": False,
                                 "text": "엔진 없음 — 먼저 접속하세요"})
                else:
                    spec = str(data.get("name", ""))
                    if data.get("arg"):
                        spec = f"{spec}:{data['arg']}"
                    val, err = s.engine.eval_guard(spec)
                    s.broadcast({"type": "probe", "sid": s.sid, "ok": err is None,
                                 "text": f"조건 {spec} = {val}"
                                         + (f" ✗ {err}" if err else "")})
            case "engine_enter":
                if s.engine is None:
                    s.broadcast({"type": "probe", "sid": s.sid, "ok": False,
                                 "text": "엔진 없음 — 먼저 접속하세요"})
                else:
                    wf = str(data.get("workflow", ""))
                    stt = data.get("state") or None
                    err = s.engine.probe_enter(wf, stt)
                    s.broadcast({"type": "probe", "sid": s.sid, "ok": err is None,
                                 "text": f"진입 {wf}.{stt or '초기'}"
                                         + (f" ✗ {err}" if err else " ✓")})
            case "engine_tick":
                if s.engine is not None:
                    s.engine.probe_tick()
                    s.broadcast({"type": "probe", "sid": s.sid, "ok": True,
                                 "text": "한 틱 실행"})
            case "engine_boot":
                # Run ONE workflow to completion on this character.
                if s.engine is None:
                    s.broadcast({"type": "probe", "sid": s.sid, "ok": False,
                                 "text": "엔진 없음 — 먼저 접속하세요"})
                else:
                    wf = str(data.get("workflow") or "")
                    s.engine_enabled = True
                    s.engine.boot(wf)
                    s._start_engine_loop()
                    s.broadcast({"type": "probe", "sid": s.sid, "ok": True,
                                 "text": f"워크플로 실행 ▶ {wf}"})
                    s._emit_state()
            case "record_start":
                s.recording = {"start": s.state.room_title, "commands": []}
                s.broadcast({"type": "record", "recording": True,
                             "start": s.recording["start"], "commands": []})
            case "record_stop":
                name = str(data.get("name", "")).strip()
                if s.recording is not None and name:
                    from .reload import save_route
                    cmds = s.recording["commands"]
                    save_route(name, cmds)
                    s.broadcast({"type": "info",
                                 "data": f"경로 저장: {name} ({len(cmds)} 스텝) — "
                                         f"시작: {s.recording['start']}"})
                elif s.recording is not None and not name:
                    s.broadcast({"type": "error", "data": "경로 이름을 입력하세요"})
                    return
                s.recording = None
                s.broadcast({"type": "record", "recording": False,
                             "start": None, "commands": []})
            case "record_cancel":
                s.recording = None
                s.broadcast({"type": "record", "recording": False,
                             "start": None, "commands": []})
            case "grid_avoid":
                # Runtime-only (never persisted): mark/unmark a room to skip
                # hunting in, on the live sweep overlay.
                sw = s.ctx.gridsweep if s.ctx else None
                if sw is not None and data.get("cell") is not None:
                    sw.set_avoid(tuple(data["cell"]), bool(data.get("avoid")))
                    s._broadcast_gridmap()
            case "shop_remove":
                # Manually release a perishable from the shopping list (user decided
                # not to re-buy it). Runtime-only; the next rot would re-add it.
                item = str(data.get("item", ""))
                if s.ctx is not None and item in s.ctx.shopping_list:
                    s.ctx.shopping_list.remove(item)
                    s._emit_state()
            case "survey_reset":
                # Clear the accumulated zone graph (e.g. when starting a fresh zone).
                if s.ctx is not None and s.ctx.survey is not None:
                    s.ctx.survey.reset()
                    s._survey_ver = None
                    s.broadcast({"type": "survey", "sid": s.sid, "nav": False,
                                 "frames": [], "portals": [], "rooms": 0})
            case "assign":
                # Character picker: put a roster character into each slot (a=leader,
                # b=supporter) and optionally connect both. Only while NOTHING is
                # connected — the ctx/engine/partner links are built at connect time,
                # so swapping a live session would desync everything.
                leader = str(data.get("leader") or "").strip()
                supporter = str(data.get("supporter") or "").strip()
                by_name = {r["name"]: r for r in self.roster}
                a, b = self.sessions.get("a"), self.sessions.get("b")
                if a is None or b is None:
                    return
                if any(x.status != "disconnected" for x in (a, b)):
                    self.broadcast({"type": "error",
                                    "data": "접속 중에는 캐릭터를 바꿀 수 없습니다 — 먼저 연결을 종료하세요"})
                    return
                if not leader or not supporter or leader == supporter:
                    self.broadcast({"type": "error",
                                    "data": "리더와 서포터를 서로 다른 캐릭터로 선택하세요"})
                    return
                if leader not in by_name or supporter not in by_name:
                    self.broadcast({"type": "error",
                                    "data": "로스터에 없는 캐릭터입니다 (.env 의 GUMIHO_CHAR<n>_NAME/_PASSWORD 확인)"})
                    return
                a.name, a.password = leader, by_name[leader]["password"]
                b.name, b.password = supporter, by_name[supporter]["password"]
                a.partner_name, b.partner_name = b.name, a.name
                a._emit_state(); b._emit_state()
                self.broadcast(self._roster_msg())
                self.broadcast(self._sessions_msg())
                self.broadcast({"type": "info", "data": f"👥 리더: {leader} · 서포터: {supporter}"})
                if data.get("connect"):
                    asyncio.ensure_future(a.start())
                    asyncio.ensure_future(b.start())
            case "add_supporter":
                # Add ANOTHER supporter (beyond slot b): pick a roster character, give it
                # its own session (sid c, d, …) following the leader, and connect it. An
                # existing disconnected session already holding that character is reused.
                name = str(data.get("name") or "").strip()
                by_name = {r["name"]: r for r in self.roster}
                if name not in by_name:
                    self.broadcast({"type": "error", "data": "로스터에 없는 캐릭터입니다"})
                    return
                if any(x.name == name and x.status != "disconnected"
                       for x in self.sessions.values()):
                    self.broadcast({"type": "error", "data": f"{name} 은(는) 이미 접속 중입니다"})
                    return
                sess = next((x for x in self.sessions.values() if x.name == name), None)
                if sess is None:
                    if self.new_supporter is None:
                        return
                    sess = self.new_supporter(name, by_name[name]["password"])
                else:
                    sess.password = by_name[name]["password"]
                leader_s = self.sessions.get("a")
                if sess.sid != "a" and leader_s is not None:
                    sess.partner_name = leader_s.name
                self.broadcast(self._sessions_msg())
                self.broadcast({"type": "info", "data": f"➕ 서포터 추가: {name}"})
                asyncio.ensure_future(sess.start())
            case "group_toggle":
                # Toggle whether THIS supporter is in the XP 그룹. Flip the flag and, if the leader
                # is connected, apply it live with the "<name> 그룹" toggle (form_party reconciles it
                # on every regroup: 모두 그룹 baseline, then toggle out the excluded). Leader/self
                # sessions have no checkbox — ignore. Re-emit so the checkbox reflects the new state.
                if s.sid == "a" or s.ctx is None:
                    return
                s.ctx.in_group = not getattr(s.ctx, "in_group", True)
                leader = self.sessions.get("a")
                if leader is not None and leader.status == "online" and s.name:
                    leader.sender.push(f"{s.name} 그룹")
                self.broadcast({"type": "info",
                                "data": f"👥 {s.name} 그룹 {'포함' if s.ctx.in_group else '제외'}"})
                s._emit_state()
            case "connect":
                asyncio.ensure_future(s.start())
            case "quit":
                asyncio.ensure_future(s.quit_game())
            case "disconnect":
                asyncio.ensure_future(s.stop())

    async def run(self) -> None:
        runner = web.AppRunner(self.app)
        await runner.setup()
        # Bind to ALL interfaces so other computers on the house LAN can reach the UI
        # (not just 127.0.0.1). NOTE: no auth — anyone on the LAN can drive the bot.
        site = web.TCPSite(runner, "0.0.0.0", self.ui_port)
        await site.start()
        local = f"http://127.0.0.1:{self.ui_port}"
        print(f"gumiho: GUI at {local}  |  LAN: http://{lan_ip()}:{self.ui_port}  (Ctrl-C to stop)")
        webbrowser.open(local)
        try:
            await asyncio.Event().wait()
        finally:
            for session in self.sessions.values():
                await session.stop()
            await runner.cleanup()


async def _safety_keeper(sessions: dict[str, GameSession]) -> None:
    """Player-safety upkeep (the only surviving piece of the old duo keeper):
    refresh the 누구 roster (KNOWN_PLAYERS — never attack a player) and re-affirm
    보험 (keep gear on a rare death), for each online session."""
    WHO_EVERY = 90.0
    INSURE_EVERY = 600.0
    last_who = 0.0
    while True:
        await asyncio.sleep(30)
        now = time.monotonic()
        do_who = now - last_who >= WHO_EVERY
        for s in sessions.values():
            if s.status != "online":
                continue
            if do_who:
                s.sender.push("누구")
            if now - s._last_insure >= INSURE_EVERY:
                s._last_insure = now
                s.sender.push("보험")
        if do_who:
            last_who = now


async def serve(host: str, port: int, log_dir: Path,
                ui_port: int = 8642, notify: bool = True) -> None:
    """Two independent game sessions (the server allows two per IP —
    the classic leader + supporter duo), one shared knowledge layer."""
    ui: WebUI | None = None

    def make_broadcast(sid: str):
        def broadcast(msg: dict) -> None:
            if ui is not None:
                ui.broadcast({**msg, "sid": sid})
        return broadcast

    env = load_env()
    shared_notifier = Notifier(enabled=notify)
    sessions = {}
    # CHARACTER ROSTER — every character selectable in the UI's leader/supporter picker.
    # Legacy GUMIHO_NAME(/_B) pairs stay the boot defaults; add more characters as
    #   GUMIHO_CHAR1_NAME=... / GUMIHO_CHAR1_PASSWORD=...  (2, 3, … up to 20)
    # in the gitignored .env. Passwords NEVER leave the server — the UI sees names only.
    roster: list[dict] = []

    def _add_char(name: str | None, pw: str | None) -> None:
        if name and pw and not any(r["name"] == name for r in roster):
            roster.append({"name": name, "password": pw})

    _add_char(env.get("GUMIHO_NAME"), env.get("GUMIHO_PASSWORD"))
    _add_char(env.get("GUMIHO_NAME_B"), env.get("GUMIHO_PASSWORD_B"))
    for i in range(1, 21):
        _add_char(env.get(f"GUMIHO_CHAR{i}_NAME"), env.get(f"GUMIHO_CHAR{i}_PASSWORD"))
    for r in roster:
        KNOWN_PLAYERS.add(r["name"])    # never attack ANY of our own characters
    for sid, name_key, pw_key in (("a", "GUMIHO_NAME", "GUMIHO_PASSWORD"),
                                  ("b", "GUMIHO_NAME_B", "GUMIHO_PASSWORD_B")):
        sessions[sid] = GameSession(
            host, port, log_dir, make_broadcast(sid), notify=notify, sid=sid,
            name=env.get(name_key), password=env.get(pw_key),
            notifier=shared_notifier)

    # Shared director, partner names, behavior hot-reload.
    director = Director()
    sessions["a"].partner_name = sessions["b"].name if "b" in sessions else None
    if "b" in sessions:
        sessions["b"].partner_name = sessions["a"].name
    for session in sessions.values():
        session.director = director
    reloader = Reloader(
        lambda: [s.engine for s in sessions.values() if s.engine is not None],
        on_status=lambda name, msg: (ui.broadcast({"type": "info",
            "data": f"🔁 {name}: {msg}"}) if ui else None))
    reloader.load_all()

    ui = WebUI(sessions, ui_port=ui_port)
    ui.roster = roster                  # names+passwords, server-side only
    for s in sessions.values():         # rebuild the group graph on each connect/disconnect
        s.on_group_change = ui.relink_group

    def new_supporter(name: str, password: str) -> GameSession:
        """An EXTRA supporter session (sid c, d, …) wired exactly like b: same shared
        director/notifier, broadcast tagged with its own sid, partner = the leader.
        The sessions dict is shared by reference, so the reloader's engine list and
        the safety keeper pick the new session up automatically."""
        sid = next(c for c in "cdefghij" if c not in sessions)
        sess = GameSession(host, port, log_dir, make_broadcast(sid), notify=notify,
                           sid=sid, name=name, password=password,
                           notifier=shared_notifier)
        sess.partner_name = sessions["a"].name
        sess.director = director
        sess.on_group_change = ui.relink_group
        sessions[sid] = sess
        return sess

    ui.new_supporter = new_supporter
    asyncio.create_task(reloader.watch())
    asyncio.create_task(_safety_keeper(sessions))
    await ui.run()
