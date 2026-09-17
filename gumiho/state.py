"""WorldState: the structured snapshot the engine and its behavior read."""

from dataclasses import dataclass, field

from . import events as ev
from .parser import target_keyword


@dataclass
class Vitals:
    hp: int = 0
    mp: int = 0
    mv: int = 0
    hp_max: int | None = None
    mp_max: int | None = None
    mv_max: int | None = None


@dataclass
class WorldState:
    vitals: Vitals = field(default_factory=Vitals)
    room_title: str | None = None
    room_description: str = ""
    exits: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    inventory: list[str] = field(default_factory=list)
    level: int | None = None
    job: str | None = None
    coins: int | None = None
    exp_remaining: int | None = None   # 남은 경험치 — XP still needed to level
    at_prompt: bool = False      # a prompt was the most recent thing seen
    last_move_blocked: bool = False
    zone: str | None = None
    zone_hostile: bool = False
    posture: str = "standing"    # standing | sitting | sleeping
    in_battle: bool = False
    battle_opponent: str | None = None
    hungry: bool = False
    thirsty: bool = False
    bottle_dry: bool = False
    exp_gained: int = 0
    coins_looted: int = 0
    dead: bool = False
    blind: bool = False

    def _remove_entity(self, keyword: str) -> None:
        """Drop the first listed entity whose keyword matches."""
        for i, entity in enumerate(self.entities):
            if target_keyword(entity) == keyword:
                del self.entities[i]
                return

    def apply(self, event: ev.Event) -> None:
        self.at_prompt = False
        # A latched death clears the instant we see a signal only a LIVING character
        # can produce — exp, a kill, or landing a hit. This unsticks a death that was
        # handled MANUALLY (otherwise `dead` stayed set and the arbiter force-entered
        # the death workflow forever). The death workflow's own revive also clears it.
        if self.dead and isinstance(event, (ev.ExpGain, ev.EnemyDown, ev.CombatHit)):
            self.dead = False
        match event:
            case ev.CombatHit(other=other):
                self.in_battle = True
                self.battle_opponent = other
            case ev.EnemyDown():
                if self.battle_opponent:
                    self._remove_entity(self.battle_opponent)
                self.in_battle = False
                self.battle_opponent = None
            case ev.Farewell():
                self.in_battle = False
                self.battle_opponent = None
            case ev.EntityArrived(text=text):
                if text not in self.entities:
                    self.entities.append(text)
            case ev.EntityLeft(text=text):
                kw = target_keyword(text)
                if kw:
                    self._remove_entity(kw)
            case ev.ItemTaken(name=name):
                for i, entity in enumerate(self.entities):
                    kw = target_keyword(entity)
                    if kw and kw in name:
                        del self.entities[i]
                        break
            case ev.ExpGain(amount=n):
                self.exp_gained += n
                if self.exp_remaining is not None:
                    self.exp_remaining = max(0, self.exp_remaining - n)
                # Every kill emits exp — a reliable battle-end signal even when the
                # death line isn't the one the EnemyDown regex matches (잿더미…).
                self.in_battle = False
                self.battle_opponent = None
            case ev.Loot(coins=n):
                self.coins_looted += n
            case ev.PlayerDead():
                self.dead = True
                self.in_battle = False
            case ev.PlayerBlinded():
                self.blind = True
            case ev.SightRestored():
                self.blind = False        # cured — the real confirmation, not an optimistic timer
            case ev.Condition(kind=kind):
                if kind == "hungry":
                    self.hungry = True
                elif kind == "thirsty":
                    self.thirsty = True
                elif kind == "fed":            # 배가 부릅니다
                    self.hungry = False
                elif kind == "quenched":       # 목이 마르지 않습니다
                    self.thirsty = False
                elif kind == "full":           # 배가 불러 더이상 마실 수 없습니다
                    self.hungry = False
                    self.thirsty = False
            case ev.Posture(kind=kind):
                self.posture = kind
            case _:
                pass
        match event:
            case ev.Prompt(hp=hp, mp=mp, mv=mv):
                self.vitals.hp, self.vitals.mp, self.vitals.mv = hp, mp, mv
                self.at_prompt = True
            case ev.RoomSeen() as r:
                self.room_title = r.title
                self.room_description = r.description
                self.exits = r.exits
                self.entities = r.entities
                self.last_move_blocked = False
            case ev.CantGo():
                self.last_move_blocked = True
            case ev.Inventory(items=items):
                self.inventory = items
            case ev.Status() as s:
                # 점수 carries CURRENT and max — used to poll vitals while asleep
                # (no prompt is pushed during recovery).
                if s.hp:
                    self.vitals.hp, self.vitals.hp_max = s.hp
                if s.mp:
                    self.vitals.mp, self.vitals.mp_max = s.mp
                if s.mv:
                    self.vitals.mv, self.vitals.mv_max = s.mv
                self.level, self.job, self.coins = s.level, s.job, s.coins
                if s.exp_to_level is not None:
                    self.exp_remaining = s.exp_to_level
            case _:
                pass
