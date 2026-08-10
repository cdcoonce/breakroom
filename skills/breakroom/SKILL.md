---
name: breakroom
description: Run a breakroom tower through the `breakroom` CLI — creating a world, advancing days, issuing director actions, and reading state. Use whenever a request maps to operating a breakroom tower conversationally instead of editing engine code.
---

# Breakroom CLI adapter

This skill is a thin adapter over the `breakroom` CLI. It maps conversational
requests to exact commands — it does not reimplement any engine logic. If a
command below is listed but missing from `uv run breakroom --help`, the CLI
doesn't support that action yet either; don't invent a flag or a call shape
to fill the gap.

## Setup

`breakroom` is a project console script, not a globally installed binary. From
the repo root, sync the environment once per clone and run every command
through `uv run`:

    uv sync
    uv run breakroom --help

A bare `breakroom …` will fail with command-not-found on a fresh clone. Read
that failure as a missing bootstrap (re-run `uv sync`, check you're in the repo
root), **not** as evidence that the CLI lacks the command.

Before trusting anything below, re-run `uv run breakroom --help` (and the
relevant subcommand's `--help`) and confirm the command/flags are still listed.
This file reflects `--help` output as of when it was written, not a promise of
future shape.

## Sealed-store rule

**Never read, list, or otherwise access anything under `.secrets/` in a world
directory.** That store is sealed: character secrets live there and may only
be revealed through in-fiction exposure mechanics, never by an agent
inspecting the filesystem directly. Do not `cat`, `grep`, `ls`, or Read any
path containing `.secrets/`, even to "just check" a world's state.

## Commands

All commands take an optional `--world PATH` (defaults to the current
directory) pointing at a world created by `init`.

### Create a fresh tower — "start a new world", "init a tower"

    uv run breakroom init [--world PATH] [--seed N]

- `--world PATH`: where to create the world.
- `--seed N`: deterministic RNG seed (defaults to 1).

### Advance the day — "advance the day", "run a tick", "what happens next"

    uv run breakroom tick [--world PATH]

Appends one workday's events and writes that day's chronicle file under
`chronicles/`. **Gap:** this command does not currently print a per-tick cost
line — the CLI cost report described in the design docs (decision calls vs.
narrator calls) is not implemented yet. Don't report a cost figure for a
tick; say plainly that no cost data is surfaced yet.

### God actions — "have management do X", "force an outcome", director actions

**Gap:** the CLI has no `direct` subcommand yet. Until `direct` appears in
`uv run breakroom --help`, there is no way to issue a director action through
this skill — say so rather than guessing a call shape.

### Building and hiring — "add a room", "build out the tower", "hire someone"

**Gap:** the CLI has no `build` or `hire` subcommand yet. State that plainly
rather than fabricating flags for either.

### Reading state, gossip, or a decision trace — "what's the gossip", "show me the trace", "check status"

**Gap:** the CLI has no `read` or `status` subcommand yet, and in particular
no trace-view or gossip-view flag exists to name. Do not invent one. Once
`read` lands, this section should be rewritten against whatever flags
`uv run breakroom read --help` actually reports.

## Why so many gaps

`init` and `tick` are the only two commands this CLI currently exposes. The
remaining experimenter verbs (`build`, `hire`, `direct`, `read`, `status`)
are scoped to a separate issue; a verb missing from `--help` means that work
hasn't landed, not a reason for this skill to guess at its shape.
