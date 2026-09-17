"""Engine — the per-character interpreter (component D's runtime).

Each prompt it runs the arbiter:
  1. armed reflex bundles (C), highest priority first — zero deliberation
  2. the current state's transitions (B) — guards fire state/workflow changes
  3. the current state's calm step (B)

It holds NO behavior: workflows, guards, actions are resolved by name from the
registry every tick, so a hot-reload is picked up on the very next prompt. All
mutable state lives in the CharCtx (A).
"""

from __future__ import annotations

import time
from typing import Callable

from . import registry
from .access import CharCtx, Cursor
from .command import Cmd


class Engine:
    def __init__(
        self,
        ctx: CharCtx,
        cmd: Cmd,
        *,
        director=None,
        on_status: Callable[[dict], None] | None = None,
        now: Callable[[], float] = time.monotonic,
    ):
        self.ctx = ctx
        self.cmd = cmd
        self.director = director
        ctx.director = director            # so hooks can coordinate the duo (lockstep)
        self.on_status = on_status
        self._now = now
        self.enabled = False

    # --- lifecycle ----------------------------------------------------------
    def boot(self, kind: str, target: str | None = None) -> None:
        """Start (or restart) the character in workflow `kind` from its top.
        `target` is the hunt destination parameter (zone / saved-route name); it
        carries on the ctx through the whole cycle (travel, hunt, resupply)."""
        self.ctx.auto_cycle = False        # a manual boot; hunting turns this on
        # Keep the standing hunt target across a TARGETLESS re-boot (the engine
        # toggle, a workflow reboot, auto-reconnect re-arm) — else travel/go_home
        # falls back to [hunt].zone (the shallow default) and stops short of the
        # deep pocket instead of heading to the recorded target route.
        if target is not None:
            self.ctx.hunt_target = target
        self.ctx.gridsweep = None          # a NEW hunt learns its zone map from scratch
        self.ctx.reset_activity()          # fresh start: no stale buffs/자동지원/gear/timers
        self.enter_workflow(kind)
        self.enabled = True
        self._status("boot")

    def stop(self) -> None:
        self.enabled = False
        self._status("stop")

    def enter_workflow(self, kind: str) -> None:
        wf = registry.get_workflow(kind)
        self.ctx.cursor = Cursor(workflow=kind, sub=wf.initial, entered_at=self._now())
        self.ctx.flow = {}                         # workflow-scoped scratch resets here
        self._run_on_enter(kind, wf.initial)

    def enter_state(self, name: str) -> None:
        c = self.ctx.cursor
        c.sub = name
        c.entered_at = self._now()
        c.scratch = {}
        self._run_on_enter(c.workflow, name)

    def reenter_current(self) -> None:
        """Reconciliation on reload: re-enter the active workflow from its top.
        Live game state in A is untouched; only in-workflow progress resets."""
        if self.enabled and self.ctx.cursor.workflow:
            self.enter_workflow(self.ctx.cursor.workflow)
            self._status("reload-reenter")

    # --- the tick -----------------------------------------------------------
    def tick(self) -> None:
        if not self.enabled:
            self._offline_tick()                  # a few reactions still fire with the engine OFF
            return
        if self.ctx.state.at_prompt:              # act when the server is ready
            self._arbiter()
            if self.ctx.request_stop:             # a workflow signalled completion
                self.ctx.request_stop = False
                self.enabled = False
                self._status("halt")
                return
        # Watchdog: while asleep (or any quiet stretch) the server pushes no prompt,
        # so our vitals go stale and we'd never see we've recovered — and we'd be
        # idle-kicked. Poll to pull a fresh prompt (점수 carries current HP/MP) and
        # keep the link alive.
        rest = self.ctx.knowledge.get("rest", {})
        if self._now() - self.ctx.last_prompt_at > rest.get("watchdog", 12):
            self.ctx.last_prompt_at = self._now()     # throttle until the reply lands
            poke = rest.get("poke", "점수")
            if poke:
                self.cmd.send(poke)
                self._status("poke")

    def _arbiter(self) -> None:
        # 0. DEATH overrides everything: on death, drop into the recovery workflow from
        #    wherever we are — the corpse rots fast (items are lost if we dawdle), so
        #    this can't wait on a per-state transition. Cleared by the death workflow
        #    once 시체 묻어 revives us.
        if self.ctx.state.dead and self.ctx.cursor.workflow != "death":
            self.enter_workflow("death")
            self._status("switch:death (died)")
            return
        # 0b. BLIND overrides hunting too: while blinded the only thing that works is a recall,
        #     so drop into the blind-recovery workflow (recall -> 치료소 -> 장님치 부탁 -> resume)
        #     from wherever we are. Death still wins above. Cleared by the cure.
        if self.ctx.state.blind and self.ctx.cursor.workflow not in ("blind", "death"):
            self.enter_workflow("blind")
            self._status("switch:blind (blinded)")
            return
        try:
            wf = registry.get_workflow(self.ctx.cursor.workflow)
            st = wf.state(self.ctx.cursor.sub)
        except KeyError as e:
            self._error(str(e))
            return
        if st is None:
            self._error(f"state {self.ctx.cursor.sub!r} missing in {self.ctx.cursor.workflow!r}")
            return

        # 1. reflexes (priority order within each armed bundle). ALWAYS-ON bundles
        #    ([engine].always_reflexes — e.g. 연타 continuation, the supporter's spell
        #    response) are APPENDED to every state's arm list, so combat-continuity
        #    fires no matter the workflow. Appended (not prepended) so the state's own
        #    bundles — survival (potions) above all — still win a same-tick tie.
        always = self.ctx.knowledge.get("engine", {}).get("always_reflexes", [])
        arm = list(st.arm) + [b for b in always if b not in st.arm]
        for bundle_name in arm:
            try:
                bundle = registry.get_bundle(bundle_name)
            except KeyError as e:
                self._error(str(e)); continue
            for rx in bundle.reflexes:
                if self._eval_guard(rx.when):
                    self._act(rx.do)
                    self._status(f"reflex:{rx.do}")
                    return

        # 2. transitions
        for t in st.transitions:
            if self._eval_guard(t.when):
                self._transition(t)
                return

        # 3. calm step
        if st.step:
            self._act(st.step)
            self._status(f"step:{st.step}")

    # --- offline reflexes (the engine toggle is OFF) ------------------------
    def _offline_tick(self) -> None:
        """The autonomous driver is OFF (manual play), but a few reactions must STILL fire so the
        duo stays safe and responsive without ever hunting, travelling, roaming, or resting on its
        own. Only three things run here:
          1. DEATH / BLIND recovery — a rotting corpse loses every item, so we recover regardless
             of the toggle, then HALT (we do NOT resume the hunt the user turned off).
          2. the supporter's spoken spell-request response + fleeing-partner chase (`support`).
          3. 연타 combat continuity for whoever is in a fight (`combo`).
        Everything here is at-prompt gated and every reflex guard is self-clearing, so an idle
        manual session sends nothing."""
        if self.ctx is None or not self.ctx.state.at_prompt:
            return
        # 1. DEATH/BLIND recovery — a safety net, but it must NEVER permanently block the reflexes
        #    below. It runs while GENUINELY dead/blind; once that clears it gets a short grace to
        #    finish the salvage and reach its terminal halt. If the character is clearly active again
        #    (in a fight) or the grace lapses (a recovery that can't complete offline, or one the
        #    user handled manually), we ABANDON it so spell/연타 resume — a STUCK recovery must not
        #    freeze the reflexes (the "all reflexes stopped without the engine" bug: the ctx was
        #    stranded in death/blind with offline_recovery set, so this branch returned every tick).
        if self.ctx.state.dead or self.ctx.state.blind:
            self._offline_recover()
            return
        if self.ctx.offline_recovery and self.ctx.cursor.workflow in ("death", "blind"):
            grace = self.ctx.knowledge.get("engine", {}).get("offline_recovery_grace", 20.0)
            lapsed = (self.ctx._now() - getattr(self.ctx, "_offline_recovery_at", 0.0)) > grace
            if self.ctx.state.in_battle or lapsed:
                self._abandon_offline_recovery()       # un-stick -> fall through to reflexes
            else:
                self._offline_recover()                # still finishing reequip/insure after revival
                return
        # 2/3. the whitelisted always-on reflex bundles (spell-on-request, chase, 연타).
        for bundle_name in self.ctx.knowledge.get("engine", {}).get("offline_reflexes", []):
            try:
                bundle = registry.get_bundle(bundle_name)
            except KeyError as e:
                self._error(str(e)); continue
            for rx in bundle.reflexes:
                if self._eval_guard(rx.when):
                    self._act(rx.do)
                    self._status(f"reflex(off):{rx.do}")
                    return

    def _offline_recover(self) -> None:
        """Drive ONLY a death/blind recovery workflow while the engine is off. Mirrors the arbiter's
        recovery override + state machinery, but never touches a hunting/travel workflow and HALTS
        (stays off) the instant recovery would hand back to travel — we recover the gear, we do not
        resume. death.toml/blind.toml are unchanged: the terminal `switch = travel` is intercepted
        here rather than in the TOML, so the engine-ON path keeps resuming as before."""
        if not self.ctx.offline_recovery:
            self.ctx._offline_recovery_at = self.ctx._now()   # stamp the start of this episode (grace)
        self.ctx.offline_recovery = True           # re-affirm each tick (revived_reset may clear it)
        if self.ctx.state.dead and self.ctx.cursor.workflow != "death":
            self.enter_workflow("death"); self._status("switch:death (offline recover)"); return
        if self.ctx.state.blind and self.ctx.cursor.workflow not in ("blind", "death"):
            self.enter_workflow("blind"); self._status("switch:blind (offline recover)"); return
        try:
            wf = registry.get_workflow(self.ctx.cursor.workflow)
            st = wf.state(self.ctx.cursor.sub)
        except KeyError as e:
            self._error(str(e)); return
        if st is None:
            self._error(f"state {self.ctx.cursor.sub!r} missing in {self.ctx.cursor.workflow!r}")
            return
        for t in st.transitions:
            if self._eval_guard(t.when):
                # a SWITCH out of the recovery workflow == recovery finished -> HALT, don't resume.
                if t.switch and t.switch not in ("death", "blind"):
                    self.ctx.offline_recovery = False
                    self._status("offline recovery complete — engine stays OFF")
                    return
                self._transition(t)
                return
        if st.step:
            self._act(st.step)
            self._status(f"step(off):{st.step}")

    def _abandon_offline_recovery(self) -> None:
        """A stuck or no-longer-needed offline recovery must not freeze the reflexes. Drop the latch
        and clear the recovery cursor so the offline path stops diverting into _offline_recover and a
        FUTURE death/blind re-enters cleanly from the top (workflow != death/blind again)."""
        self.ctx.offline_recovery = False
        self.ctx.cursor = Cursor(workflow="", sub="", entered_at=self._now())
        self._status("offline recovery abandoned (active/timeout) — reflexes resume")

    # --- Behavior Lab probes (manual, one-shot; ignore the enabled/gate) -----
    def run_action(self, name: str) -> str | None:
        """Fire a single action/step once against the live ctx. Returns an error
        string if it failed, else None."""
        self.ctx.last_error = None
        self._act(name)
        return self.ctx.last_error

    def eval_guard(self, spec: str) -> tuple[bool, str | None]:
        """Evaluate a guard (no side effects). Returns (result, error)."""
        self.ctx.last_error = None
        val = self._eval_guard(spec)
        return val, self.ctx.last_error

    def probe_enter(self, kind: str, state: str | None = None) -> str | None:
        """Jump the cursor into a workflow/state and run its on_enter, without
        enabling the autonomous loop — to watch an entry sequence live."""
        self.ctx.last_error = None
        try:
            wf = registry.get_workflow(kind)
        except KeyError as e:
            self._error(str(e)); return self.ctx.last_error
        target = state or wf.initial
        if wf.state(target) is None:
            self._error(f"no state {target!r} in {kind!r}"); return self.ctx.last_error
        self.ctx.cursor = Cursor(workflow=kind, sub=target, entered_at=self._now())
        self._run_on_enter(kind, target)
        self._status(f"probe-enter:{kind}.{target}")
        return self.ctx.last_error

    def probe_tick(self) -> None:
        """Run one arbiter pass now, regardless of the enabled flag / gate."""
        self.ctx.last_error = None
        self._arbiter()

    def _transition(self, t) -> None:
        if t.switch:
            if t.sync and self.director is not None:
                self.director.request_switch(self.ctx.sid, t.switch)
                self._status(f"switch(sync):{t.switch}")
            else:
                self.enter_workflow(t.switch)
                self._status(f"switch:{t.switch}")
        elif t.goto:
            self.enter_state(t.goto)
            self._status(f"goto:{t.goto}")

    # --- helpers ------------------------------------------------------------
    def _run_on_enter(self, kind: str, state_name: str) -> None:
        st = registry.get_workflow(kind).state(state_name)
        if st is None:
            return
        for a in st.on_enter:
            self._act(a)

    def _eval_guard(self, spec: str) -> bool:
        name, sep, arg = spec.partition(":")
        try:
            fn = registry.get_guard(name)
            return bool(fn(self.ctx, arg) if sep else fn(self.ctx))
        except Exception as e:                     # a bad guard must not crash the loop
            self._error(f"guard {spec!r}: {e}")
            return False

    def _act(self, name: str) -> None:
        before = self.cmd.n
        try:
            registry.get_action(name)(self.ctx, self.cmd)
        except Exception as e:                     # a bad action must not crash the loop
            self._error(f"action {name!r}: {e}")
        # If this actually emitted a command, consume the server-ready window so
        # neither the prompt-tick nor the periodic driver double-acts before the
        # next prompt. Pure state/setup actions (no command) don't consume it.
        if self.cmd.n > before:
            self.ctx.state.at_prompt = False

    def _error(self, msg: str) -> None:
        self.ctx.last_error = msg
        self._status(f"error: {msg}")

    def _status(self, note: str) -> None:
        if self.on_status is not None:
            c = self.ctx.cursor
            self.on_status({
                "sid": self.ctx.sid,
                "workflow": c.workflow,
                "state": c.sub,
                "note": note,
                "error": self.ctx.last_error,
            })
