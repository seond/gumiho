"""Line-oriented stream parser: decoded text in, typed events out.

Formats are those observed on 고블린 머드 III (see logs/):

    31:100:85>                                  <- prompt, HP:MP:MV, no newline
                       <*> 갈림길 <*>           <- room title
    ――――――――――――――――                            <- divider
    길이 두 갈래로 나뉘어지고 있습니다. ...      <- description
    [ 출구: 남 아래 ]                            <- exits
    병든 닭이 궁금한 표정으로 ...                <- entities until blank/prompt

The prompt arrives WITHOUT a trailing newline, so it is detected on the
partial (unterminated) line as well as on complete lines.
"""

import re
from typing import Callable

from .ansi import strip_ansi
from . import events as ev

PROMPT_RE = re.compile(r"^\s*(\d+):(\d+):(\d+)>\s*$")
ROOM_TITLE_RE = re.compile(r"^\s*<\*>\s*(.+?)\s*<\*>\s*$")
DIVIDER_RE = re.compile(r"^[―=\-─━﹘_]{5,}\s*$")
EXITS_RE = re.compile(r"^\s*\[\s*출구\s*:\s*(.*?)\s*\]\s*$")
CANTGO_RE = re.compile(r"그쪽으로는 갈 수 없습니다|닫혀 있습니다|잠겨 있습니다")
# "당신은 쑥을 가지고 있지 않은것 같군요." — used an item we no longer carry; refresh 소지품.
MISSING_ITEM_RE = re.compile(r"가지고 있지 않은것 같군요")
HUH_RE = re.compile(r"^\s*뭐라구요\?")
INV_HEADER_RE = re.compile(r"^당신이 가지고 있는 물건\s*:")
EQUIP_HEADER_RE = re.compile(r"^당신이 사용하고 있는 물건\s*:")
# One 장비 line: "[슬롯]   이름 [c/m] ...설명".  ansi already stripped upstream.
EQUIP_LINE_RE = re.compile(r"^\[([^\]]+)\]\s+(.*)$")
EQUIP_DUR_RE = re.compile(r"\[(\d+)\s*/\s*(\d+)\]")


def parse_equip_line(line: str) -> dict | None:
    m = EQUIP_LINE_RE.match(line.strip())
    if not m:
        return None
    slot = m.group(1).strip()
    rest = m.group(2)
    dur = EQUIP_DUR_RE.search(rest)
    cur = mx = None
    name = rest
    if dur:
        cur, mx = int(dur.group(1)), int(dur.group(2))
        name = rest[:dur.start()]
    name = re.split(r"\.{2,}", name)[0].strip()   # drop trailing "...glow" text
    return {"slot": slot, "name": name, "cur": cur, "max": mx}
# The quotative particle is "라고" after a vowel-final word but "이라고" after a
# consonant-final one ("방비'라고" vs "힘!'이라고") — accept BOTH, or consonant-
# ending speech (and thus consonant-ending spell requests like 힘!) silently
# never parses.
SPEECH_RE = re.compile(r"^(.+?)[이가] '(.*)'(?:이)?라고 말합니다\.?\s*$")
# Directed at you / channel traffic. Best-effort patterns pending live
# confirmation of each receive format; replay flags anything they miss.
TELL_RES = [
    re.compile(r"^(.+?)님?[이가] 당신에게 '(.*)'(?:이)?라고 (?:말합니다|속삭입니다)"),
    re.compile(r"^(.+?)님?의 귓속말\s*:\s*(.*)$"),
    re.compile(r"^(.+?)[이가] 당신에게.*?'(.*)'"),
]
CHANNEL_RES = [
    ("잡담", re.compile(r"^(.+?)님?[이가] '(.*)'라고 잡담")),
    ("잡담", re.compile(r"^\[잡담\]\s*(.+?)\s*:\s*(.*)$")),
    ("외침", re.compile(r"^(.+?)님?[이가] '(.*)'라고 외(?:칩니다|쳤습니다)")),
    ("교신", re.compile(r"^(.+?)님?[이가] '(.*)'라고 교신")),
    ("교신", re.compile(r"^\[교신\]\s*(.+?)\s*:\s*(.*)$")),
]
COMM_ECHO_RE = re.compile(r"^당신[은이] '(.*)'라고 (잡담|외|교신|말)")
# Combat, from the captured 훈련장 fight. Verb whitelist keeps these from
# swallowing non-combat 당신은/당신을 sentences.
_HIT_VERBS = r"(?:때렸|쳤|찔렀|찔러|찌르|꿰뚫|베었|후려|강타|내리쳤|물었|물어뜯|할퀴|쑤셨|쑤시)"
COMBAT_DEALT_RE = re.compile(r"^당신[은이] (.+?)[를을] .*" + _HIT_VERBS)
COMBAT_TAKEN_RE = re.compile(r"^(.+?)[이가] 당신을 .*" + _HIT_VERBS)
ENEMY_CRIT_RE = re.compile(r"치명적인 상처를 입었습니다|회복불능입니다")
ENEMY_DOWN_RE = re.compile(r"쓰러[집졌]")
EXP_RE = re.compile(r"(\d+)의 경험치를 (?:나누어 )?받았습니다")
LOOT_RE = re.compile(r"시체에서 (\d+)원을 가집니다|(\d+)원을 나누어받았습니다")
PLAYER_DEAD_RE = re.compile(r"당신[은이] 죽었")
PLAYER_BLIND_RE = re.compile(r"당신의 눈이 멀었")
SIGHT_RESTORED_RE = re.compile(r"시력이 회복")   # "당신의 시력이 회복되었습니다!" — blindness cured
CONDITION_RE = re.compile(r"배가 고픕니다|목이 마릅니다")
# The inverse — the server telling us we're no longer hungry/thirsty — so the
# flags actually CLEAR (they used to latch True forever). "배가 부릅니다" /
# "배가 불러 더이상 마실 수 없습니다" = belly full (fed, and can't drink more);
# "목이 마르지 않습니다" = thirst quenched.
SATED_RE = re.compile(r"배가 부릅니다|배가 불러|목이 마르지 않")
# Buff land/fade confirmation is knowledge-driven now (webui scans [buff_signals]),
# so no per-buff regex lives here anymore.
CONSIDER_RE = re.compile(
    r"상대입니다|상대가 안|어림도 없|바늘로 싸워도|이기겠군요"
    r"|버거|힘겨|압도|죽음|감당|무리|훨씬 강|강합니다|강한 것")
# Difficulty phrases that mean "too strong — do NOT engage". Everything else a
# 고려 returns (쉬운/해볼만한/꼭 맞는/바늘로.../적수가 안...) is safe to fight.
CONSIDER_HARD_RE = re.compile(
    r"버거|어림도 없|죽음|압도|힘겨|감당|무리|훨씬 강|강합니다|강한 것")
ARRIVED_RE = re.compile(
    r"여기 도착했습니다"            # walked in on foot
    r"|여기 날아왔습니다"           # flew in (a flying character)
    r"|갑자기 나타났습니다"         # appeared out of nowhere — recall / teleport
    r"|다시 접속했습니다"           # reconnected in-room (back from inactivity)
    r"|쪽에서 (?:들어왔습니다|들어옵니다|왔습니다)")   # entered from a direction
# A partner leaving through an exit — WALKING ("…쪽으로 떠났습니다") or FLYING
# ("…쪽으로 날아갔습니다"). The flying form was uncaught, so a flying supporter's
# departure never cleared `together` and the leader mis-tracked co-location.
LEFT_RE = re.compile(r"[이가] .{0,4}쪽으로 (?:떠났|날아갔)습니다")
# Non-directional exits still remove an occupant from the room: recall/teleport
# ("… 사라졌습니다" / "… 귀환합니다"), or a logout ("… 고블린 머드를 떠났습니다").
# Catching these keeps the duo's co-location sighting from going stale when a
# partner leaves without walking through an exit.
VANISH_RE = re.compile(r"사라졌습니다|머드를 떠났습니다|귀환합니다")

# "병든 닭이 …" -> 닭, "중급 훈련생(2)이 …" -> 훈련생. Commands are
# object-first, so a keyword is enough to act on an entity. The LAST
# 이/가-marked noun wins: embedded clauses come first ("용과 기사의 싸움이
# 어우러진 석상이 …" -> 석상, not 싸움).
_TARGET_KW_RE = re.compile(r"(?:^|\s)(\S+?)(?:\(\d+\))?[이가]\s")
_KW_STOPWORDS = {"당신"}


def parse_map_neighbors(lines: list[str]) -> dict | None:
    """Extract the player's 4-neighbor room names from 지도 ASCII output.
    Cells sit in fixed ~13-char columns; names may wrap over two lines.
    Returns {'north':…, 'south':…, 'east':…, 'west':…} fragments or None."""
    CELL = 13

    def cell_text(li: int, col: int) -> str:
        parts = []
        for l2 in (li, li + 1):
            if 0 <= l2 < len(lines):
                seg = lines[l2][max(0, col - 1):col + CELL - 2]
                seg = re.sub(r"[\[\]_|]", " ", seg).strip()
                if seg and "당신" not in seg and "있는곳" not in seg:
                    parts.append(seg)
        return " ".join(parts)

    try:
        marker_line = next(i for i, l in enumerate(lines) if "당신이" in l)
    except StopIteration:
        return None
    col = lines[marker_line].find("[")
    if col < 0:
        return None
    east = cell_text(marker_line, col + CELL)
    west = cell_text(marker_line, col - CELL)
    # North/South: the nearest band is 3 lines away (name1/name2/connector).
    north = cell_text(marker_line - 3, col)
    south = cell_text(marker_line + 3, col)
    if not any((north, south, east, west)):
        return None
    return {"north": north, "south": south, "east": east, "west": west}


def target_keyword(entity: str) -> str | None:
    if entity.startswith("["):  # [수호] guardians and other tagged lines
        return None
    last = None
    for m in _TARGET_KW_RE.finditer(entity):
        if m.group(1) not in _KW_STOPWORDS:
            last = m.group(1)
    return last
FAREWELL_RE = re.compile(r"안녕히 가십시오")
# A perishable item decays in hand: "<item> [이/가] 당신의 손안에서 썩어 없어집니다".
# The name precedes the subject particle (attached "완장이" or spaced "장화 가"); it
# matches the 장비 equipped name, so the shopping list can clear it on re-equip.
# [^>] keeps a leaked prompt prefix ("2373:100:387> ") out of the captured name.
ITEM_ROT_RE = re.compile(
    r"([^>]+?)\s*[이가]\s*당신의\s*손안에서\s*썩어\s*없어집니다")
STATUS_FIELD_RE = re.compile(r"^\*\s")
STATUS_VITALS_RE = re.compile(
    r"체\s*력\s*:\s*\(\s*(\d+)/\s*(\d+)\)\s*"
    r"\*\s*마\s*법\s*:\s*\(\s*(\d+)/\s*(\d+)\)\s*"
    r"\*\s*이동력\s*:\s*\(\s*(\d+)/\s*(\d+)\)"
)
# Job can be MULTI-WORD (a dual class, e.g. "전사 검사") — capture everything up to
# "(레벨". A single-token (\S+) silently failed the WHOLE match on dual classes,
# leaving job AND level None (single-class chars parsed, warriors didn't).
STATUS_JOB_RE = re.compile(r"직\s*업\s*:\s*(.+?)\s*\(레벨\s*:\s*(\d+)\)")
STATUS_COIN_RE = re.compile(r"동\s*전\s*:\s*(\d+)")
STATUS_EXP_RE = re.compile(r"경험치\s*:\s*(\d+)\s*\*\s*남은 경험치\s*:\s*(\d+)")
TAGGED_RE = re.compile(r"^\[[^\]]+\]")          # [수호] ambient, [*] notices
WHO_ROW_RE = re.compile(r"^\[\s*\d+\s+\S+\s+\S+\]\s")
WHO_FOOT_RE = re.compile(r"명의 사용자가 있습니다|시스템 시동 이후")
# The player NAME is the token right after each "[<lv> <race> <class>]" bracket;
# a row carries two of them, e.g. "[200 거인 장군] 담신우   [200 요정 교황] 벽력자".
WHO_NAME_RE = re.compile(r"\[\s*\d+\s+\S+\s+\S+\s*\]\s*(\S+)")


class StreamParser:
    """Feed decoded text chunks; emits events via callback and labels every
    line with a category (the replay harness scores those labels)."""

    def __init__(self, on_event: Callable[[ev.Event], None]) -> None:
        self.on_event = on_event
        self._partial = ""          # unterminated tail of the stream
        self._mode = "login"        # login | none | room | room_entities | inventory
        self._room: dict | None = None
        self._map_lines: list[str] = []
        self._inv: list[str] | None = None
        self._equip: list[dict] | None = None
        self._status: ev.Status | None = None
        self.line_labels: list[tuple[str, str]] = []  # (category, line)

    # ---- feeding ---------------------------------------------------------

    def feed(self, text: str) -> None:
        text = strip_ansi(self._partial + text.replace("\r", ""))
        lines = text.split("\n")
        self._partial = lines.pop()  # last piece has no newline yet
        for line in lines:
            self._line(line)
        # A prompt is only ever an unterminated line — check the partial now.
        m = PROMPT_RE.match(self._partial)
        if m:
            self._line(self._partial)
            self._partial = ""

    def flush(self) -> None:
        if self._partial:
            self._line(self._partial)
            self._partial = ""
        self._end_block()

    # ---- line classification --------------------------------------------

    def _label(self, category: str, line: str) -> None:
        self.line_labels.append((category, line))

    def _line(self, line: str) -> None:
        stripped = line.strip()

        m = PROMPT_RE.match(line)
        if m:
            if self._mode == "map" and self._map_lines:
                from .mapgrid import parse_map_grid
                grid = parse_map_grid("\n".join(self._map_lines))
                fp = parse_map_neighbors(self._map_lines)
                self._map_lines = []
                if grid is not None:
                    self.on_event(ev.MapGrid(grid))
                if fp is not None:
                    self.on_event(ev.MapNeighbors(**fp))
            self._end_block()
            self._mode = "none"
            self._label("prompt", line)
            self.on_event(ev.Prompt(*map(int, m.groups())))
            return

        if self._mode == "login":
            self._label("login", line)
            return

        if self._mode == "map":  # 지도 output: ASCII map until the next prompt
            self._label("map", line)
            self._map_lines.append(line)
            return

        if not stripped:
            if self._mode in ("inventory", "equipment"):
                self._end_block()
            self._label("blank", line)
            return

        m = ROOM_TITLE_RE.match(line)
        if m:
            self._end_block()
            self._mode = "room"
            self._room = {"title": m.group(1), "desc": [], "exits": [], "entities": []}
            self._label("room.title", line)
            return

        if self._mode == "room":
            if DIVIDER_RE.match(stripped):
                self._label("room.divider", line)
                return
            m = EXITS_RE.match(line)
            if m:
                self._room["exits"] = m.group(1).split()
                self._mode = "room_entities"
                self._label("room.exits", line)
                return
            if TAGGED_RE.match(stripped):  # [*] notices inside descriptions
                self._label("room.notice", line)
                self._room["desc"].append(stripped)
                return
            self._room["desc"].append(stripped)
            self._label("room.desc", line)
            return

        if self._mode == "room_entities":
            self._room["entities"].append(stripped)
            self._label("room.entity", line)
            return

        if self._mode == "inventory":
            self._inv.append(stripped)
            self._label("inventory.item", line)
            return

        if self._mode == "equipment":
            item = parse_equip_line(stripped)
            if item is not None:
                self._equip.append(item)
            self._label("equipment.item", line)
            return

        if INV_HEADER_RE.match(stripped):
            self._end_block()
            self._mode = "inventory"
            self._inv = []
            self._label("inventory.header", line)
            return

        if EQUIP_HEADER_RE.match(stripped):
            self._end_block()
            self._mode = "equipment"
            self._equip = []
            self._label("equipment.header", line)
            return

        if ARRIVED_RE.search(stripped):
            self._label("movement", line)
            self.on_event(ev.EntityArrived(stripped))
            return
        if LEFT_RE.search(stripped) or VANISH_RE.search(stripped):
            self._label("movement", line)
            self.on_event(ev.EntityLeft(stripped))
            return
        if re.search(r"주문을 외웁니다|나아짐을 느낍니다", stripped):
            self._label("effect", line)
            return
        if "싸움의 대상이 없습니다" in stripped:
            self._label("error.notarget", line)
            self.on_event(ev.TargetGone())
            return
        m = re.match(r"^당신은 (.+?)[를을] 줍습니다", stripped)
        if m:
            self._label("item.taken", line)
            self.on_event(ev.ItemTaken(m.group(1)))
            return
        if re.search(r"없는 것 같군요", stripped):
            self._label("error.nothere", line)   # stale state, same as no-target
            self.on_event(ev.TargetGone())
            return
        if re.search(r"하기 위해서는 레벨", stripped):
            self._label("error.levelgate", line)
            return
        if CANTGO_RE.search(stripped):
            self._label("error.cantgo", line)
            self.on_event(ev.CantGo())
            return
        if MISSING_ITEM_RE.search(stripped):
            self._label("error.missing_item", line)
            self.on_event(ev.MissingItem())
            return
        if HUH_RE.match(stripped):
            self._label("error.huh", line)
            self.on_event(ev.Huh())
            return
        if FAREWELL_RE.search(stripped):
            self._label("farewell", line)
            self.on_event(ev.Farewell())
            return
        m = ITEM_ROT_RE.search(stripped)
        if m:
            self._label("item.rotted", line)
            self.on_event(ev.ItemRotted(m.group(1).strip()))
            return

        # 점수 sheet fields ("* 체  력 : …") before anything colon-shaped —
        # the NPC-speech pattern would otherwise swallow them as messages.
        if STATUS_FIELD_RE.match(stripped):
            self._status_line(stripped)
            self._label("status.field", line)
            return

        # Combat next: these lines arrive every round and must never be
        # mistaken for anything else.
        if PLAYER_DEAD_RE.search(stripped):
            self._label("combat.death", line)
            self.on_event(ev.PlayerDead())
            return
        if PLAYER_BLIND_RE.search(stripped):
            self._label("combat.blind", line)
            self.on_event(ev.PlayerBlinded())
            return
        if SIGHT_RESTORED_RE.search(stripped):
            self._label("combat.sight_restored", line)
            self.on_event(ev.SightRestored())
            return
        m = EXP_RE.search(stripped)
        if m:
            self._label("combat.exp", line)
            self.on_event(ev.ExpGain(int(m.group(1))))
            return
        m = LOOT_RE.search(stripped)
        if m:
            self._label("combat.loot", line)
            self.on_event(ev.Loot(int(m.group(1) or m.group(2))))
            return
        if ENEMY_CRIT_RE.search(stripped):
            self._label("combat.crit", line)
            self.on_event(ev.EnemyCritical())
            return
        if ENEMY_DOWN_RE.search(stripped) and ("피를" in stripped or "숨" in stripped
                                               or COMBAT_DEALT_RE.match(stripped)):
            self._label("combat.down", line)
            self.on_event(ev.EnemyDown())
            return
        if SATED_RE.search(stripped):
            self._label("condition", line)
            if "목이 마르지" in stripped:
                self.on_event(ev.Condition("quenched"))     # thirst gone
            elif "배가 불러" in stripped:
                self.on_event(ev.Condition("full"))         # belly full: hunger+thirst
            else:                                            # 배가 부릅니다
                self.on_event(ev.Condition("fed"))          # hunger gone
            return
        if CONDITION_RE.search(stripped):
            kind = "hungry" if "배가" in stripped else "thirsty"
            self._label("condition", line)
            self.on_event(ev.Condition(kind))
            return
        m = COMBAT_TAKEN_RE.match(stripped)
        if m:
            self._label("combat.taken", line)
            self.on_event(ev.CombatHit("taken", m.group(1), stripped))
            return
        m = COMBAT_DEALT_RE.match(stripped)
        if m:
            self._label("combat.dealt", line)
            self.on_event(ev.CombatHit("dealt", m.group(1), stripped))
            return
        if CONSIDER_RE.search(stripped):
            self._label("combat.consider", line)
            verdict = "hard" if CONSIDER_HARD_RE.search(stripped) else "easy"
            self.on_event(ev.Consider(verdict, stripped))
            return

        if COMM_ECHO_RE.match(stripped):
            self._label("comm.echo", line)  # our own message echoed back
            return
        for tell_re in TELL_RES:
            m = tell_re.match(stripped)
            if m:
                self._label("comm.tell", line)
                self.on_event(ev.Tell(m.group(1), m.group(2)))
                return
        for channel, chan_re in CHANNEL_RES:
            m = chan_re.match(stripped)
            if m:
                self._label(f"comm.{channel}", line)
                self.on_event(ev.ChannelMessage(channel, m.group(1), m.group(2)))
                return

        m = SPEECH_RE.match(stripped)
        if m:
            self._label("speech", line)
            self.on_event(ev.Speech(m.group(1), m.group(2)))
            return
        m = re.match(r"^([^*=\-\s].{0,24}?)\s:\s(.+)$", stripped)  # NPC "이름 : 대사"
        if m:
            self._label("speech", line)
            self.on_event(ev.Speech(m.group(1), m.group(2)))
            return
        if stripped == "완료.":
            self._label("system", line)
            return
        if "죽어가며" in stripped:
            self._label("combat.flavor", line)
            return

        if DIVIDER_RE.match(stripped):
            self._label("divider", line)
            return

        if WHO_ROW_RE.match(stripped) or WHO_FOOT_RE.search(stripped) or stripped == "사용자":
            self._label("who", line)
            # Extract the connected players from this COMPLETE roster line
            # "[<lv> <race> <class>] <name>" (two per line) so the hunter never
            # attacks a player. Done here, on a whole line, so a TCP chunk split
            # mid-name can never capture a partial ("담" instead of "담신우").
            # "누군가" is the placeholder for an invisible player — not a name.
            names = [n for n in WHO_NAME_RE.findall(stripped) if n != "누군가"]
            if names:
                self.on_event(ev.Roster(names))
            return

        if stripped.startswith("[계속(리턴)") or "RETURN, Q" in stripped:
            self._label("pager", line)
            return
        if re.match(r"^[▲▶●◆■]", stripped):
            self._label("notice", line)
            return
        if TAGGED_RE.match(stripped):
            self._label("ambient", line)
            return

        if re.match(r"^>{2,}.*<{2,}$", stripped) or "축하합니다" in stripped:
            self._label("announce", line)
            return
        if "자료를 저장합니다" in stripped:
            self._label("system.save", line)
            return
        if re.search(r"동일한 아이디로 접속|다중접속", stripped):
            self._label("system.session", line)
            return
        m = re.match(r'^-+\s*여기는\s*"?(.+?)"?\s*입니다\.?\s*-+$', stripped)
        if m:
            self._mode = "map"  # zone header opens the 지도 ASCII map
            self._map_lines = [line]  # keep the header so the grid parser gets the zone
            self._label("zone", line)
            self.on_event(ev.ZoneInfo(m.group(1)))
            return
        if stripped.startswith(".o00~"):
            self._label("announce", line)
            return
        if re.search(r"땅거미|밤이 시작|아침이 밝|해가 (뜨|지)|날이 (밝|저물)", stripped):
            self._label("world.time", line)
            return

        # Own posture changes (sleep/wake/stand — exact server phrasings are
        # best-effort until seen live; refine from logs).
        if stripped.startswith("당신"):
            if re.search(r"잠에서 깨", stripped):
                self._label("posture", line)
                self.on_event(ev.Posture("sitting"))
                return
            if re.search(r"잠[이을에]|잠자리", stripped):
                self._label("posture", line)
                self.on_event(ev.Posture("sleeping"))
                return
            if re.search(r"일어[서섰섭납]", stripped):
                self._label("posture", line)
                self.on_event(ev.Posture("standing"))
                return
            if re.search(r"앉[았습]", stripped):
                self._label("posture", line)
                self.on_event(ev.Posture("sitting"))
                return

        # Known standalone messages around the 상태 sheet and login flow.
        if re.search(r"당신은 .*이용하셨습니다|생일입니다|당신은 (서|앉아|누워) 있습니다", stripped):
            self._label("status.tail", line)
            return
        if re.search(r"잠시 기다리십시오", stripped):
            self._label("system", line)
            return

        self._label("unknown", line)

    # ---- block completion ------------------------------------------------

    def _status_line(self, line: str) -> None:
        if self._status is None:
            self._status = ev.Status()
        s = self._status
        m = STATUS_VITALS_RE.search(line)
        if m:
            g = list(map(int, m.groups()))
            s.hp, s.mp, s.mv = (g[0], g[1]), (g[2], g[3]), (g[4], g[5])
        m = STATUS_JOB_RE.search(line)
        if m:
            s.job, s.level = m.group(1), int(m.group(2))
        m = STATUS_COIN_RE.search(line)
        if m:
            s.coins = int(m.group(1))
        m = STATUS_EXP_RE.search(line)
        if m:
            s.exp, s.exp_to_level = int(m.group(1)), int(m.group(2))

    def _end_block(self) -> None:
        # Info pages (도움, 정책) reuse the <*> title <*> header; only a block
        # that reached an exits line is a real room.
        if self._room is not None:
            if self._room["exits"]:
                self.on_event(ev.RoomSeen(
                    title=self._room["title"],
                    description=" ".join(self._room["desc"]),
                    exits=self._room["exits"],
                    entities=[e for e in self._room["entities"] if e],
                ))
            self._room = None
        if self._inv is not None:
            self.on_event(ev.Inventory([i for i in self._inv if i]))
            self._inv = None
        if self._equip is not None:
            self.on_event(ev.Equipment(self._equip))
            self._equip = None
        if self._status is not None:
            self.on_event(self._status)
            self._status = None
        if self._mode != "login":
            self._mode = "none"
