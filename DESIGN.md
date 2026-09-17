# gumiho — Engine Design Spec

Status: **agreed design, pre-build** (2026-08-23). This is the spec we build against.
It supersedes the free-form research-phase logic (planner LLM hot-loop, ad-hoc walker
decisions). It does **not** throw away the research-phase code — the parser, event model,
graph mapper, telnet layer, and state store are kept as the substrate.

---

## 0. The shift

The research phase proved one thing decisively: **an LLM in the per-turn decision loop
does not work** — too slow, too inaccurate, and the pace is server-controlled. The
intelligence has to live in the *structure*, not in per-turn inference.

So the engine is built on three commitments:

1. **Structure over inference.** Behavior is explicit, authored workflows (state
   machines) and zero-latency reflexes. Nothing in the hot loop asks an LLM "what next?".
2. **Mendability without reconnect.** The connection and the character sessions are
   long-lived and expensive to re-establish. Editing behavior must take effect on the
   next prompt with no disconnect. (See §3 — this is the dominant architectural driver.)
3. **The LLM is an offline analyst.** It answers knowledge questions *between* sessions
   (what to stock, which zone, a mob's historic damage) and writes structured advice
   into the state store. It is never on the critical path.

---

## 1. Component map (the FRAMEWORK.md A–G, made concrete)

```
 SERVER (telnet, EUC-KR)
   │ bytes
   ▼
 TELNET/CLIENT ─► PARSER ─► EVENTS            [substrate · keep from research phase]
                              │
                              ▼
┌───────────────────────────────────────────────────────────────┐
│ A · WORLD STATE + KNOWLEDGE   (single source of truth, DATA)    │
│   live:   HP/MP/MV, room, entities, ACTIVE BUFFS+EXPIRY, inv    │
│   stored: zones, mobs(dmg/exp), potions, buffs, nav paths       │
└───────────────┬───────────────────────────────────────────────┘
                ▼  every prompt / event
┌───────────────────────────────────────────────────────────────┐
│ D · CONTEXT MANAGER  ── one active workflow per character ──    │
│     Travel · Hunt · Restock · Rest · Retreat · Wait · Death     │
│     arms the matching reflex bundle, runs the matching step     │
└───────┬───────────────────────────────────────┬───────────────┘
        ▼ priority 1: URGENT                      ▼ priority 2: CALM
┌───────────────────────┐          ┌────────────────────────────┐
│ C · REFLEX BUNDLE      │  before  │ B · TASK DRIVER            │
│  armed pattern→action  │ ───────► │  the active workflow's     │
│  0-latency             │          │  next deliberate step      │
└───────────┬───────────┘          └───────────────┬────────────┘
            └───────────────┬───────────────────────┘
                            ▼
                  COMMAND QUEUE (human pace) → SERVER

 E · TOOLS      Map+Nav: inter-zone macros + in-zone coverage      [B calls these]
 F · LOCAL LLM  offline analyst: writes zone/potion/mob advice → A
 G · UI         game console + state / entity / map viewers
 (duo) DIRECTOR thin coordinator for shared decisions (see §7)
```

### Component → module map

| Comp | Role | Module(s) | Keep / New |
|------|------|-----------|------------|
| — | connection | `telnet.py`, `client.py`, `login.py` | keep |
| — | parse → events | `parser.py`, `events.py` | keep (patterns → reloadable later) |
| A | live state | `state.py` | keep + expand (buffs, inventory, timers) |
| A | knowledge | `knowledge/*.sqlite` + tables | keep + expand |
| D | context manager | `engine.py` | **new** (interpreter loop) |
| B | task driver | `behavior/hooks/*.py` (steps) | **new** (replaces `planner.py` hot-loop) |
| C | reflexes | `behavior/reflexes/*.toml` + hooks | **new** (formalizes `reflex.py`) |
| — | hot-reload | `registry.py`, `reload.py` | **new** |
| — | command pacing | `command_queue.py` | **new** (extract from client) |
| E | in-zone map | `mapper.py`, `mapgrid.py` | keep |
| E | navigation | `nav.py` | **new** (macros + coverage facade) |
| F | LLM analyst | `advisor.py`, `llm.py` | new + keep |
| G | UI | `webui.py`, `static/` | keep + trim to viewers |
| duo | director | `director.py` | **new** (formalizes `duo.py`) |

---

## 2. The control model

The engine is an **interpreter**, not compiled behavior. Every server prompt (and
relevant mid-prompt events), for each character, it runs the **arbiter**:

```
on_prompt(character):
    s = state[character]                       # A, all live data
    wf = workflows[s.workflow]                 # D, looked up BY NAME (fresh each tick)
    # 1. REFLEXES (C) — highest priority, zero deliberation
    for reflex in wf.armed_bundles(s):         # bundles this state arms
        if guard[reflex.when](s):
            action[reflex.do](s, cmd); return  # a reflex fired → done this tick
    # 2. TASK DRIVER (B) — one calm, deliberate step
    for t in wf.state(s.sub).transitions:
        if guard[t.when](s):
            enter(s, t.target); return         # transition (may switch workflow)
    step[wf.state(s.sub).step](s, cmd)         # else: take the state's step
```

Key properties:

- **No behavior state in code.** `wf`, `guard`, `action`, `step` are stateless
  definitions/functions resolved by name. Every counter ("rooms cleared", "buffs
  requested") lives in `s` (A). This is what makes hot-reload lossless.
- **Reflexes always win.** Urgent survival (drink, fight back, wake, flee) never waits
  on the task driver, and stays live even while you're editing a workflow.
- **One deliberate action per tick**, at human pace (see §8 pacing).

---

## 3. Mendability (the dominant driver)

A hard boundary between the **substrate** (owns the sockets, never restarts) and the
**behavior** (hot-swappable):

```
╔═══════════ STABLE SUBSTRATE (never restarts, owns the sockets) ═══════════╗
║  telnet ⇄ parser ⇄  A: WORLD STATE  (all data, no code)                    ║
║  command queue      engine LOOP (pure interpreter, resolves by name)       ║
║  UI server          auto-reconnect + rehydrate (봐/점수) on link drop       ║
╚═════════════════════════════════╤═════════════════════════════════════════╝
                                  │ resolves definitions by NAME each prompt
                     ┌────────────▼──────────────┐  ◄── file-watch (auto-on-save)
                     │   HOT-SWAPPABLE BEHAVIOR   │
                     │   workflows/*.toml         │
                     │   reflexes/*.toml          │
                     │   hooks/*.py (name registry)│
                     │   knowledge tables         │
                     └────────────────────────────┘
```

**Decided rules (2026-08-23):**

- **Syntax guard only.** On save: parse the file / import the hook module. If it parses,
  swap it into the registry. If it raises, keep the last-good version and surface the
  error in the UI. No attempt to validate "semantic" correctness.
- **Auto-on-save.** A file watcher on `behavior/` triggers the reload. No button.
- **One workflow per kind.** Exactly one `hunting`, one `restock`, etc. Editing the file
  *is* the update. No variants, no versioning.
- **Reconciliation = re-enter from the top.** When the workflow a character is *currently
  running* changes, it restarts that workflow from its `initial` state and re-runs entry
  logic. Live game state in A (HP, position, active buffs, connection) is untouched;
  only in-workflow progress (e.g. the coverage sweep) resets. Editing a workflow the
  character is *not* in just updates it for the next entry.

**Reload mechanics:**

- `registry.py` holds `guard`, `action`, `step` name→fn maps and the parsed workflow /
  reflex definitions. The engine only ever looks things up here.
- `reload.py` watches `behavior/`, on change re-parses the affected file (`tomllib` for
  `.toml`, `importlib` for hook modules), and on success atomically replaces the registry
  entry. On a hook-module reload it re-runs the module's `@guard/@action/@step`
  registrations, so name bindings are refreshed without holding stale references.
- Boundary of the guarantee: workflow/reflex/hook/knowledge edits are live. Substrate
  changes (telnet/parser core, an A schema change) still need a restart. We minimize this
  by keeping parser *patterns* in a reloadable table (a lot of research-phase iteration
  was "the server said a new thing we didn't parse").

---

## 4. State store (A)

Single source of truth. Two tiers:

**Live state** (`state.py`, per character, in memory, mirrored to disk for reconnect):
- vitals: `hp/mp/mv` (current + max), hunger/thirst flags
- location: current room node id, zone, exits, entities in room
- **buffs**: `{name: {cast_at, expires_at}}` — tracked per §6
- inventory: potions counts, food, drink bottle level, gear + endurance
- workflow cursor: `workflow` (kind), `sub` (state name), per-workflow scratch counters
- co-location: last sighting of partner (see §7)

**Knowledge** (SQLite + reloadable tables under `behavior/knowledge/`):
- `zones`: name, entry-macro key, danger, exp-per-level fit
- `mobs`: keyword, zone, observed exp, observed damage range, first-attacks?
- `potions`: item, kind (hp/mp), restore amount, buy location, price
- `buffs`: name, required-for (class), observed duration, cast phrase
- `paths`: inter-zone macros (§9), in-zone coverage is derived live from `mapper`

Guards/actions read A through a thin typed accessor so hook code stays readable
(`s.hp_pct()`, `s.buffs.active("방비")`, `s.room.mobs()`, `s.potions("hp")`).

---

## 5. Authoring format (hybrid)

### Workflow — `behavior/workflows/hunting.toml`

```toml
kind    = "hunting"
initial = "ensure_buffs"

[state.ensure_buffs]
arm      = ["survival", "combat"]        # reflex bundles armed in this state
on_enter = ["request_missing_buffs"]     # actions run once on entry
step     = "await_buffs"                  # the calm step
transition = [
  { when = "all_buffs_active", goto = "clear_room" },
]

[state.clear_room]
arm  = ["survival", "combat"]
step = "attack_next_mob"
transition = [
  { when = "buff_expired",  goto   = "ensure_buffs" },
  { when = "room_clear",    goto   = "advance"      },
  { when = "hp_or_mp_low_and_safe", switch = "rest",    sync = true },
  { when = "potions_low",           switch = "restock", sync = true },
  { when = "self_died",             switch = "death"                },
]

[state.advance]
arm  = ["survival"]
step = "move_to_next_room"                # uses E (coverage planner)
transition = [
  { when = "zone_cleared", switch = "wait" },   # all rooms empty → regen wait
  { when = "arrived",      goto   = "clear_room" },
]
```

- `goto` = a state in **this** workflow. `switch` = hand off to **another** workflow
  (kind). `sync = true` routes the switch through the director so the duo switches
  together (§7).
- Guards (`when`) and steps/actions are **names**, resolved in the registry.

### Reflex bundle — `behavior/reflexes/survival.toml`

```toml
name = "survival"
reflex = [
  { when = "hp_below:40",      do = "drink_hp_potion",  priority = 100 },
  { when = "asleep_and_full",  do = "wake_up",          priority = 90  },
  { when = "attacked_in_safe_transit", do = "flee",     priority = 80  },
]
```

### Hooks — `behavior/hooks/hunting.py`

```python
from gumiho.registry import guard, action, step

@guard("all_buffs_active")
def _(s): return all(s.buffs.active(b) for b in s.required_buffs())

@guard("room_clear")
def _(s): return not s.room.hostiles()

@action("request_missing_buffs")
def _(s, cmd):
    for b in s.required_buffs():
        if not s.buffs.active(b):
            cmd.say(f"{b}!")            # leader requests via 말; supporter reflex answers

@step("attack_next_mob")
def _(s, cmd):
    m = s.room.next_target()           # skips known players (KNOWN_PLAYERS safety)
    if m: cmd.send(f"{m.keyword} 공격")
```

`s` = state accessor (A). `cmd` = command queue (`.send`, `.say`, paced). Guards are pure
reads; actions/steps read state and emit commands. All logic here reloads on save.

---

## 6. Buff tracking

Per FRAMEWORK.md, buffs have per-spell expiries and must be re-requested.

- On session start, `ensure_buffs` assumes **no** buffs active and requests all
  `required_buffs()` for the leader's class.
- The parser recognizes cast-confirmations ("누군가 당신을 보호함을 느낍니다") and
  expiry warnings ("당신은 보호가 덜해지는 것을 느낍니다") → updates `s.buffs`.
- Duration per buff is **learned**: on a confirmed cast we stamp `cast_at`; on the
  observed expiry we record `expires_at - cast_at` into the `buffs` knowledge table
  (rolling estimate). `buff_expired` guard fires slightly *before* the estimated expiry
  so the re-request overlaps.
- The supporter side is a reflex: `on 말 request matching "<spell>!"` → cast
  `"<leader> <spell> 걸어"`, retry on 집중 failure, stop on 허공에 메아리 / no-MP
  (the working research-phase protocol, formalized as a reflex bundle).

---

## 7. Duo model — two peer engines + thin director

Each character runs its **own** engine (its own workflow, its own reflexes, its own
state in A). They coordinate the way a human duo does — through the game:

- **In-game channel:** leader `말` requests → supporter reflex casts; sighting-based
  co-location (partner name in `state.entities`) is the authoritative "are we together?"
  signal (never coordinate-compare — the research phase proved that fails).
- **Director** (`director.py`) owns only shared decisions, reading both characters'
  state from A:
  - `sync`-flagged workflow switches: both enter `restock` / `rest` / regroup together.
  - post-death regroup: when one finishes `death` recovery, re-establish follow + group +
    re-buff before resuming `hunting`.
  - session start/stop.
- The director is **behavior too** (hot-reloadable); its coordination state lives in A.
- Fault isolation: if the leader's workflow errors mid-edit, the supporter's survival
  reflexes still keep it alive.

Reload interaction: a `sync` switch or a reload that re-enters `hunting` on both prompts
the director to re-sync (follow/group/buffs) as part of `on_enter`.

---

## 8. Command pacing & safety (invariants that survive all edits)

These live in the substrate, not in editable behavior, so a bad edit can't violate them:

- **Human pace:** the command queue enforces a minimum inter-command delay (jittered) and
  never floods — the anti-bot invariant. Reflexes may pre-empt the queue but still respect
  the floor.
- **Never attack a player:** `next_target()` and the attack action consult
  `KNOWN_PLAYERS` (seeded from `.env` + the `누구` roster); a player is never targeted.
  This stays in the substrate accessor, not in reloadable hooks.
- **Insurance:** `보험` maintained by a director tick (death safety net).
- **Reconnect throttle:** auto-reconnect respects the ~75s server throttle.

---

## 9. Navigation (E)

Two roles, matching FRAMEWORK.md:

- **Inter-zone (world travel):** predefined action macros from the `중앙 광장` anchor to a
  destination zone, stored in `paths` keyed by zone name. If a destination has no macro,
  the `travel` workflow enters **record mode**: human is prompted (UI) to drive from
  중앙 광장 to the destination; the actions are recorded and saved under the zone name.
- **In-zone (coverage):** the graph mapper (`mapper.py` + `mapgrid.py`, kept) builds the
  zone graph live from walked edges and the `지도` command. The coverage planner walks a
  route that covers every room without random straying (formalize the walker's
  frontier/coverage logic; drop the ad-hoc random fallback).

The manhole/warp knowledge (`knowledge/notes/맨홀.md`) and graph-node identity from the
research phase carry over unchanged.

---

## 10. LLM analyst (F) — offline only

Runs on Ollama/MLX, invoked between sessions or on explicit UI request. Reads accumulated
knowledge in A and writes structured advice back into A:

- which zone fits the leader's current level (exp/risk balance)
- what and how many potions to stock
- a mob's historic damage range / whether it first-attacks

Output is always **data written into knowledge tables** that guards/steps read — never a
live command. Example: `advisor.recommend_zone(level)` → updates `zones.fit` → the
`travel` workflow's `pick_zone` step reads it.

---

## 11. UI (G)

Trim `webui.py` to:
- the embedded game console(s) for both characters (keep)
- live engine panel: current workflow + state, armed reflexes, last transition + why,
  reload status / last syntax error
- **entity viewers**: zones / mobs / potions / buffs tables, editable where FRAMEWORK.md
  needs human input
- **local map viewer**: the zone graph, with click-to-mark hunt / no-hunt rooms

---

## 12. Build plan

**Milestone 1 — the live-tunable spine.**
Substrate (telnet/parser/A/command-queue/engine/reload/UI-panel) + `travel → hunt → rest`
+ `survival`/`combat` reflex bundles + the two-engine/director skeleton. Exit: a duo that
reliably travels to a zone, clears rooms on a coverage path, drinks/fights via reflexes,
and sleeps to recover — and every bit of that is hot-editable with no reconnect.

**Milestone 2 — restock.** `restock` workflow (대장간 repair · 떡집 시루떡 · 분수 water),
`sync`-switched for the duo, bundled trip.

**Milestone 3 — resilience.** `death` recovery (시체수습 → wear → 시체 묻어 → 보험 →
regroup) + `wait` (mob regen ~20m then resume).

**Milestone 4 — intelligence & polish.** LLM advisor writing zone/potion advice; entity +
map UI viewers with human-input hooks.

Each milestone ships runnable; later milestones are added largely *by mending* the running
system, which is the whole point.

---

## 13. What carries over vs. what's replaced

**Keep:** `telnet/client/login`, `parser/events`, `state` (expanded), `mapper/mapgrid`,
graph-node identity + manhole/warp knowledge, `누구`/KNOWN_PLAYERS player-safety, `보험`,
the `말` spell-support protocol, EUC-KR incremental decode, `logs/` fixture corpus,
`replay.py` harness.

**Replace:** `planner.py` (LLM hot-loop) → workflows + task driver; `walker.py` ad-hoc
decisions → `hunting` workflow + coverage planner; `duo.py` → peer engines + `director.py`;
`reflex.py` → declarative reflex bundles + hooks.

**Retire from the hot path:** all per-turn LLM calls.
