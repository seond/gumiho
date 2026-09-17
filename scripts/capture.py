"""Scripted login + command tour to generate parser fixture logs.

Logs in via gumiho.login, runs the given commands (or a default cautious
tour), quits with 끝, and logs everything.
Usage: python3 scripts/capture.py [command ...]
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

DEFAULT_TOUR = [
    "봐", "소지품", "지도", "도움",
    "동", "봐", "서", "남", "봐", "북",
    "누구",
]


async def main(tour: list[str]) -> None:
    env = load_env()
    name, password = env.get("GUMIHO_NAME"), env.get("GUMIHO_PASSWORD")
    if not name or not password:
        sys.exit("GUMIHO_NAME / GUMIHO_PASSWORD missing from .env")

    logger = SessionLogger(ROOT / "logs")
    watcher = TextWatcher(tee=lambda t: (sys.stdout.write(t), sys.stdout.flush()))
    conn = MudConnection("ggai.tv", 4000, logger, on_text=watcher.on_text)
    await conn.connect()
    read_task = asyncio.create_task(conn.read_loop())

    async def paced_send(line: str) -> None:
        await asyncio.sleep(random.uniform(0.6, 1.4))
        watcher.clear()
        await conn.send_line(line)

    try:
        result = await auto_login(conn, watcher, name, password)
        print(f"\n[capture] logged in ({result})")
        await watcher.settle(idle=2.0, cap=10)

        async def drain_output() -> None:
            """Settle, pressing return through any pager pages."""
            for _ in range(30):
                await watcher.settle(idle=1.8, cap=10)
                if re.search(r"\[계속\(리턴\)", watcher.buffer):
                    watcher.clear()
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                    await conn.send_line("")
                else:
                    return

        for cmd in tour:
            await paced_send(cmd)
            await drain_output()

        await paced_send("끝")
        await watcher.expect_any({"bye": r"안녕히 가십시오"}, timeout=5)
    except LoginError as e:
        print(f"\n[capture] login failed: {e}")
    finally:
        read_task.cancel()
        await conn.close()
        logger.close()
        print(f"\n--- capture done: {logger.raw_path.name} ---")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or DEFAULT_TOUR))
