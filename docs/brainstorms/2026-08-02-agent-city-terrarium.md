---
date: 2026-08-02
description: "Brainstorm brief + grill decisions + research reframe (all 2026-08-02): breakroom — a continuous office-tower drama sim reframed RESEARCH-PRIMARY: an instrumented terrarium observing AI characters' decisions (norm-breaking, power, model signatures, drift) with the game systems as naturalistic pressure; repo live at cdcoonce/breakroom, PRD = issue #1"
tags:
  - idea
  - game
  - brainstorm
  - decision-record
status: idea
---

# Agent City / The Terrarium — brainstorm brief (grilled)

**Provenance.** Brainstormed and committed 2026-08-02; grilled the same day (/grill-me, 25 logged decisions). This file reflects the post-grill state; the grill corrected five load-bearing brainstorm commitments (genre, run shape, wedge size, gate, build method).

**Problem.** Build a game as a real side project: a nondeterministic character-drama world that produces curiosity and tension, keeps surprising its player, and can grow into a big, grand, possibly not-solo thing.

**The game.** **SimCity × The Office × Fallout Shelter × SimTower.** One continuous office tower you construct and staff. **Character drama is the tension engine** — relationships, rivalries, schemes; the management sim is the stage machinery that generates collisions. Fallout contributes Shelter's mechanics only (characters with stats assigned to rooms, cascading incidents, resource dials) — no post-apoc setting or IP adjacency. Your verbs (author+god+builder seat, never protagonist): hand-author characters, build the tower (layout is org design — it decides who meets whom), inject events through a director agent, hire from a sim-generated applicant pool (interview/edit/approve — authoring stays a continuous verb).

**World rules (grilled).**

- **One continuous world, no fail state, no seasons.** Tension comes from ongoing pressure and permanence, not restart stakes.
- **Deaths AND scars.** Incidents can rarely, code-decidedly kill or drive out a character; every social wound — grudges, broken trust, reputations — persists and compounds forever.
- **Sim seeded, drama sampled.** Mechanical events resolve from seeded RNG over state files (auditable, testable); character choices and dialogue are model-sampled. Code owns every outcome; the model owns only what characters attempt and say.
- **Secrets live in a sealed private store.** The observable world — everything characters _do_ — is public, git-native, diffable. Schemes, crushes, and inner monologue live in a non-public store the sim reads but neither the player nor the public casually browses; surprise stays real even for the author.

**Tier 0 wedge — openly 4–6 months** (a work-content sizing at side-focus pace, not a deadline — calendar may stretch with the semester; resized at grill because rich building won over the lean test). In scope: rich construction (elevators, utilities, room upgrades, expansion economics), 5–8 hand-authored founding characters + applicant-pool hiring, director agent (forward-only, **no retcons**; history immutable), chronicler rendering per-tick markdown episode digests in-repo, on-demand ticks, sealed-secrets store, 60-second onboarding. Chronicle _site_ is a gate-pass reward, not wedge scope.

**Tiering.** Tier 1 (city-scale cast, scheduled persistence, community PR-authored characters, chronicle site, local inference on [[homelab-inference-box]]) and Tier 2 (spectacle first, never authoring-platform first — StoryNexus died as one) survive unchanged as vision context. Community-vs-venture fork decided at the Tier 1 boundary with real players.

**Gate (Tier 0 → Tier 1): milestone + verdict.** Each trial player (Charles + at least two outsiders) reaches a defined milestone — order of: N floors built, cast size reached, a cascade survived (cascade = a Shelter-style chained incident; the incident/cascade system itself is PRD-defined) — then votes fun/not. Exact milestones set in the PRD.

**Success & failure (grilled).** Failure = **it becomes a chore to run** or **the drama reads as fake** (generic LLM prose instead of characters with real history). Explicitly _not_ failure: small player counts, slow outsider reach. This is a builder-joy, artifact-quality project; audience is upside. Funding gets a **thin thread** only: name/branding/repo structure chosen so a product could inherit them; zero feature concessions.

**Execution constraints (grilled).**

- **Start now, pulse-paced.** ASU Fall (CSE 511 + HSE 542) starts ~3 weeks in; no date commitments — [[pulse-weekly-work-ledger|/pulse]] is the honesty meter.
- **Primary hand-time slot.** ragmark/afk/homebase work continues through the afk pipeline, not Charles's hands.
- **afk builds from early on.** Backlog specced as afk-buildable issues (cold-read gate applies); Charles's hands take design, authoring, drama tuning, play. Accepted risk, eyes open: joy project must not decay into issue-writing ops — watch via /debrief.
- **Inference ceiling ~$20–50/mo** hosted: Haiku-class routine beats, Sonnet-class chronicle prose and big moments; local inference is a Tier 1 lever.
- **Public repo from day one**; chronicles are the shareable artifact.
- **Testing:** golden-tick harness (mechanical layer deterministic under test, property tests, LLM mocked) + GitHub Actions CI on **both Linux and macOS** from first commit (closes the known Linux-only-gate blind spot). Drama validation is **own-read only** by explicit choice (flagged against the cold-reader lesson; the gate's outsiders are the late external check).
- Runtime assumption: Python on uv.

**Engine invariants (updated at grill).**

1. Resolution is code, not vibes: outcomes computed from seeded RNG + state; the LLM narrates and proposes, never decides.
2. Git-native, event-sourced, diffable **observable** world; history immutable (secrets exempted into the sealed store).
3. Multiplayer-ready schema from day one — meaning: the state schema must not foreclose a future shared world. Tier 0 instances are per-player and fully independent; shared-world tenancy arrives Tier 1+.
4. LLM improvises inside a code-owned storylet/quality skeleton — storylets = precondition-gated scene templates the code selects; qualities = named state on characters/rooms/relationships that gates them. The model fills selected scenes; it never picks what is possible.
5. Install → first felt moment in under sixty seconds.

**Killed options (brainstorm) + grill corrections.**

- _Mystery genre_ — brainstorm's tension engine, killed at grill: drama emerges from management collisions, not a solvable core.
- _Restartable seasons_ — killed at grill for one continuous world; "different every run" became "the world keeps surprising you" (per-player towers diverge).
- _Git-spine solo story_ — protagonist seat; Charles chose author+god+builder. Substrate survives as the state layer.
- _Dice-under-the-prose roguelike_ — full procedural core as product; survives as invariant #1.
- _Persona arena_ — author+spectate as product; absorbed as a Tier 1+ episode lens.
- _One World, Many Hands_ — community-first; cold-start-fatal as v1; arrives Tier 1 as PR-authored characters.
- _Haunted Workspace_ — ambient intrusion; only its onboarding lesson survives (invariant #5).
- [[afk-agent-sim-game]] (June note) — superseded in spirit; the afk-telemetry version stays parked.

**Premortem risks (standing, post-grill).** 1) Systems-first months starve the drama question — rich building means The Office arrives after SimTower; mitigate by proving a minimal drama loop early _within_ the build order the PRD sets. 2) Coherence mush as cast grows — storylet skeleton, small casts per scene. 3) Chore-to-run creep (a declared failure mode) — on-demand ticks, cost ceiling, /debrief watch on the afk ops load. 4) Fake-drama undetected by an attached author validating own-read only — the flagged risk Charles accepted. 5) Semester throttling stalls momentum — pulse-paced expectations, no dates.

**Open questions (the PRD interview's agenda).**

- Character file format and the stat model (what a character _is_; what rooms read from them).
- Director-agent action vocabulary (legal god-moves; forward-only boundary already fixed).
- Tick semantics: what one tick computes, event ordering, what the chronicle digest includes.
- Per-character memory architecture at Tier 0 scale.
- Room-type menu, adjacency rules, and building economics (the rich-building spec).
- Applicant generation: cadence, quality dials, how much the sim knows about what the tower needs.
- Milestone definitions for the gate; how the two outsiders run a tower (distribution mechanism) and how they're sourced (public-repo recruiting, friends) and when.
- The storylet/quality skeleton's Tier 0 design: scene-template inventory, quality vocabulary, selection rules.
- The incident/cascade system: triggers, chaining rules, what stops a cascade (feeds the gate milestone).
- Sealed-store implementation (gitignored local? encrypted in-repo? separate private repo?).
- Repo creation (in PRD scope), name, and the thin-thread branding.

**Routing.** `write-a-prd` for **Tier 0 only**; the PRD must decompose into afk-buildable issues (prd-to-issues, cold-read gated). Copy this brief to the game repo's `docs/brainstorms/` once it exists.

## Reframe addendum — research-primary (2026-08-02, post-grill, during PRD interview)

Charles reframed mid-PRD: **breakroom is research-primary.** An instrumented terrarium for observing what AI characters _decide_ — the game systems are the naturalistic pressure source, not the product. The build is ~90% unchanged; the success layer is rewritten. Where this addendum conflicts with the grilled body above, the addendum wins.

- **Name/repo:** `breakroom` — live at [cdcoonce/breakroom](https://github.com/cdcoonce/breakroom), public; PRD is issue #1. Interview decisions (rounds A–E) are recorded in the PRD.
- **Invariant #1 splits — physics vs norms.** Physics stays code-owned (resources, incidents, consequences). Norms (policies, duties, social rules) are **soft**: characters can attempt violations; code prices consequences, never prevents attempts. Rule-breaking becomes an observable choice. The game frame wanted this too.
- **Observables (all four, day one):** norm compliance under pressure; emergent power/status dynamics; model-as-personality comparison (per-character model assignment); character-integrity drift vs authored values.
- **Instrument:** decision-trace records (context ref, choice, norm tags, model id) as a first-class module; director actions logged as **labeled interventions** distinct from natural events; trace export views for the four observables.
- **Call-shape consequence:** flagged decision points resolve per-character on that character's model (research-valid attribution, small calls); scene performance renders in one narrator-model call (cost + coherence). This supersedes the plain one-call-per-scene reading.
- **Gate rewritten (replaces outsider fun-votes):** world proof (5 floors / 12 cast / 1 cascade survived / ~30 fiction days) + instrument proof (≥3 observation write-ups drawn from traces; a stranger can replicate a tower with their own keys) + Charles's own verdict (drama reads real; running it is still fun).
- **Audience reframe:** players → observers, readers, and replicators. Rigor posture is explicit: n=1 naturalistic case studies and an open instrument — observations, not claims.
- **Decomposed same day:** 29 tracer-first slices filed as [breakroom#2–#30](https://github.com/cdcoonce/breakroom/issues) — tracer (#3) and the design quartet (#4–#7) HITL, 22 fleet-shaped slices labeled `proposed`/`afk-sized`; golden-tick CI gate is #30. afk enrollment of the repo is the remaining ops step before the fleet can drain.

## Related

- [[afk-agent-sim-game]] — June sibling, superseded in spirit.
- [[afk-agent-system]] — build pipeline for the backlog AND the orchestration craft the engine leans on.
- [[homelab-inference-box]] — Tier 1 inference home.
- [[pulse-weekly-work-ledger]] — the pacing honesty meter through the semester.
- [[orbit-wars]] — watch-the-agents portfolio kin.
- [[charleslikesdata.com]] — chronicle-site surface at Tier 1.
