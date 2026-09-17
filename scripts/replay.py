"""Replay harness: run every logs/*.raw through the full pipeline and score it.

This is the Phase 2 exit-criteria check: telnet filter -> EUC-KR decode ->
ANSI strip -> parser, with per-category line counts, the unknown-line rate,
and the events each session produced.

Usage: python3 scripts/replay.py [-v] [file.raw ...]
"""

import codecs
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import events as ev
from gumiho.parser import StreamParser
from gumiho.state import WorldState
from gumiho.telnet import TelnetFilter


def replay(path: Path, verbose: bool) -> tuple[Counter, list[str], WorldState, Counter]:
    tf = TelnetFilter()
    dec = codecs.getincrementaldecoder("euc-kr")(errors="replace")
    state = WorldState()
    event_counts: Counter = Counter()

    def on_event(e: ev.Event) -> None:
        event_counts[type(e).__name__] += 1
        state.apply(e)

    parser = StreamParser(on_event)
    raw = path.read_bytes()
    # Feed in small chunks to exercise split IAC/hangul/prompt handling.
    for i in range(0, len(raw), 7):
        data, _, _ = (lambda e: (e.data, e.replies, e.prompt_marks))(tf.feed(raw[i:i + 7]))
        text = dec.decode(data)
        if text:
            parser.feed(text)
    parser.flush()

    counts: Counter = Counter()
    unknowns: list[str] = []
    for category, line in parser.line_labels:
        counts[category] += 1
        if category == "unknown":
            unknowns.append(line)
    return counts, unknowns, state, event_counts


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "-v"]
    verbose = "-v" in sys.argv[1:]
    root = Path(__file__).resolve().parent.parent
    paths = [Path(a) for a in args] or sorted((root / "logs").glob("*.raw"))

    total = Counter()
    all_unknown: list[str] = []
    for path in paths:
        counts, unknowns, state, events = replay(path, verbose)
        total += counts
        all_unknown += unknowns
        classified = sum(n for c, n in counts.items() if c not in ("unknown", "blank"))
        lines = classified + counts["unknown"]
        rate = 100.0 * classified / lines if lines else 100.0
        print(f"{path.name}: {lines} lines, {rate:.1f}% classified, "
              f"{counts['unknown']} unknown | events: {dict(events)}")
        if verbose and state.room_title:
            print(f"  final state: room={state.room_title!r} exits={state.exits} "
                  f"hp={state.vitals.hp}/{state.vitals.hp_max} "
                  f"inv={state.inventory}")

    classified = sum(n for c, n in total.items() if c not in ("unknown", "blank"))
    lines = classified + total["unknown"]
    print(f"\nTOTAL: {lines} lines, "
          f"{100.0 * classified / lines:.1f}% classified, {total['unknown']} unknown")
    if all_unknown:
        print("\nunclassified lines:")
        for u in dict.fromkeys(all_unknown):
            print(f"  {u!r}")


if __name__ == "__main__":
    main()
