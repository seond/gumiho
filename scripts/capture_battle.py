"""Supervised test fight in the 훈련장 to capture battle formats.

Route: lake -> 갈림길 -> 아래(한성) -> 훈련장 -> statue room, then search
adjacent rooms for a 초급 훈련생 (beginner trainee). 고려 first for the
record, engage with 공격, hard safety: HP < FLEE_AT -> 도망.

Usage: .venv/bin/python scripts/capture_battle.py
"""

import asyncio
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho.client import MudConnection
from gumiho.config import ROOT, load_env
from gumiho.expect import TextWatcher
from gumiho.login import LoginError, auto_login
from gumiho.session_log import SessionLogger

PROMPT_RE = re.compile(r"(\d+):(\d+):(\d+)>")
FLEE_AT = 12
ROUTE = ["남", "아래", "서", "훈련장 열어", "서"]
# From the statue room: probe each direction and come straight back.
PROBES = [("남", "북"), ("서", "동"), ("동", "서"), ("북", "남")]
# Weak wandering critters; commands are object-first: "<대상> 공격".
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
    transcript: list[str] = []

    def tee(t: str) -> None:
        sys.stdout.write(t)
        sys.stdout.flush()
        transcript.append(t)

    watcher = TextWatcher(tee=tee)
    conn = MudConnection("ggai.tv", 4000, logger, on_text=watcher.on_text)
    await conn.connect()
    read_task = asyncio.create_task(conn.read_loop())

    def hp_now() -> int | None:
        tail = "".join(transcript[-30:])
        m = None
        for m in PROMPT_RE.finditer(tail):
            pass
        return int(m.group(1)) if m else None

    async def paced(line: str) -> str:
        await asyncio.sleep(random.uniform(0.7, 1.3))
        mark = len(transcript)
        watcher.clear()
        await conn.send_line(line)
        await watcher.settle(idle=1.5, cap=8)
        return "".join(transcript[mark:])

    try:
        await auto_login(conn, watcher, name, password)
        print("\n[battle] in game — walking to 훈련장")
        await watcher.settle(idle=2.0, cap=8)
        for step in ROUTE:
            await paced(step)

        out = await paced("봐")
        target = pick_target(out)
        if target is None:
            for go, back in PROBES:
                out = await paced(go)
                target = pick_target(out)
                if target is not None:
                    break
                await paced(back)

        if target is None:
            print("\n[battle] no weak critter found — quitting")
            await paced("끝")
            return

        print(f"\n[battle] target: {target} — 고려 then 공격")
        await paced(f"{target} 고려")
        await paced(f"{target} 공격")

        for round_no in range(60):
            await watcher.settle(idle=1.0, cap=5)
            recent = "".join(transcript[-80:])
            hp = hp_now()
            if re.search(r"당신은 죽었|당신이 죽었", recent):
                print("\n[battle] we died — capturing death flow")
                await watcher.settle(idle=2.5, cap=15)
                break
            if re.search(r"죽었습니다|쓰러졌|경험치", recent):
                print(f"\n[battle] enemy down (round {round_no})")
                break
            if hp is not None and hp < FLEE_AT:
                print(f"\n[battle] hp {hp} < {FLEE_AT} — 도망")
                await paced("도망")
                await watcher.settle(idle=2.0, cap=8)
                break

        await paced("봐")
        await paced("점수")
        await paced("끝")
        await watcher.expect_any({"bye": r"안녕히 가십시오"}, timeout=6)
    except LoginError as e:
        print(f"\n[battle] login failed: {e}")
    finally:
        read_task.cancel()
        await conn.close()
        logger.close()
        print(f"\n--- battle capture done: {logger.raw_path.name} ---")


if __name__ == "__main__":
    asyncio.run(main())
