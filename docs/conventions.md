# Conventions

## Validation errors: registry loaders and event-payload handlers

Any new registry loader (a function that reads and validates a file under
the world/repo root, e.g. `norms.py`, `storylets.py`,
`resolution/incidents.py`) or event-payload handler (a function that
validates a dict passed at runtime, e.g. `worldstate.apply_event`,
`decisions.decide_norm_pressure`) must reject malformed or unrecognized
input by raising `breakroom.worldstate.ValidationError` — not a bare
`KeyError`, `AttributeError`, or `TypeError`.

**Use `breakroom.worldstate.ValidationError`, not `breakroom.secrets.ValidationError`.**
`secrets.py` independently defines its own `ValidationError` class. It is a
separate, non-canonical definition: it is not the class that `norms.py`,
`storylets.py`, `resolution/incidents.py`, and `worldstate.py`'s own
validation share and reuse. (A few sites outside `secrets.py` do raise it
— e.g. `decisions.py:511` — but that is existing code, not a pattern to
extend.) New validation code should import and raise
`breakroom.worldstate.ValidationError`.

**Silently skipping unrecognized input is equally unacceptable.** A
validator that quietly ignores or drops an unrecognized value (rather than
raising) is just as much a bug as one that lets a builtin exception
escape — it fails silently instead of failing loudly, which is worse
because nothing signals that anything went wrong. (This was the shape of
the PR #37 bug: a typo in `norms.toml` disabled a norm permanently with no
error, because the lookup silently skipped unknown ids instead of
rejecting them.)

### Message shape

The message shape to use depends on whether the validation is backed by a
file on disk:

- **File-backed validation (registry loaders).** Use
  `f"{relative}: ..."`, where `relative` is the path relative to the
  world (or repo) root. This is the shape used throughout `norms.py`,
  `storylets.py`, `resolution/incidents.py`, and `worldstate._load_character`,
  e.g. `f"{relative}: missing file"`, `f"{relative}: invalid detection {entry['detection']!r} for {entry['id']}"`.

  Known non-canonical exception: `worldstate._read_json` and
  `secrets.py`'s file-loading (JSON-store parsing) raise sites
  (`secrets.py:227-276`) currently use `f"{path.name}: ..."` — the bare
  filename only, with no directory context. This is the pattern to
  *avoid* in new code, not an indication that the codebase already
  agrees on one shape.

- **Payload-only validation (event-payload handlers).** These validate a
  dict passed at runtime, not a file read from disk, so there is no
  `relative` path to interpolate. Use a descriptive prefix naming the
  event type or field instead. Existing precedent:
  - `worldstate.apply_event` / `worldstate._apply_edge_delta`:
    `f"edge_delta event: missing {field}"`, `f"unknown dial: {dial}"`
  - `decisions.decide_norm_pressure`:
    `f"candidate_action['action'] must be one of {sorted(NORM_PRESSURE_OPTIONS)}"`
  - `secrets.py`'s own non-file-backed (id-based) checks:
    `f"secret already sealed: {id}"` (`secrets.py:76`),
    `f"unknown secret: {secret.id}"` (`secrets.py:110`)

### Tests

Either way, new validation code must include a test asserting that
unrecognized or malformed input is rejected — i.e. that
`breakroom.worldstate.ValidationError` is actually raised. A test that
only exercises the happy path does not demonstrate that bad input is
rejected rather than silently skipped.
