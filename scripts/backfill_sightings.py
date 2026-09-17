"""Backfill the zone-mob registry from every historical session log.

Zone-mob data is static (mobs spawn within their zone), so observations
from old sessions are as valid as live ones. Replays each logs/*.raw
through the parser; entities seen in rooms whose zone is known (from the
persistent map) are recorded into zone_mobs.

Usage: .venv/bin/python scripts/backfill_sightings.py
"""

import codecs
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gumiho import events as ev
from gumiho.config import ROOT
from gumiho.mapper import Mapper, room_id
from gumiho.parser import StreamParser
from gumiho.telnet import TelnetFilter
from gumiho.webui import entity_info


def main() -> None:
    mapper = Mapper(ROOT / "knowledge" / "map.sqlite")
    recorded = 0

    for path in sorted((ROOT / "logs").glob("*.raw")):
        zone_ctx: list[str | None] = [None]   # zone of the room we stand in

        def on_event(event: ev.Event, _zone=zone_ctx) -> None:
            nonlocal recorded
            match event:
                case ev.RoomSeen() as r:
                    rid = room_id(r.title, r.description)
                    _zone[0] = mapper.zone_of(rid)
                    if _zone[0]:
                        for e in r.entities:
                            info = entity_info(e)
                            if info["kind"] == "mob" and info["kw"]:
                                mapper.record_sighting(_zone[0], info["kw"])
                                recorded += 1
                case ev.ZoneInfo(name=name):
                    _zone[0] = name
                case ev.EntityArrived(text=text):
                    if _zone[0]:
                        info = entity_info(text)
                        if info["kind"] == "mob" and info["kw"]:
                            mapper.record_sighting(_zone[0], info["kw"])
                            recorded += 1
                case _:
                    pass

        parser = StreamParser(on_event)
        tf = TelnetFilter()
        dec = codecs.getincrementaldecoder("euc-kr")(errors="replace")
        raw = path.read_bytes()
        for i in range(0, len(raw), 4096):
            text = dec.decode(tf.feed(raw[i:i + 4096]).data)
            if text:
                parser.feed(text)
        parser.flush()

    print(f"backfill complete: {recorded} sightings ingested")
    for z in mapper.db.execute("SELECT DISTINCT zone FROM zone_mobs ORDER BY zone"):
        zone = z["zone"]
        mobs = mapper.mobs_in_zone(zone)
        listing = ", ".join(f"{m['keyword']}×{m['count']}" for m in mobs)
        print(f"  {zone}: {listing}")


if __name__ == "__main__":
    main()
