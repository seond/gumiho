"""Map health report: connectivity, coverage, and clone evidence.

Usage: .venv/bin/python scripts/map_doctor.py
"""

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho.config import ROOT
from gumiho.mapper import Mapper
from gumiho.walker import parse_exit


def main() -> None:
    m = Mapper(ROOT / "knowledge" / "map.sqlite")
    rooms = {r["id"]: r for r in m.rooms_list()}
    print(f"rooms: {len(rooms)}, edges: {m.stats()['edges']}")

    # --- connected components over navigable (bidirectional) edges ---
    seen: set[str] = set()
    components: list[list[str]] = []
    for rid in rooms:
        if rid in seen:
            continue
        comp, stack = [], [rid]
        seen.add(rid)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in m.neighbors_all(cur).values():
                if nb not in seen and nb in rooms:
                    seen.add(nb)
                    stack.append(nb)
        components.append(comp)
    components.sort(key=len, reverse=True)
    print(f"\nconnected components: {len(components)}")
    for i, comp in enumerate(components):
        titles = [rooms[r]["title"] for r in comp[:4]]
        suffix = " …" if len(comp) > 4 else ""
        print(f"  #{i + 1}: {len(comp)} rooms — {', '.join(titles)}{suffix}")

    recall = m.kv_get("recall_room")
    if recall and components:
        main_comp = set(components[0])
        bridged = recall in main_comp
        island_rooms = sum(len(c) for c in components[1:])
        print(f"\nrecall room known: {rooms.get(recall, {}).get('title', recall)}"
              f" — 귀환 경유로 본토({len(components[0])}방) 접근 "
              f"{'가능' if bridged else '불가'}; 고립 방 {island_rooms}개")
    elif not recall:
        print("\nrecall room not yet learned (첫 귀환 시 자동 기록)")

    # --- dangling (unexplored) exits per zone ---
    dangling = defaultdict(int)
    total_dangling = 0
    for rid, room in rooms.items():
        walked = m.neighbors(rid)
        full = m.room(rid)
        for label in full["exits"]:
            d, _ = parse_exit(label)
            if d and d not in walked:
                # reverse-inferred edges count as explored for coverage
                if m.neighbors_all(rid).get(d) is None:
                    dangling[room["zone"] or "미확인"] += 1
                    total_dangling += 1
    print(f"\nunexplored exits: {total_dangling}")
    for zone, n in sorted(dangling.items(), key=lambda x: -x[1]):
        print(f"  {zone}: {n}")

    # --- clone evidence (deterministic map ⇒ contradictions are clones) ---
    anomalies = [dict(r) for r in m.db.execute("SELECT * FROM edge_anomalies")]
    print(f"\nedge contradictions (room-clone evidence): {len(anomalies)}")
    for a in anomalies[:10]:
        s = rooms.get(a["src"], {}).get("title", a["src"])
        e = rooms.get(a["expected_dst"], {}).get("title", "?")
        g = rooms.get(a["actual_dst"], {}).get("title", "?")
        print(f"  {s} --{a['dir']}--> {e} 또는 {g} (×{a['count']})")

    warps = [dict(r) for r in m.db.execute("SELECT * FROM warps")]
    print(f"\nwarps observed: {len(warps)}")
    for w in warps[:10]:
        print(f"  {rooms.get(w['src'], {}).get('title', '?')} ⇒ "
              f"{rooms.get(w['dst'], {}).get('title', '?')} (×{w['count']})")


if __name__ == "__main__":
    main()
