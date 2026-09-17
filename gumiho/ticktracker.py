"""Master server-tick tracking layer.

The game runs on a fixed ~74.5s master tick: 분노/빨리가기 fade ON it (together, to the same
instant) and natural HP/MP/MV regen rides it. Other tick-locked effects (철면, on the half-tick)
run on top of this layer via `grid_point`.

The phase is NOT knowable at connect time — it's learned by OBSERVING tick-boundary events (the
buff fades). Until two consistent boundaries are seen, `synced` is False (the UI shows "syncing").
A server reboot shifts the phase; an inconsistent observation drops sync and re-learns it.
"""
import math


class TickTracker:
    def __init__(self, period=74.5, tol=3.0, min_confirm=2, max_gap_ticks=8, dedup_secs=5.0):
        self.period = float(period)      # master tick length; refined from observations
        self.tol = tol                   # how far a boundary may sit from k whole periods
        self.min_confirm = min_confirm   # consistent boundaries needed to declare `synced`
        self.max_gap_ticks = max_gap_ticks
        self.dedup_secs = dedup_secs     # boundaries closer than this are the SAME tick (분노+빨리)
        self.anchor = None              # monotonic of the last confirmed boundary (phase); None=unlearned
        self.confirms = 0
        self.synced = False

    def observe_boundary(self, t: float) -> None:
        """Feed a monotonic time that lands ON a master-tick boundary (a 분노/빨리가기 fade)."""
        if self.anchor is None:
            self.anchor = t
            self.confirms = 1
            return
        gap = t - self.anchor
        if gap < self.dedup_secs:
            return                        # same boundary (분노 & 빨리 fade together) — ignore the dup
        k = round(gap / self.period)
        if k < 1 or k > self.max_gap_ticks:
            self.anchor = t; self.confirms = 1; self.synced = False   # unusable span -> re-anchor
            return
        if abs(gap - k * self.period) <= self.tol:
            self.period += 0.25 * ((gap / k) - self.period)           # EMA-refine the period
            self.anchor = t
            self.confirms += 1
            if self.confirms >= self.min_confirm:
                self.synced = True
        else:
            self.anchor = t; self.confirms = 1; self.synced = False   # inconsistent -> re-learn

    def remaining(self, now: float):
        """Seconds until the next master boundary (None until synced)."""
        if not self.synced or self.anchor is None:
            return None
        k = math.ceil((now - self.anchor) / self.period - 1e-9)
        return max(0.0, self.anchor + k * self.period - now)

    def grid_point(self, at: float, base: float, div: int = 1):
        """First (period/div)-grid point at/after (at+base). div=2 -> the 철면 half-tick grid.
        None until synced (callers fall back to their own estimate)."""
        if not self.synced or self.anchor is None:
            return None
        step = self.period / div
        k = math.ceil((at + base - self.anchor) / step)
        return self.anchor + k * step
