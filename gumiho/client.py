"""Async MUD connection: socket + telnet filter + incremental EUC-KR decoding."""

import asyncio
import codecs
import socket
from typing import Callable, Optional

from .session_log import SessionLogger
from .telnet import TelnetFilter


class MudConnection:
    """Owns the socket. Delivers decoded text via the on_text callback and
    logs every raw byte before any processing."""

    def __init__(
        self,
        host: str,
        port: int,
        logger: SessionLogger,
        on_text: Callable[[str], None],
        on_prompt_mark: Optional[Callable[[], None]] = None,
        encoding: str = "euc-kr",
    ) -> None:
        self.host = host
        self.port = port
        self.logger = logger
        self.on_text = on_text
        self.on_prompt_mark = on_prompt_mark
        self.encoding = encoding
        self._filter = TelnetFilter()
        # Incremental: a hangul character split across packets decodes correctly.
        self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        sock = self._writer.get_extra_info("socket")
        if sock is not None:
            # detect silently-dead peers (idle NAT drops, RST-less kicks)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # Disable Nagle's algorithm: a MUD command is one tiny packet, so
            # coalescing buys nothing but adds ~40-200ms of lag per command (the
            # Nagle + delayed-ACK interaction). Interactive typing should feel
            # instant — no trade-off for our small, latency-sensitive writes.
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.logger.note(f"connected to {self.host}:{self.port}")

    async def read_loop(self) -> None:
        assert self._reader is not None
        while True:
            chunk = await self._reader.read(4096)
            if not chunk:
                self.logger.note("connection closed by server")
                break
            self.logger.raw_in(chunk)
            event = self._filter.feed(chunk)
            if event.replies and self._writer is not None:
                self._writer.write(event.replies)
                await self._writer.drain()
            text = self._decoder.decode(event.data)
            if text:
                self.logger.text_in(text)
                self.on_text(text)
            if event.prompt_marks and self.on_prompt_mark is not None:
                for _ in range(event.prompt_marks):
                    self.on_prompt_mark()

    async def send_line(self, line: str, log_as: str | None = None) -> None:
        assert self._writer is not None
        payload = line.encode(self.encoding, errors="replace") + b"\r\n"
        self._writer.write(payload)
        await self._writer.drain()
        self.logger.sent(log_as if log_as is not None else line)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
