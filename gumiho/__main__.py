"""Passthrough mode: you play, gumiho logs.

Usage:
    python3 -m gumiho                # uses config.toml next to the package
    python3 -m gumiho --host H --port P

Type normally to send commands (Korean input is encoded to EUC-KR).
/quit closes the connection. Every session writes logs/session-*.raw
(exact bytes) and logs/session-*.log (timestamped transcript).
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from . import events as ev
from .client import MudConnection
from .config import ROOT, load_config, load_env
from .expect import TextWatcher
from .login import LoginError, auto_login
from .notify import Notifier
from .parser import StreamParser
from .session_log import SessionLogger
from .state import WorldState


async def run(host: str, port: int, encoding: str, log_dir: Path,
              notify: bool = True) -> None:
    logger = SessionLogger(log_dir)
    print(f"gumiho: connecting to {host}:{port} ({encoding})")
    print(f"gumiho: logging to {logger.raw_path.name} / {logger.text_path.name}")
    print("gumiho: type /quit to exit\n")

    watcher = TextWatcher(tee=lambda t: (sys.stdout.write(t), sys.stdout.flush()))
    notifier = Notifier(enabled=notify)
    state = WorldState()

    def on_event(event: ev.Event) -> None:
        state.apply(event)
        match event:
            case ev.Tell(speaker=who, text=text):
                notifier.notify(f"{who} → 나", text)
            case ev.ChannelMessage(channel=ch, speaker=who, text=text):
                notifier.notify(f"{who} [{ch}]", text)
            case ev.Speech(speaker=who, text=text):
                notifier.notify(who, text, dedupe=True)
            case _:
                pass

    parser = StreamParser(on_event)
    conn = MudConnection(
        host, port, logger,
        on_text=lambda t: (watcher.on_text(t), parser.feed(t)),
    )
    await conn.connect()
    read_task = asyncio.create_task(conn.read_loop())

    env = load_env()
    name, password = env.get("GUMIHO_NAME"), env.get("GUMIHO_PASSWORD")
    if name and password:
        try:
            result = await auto_login(conn, watcher, name, password)
            print(f"\ngumiho: auto-login ok ({result}) as {name}")
        except LoginError as e:
            print(f"\ngumiho: auto-login failed ({e}) — continue manually")
    else:
        print("gumiho: no credentials in .env — manual login")

    loop = asyncio.get_running_loop()
    stdin_q: asyncio.Queue[str | None] = asyncio.Queue()
    stdin_buf = bytearray()

    # Read raw bytes and split lines ourselves: buffered readline() can strand
    # lines when several arrive in one readability event (e.g. a paste).
    def on_stdin() -> None:
        data = os.read(sys.stdin.fileno(), 4096)
        if not data:
            stdin_q.put_nowait(None)
            return
        stdin_buf.extend(data)
        while (nl := stdin_buf.find(b"\n")) >= 0:
            line = stdin_buf[:nl].decode("utf-8", errors="replace")
            del stdin_buf[:nl + 1]
            stdin_q.put_nowait(line)

    loop.add_reader(sys.stdin.fileno(), on_stdin)

    async def input_loop() -> None:
        while True:
            line = await stdin_q.get()
            if line is None or line.strip() == "/quit":
                return
            # Trailing whitespace is never meaningful to the server, and trimming
            # it makes "commit the IME composition with Space, then Enter" safe.
            await conn.send_line(line.rstrip())

    input_task = asyncio.create_task(input_loop())
    try:
        done, pending = await asyncio.wait(
            {read_task, input_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        for t in done:
            if (exc := t.exception()) is not None:
                raise exc
    finally:
        loop.remove_reader(sys.stdin.fileno())
        await conn.close()
        logger.close()
        print("\ngumiho: session closed")


def main() -> None:
    cfg = load_config()
    server = cfg.get("server", {})
    parser = argparse.ArgumentParser(prog="gumiho")
    parser.add_argument("--host", default=server.get("host", "ggai.tv"))
    parser.add_argument("--port", type=int, default=server.get("port", 4000))
    parser.add_argument("--encoding", default=server.get("encoding", "euc-kr"))
    parser.add_argument("--no-notify", action="store_true",
                        help="disable desktop notifications for messages")
    parser.add_argument("--term", action="store_true",
                        help="terminal passthrough mode instead of the GUI")
    gui_cfg = cfg.get("gui", {})
    parser.add_argument("--gui-port", type=int, default=gui_cfg.get("port", 8642))
    args = parser.parse_args()

    log_dir = ROOT / cfg.get("logging", {}).get("dir", "logs")
    use_gui = not args.term
    if use_gui:
        try:
            from .webui import serve
        except ImportError:
            print("gumiho: aiohttp not installed — falling back to --term "
                  "(pip install -r requirements.txt for the GUI)")
            use_gui = False
    try:
        if use_gui:
            asyncio.run(serve(args.host, args.port, log_dir,
                              ui_port=args.gui_port, notify=not args.no_notify))
        else:
            asyncio.run(run(args.host, args.port, args.encoding, log_dir,
                            notify=not args.no_notify))
    except KeyboardInterrupt:
        print("\ngumiho: bye")


if __name__ == "__main__":
    main()
