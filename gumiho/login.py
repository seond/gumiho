"""Automated login for 고블린 머드 III.

Observed flows (see logs/):
  normal:     이름 -> 암호 -> MOTD "엔터를 눌러주세요" -> menu "선택하세요" -> 1 -> in game
  reconnect:  이름 -> 암호 -> "이미 접속하고 있는 당신의 몸에 들어갑니다!" -> in game
  new user:   이름 -> "새로운 사용자입니다" -> creation dialogue (NOT automated;
              character creation is deliberately manual)
"""

import asyncio
import random

from .client import MudConnection
from .expect import TextWatcher


async def _pace() -> None:
    """Humans don't answer a prompt in 0 ms."""
    await asyncio.sleep(random.uniform(0.6, 1.4))

PROMPT_TAIL = r"\d+:\d+:\d+>\s*$"


class LoginError(RuntimeError):
    pass


async def auto_login(conn: MudConnection, watcher: TextWatcher,
                     name: str, password: str, timeout: float = 45.0) -> str:
    """Drive login to the in-game prompt. Returns 'entered' or 'reconnected'."""
    if await watcher.expect_any({"name": r"이름을 입력"}, 20) is None:
        raise LoginError("never saw the name prompt")
    await _pace()
    await conn.send_line(name)

    key = await watcher.expect_any(
        {"new": r"새로운 사용자", "pw": r"암호"}, 15)
    if key == "new":
        raise LoginError(
            f"'{name}' does not exist — create the character manually first")
    if key is None:
        raise LoginError("never saw the password prompt")
    await _pace()
    await conn.send_line(password, log_as="*****")

    deadline = asyncio.get_running_loop().time() + timeout
    result = "entered"
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        key = await watcher.expect_any({
            "badpw": r"틀렸습니다|잘못.*암호|암호가 맞지",
            "again": r"암호를 다시",          # new-user creation slipped through
            "reconnect": r"이미 접속하고 있는",
            "enter": r"엔터를 눌러주세요",
            "menu": r"선택하세요\s*:",
            "prompt": PROMPT_TAIL,
        }, max(remaining, 0.1))
        match key:
            case "badpw":
                raise LoginError("wrong password")
            case "again":
                raise LoginError("unexpected creation dialogue — aborting")
            case "reconnect":
                result = "reconnected"
            case "enter":
                await _pace()
                await conn.send_line("")
            case "menu":
                await _pace()
                await conn.send_line("1")
            case "prompt":
                return result
            case None:
                raise LoginError("login timed out")
