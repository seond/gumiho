"""Offline planner evaluation: real-shaped scenarios, real LLM, no server.

Each scenario is a WorldState snapshot; the planner builds its actual prompt,
qwen3 answers, and the decision is checked against an acceptable-action set.
Usage: .venv/bin/python scripts/planner_eval.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho.bestiary import Bestiary
from gumiho.config import ROOT, load_config
from gumiho.llm import OllamaClient
from gumiho.mapper import Mapper
from gumiho.planner import Planner
from gumiho.state import Vitals, WorldState


class FakeWalker:
    mode = "idle"
    zone = None


class FakeSession:
    """Just enough session surface for Planner.build_prompt/execute."""

    def __init__(self, state, mapper, bestiary):
        self.state = state
        self.mapper = mapper
        self.bestiary = bestiary
        self.walker = FakeWalker()
        self.status = "online"
        self.sent: list[str] = []
        self.logger = None

    async def send(self, line):
        self.sent.append(line)

    def broadcast(self, msg):
        pass


def make_state(**kw) -> WorldState:
    st = WorldState()
    st.vitals = Vitals(hp=36, mp=100, mv=80, hp_max=36, mp_max=100, mv_max=88)
    st.level, st.job, st.coins, st.exp_remaining = 2, "전사", 2200, 380
    st.room_title = "훈련장의 테두리"
    st.zone = "훈련장"
    st.exits = ["북", "동", "남", "서"]
    st.inventory = ["고블린티셔츠", "구리단검", "호롱등", "성수 (2)"]
    for k, v in kw.items():
        setattr(st, k, v)
    return st


SCENARIOS = [
    ("healthy, weak mobs present, needs XP",
     make_state(entities=["벌이 당신을 향해 날아든다.",
                          "기형적으로 커다란 거미가 기어가고 있다."]),
     {"attack", "hunt", "consider"}),
    ("empty room, healthy — should make progress",
     make_state(entities=[]),
     {"explore", "hunt", "move", "look"}),
    ("HP 30%, out of battle — must recover",
     make_state(entities=[], vitals=Vitals(hp=11, mp=100, mv=80, hp_max=36,
                                           mp_max=100, mv_max=88)),
     {"recover", "use_potion"}),
    ("item on the ground worth taking",
     make_state(entities=["훈련생바지가 땅에 떨어져 있다. (바지 가져)"]),
     {"take", "explore", "hunt", "move"}),
    ("operator advice pending — must follow it",
     make_state(entities=["벌이 당신을 향해 날아든다."]),
     {"move", "look"}),   # advice says go south, do not fight
    ("HP 45%, mob present — heal before fighting",
     make_state(entities=["벌이 당신을 향해 날아든다."],
                vitals=Vitals(hp=16, mp=100, mv=80, hp_max=36,
                              mp_max=100, mv_max=88)),
     {"use_potion", "recover"}),
]


async def main() -> None:
    llm_cfg = load_config().get("llm", {})
    llm = OllamaClient(host=llm_cfg.get("host", "http://127.0.0.1:11434"),
                       model=llm_cfg.get("model", "qwen3:8b"))
    with tempfile.TemporaryDirectory() as td:
        mapper = Mapper(Path(td) / "m.sqlite")
        bestiary = Bestiary(Path(td) / "b.sqlite")
        bestiary.mark("벌", True, "auto", zone="훈련장")
        bestiary.mark("거미", True, "auto", zone="훈련장")
        for kw in ("벌", "거미", "개"):
            mapper.db.execute(
                "INSERT INTO zone_mobs (zone, keyword, count) VALUES ('훈련장', ?, 5)",
                (kw,))
        mapper.db.commit()

        passed = 0
        for name, state, acceptable in SCENARIOS:
            session = FakeSession(state, mapper, bestiary)
            planner = Planner(session, llm, config=llm_cfg)
            if "advice" in name:
                planner.pending_advice.append(
                    "지금은 싸우지 말고 남쪽으로 이동해라")
            user_prompt, ctx = planner.build_prompt()
            reply = await llm.chat(
                __import__("gumiho.planner", fromlist=["SYSTEM_PROMPT"]).SYSTEM_PROMPT,
                user_prompt)
            from gumiho.llm import extract_json
            try:
                d = extract_json(reply)
            except Exception as e:
                print(f"✗ {name}: unparseable reply ({e})")
                continue
            action = d.get("action")
            ok = action in acceptable
            passed += ok
            mark = "✓" if ok else "✗"
            print(f"{mark} {name}\n   → {json.dumps(d, ensure_ascii=False)}"
                  f"\n   (acceptable: {sorted(acceptable)})")
        print(f"\n{passed}/{len(SCENARIOS)} scenarios acceptable "
              f"({100 * passed // len(SCENARIOS)}%)")


if __name__ == "__main__":
    asyncio.run(main())
