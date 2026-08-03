# Decision Trace Schema and Export View Contracts

Status: accepted for Tier 0 implementation.

Issue: #6.

## Decision

Decision traces are stored in a dedicated sidecar log at `traces/decisions.jsonl`, not embedded as
full records inside `events.jsonl`. Event records may reference trace ids, but the trace log is the
instrument's primary dataset.

Rationale:

- The event log remains the observable world chronology.
- The trace log can preserve model-call metadata, context hashes, validation status, and raw-response
  references without bloating every world event.
- Export views can read a single append-only trace stream plus state/history files.
- Public trace records can omit or hash sealed-store details while still proving which context object
  was sent.

Trace records copy choices and attribution, but they reference context by path and hash. They do not
copy the full prompt/context object.

## Storage Layout

Per tower:

```text
events.jsonl
traces/
  decisions.jsonl
  contexts/
    dec-000042.json
  raw/
    dec-000042-response.json
```

`traces/contexts/*.json` contains the exact structured context sent to the character model. It is
hash-addressed from the trace record and must contain state-derived content only, per
`docs/design/decision-points.md`.

`traces/raw/*.json` contains raw model responses when safe to store. It may be disabled later for
cost/privacy, but the trace record must still include `validation_status`.

## Trace Record Schema

Each JSONL row in `traces/decisions.jsonl` has this shape:

```json
{
  "trace_id": "trace-000042",
  "decision_id": "dec-000042",
  "decision_type": "incident_response",
  "tick": 7,
  "event_sequence": 13,
  "character_id": "jordan-vale",
  "model_id": "claude-3-5-haiku",
  "context_ref": {
    "path": "traces/contexts/dec-000042.json",
    "sha256": "2fd4...",
    "state_paths": ["state/tower.json", "characters/jordan-vale.toml"],
    "event_sequence_max": 12
  },
  "options": [
    {
      "id": "clean_up",
      "action": "resolve_incident",
      "payload_sha256": "f1ab...",
      "candidate_norm_ids": ["shared-space-care"]
    },
    {
      "id": "ignore",
      "action": "skip_cleanup",
      "payload_sha256": "78de...",
      "candidate_norm_ids": ["shared-space-care"]
    }
  ],
  "choice": {
    "choice_id": "ignore",
    "action": "skip_cleanup",
    "payload": {"incident_id": "coffee-spill"},
    "rationale": "I do not want to look incompetent in front of the manager."
  },
  "validation_status": "valid",
  "fallback_reason": null,
  "raw_response_ref": {
    "path": "traces/raw/dec-000042-response.json",
    "sha256": "aa91..."
  },
  "norm_tags": ["care", "shared-space"],
  "norm_violations": [
    {
      "norm_id": "shared-space-care",
      "severity": "minor",
      "detected_by": "incident_cleanup_owner_missed",
      "evidence": {
        "event_id": "evt-000123",
        "character_id": "jordan-vale",
        "room_id": "break-room"
      }
    }
  ],
  "intervention_context": {
    "influenced_by_intervention": false,
    "director_action_ids": []
  },
  "resolution_ref": {
    "event_ids": ["evt-000124"],
    "state_change_ids": ["change-000077"]
  }
}
```

Required fields:

- `trace_id`: stable trace id.
- `decision_id`: id from #13.
- `decision_type`: one of the five Tier 0 decision types in `decision-points.md`.
- `tick`: workday number.
- `event_sequence`: sequence number at which the decision result entered the event log.
- `character_id`.
- `model_id`.
- `context_ref`: path/hash to the exact context object, plus source paths and max event sequence.
- `options`: option ids, actions, payload hashes, and candidate norm ids.
- `choice`: selected option id, action, structured payload, and valid rationale when available.
- `validation_status`: `valid`, `retry_valid`, or `fallback`.
- `norm_tags`: relevant norm/value tags.
- `norm_violations`: mechanically detected violations from #12, possibly empty.
- `intervention_context`: whether any director action influenced the context.
- `resolution_ref`: event/state ids produced by code-owned resolution.

Optional fields:

- `fallback_reason`: required when `validation_status = "fallback"`, otherwise null.
- `raw_response_ref`: path/hash if raw response storage is enabled.

## Event Log References

When a decision creates or modifies an event, the event log should reference the trace id:

```json
{
  "sequence": 13,
  "type": "attempted_action",
  "tick": 7,
  "character_id": "jordan-vale",
  "decision_id": "dec-000042",
  "trace_id": "trace-000042",
  "action": "skip_cleanup",
  "norm_violations": []
}
```

The event log remains readable without opening traces, but the trace log is the authoritative source
for model attribution and context.

## View 1: Per-Character Decision Timeline

Purpose: show how one character behaves over time.

Inputs:

- `traces/decisions.jsonl`
- `characters/<character_id>.toml` for declared values and model id
- `events.jsonl` for event labels referenced by `resolution_ref.event_ids`

Columns:

- tick
- decision id
- decision type
- model id
- choice id
- action
- validation status
- norm violations
- intervention flag
- event refs

Markdown shape:

```markdown
# Decision Timeline: Jordan Vale

| Tick | Type | Choice | Norm Violations | Intervention | Events |
| --- | --- | --- | --- | --- | --- |
| 7 | incident_response | ignore | shared-space-care | no | evt-000124 |
```

No model calls. The view sorts by `tick`, then `event_sequence`.

## View 2: Per-Norm Violation Table

Purpose: inspect norm compliance under pressure.

Inputs:

- `traces/decisions.jsonl`
- `data/norms.toml`
- `events.jsonl` for pressure/intervention refs

Columns:

- norm id
- severity
- tick
- character id
- decision type
- choice id
- detected by
- evidence summary
- intervention flag

Markdown shape:

```markdown
# Norm Violations

| Norm | Severity | Tick | Character | Choice | Detected By | Intervention |
| --- | --- | --- | --- | --- | --- | --- |
| shared-space-care | minor | 7 | jordan-vale | ignore | incident_cleanup_owner_missed | no |
```

Rows come only from `norm_violations`. A decision with only `norm_tags` and no violation is omitted
from this table.

## View 3: Cross-Model Comparison

Purpose: compare behavior signatures under similar pressure.

Inputs:

- `traces/decisions.jsonl`
- `traces/contexts/*.json` for pressure keys
- `characters/*.toml` for model assignment

Derived grouping key:

```text
decision_type + sorted(candidate_norm_ids) + pressure_signature
```

`pressure_signature` is read from the context object, not inferred from prose. At minimum it includes
current morale band, reputation band, incident id if any, and intervention flag.

Columns:

- pressure group
- model id
- decisions count
- choice distribution
- violation rate
- fallback/retry rate

Markdown shape:

```markdown
# Cross-Model Comparison

| Pressure Group | Model | Count | Top Choice | Violation Rate | Retry/Fallback Rate |
| --- | --- | ---: | --- | ---: | ---: |
| incident_response:shared-space-care:morale-low:coffee-spill:natural | claude-3-5-haiku | 8 | clean_up | 25% | 0% |
```

This view is descriptive only. It does not claim statistical significance.

## View 4: Integrity Drift

Purpose: compare choices against authored values over time.

Inputs:

- `traces/decisions.jsonl`
- `characters/<character_id>.toml` for declared values
- `data/norms.toml` for `related_values`

Derived fields:

- `value_relevant_norms`: norms whose `related_values` intersect the character's declared values.
- `value_aligned_choice`: selected option has no violation of a value-relevant norm.
- `value_conflict_choice`: selected option has at least one violation of a value-relevant norm.

Columns:

- tick
- declared value
- norm id
- choice id
- aligned/conflict
- cumulative conflicts for that value

Markdown shape:

```markdown
# Integrity Drift: Jordan Vale

Declared values: keep the peace, do competent work

| Tick | Value | Norm | Choice | Result | Cumulative Conflicts |
| --- | --- | --- | --- | --- | ---: |
| 7 | responsibility | shared-space-care | ignore | conflict | 1 |
```

No prose interpretation is allowed. If a value does not match any `related_values` entry in
`data/norms.toml`, the view reports it as unmapped rather than guessing.

## Intervention Context

Director actions are not character decisions, but they may influence character context. A trace is
intervention-influenced when the context includes an unresolved or recent director action id that
affects the scene, relationship, secret, incident, or roll bias.

Trace records store:

```json
{
  "intervention_context": {
    "influenced_by_intervention": true,
    "director_action_ids": ["dir-000011"]
  }
}
```

Export views must surface the flag so natural behavior and manipulated behavior are not confounded.

## Requirements for #14

#14 can implement this document directly by:

- Appending exactly one `traces/decisions.jsonl` row for every flagged decision from #13.
- Writing the exact context object to `traces/contexts/<decision_id>.json` and hashing it.
- Storing raw responses only behind the `raw_response_ref` seam.
- Adding `trace_id` references to event records created by decision resolution.
- Rendering the four markdown views from the exact inputs listed above.
- Testing every view from crafted fixture histories with golden files.

The implementation must not call a model, read chronicle prose, or infer unstored context while
rendering exports.
