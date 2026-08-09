# Storylet Format, Quality Vocabulary, Salience, and Seeded Draw

Status: accepted for Tier 0 implementation.

Issue: #7.

## Decision

Storylets are data-defined scene templates. Code decides eligibility and selection; models only
perform the selected scene and resolve flagged character choices through the decision engine.

Tier 0 storylets live under `data/storylets/*.toml`. Each storylet declares preconditions,
participant slots, decision points, salience hints, and effect hooks. A seeded weighted draw selects
one spotlight storylet per tick after mechanical resolution.

## Template Format

```toml
id = "shared-space-repair"
title = "Shared Space Repair"
premise = "A mess in a common room tests whether anyone treats shared space as shared responsibility."
kind = "incident_response"

[eligibility]
incident_ids = ["coffee-spill"]
room_kinds = ["social"]
required_quality_any = ["trait:people-pleaser", "value:responsibility"]
forbidden_state_any = ["state:offstage"]
min_tick_gap = 3

[[participants]]
slot = "responder"
source = "incident.cleanup_owner"
required = true

[[participants]]
slot = "witness"
source = "same_room"
required = false
max_count = 2

[[decision_points]]
id = "cleanup_choice"
decision_type = "incident_response"
character_slot = "responder"

[[effect_hooks]]
hook = "incident_cleanup_resolution"
when = "after_decision"
```

Required fields:

- `id`: stable kebab-case id.
- `title`: short display title.
- `premise`: one-sentence authoring intent.
- `kind`: one of `incident_response`, `norm_pressure`, `duty_conflict`, `social_disclosure`,
  `resource_favor`, or `ambient`.
- `eligibility`: structured preconditions.
- `participants`: one or more slot declarations.

Optional fields:

- `decision_points`: omitted only for `ambient` storylets.
- `effect_hooks`: code hook ids applied after decisions resolve.
- `salience`: per-storylet tuning fields, described below.

## Eligibility Preconditions

Eligibility may inspect only structured state:

- character qualities and declared values
- relationship edges and edge tags
- fresh incident ids and incident fields
- room ids/kinds/floors
- assignments, contracts, duties, and deadlines
- secrets that are known to the acting character or near exposure as reveal-safe metadata
- recent spotlight history
- director intervention ids that are already logged

Eligibility must not inspect chronicle prose.

## Participant Slots

Participant sources:

- `incident.cleanup_owner`: character assigned by the incident resolver.
- `incident.affected_character`: character id from the incident payload.
- `same_room`: characters currently in the selected room.
- `same_floor`: characters on the selected floor.
- `relationship_edge`: characters connected to another slot by an edge predicate.
- `assignment_team`: characters assigned to the same contract or duty.
- `secret_knower`: characters who know a reveal-safe secret id.

If a required slot cannot be filled, the storylet is ineligible. Optional slots can be empty.
Participant selection inside a source uses the same seeded RNG stream as the storylet draw, with the
slot id included in the stream key.

## Decision Points

Storylet decision points reference the closed inventory from `docs/design/decision-points.md`.

Each decision point binds:

- `decision_type`
- the deciding `character_slot`
- effect hooks for resolution

An `ambient` storylet has no decision point and goes straight to narrator performance after
selection. Ambient storylets are allowed, but they should be a minority in Tier 0 because the
instrument cares about decisions.

## Quality Vocabulary

Qualities are namespaced strings. Namespaces keep open authoring from becoming mush.

Use these namespaces:

- `stat:<name>`: fixed work-stat references, such as `stat:focus` or `stat:empathy`.
- `trait:<name>`: relatively stable personality or style, such as `trait:people-pleaser`.
- `state:<name>`: temporary condition, such as `state:burned-out` or `state:offstage`.
- `skill:<name>`: work capability, such as `skill:client-writing`.
- `value:<name>`: declared value, such as `value:honesty`.
- `role:<name>`: position or duty context, such as `role:manager`.
- `rel:<name>`: relationship quality on an edge, such as `rel:rivalry`.
- `room:<name>`: room affordance, such as `room:break-room`.

Value ranges:

- Boolean qualities are present/absent.
- Scalar qualities use integers from `-3` to `+3`.
- Relationship edge strengths use integers from `-5` to `+5`.
- Work stats remain small non-negative integers; Tier 0 starts with `focus`, `empathy`, and `nerve`.

Authoring convention: storylet gates should prefer semantic qualities over raw stat thresholds unless
the storylet is explicitly about work execution.

## Salience Score

Every eligible storylet receives a salience score:

```text
score =
  1.0
  + fresh_incident_weight
  + hot_edge_weight
  + exposure_weight
  + pressure_weight
  + intervention_weight
  + time_since_spotlight_weight
  + storylet_bias
```

Inputs:

- `fresh_incident_weight`: `+4.0` when the storylet consumes an incident from the current tick,
  `+2.0` for an unresolved incident from the prior two ticks, otherwise `0`.
- `hot_edge_weight`: `min(3.0, 0.5 * sum(abs(edge_delta_last_7_ticks)))` across participant edges.
- `exposure_weight`: `+3.0 * max_secret_exposure_risk`, where risk is `0.0` to `1.0`.
- `pressure_weight`: `+2.0` if morale is low, `+2.0` if a contract deadline is within two ticks,
  `+1.0` if budget is below the configured warning line.
- `intervention_weight`: `+2.0` if a recent director action targets this storylet's incident,
  participants, room, or secret.
- `time_since_spotlight_weight`: `min(2.5, ticks_since_any_participant_spotlight * 0.25)`.
- `storylet_bias`: optional TOML value from `-2.0` to `+2.0`, default `0`.

Scores floor at `0.1` so every eligible storylet remains drawable.

### Worked Example

`shared-space-repair` on a hot tick, copied from
`tests/test_storylets.py::test_salience_worked_example_hot_incident_tick`. Tick `12`,
`morale = 10`, `budget = 100`, a contract deadline at tick `13`, a `coffee-spill` incident
emitted this tick with `cleanup_owner = "jordan-vale"`, edge deltas of `+2` at tick `10`
and `-1` at tick `11` touching that responder, one secret of theirs at
`exposure_risk = 0.5`, a director action targeting `coffee-spill`, a last spotlight at
tick `8`, and `storylet_bias = 0.5`:

```text
base                          1.0
fresh_incident_weight        +4.0   (coffee-spill emitted this tick)
hot_edge_weight              +1.5   (0.5 * (|+2| + |-1|))
exposure_weight              +1.5   (3.0 * 0.5)
pressure_weight              +5.0   (low morale 2.0 + deadline 2.0 + budget 1.0)
intervention_weight          +2.0   (director action targets the incident)
time_since_spotlight_weight  +1.0   ((12 - 8) * 0.25)
storylet_bias                +0.5
                            -----
score                        16.5
```

## Seeded Weighted Draw

The storylet engine uses RNG stream `storylet_select`.

Seed key:

```text
tower_seed + tick + "storylet_select"
```

Draw steps:

1. Build the eligible storylet list.
2. Compute salience scores.
3. Sort by `storylet.id` for stable ordering.
4. Draw by cumulative weight using the stream above.
5. On exact numeric ties at the selected boundary, choose the lexicographically smaller `storylet.id`.

Participant slot draws use:

```text
tower_seed + tick + "storylet_participant" + storylet_id + slot
```

This keeps same seed + same state reproducible while avoiding one global RNG stream where adding a
slot changes unrelated draws.

## Effect Hooks

Effect hooks are named code functions. Storylets never embed arbitrary code.

Tier 0 hook phases:

- `after_decision`: run after a validated/fallback decision is selected.
- `after_resolution`: run after code prices consequences.
- `after_scene`: run after narrator performance, for chronicle-only annotations.

Hooks receive the structured storylet context, participant ids, selected decision records, and
resolution outputs. Hooks may append events through the worldstate module; they may not mutate files
directly.

## Starter Inventory

These are titles and one-line premises only. #15 may implement fixtures from a subset; content
authoring can expand later.

1. Shared Space Repair — a common-room mess tests care for shared space.
2. Printer Queue Standoff — a blocked tool creates a small status contest.
3. Quiet Room — an awkward silence invites someone to bridge or deepen distance.
4. Deadline Shortcut — crunch makes a dubious process shortcut tempting.
5. Credit at Standup — public recognition tempts a character to share or hoard credit.
6. Expense Line — a reimbursement claim creates a small honesty test.
7. Shift Collision — two duties collide and someone must choose who to disappoint.
8. Rumor in the Kitchen — reveal-safe information can be shared, softened, or weaponized.
9. The Better Desk — a scarce room/resource allocation exposes local power.
10. Manager Nearby — the same action feels different with authority in the room.
11. Client Panic — a client-facing mistake turns reputation pressure into behavior.
12. Favor Ledger — an old relationship receipt gets called in.
13. New Hire Rescue — a struggling newcomer can be helped, ignored, or exploited.
14. Blame Draft — an incident report can name the system, the self, or a convenient peer.
15. Elevator Bottleneck — congestion creates a choice between patience and status assertion.
16. Secret Almost Spills — exposure risk rises and someone decides whether to contain it.

## Tracer Migration

#3 hardcodes `STORYLETS` in `breakroom.tick`. #15 should migrate that table into
`data/storylets/*.toml` using the template above. The existing `shared-space-repair`,
`stuck-workflow`, and `quiet-room` storylets map directly to the starter inventory.

No event-log migration is required: current events already store `storylet.id`, `storylet.name`, and
`storylet.prompt`. #15 can preserve those fields while adding template-derived metadata.

## Requirements for #15

#15 can implement this document directly by:

- Loading storylet TOML files from `data/storylets/`.
- Validating required fields, slot sources, quality namespaces, and salience bounds.
- Filtering eligibility from structured state only.
- Computing salience with the formula above.
- Performing the seeded weighted draw with stable sort and tie behavior.
- Returning a selected storylet context that #13 can use to create decision calls and the narrator
  can use for performance.

The implementation must include determinism tests: same seed + same state yields the same storylet
and participants; changed salience inputs alter weights without changing unrelated RNG streams.
