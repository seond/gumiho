"""Typed events emitted by the stream parser."""

from dataclasses import dataclass, field


@dataclass
class Event:
    pass


@dataclass
class Prompt(Event):
    """The `HP:MP:MV> ` command prompt — also the 'server is ready' signal."""
    hp: int
    mp: int
    mv: int


@dataclass
class RoomSeen(Event):
    title: str
    description: str
    exits: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)


@dataclass
class CantGo(Event):
    pass


@dataclass
class MissingItem(Event):
    """'당신은 X을 가지고 있지 않은것 같군요.' — we tried to use an item we no longer
    have (e.g. drinking the last 쑥). Our tracked inventory is stale; refresh it."""
    pass


@dataclass
class Huh(Event):
    """뭐라구요? — the server did not understand the command."""
    pass


@dataclass
class Inventory(Event):
    items: list[str] = field(default_factory=list)


@dataclass
class Speech(Event):
    speaker: str
    text: str


@dataclass
class Roster(Event):
    """Connected-player names from one 누구 row (excludes 누군가 placeholder)."""
    names: list


@dataclass
class Tell(Event):
    """A message directed at you (당신에게 / 귓속말)."""
    speaker: str
    text: str


@dataclass
class ChannelMessage(Event):
    """Broadcast channel traffic: 잡담, 외침, 교신, ..."""
    channel: str
    speaker: str
    text: str


@dataclass
class Status(Event):
    """Parsed fields from the 상태 sheet (only what we currently use)."""
    hp: tuple[int, int] | None = None       # (current, max)
    mp: tuple[int, int] | None = None
    mv: tuple[int, int] | None = None
    level: int | None = None
    job: str | None = None
    coins: int | None = None
    exp: int | None = None
    exp_to_level: int | None = None


@dataclass
class Farewell(Event):
    pass


@dataclass
class ItemRotted(Event):
    """A perishable item decayed away in hand ("<item> 당신의 손안에서 썩어
    없어집니다"). The shopping list re-adds it until it's re-equipped (장비)."""
    name: str


@dataclass
class CombatHit(Event):
    """One combat line. direction: 'dealt' (당신은 X를 ...) or 'taken'."""
    direction: str
    other: str
    line: str


@dataclass
class EnemyCritical(Event):
    pass


@dataclass
class EnemyDown(Event):
    pass


@dataclass
class ExpGain(Event):
    amount: int


@dataclass
class Loot(Event):
    coins: int


@dataclass
class PlayerDead(Event):
    pass


@dataclass
class PlayerBlinded(Event):
    """The player was BLINDED ("당신의 눈이 멀었습니다!") — forces the blind-recovery workflow."""
    pass


@dataclass
class SightRestored(Event):
    """Blindness was CURED ("당신의 시력이 회복되었습니다!") — clears state.blind so the blind
    workflow completes on the real confirmation, not an optimistic timer."""
    pass


@dataclass
class Condition(Event):
    """Body-state message: hungry / thirsty."""
    kind: str




@dataclass
class EntityArrived(Event):
    text: str


@dataclass
class EntityLeft(Event):
    text: str


@dataclass
class ZoneInfo(Event):
    """지도 header: --- 여기는 "한성"입니다. ---"""
    name: str


@dataclass
class Posture(Event):
    """Own body posture change: sleeping | sitting | standing."""
    kind: str


@dataclass
class MapNeighbors(Event):
    """4-neighbor room-name fragments parsed from the 지도 minimap —
    a positional fingerprint that distinguishes clone rooms."""
    north: str
    south: str
    east: str
    west: str


@dataclass
class MapGrid(Event):
    """Full parsed 지도 local map — a gumiho.mapgrid.MapGrid with the drawn
    rooms, the player's cell, and the connections between them."""
    grid: object


@dataclass
class TargetGone(Event):
    """싸움의 대상이 없습니다 — the target left/died; our state is stale."""
    pass


@dataclass
class Consider(Event):
    """Result of a 고려 (assess) — verdict is 'easy' (safe to fight), 'hard'
    (too strong; avoid), or 'unknown'. text is the raw server phrase."""
    verdict: str
    text: str


@dataclass
class ItemTaken(Event):
    """당신은 X를 줍습니다."""
    name: str


@dataclass
class Equipment(Event):
    """Parsed 장비 (사용하고 있는 물건) list. Each item: {slot, name, cur, max}
    — cur/max are None when the server shows no durability for that slot."""
    items: list
