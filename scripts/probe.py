"""Connect, capture the server banner for a few seconds, print it, exit.

Validates telnet negotiation and EUC-KR decoding against the live server
without an interactive session. Usage: python3 scripts/probe.py [seconds]
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho.client import MudConnection
from gumiho.session_log import SessionLogger


async def main(duration: float) -> None:
    root = Path(__file__).resolve().parent.parent
    logger = SessionLogger(root / "logs")
    marks = 0

    def on_mark() -> None:
        nonlocal marks
        marks += 1

    conn = MudConnection(
        "ggai.tv", 4000, logger,
        on_text=lambda t: (sys.stdout.write(t), sys.stdout.flush()),
        on_prompt_mark=on_mark,
    )
    await conn.connect()
    try:
        await asyncio.wait_for(conn.read_loop(), timeout=duration)
    except asyncio.TimeoutError:
        pass
    await conn.close()
    logger.close()
    print(f"\n--- probe done: {marks} GA/EOR prompt marks, "
          f"raw log: {logger.raw_path.name} ---")


if __name__ == "__main__":
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    asyncio.run(main(secs))
