"""Zone administration: list zones, mark hostile ones.

Usage:
  .venv/bin/python scripts/zone.py list
  .venv/bin/python scripts/zone.py hostile <이름> [--off] [--notes "설명"]
  .venv/bin/python scripts/zone.py nohunt <이름> [--off]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho.config import ROOT
from gumiho.mapper import Mapper


def main() -> None:
    mapper = Mapper(ROOT / "knowledge" / "map.sqlite")
    args = sys.argv[1:]
    if not args or args[0] == "list":
        rows = mapper.db.execute(
            """SELECT z.name, z.hostile, z.notes, COUNT(r.id) AS rooms
               FROM zones z LEFT JOIN rooms r ON r.zone = z.name
               GROUP BY z.name ORDER BY z.name""").fetchall()
        if not rows:
            print("(no zones recorded yet)")
        for r in rows:
            flag = "⚠ hostile" if r["hostile"] else "safe"
            notes = f" — {r['notes']}" if r["notes"] else ""
            print(f"{r['name']}: {r['rooms']} rooms, {flag}{notes}")
    elif args[0] == "nohunt" and len(args) >= 2:
        mapper.set_no_hunt(args[1], "--off" not in args)
        print(f"{args[1]}: no_hunt={'--off' not in args}")
    elif args[0] == "hostile" and len(args) >= 2:
        name = args[1]
        hostile = "--off" not in args
        notes = None
        if "--notes" in args:
            notes = args[args.index("--notes") + 1]
        mapper.set_hostile(name, hostile, notes)
        print(f"{name}: hostile={hostile}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
