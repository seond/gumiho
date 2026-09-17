"""Live Phase 3 integration test: the full pipeline runs one supervised fight.

parser -> WorldState -> ReflexEngine -> PacedSender, exactly as in the GUI.
The script only navigates and starts the fight; from then on it observes.

Usage: .venv/bin/python scripts/reflex_test.py
"""

import asyncio
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import events as ev
from gumiho.client import MudConnection
from gumiho.config import ROOT, load_config, load_env
from gumiho.expect import TextWatcher
from gumiho.login import LoginError, auto_login
from gumiho.parser import StreamParser
from gumiho.reflex import PacedSender, ReflexEngine
from gumiho.session_log import SessionLogger
from gumiho.state import WorldState

ROUTE = ["남", "아래", "서", "훈련장 열어", "서"]
PROBES = [("남", "북"), ("서", "동"), ("동", "서"), ("북", "남")]
TARGETS = ["벌", "거미", "개"]


def pick_target(text: str) -> str | None:
    for kw in TARGETS:
        if re.search(kw + r"[이가 ]", text):
            return kw
    return None


async def main() -> None:
    env = load_env()
    name, password = env.get("GUMIHO_NAME"), env.get("GUMIHO_PASSWORD")
    logger = SessionLogger(ROOT / "logs")
    state = WorldState()
    done = asyncio.Event()
    outcome = {"result": "timeout"}

    watcher = TextWatcher(tee=lambda t: (sys.stdout.write(t), sys.stdout.flush()))

    def on_event(event: ev.Event) -> None:
        state.apply(event)
        reflex.on_event(event)
        match event:
            case ev.EnemyDown():
                outcome["result"] = "victory"
                done.set()
            case ev.PlayerDead():
                outcome["result"] = "death"
                done.set()
            case ev.ExpGain(amount=n):
                print(f"\n[pipeline] ExpGain({n})")
            case ev.Loot(coins=n):
                print(f"\n[pipeline] Loot({n})")
            case _:
                pass

    parser = StreamParser(on_event)
    conn = MudConnection(
        "ggai.tv", 4000, logger,
        on_text=lambda t: (watcher.on_text(t), parser.feed(t)),
    )
    sender = PacedSender(conn.send_line)
    reflex = ReflexEngine(
        state, sender,
        on_fire=lambda r, c: print(f"\n[reflex] {r} -> {c}"),
        config=load_config().get("reflex", {}),
    )

    await conn.connect()
    read_task = asyncio.create_task(conn.read_loop())
    sender.start()

    async def paced(line: str) -> str:
        await asyncio.sleep(random.uniform(0.7, 1.3))
        mark_buffer = watcher.buffer
        watcher.clear()
        await conn.send_line(line)
        await watcher.settle(idle=1.5, cap=8)
        return watcher.buffer

    try:
        await auto_login(conn, watcher, name, password)
        await watcher.settle(idle=2.0, cap=8)
        for step in ROUTE:
            await paced(step)

        out = await paced("봐")
        target = pick_target(out)
        if target is None:
            for go, back in PROBES:
                out = await paced(go)
                target = pick_target(out)
                if target:
                    break
                await paced(back)
        if target is None:
            print("\n[test] no target found")
            await paced("끝")
            return

        print(f"\n[test] engaging {target}; pipeline takes over")
        await paced(f"{target} 공격")
        try:
            await asyncio.wait_for(done.wait(), timeout=90)
        except asyncio.TimeoutError:
            pass
        print(f"\n[test] outcome: {outcome['result']}, "
              f"in_battle={state.in_battle}, hp={state.vitals.hp}, "
              f"exp+{state.exp_gained}, coins+{state.coins_looted}")
        await asyncio.sleep(2)
        await paced("끝")
        await watcher.expect_any({"bye": r"안녕히 가십시오"}, timeout=6)
    except LoginError as e:
        print(f"\n[test] login failed: {e}")
    finally:
        sender.stop()
        read_task.cancel()
        await conn.close()
        logger.close()
        print(f"\n--- reflex test done: {logger.raw_path.name} ---")


if __name__ == "__main__":
    asyncio.run(main())
