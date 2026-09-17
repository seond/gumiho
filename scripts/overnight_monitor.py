#!/usr/bin/env python
"""Overnight health monitor for the gumiho duo.

OBSERVE-ONLY: it never sends a game command (the engine self-heals — persistent
recall, stuck-caps, auto-reconnect). It logs a health line each cycle and EXITS
(waking the supervising session) only on a condition the auto-recovery can't fix:
  - the webui/game server is unreachable (process died),
  - a character is stuck offline across several checks (reconnect failing),
  - the duo is separated for too long (recovery didn't reunite them).

Run in the background; when it exits, the supervisor is re-invoked with its tail.
"""

import asyncio
import glob
import json
import os
import re
import time
import sys

WS = "ws://127.0.0.1:8642/ws"
CHECK = 360                 # seconds between checks (6 min)
NIGHT = int(sys.argv[1]) if len(sys.argv) > 1 else 9 * 3600
OFFLINE_LIMIT = 2          # consecutive checks a char may be offline before waking
SEPARATE_LIMIT = 3         # consecutive checks the duo may be apart before waking
STUCK_LIMIT = 2            # consecutive checks the leader looks stuck-looping before waking

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def leader_stuck():
    """True if the newest leader log ends in a move+지도 loop with no combat — the
    'chasing a phantom cell' hang (지도/북/지도/북…) the overnight drift produced."""
    logs = sorted(glob.glob("logs/*.log"), key=os.path.getmtime, reverse=True)
    for fn in logs[:3]:
        try:
            t = _ANSI.sub("", open(fn, encoding="utf-8", errors="replace").read())
        except OSError:
            continue
        if "플레이어제로" not in t:
            continue
        sent = re.findall(r">> '([^']*)'", t)[-24:]
        if len(sent) < 12:
            return False
        if any("공격" in c for c in sent):     # actively fighting -> not stuck
            return False
        dirs = [c for c in sent if c in ("북", "남", "동", "서", "위", "아래")]
        jido = sum(1 for c in sent if c == "지도")
        # one direction hammered, paired with 지도, and zero kills = phantom loop
        if jido >= 6 and dirs and max(dirs.count(d) for d in set(dirs)) >= 6:
            return True
        return False
    return False

try:
    import aiohttp
except Exception as e:      # pragma: no cover
    print("monitor: aiohttp missing:", e)
    raise SystemExit(1)


async def snapshot():
    """Return {sid: state} or None if the server is unreachable."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(WS, timeout=aiohttp.ClientTimeout(total=10)) as ws:
                st = {}
                t = asyncio.get_event_loop().time()
                while asyncio.get_event_loop().time() - t < 4:
                    try:
                        m = await asyncio.wait_for(ws.receive(), timeout=1)
                    except asyncio.TimeoutError:
                        continue
                    if m.type == aiohttp.WSMsgType.TEXT:
                        d = json.loads(m.data)
                        if d.get("type") == "state":
                            st[d.get("sid")] = d
                return st
    except Exception:
        return None


def _room(v):
    return (v or {}).get("room")


async def main():
    start = time.time()
    offline = apart = stuck = 0
    print(f"monitor: started, {NIGHT // 3600}h, checking every {CHECK}s", flush=True)
    while time.time() - start < NIGHT:
        st = await snapshot()
        stamp = time.strftime("%H:%M:%S")
        if st is None:
            print(f"[{stamp}] SERVER UNREACHABLE -> waking supervisor", flush=True)
            return
        online = [k for k, v in st.items() if v.get("status") == "online"]
        rooms = {k: _room(v) for k, v in st.items()}
        if len(online) < 2:
            offline += 1
            missing = set(st) - set(online)
            print(f"[{stamp}] offline={missing or '?'} ({offline}/{OFFLINE_LIMIT}) rooms={rooms}", flush=True)
            if offline >= OFFLINE_LIMIT:
                print(f"[{stamp}] CHARACTER STUCK OFFLINE -> waking supervisor", flush=True)
                return
            stuck = apart = 0
        else:
            offline = 0
            same = len({r for r in rooms.values() if r}) <= 1
            if not same:
                apart += 1
                print(f"[{stamp}] APART {rooms} ({apart}/{SEPARATE_LIMIT})", flush=True)
                if apart >= SEPARATE_LIMIT:
                    print(f"[{stamp}] DUO SEPARATED TOO LONG -> waking supervisor", flush=True)
                    return
            else:
                apart = 0
                if leader_stuck():
                    stuck += 1
                    print(f"[{stamp}] LEADER LOOPING (move+지도, no kills) ({stuck}/{STUCK_LIMIT}) in {list(rooms.values())[0]!r}", flush=True)
                    if stuck >= STUCK_LIMIT:
                        print(f"[{stamp}] LEADER STUCK IN A LOOP -> waking supervisor", flush=True)
                        return
                else:
                    stuck = 0
                    print(f"[{stamp}] OK both online in {list(rooms.values())[0]!r}", flush=True)
        await asyncio.sleep(CHECK)
    print("monitor: night complete, exiting normally", flush=True)


asyncio.run(main())
