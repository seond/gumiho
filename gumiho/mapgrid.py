"""Parse the 지도 (map) command's ASCII local map into a structured grid.

The game draws the rooms around you as a grid WITH their connections — reliable
local topology straight from the server, far better than dead-reckoned coords:

    --- 여기는 "훈련장"입니다. ---
           |            |            |            |
     _  훈련장의  _  훈련장의  _  훈련장의  _  훈련장의
          중앙         중앙        테두리        동쪽
           |            |            |            |
     _  훈련장의  _  훈련장의  _ [ 당신이 ] _  훈련장의
          중앙         중앙      [ 있는곳 ]      동쪽

Layout: cells are on a fixed 13-DISPLAY-COLUMN stride (Korean glyphs are two
columns wide, so we index by display column, not by character). A room name
wraps over two text lines. Between two cells a "_" means they connect (동/서);
a "|" on the connector line above a cell means it connects upward (북/남); a
trailing "-"/"_" at a row edge is a link continuing off the drawn map. The
player's own cell is "[ 당신이 ] / [ 있는곳 ]".

parse_map_grid returns a MapGrid: cell titles keyed by (row, col), the player's
cell, undirected grid edges the map drew, and off-map links per direction.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

STRIDE = 13                      # display-column width of one map cell
_HDR_RE = re.compile(r'^-+\s*여기는\s*"?(.+?)"?\s*입니다')
_DELTA = {"북": (-1, 0), "남": (1, 0), "동": (0, 1), "서": (0, -1)}


def _dwidth(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _to_disp(line: str) -> str:
    """Expand a line so index == display column: each 2-wide glyph is followed
    by a filler byte, so slicing [a:b] yields the display-column window."""
    out = []
    for ch in line:
        out.append(ch)
        if _dwidth(ch) == 2:
            out.append("\x00")
    return "".join(out)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\x00", "")).strip()


@dataclass
class MapGrid:
    zone: str | None = None
    cells: dict[tuple[int, int], str] = field(default_factory=dict)
    here: tuple[int, int] | None = None
    edges: set = field(default_factory=set)          # frozenset({(r,c),(r,c)})
    offmap: set = field(default_factory=set)          # (row, col, direction)

    def neighbor(self, direction: str) -> str | None:
        if self.here is None:
            return None
        dr, dc = _DELTA.get(direction, (0, 0))
        return self.cells.get((self.here[0] + dr, self.here[1] + dc))

    def here_title(self) -> str | None:
        return self.cells.get(self.here) if self.here else None


def _window(disp: str, col: int) -> str:
    """Display-column window [1+13c, 14+13c) of a cell, connectors stripped."""
    seg = disp[1 + STRIDE * col:1 + STRIDE * (col + 1)]
    return re.sub(r"[\[\]_|~\-]", " ", seg)


def _ncols(disp: str) -> int:
    return max(0, (len(disp) - 1 + STRIDE - 1) // STRIDE)


def parse_map_grid(text: str) -> MapGrid | None:
    lines = text.split("\n")
    zone = start = None
    for i, ln in enumerate(lines):
        m = _HDR_RE.match(ln.strip())
        if m:
            zone, start = _norm(m.group(1)), i + 1
            break
    if start is None:
        return None
    body = [_to_disp(ln.rstrip("\r")) for ln in lines[start:]]

    def has_text(disp: str) -> bool:
        return bool(re.search(r"[가-힣]", disp)) or "[" in disp

    grid = MapGrid(zone=zone)
    row = 0
    i = 0
    while i < len(body):
        ln = body[i]
        nxt = body[i + 1] if i + 1 < len(body) else ""
        # A name band = a text line whose next line is also text (name1/name2).
        if has_text(ln) and has_text(nxt):
            conn = body[i - 1] if i - 1 >= 0 else ""
            _read_band(grid, ln, nxt, conn, row)
            row += 1
            i += 2
        else:
            i += 1
    return grid if grid.cells or grid.here else None


def _read_band(grid: MapGrid, n1: str, n2: str, conn: str, row: int) -> None:
    ncols = max(_ncols(n1), _ncols(n2))
    for c in range(ncols):
        w1, w2 = _window(n1, c), _window(n2, c)
        title = _norm(w1 + " " + w2)     # _norm drops the \x00 display fillers
        if "당신" in title or "있는곳" in title:
            grid.here = (row, c)         # the player's own cell — not a room name
        elif title:
            grid.cells[(row, c)] = title
        # horizontal link: a "_" sits at the START of a cell's window, joining it
        # to the cell on its left (for c==0 it is an off-map link to the west).
        lead = n1[1 + STRIDE * c:2 + STRIDE * c]
        if "_" in lead:
            if c > 0:
                grid.edges.add(frozenset({(row, c - 1), (row, c)}))
            else:
                grid.offmap.add((row, c, "서"))
        # off-map link to the right of the last drawn cell on this row ("-"/"_")
        if c == ncols - 1:
            tail = n1[2 + STRIDE * (c + 1): 4 + STRIDE * (c + 1)]
            if "-" in tail or "_" in tail:
                grid.offmap.add((row, c, "동"))
        # vertical link: "|" on the connector line within this cell's window
        vseg = conn[1 + STRIDE * c:1 + STRIDE * (c + 1)]
        if "|" in vseg and row > 0:
            grid.edges.add(frozenset({(row - 1, c), (row, c)}))
