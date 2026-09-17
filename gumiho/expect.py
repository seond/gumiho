"""TextWatcher: expect-style matching over the decoded output stream."""

import asyncio
import re
from typing import Callable, Optional


class TextWatcher:
    def __init__(self, tee: Optional[Callable[[str], None]] = None) -> None:
        self.buffer = ""
        self._tee = tee
        self._got_text = asyncio.Event()

    def on_text(self, text: str) -> None:
        if self._tee is not None:
            self._tee(text)
        self.buffer += text
        self._got_text.set()

    def clear(self) -> None:
        self.buffer = ""

    async def expect_any(self, patterns: dict[str, str], timeout: float) -> Optional[str]:
        """Wait until any pattern matches; returns its key, or None on timeout.
        Matched text (and everything before it) is consumed from the buffer,
        so the same banner can't match twice."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for key, pattern in patterns.items():
                m = re.search(pattern, self.buffer)
                if m:
                    self.buffer = self.buffer[m.end():]
                    return key
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            self._got_text.clear()
            try:
                await asyncio.wait_for(self._got_text.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

    async def settle(self, idle: float = 1.5, cap: float = 8.0) -> None:
        """Wait until output has been quiet for `idle` seconds (max `cap`)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + cap
        while loop.time() < deadline:
            self._got_text.clear()
            try:
                await asyncio.wait_for(self._got_text.wait(), timeout=idle)
            except asyncio.TimeoutError:
                return
