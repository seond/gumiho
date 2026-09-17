"""Bestiary administration.

Usage:
  .venv/bin/python scripts/bestiary.py list
  .venv/bin/python scripts/bestiary.py set <키워드> yes|no [--notes "설명"]
  .venv/bin/python scripts/bestiary.py clear <키워드>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho.bestiary import Bestiary
from gumiho.config import ROOT


def main() -> None:
    b = Bestiary(ROOT / "knowledge" / "bestiary.sqlite")
    args = sys.argv[1:]
    if not args or args[0] == "list":
        rows = b.rows()
        if not rows:
            print("(bestiary is empty)")
        for r in rows:
            mark = {"yes": "⚔", "no": "✕", "unknown": "?"}[r["attackable"]]
            extra = f" — {r['notes']}" if r["notes"] else ""
            print(f"{mark} {r['keyword']}: {r['attackable']} ({r['source']}, "
                  f"{r['zone'] or '?'}) W{r['wins']}/L{r['losses']}{extra}")
    elif args[0] == "set" and len(args) >= 3 and args[2] in ("yes", "no"):
        notes = args[args.index("--notes") + 1] if "--notes" in args else None
        b.mark(args[1], args[2] == "yes", source="manual", notes=notes)
        print(f"{args[1]}: attackable={args[2]} (manual)")
    elif args[0] == "clear" and len(args) >= 2:
        b.db.execute("DELETE FROM entities WHERE keyword = ?", (args[1],))
        b.db.commit()
        print(f"{args[1]}: cleared")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
