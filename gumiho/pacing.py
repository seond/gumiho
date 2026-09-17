"""Paced command sending — a human-like minimum gap between outgoing commands.

Extracted from the retired reflex layer; the engine sends everything through a
PacedSender so an accidental back-to-back burst is throttled without adding delay
to the normal one-command-per-prompt cadence (the server round-trip is the pace).
"""

import asyncio
import random
import time
from typing import Awaitable, Callable


class PacedSender:
    """Serializes outgoing commands with a small human-like floor between sends."""

    def __init__(self, send_fn: Callable[[str], Awaitable[None]]) -> None:
        self._send_fn = send_fn
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        while not self._queue.empty():
            self._queue.get_nowait()

    def push(self, line: str) -> None:
        self._queue.put_nowait(line)

    async def _run(self) -> None:
        # Rate-limit, don't pre-delay: only wait as much as is needed to keep a
        # small minimum gap since the LAST send. In the normal one-command-per-
        # prompt case the server round-trip already exceeds this, so commands go
        # out with no added delay (gapless); the tiny floor only throttles an
        # accidental back-to-back burst.
        last = 0.0
        while True:
            line = await self._queue.get()
            # Human minimum gap between sends. Raised from 0.05-0.15 to ~0.25s to slow the
            # overall cadence ~20% and, importantly, to throttle same-prompt BURSTS (combat
            # 연타+봐, or any back-to-back) to a deliberate human rhythm instead of firing them
            # in one packet. For the normal one-command-per-prompt roam the server round-trip
            # usually exceeds this, so it adds little there; the roam's own move-confirm wait
            # ([hunt].move_confirm_wait/move_gap) is what paces navigation.
            gap = random.uniform(0.20, 0.30)
            wait = gap - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            await self._send_fn(line)
            last = time.monotonic()
