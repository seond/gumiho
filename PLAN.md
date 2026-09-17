# Work Plan — Korean MUD Autoplayer (working name: *gumiho* — rename freely)

Phased, incremental build plan for the AI-driven MUD client described in `CLAUDE.md`.
Each phase ships something runnable on its own, and each ends with explicit **exit
criteria** and the **human inputs** it needs. Effort estimates assume part-time,
focused sessions.

**Stack decision (proposed, confirm in Phase 0):** Python 3.12+ / asyncio, raw socket +
minimal IAC filter, SQLite for persistence, Ollama for LLM iteration, MLX for daily
runs on the Mac. Operational UI: a local web GUI (aiohttp + WebSocket, `localhost:8642`)
embedded in the client — terminal passthrough stays available via `--term`.

---

## Phase 0 — Decisions & access *(human-only, ~1 hour)*

Nothing can be built against a real server without these. Needed from you:

1. **Server host and port** of the target MUD.
2. **An account and character**, created manually (character creation is usually an
   interactive Korean form — not worth automating).
3. **Bot policy check** — confirm the server's rules on automation/triggers, and how
   far you're comfortable going (semi-auto with you present vs. unattended).
4. **Primary machine** — Mac/MLX assumed first; Windows/Ollama as a later port.
5. **Confirm the stack** above (or veto Python now, before code exists).
6. `git init` the repo.

**Exit:** all six answered; credentials stored outside the repo (e.g. `.env`, gitignored).

---

## Phase 1 — Wire client + session logging *(1–2 days)*

**Goal:** a thin client that connects, decodes EUC-KR correctly, logs everything, and
lets a human play through it.

Build:
- asyncio TCP client; minimal telnet IAC filter (answer WONT/DONT, strip the rest).
- **Incremental** EUC-KR decoder (`codecs.getincrementaldecoder`) so multi-byte hangul
  split across packets never corrupts.
- Passthrough mode: your keyboard → server, server → your terminal.
- Session logger: raw bytes + decoded text, timestamped, one file per session.

**Human input:** play 30–60 minutes *through this client* and deliberately cover: idle
prompts, movement, `봐`/look, a battle (win one, flee one), a shop transaction, chat
channels, and — if cheap — a death. These logs become the test fixtures for everything
after.

**Exit:** hangul renders perfectly; a labeled log library covering all the above exists.

---

## Phase 2 — Parser & world state *(2–4 days)* — ⚠ risk gate

**Goal:** turn the byte stream into structured state. Everything downstream depends on
this; if it can't be made reliable, stop and fix before proceeding.

Build:
- ANSI escape stripper.
- Prompt detector: HP/MP/MV regex **plus** an idle-timeout fallback for prompts sent
  without a trailing newline.
- Room parser: title, description, exits, entities present.
- Line/event classifier: combat, movement, chat, system, unknown.
- `WorldState` object (current room, vitals, in-battle flag, entities).
- **Replay harness**: re-run any Phase 1 log through the parser as a test.

**Human input:** confirm the exact prompt format and core command vocabulary
(directions, look, buy/sell, flee, rest); ~30 minutes annotating log samples; review
parser output side-by-side against raw logs.

**Exit:** replaying all Phase 1 logs classifies >95% of lines with zero missed prompts.

---

## Phase 3 — Reflex engine + paced sender *(2–3 days)*

**Goal:** the real-time layer — triggers, safety, pacing. No intelligence yet.

Build:
- Trigger engine: regex/event → immediate action, with priorities.
- Command queue with human pacing (randomized 0.4–1.5 s delays, longer after long output).
- Safety monitors: HP below floor → heal or flee; MV depleted → rest.
- Battle-round handler skeleton (attack/skill rotation per round).
- Global kill switch (single keypress drops to passthrough mode).
- Operator advice channel (delivered early, in the web GUI): a dedicated 조언 input
  captures **advice** separately from game commands, each entry stored in
  `knowledge/advice.jsonl` with a **context snapshot** (room, exits, entities,
  vitals) — the coaching channel that Phases 5–6 build on.

**Human input:** set the safety thresholds and the exact heal/flee/rest commands;
approve the initial **action whitelist**; sit with the first semi-automated session.

**Exit:** bot survives a supervised fight and auto-recovers HP/MV without intervention.

---

## Phase 4 — Mapper & scripted autonomy *(3–5 days)* — natural pause point

**Goal:** a competent *non-AI* bot: explores, maps, grinds, comes home.

Build:
- Room identity via title+description hash; directed graph in SQLite.
- Auto-explore (frontier walk) and BFS pathfinding; recall-to-safe-spot.
- Loop detector (same room N times → change strategy).

**Human input:** supervise the first autonomous walkabouts; mark safe zones and
danger zones on the map; spot-check map correctness.

**Exit:** explores ≥50 rooms unattended, returns to start, map survives restart.
**Decision gate:** re-evaluate here — how much does the LLM actually need to own?

---

## Phase 5 — LLM planner *(4–7 days)*

**Goal:** the deliberative layer. Offline evaluation before any live control.

Build:
- Model bake-off via Ollama on captured logs (candidates: EXAONE, Qwen 7–14B —
  strongest small-model Korean). Offline: feed logged situations, rate the decisions.
- Planner prompt: state summary + current goal + explicit **action menu**; output
  constrained to menu choice + rationale (JSON).
- Validation layer: any command not on the whitelist is rejected, never sent.
- Async consult loop: LLM is asked only at calm prompts, never mid-battle round.
- **Advice hot path**: any pending `#` advice is injected verbatim into the next
  planner consult (marked as operator guidance), so coaching changes behavior within
  one decision — no restart, no KB round-trip.
- MLX port once behavior is right under Ollama.

**Human input:** ~1 hour rating bake-off outputs to pick the model; review live
decision transcripts; iterate on the prompt with real failures.

**Exit:** ≥80% of decisions rated acceptable on replayed scenarios; one supervised
live session where the LLM sets goals end-to-end.

---

## Phase 6 — Knowledge base *(3–5 days)*

**Goal:** the "learns the game" requirement, scoped as structured memory.

Build:
- Fact store (SQLite + FTS): auto-extracted facts (shop inventories, NPC locations,
  kill outcomes, command effects).
- Post-event lessons: after deaths/quests, the LLM writes a short free-text lesson.
- **Advice distillation**: each captured `#` advice line + its context snapshot is
  turned by the LLM into a durable KB entry — a generalized lesson with a scope
  (this room / this zone / this NPC / global) and trigger keywords — stored with
  `source: human` provenance and the raw advice kept verbatim alongside.
- **Ack loop**: the bot echoes back its interpretation ("Saved: near 여관, flee if
  two or more enemies") so you can correct it; `#undo` deletes the last entry,
  `#show` lists recent ones.
- Retrieval: relevant facts injected into the planner prompt by room/keyword.
  On conflict, human-sourced entries outrank auto-extracted facts and the model's
  own lessons.

**Human input:** seed with any existing game knowledge (wiki, help text, your own
notes); coach live via the `#` channel whenever you're watching a run; ~15 minutes
weekly pruning wrong or stale facts.

**Exit:** planner demonstrably uses stored facts (e.g. completes "go buy X" using the
KB, not exploration), and a live `#` advice line both changes the next decision and
survives restart as a scoped KB entry.

---

## Phase 7 — Endurance & operations *(2–4 days, then ongoing)*

**Goal:** unattended runs you can trust.

Build:
- Auto-reconnect + scripted login sequence; run scheduler.
- Metrics: XP/gold per hour, deaths, LLM latency, unknown-line rate.
- End-of-run report; hard-stop policies (repeated deaths → disconnect and wait).

**Human input:** decide run windows and monitoring cadence; read run reports; define
what should page you vs. just log.

**Exit:** a 4-hour unattended run with zero interventions and a readable report.

---

## Summary

| Phase | Deliverable | Key human input | Effort |
|---|---|---|---|
| 0 | Decisions & access | server, account, policy, stack confirm | ~1 h |
| 1 | Wire client + logs | 30–60 min manual play session | 1–2 d |
| 2 | Parser & state ⚠ | format confirmation, log annotation | 2–4 d |
| 3 | Reflex engine | thresholds, whitelist, supervision | 2–3 d |
| 4 | Mapper, scripted bot | supervise, mark zones | 3–5 d |
| 5 | LLM planner | model rating, transcript review | 4–7 d |
| 6 | Knowledge base + coaching | seed knowledge, live `#` coaching, weekly pruning | 3–5 d |
| 7 | Endurance & ops | run policy, report review | 2–4 d |

Roughly 3–5 weeks part-time to a supervised LLM-driven bot (end of Phase 5).
Phases 6–7 are iterative after that. Cross-cutting rules: every raw byte is logged
from day one; logs are test fixtures; the LLM never bypasses the command whitelist.
