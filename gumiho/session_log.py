"""Per-session logging: raw inbound bytes plus a timestamped decoded transcript.

Raw logs are the ground truth — Phase 2's parser is developed and tested by
replaying them, so they must capture exactly what the socket delivered,
before any filtering or decoding.
"""

import datetime
from pathlib import Path


class SessionLogger:
    def __init__(self, log_dir: Path) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        self.raw_path = log_dir / f"session-{stamp}.raw"
        self.text_path = log_dir / f"session-{stamp}.log"
        self._raw = open(self.raw_path, "ab")
        self._text = open(self.text_path, "a", encoding="utf-8")

    def _ts(self) -> str:
        return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

    def raw_in(self, chunk: bytes) -> None:
        self._raw.write(chunk)
        self._raw.flush()

    def text_in(self, text: str) -> None:
        if text:
            self._text.write(f"[{self._ts()}] {text!r}\n")
            self._text.flush()

    def sent(self, line: str) -> None:
        self._text.write(f"[{self._ts()}] >> {line!r}\n")
        self._text.flush()

    def note(self, msg: str) -> None:
        self._text.write(f"[{self._ts()}] ## {msg}\n")
        self._text.flush()

    def close(self) -> None:
        self._raw.close()
        self._text.close()
