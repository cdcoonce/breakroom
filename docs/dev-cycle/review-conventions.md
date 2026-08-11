# Review conventions

Process conventions to run before submitting a slice, alongside `cycle-log.md` and
`quarantine-log.md`.

## Unused parameters and unconsulted schema fields

Before submission, for every new or changed function and every new or changed schema
field:

- Grep the function's parameter list against its own body. A parameter that is
  accepted but never read inside the function is a gap.
- Grep each new schema field name against its consumers (every place that reads the
  parsed value, not just the place that assigns it). A field that is loaded/parsed
  into a data model but never read anywhere downstream is a gap.

Anything the grep turns up unused must be wired in before submission, or given an
explicit comment at the point it's ignored explaining the deferral and naming the
tracker issue that will consume it. Neither an unused parameter nor an unconsulted
field should ship silently.

This check is mechanical — a parameter/field name search, not code comprehension — so
run it as a matter of course, not only when something looks suspicious.

### Motivating cases (PR #37)

This check exists because two independent reviews of the same PR converged on the
identical findings, which is what a grep would have caught before either review ran.
Verified against `git diff 5eba2f4d..5fbb70b -- src/breakroom/norms.py` (PR #37's base
commit to its tip, in a local clone — `gh pr diff` has no path-filter argument, so
`gh pr diff 37 -- src/breakroom/norms.py` fails):

- `_incident_cleanup_owner_missed` declares `events: list[dict[str, Any]] | None = None`
  in its signature but never references `events` in its body — the parameter is
  accepted and dropped. A grep for `events` inside the function turns up nothing. This
  gap is still present on `main`; it is cited here as a motivating example, not fixed
  by this doc.
- `Norm.applies_to_roles` and `Norm.applies_to_rooms` are parsed out of each norm's
  TOML entry in `_validate_norm` and stored on the `Norm` dataclass, but `tag_record` —
  the only place that walks the registry and applies norms to a record — never reads
  either field. A grep for `applies_to_roles`/`applies_to_rooms` outside of
  `_validate_norm`'s parsing and the dataclass field declaration turns up no consumer.
  These fields have since been removed entirely (PR #70), so this gap is not
  reproducible against current `main` — it is cited here as the motivating case for
  the schema-field half of this check.

### Related context this check does not cover

`_assigned_shift_unattended` had a real bug, fixed within PR #37 itself (commit
`5fbb70b`) before merge: it computed `observed_room` and `required_room` but never
*compared* them in a conditional, producing self-contradicting evidence. This is
**not** a case this check would have flagged. `assignment.get("required_room")` is
referenced in the function body both before and after the fix — it's always present in
the returned evidence dict — so a grep for whether `required_room` is used inside the
function returns "yes" at both diff endpoints. The bug was in the missing comparison,
not in an unread parameter or field. Treat this class of bug (a value that's read but
never compared/branched on) as a separate concern from the unused-parameter /
unconsulted-field check above; this doc's grep step will not catch it.
