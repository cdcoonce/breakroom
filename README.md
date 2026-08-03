# breakroom

An office-tower drama sim. Nondeterministic AI characters generate the drama; seeded code resolves the world.

You are the author, the architect, and the meddling god: write the cast as data, build the tower floor by floor (the layout is the org chart — it decides who collides), take contracts, hire from the applicant pool, and twist fates through a director that can only push the story forward — never rewrite it. One tick is one workday. Every day ends in a chronicle episode. Secrets live in a sealed store even you can't casually read, until the exposure machinery earns their reveal.

**SimCity × The Office × Fallout Shelter × SimTower**, played through your AI agent or a plain CLI, with the entire observable world as diffable, versioned files — the repo is the world, and its history is the story.

## Status

First tracer. The Tier 0 PRD is [issue #1](https://github.com/cdcoonce/breakroom/issues/1). Design provenance: [docs/brainstorms/2026-08-02-agent-city-terrarium.md](docs/brainstorms/2026-08-02-agent-city-terrarium.md) (brainstormed, grilled through 25 logged decisions, and interviewed into a PRD on 2026-08-02).

## Quickstart

```bash
uv sync
uv run breakroom init --world .world --seed 42
uv run breakroom tick --world .world
uv run breakroom tick --world .world
```

The starter tracer writes structured state under `.world/state/`, a provisional TOML character under `.world/characters/`, append-only events to `.world/events.jsonl`, and daily chronicle digests under `.world/chronicles/`.

## The bet

Every LLM story game forgets itself — they all patch memory with retrieval over transcripts. breakroom's world state is structured, versioned, and diffable by construction: relationships carry provenance receipts, scars never heal, and the scene brief is assembled from state, not vibes. If structured state can't carry believable long-lived characters, we want to find out fast.
