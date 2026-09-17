"""Desktop notifications for incoming messages.

macOS: Notification Center via osascript (no dependencies). Everything else:
terminal bell only. Repeated identical messages are deduped for a cooldown
window so ambient NPC chatter (the lake NPC repeats itself forever) doesn't
spam the user.
"""

import asyncio
import sys
import time


class Notifier:
    def __init__(self, cooldown: float = 120.0, enabled: bool = True) -> None:
        self.cooldown = cooldown
        self.enabled = enabled
        self._recent: dict[tuple[str, str], float] = {}

    def _deduped(self, key: tuple[str, str]) -> bool:
        now = time.monotonic()
        last = self._recent.get(key)
        self._recent = {k: t for k, t in self._recent.items()
                        if now - t < self.cooldown}
        if last is not None and now - last < self.cooldown:
            return True
        self._recent[key] = now
        return False

    def notify(self, title: str, message: str, dedupe: bool = False) -> None:
        if not self.enabled:
            return
        if dedupe and self._deduped((title, message)):
            return
        sys.stdout.write("\a")  # terminal bell in every environment
        sys.stdout.flush()
        if sys.platform == "darwin":
            script = (
                f'display notification "{_esc(message)}" '
                f'with title "gumiho" subtitle "{_esc(title)}" sound name "Glass"'
            )
            try:
                asyncio.get_running_loop()
                asyncio.ensure_future(_osascript(script))
            except RuntimeError:  # no running loop (tests) — fire synchronously
                import subprocess
                subprocess.run(["osascript", "-e", script],
                               capture_output=True, timeout=5)


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


async def _osascript(script: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except OSError:
        pass  # notifications are best-effort; never break the client
