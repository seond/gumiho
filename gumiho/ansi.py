"""ANSI escape sequence stripping (the server uses e.g. ESC[2K)."""

import re

_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]"      # CSI sequences: colors, erase-line, ...
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"    # OSC sequences
    r"|[@-Z\\-_])"                        # two-byte sequences
)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)
