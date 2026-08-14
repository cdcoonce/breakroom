# Issue Authoring Conventions

Conventions for writing the `Scope` / `Non-scope` (a.k.a. `Anti-scope`) sections of an
AFK-dispatched issue, so the automated scope gate can tell an authorized change from an
out-of-scope one without a human having to reconstruct intent from prose elsewhere in the
issue.

## Existing test files must be enumerated in Scope

The `Scope` section must include an explicit list of every **existing** test file the
issue authorizes editing — not just the new files it authorizes creating. If no existing
test file is authorized for edits, the section must say so explicitly (e.g. "Existing test
files: none"). A Scope section that only lists new files, with existing-test-file
authorization left implicit in the issue's prose, is incomplete.

This matters because the scope gate quarantines any touched file that isn't named in
Scope, and it does not read prose outside that section to infer intent. Two prior
incidents show the failure mode:

- **#94**: the issue body said the change touched "the new module **and its tests**," but
  the Non-scope list never named `test_storylets.py` by path. The gate quarantined the PR
  for touching a file the issue's own acceptance criteria required, and a human had to
  manually litigate that the touch was authorized.
- **#93**: `tests/test_tick_norms.py` was flagged even though the authorized `tick.py`
  rewrite mechanically invalidated its existing assertions. The Non-scope list never named
  it either. This was the third recorded instance of the scope gate flagging an
  AC-mandated file.

In both cases, the fix that would have prevented the quarantine was the same: name the
existing test file, by path, in Scope — the same way a new file must be named.

## Recurring case: a production rewrite invalidates existing test assertions

Call out explicitly, in Scope, any existing test file whose assertions an authorized
production change will mechanically invalidate — even when no new test behavior is being
added and the issue is not "about" tests at all. This is a distinct case from adding new
test coverage: the test file isn't being extended, it's being *corrected* because the
production code it exercises changed shape underneath it.

The recurring pattern is a production rewrite that changes **control flow** an existing
test's assertions depend on, most often:

- **loop cardinality** — a test asserts an exact number of iterations, calls, or emitted
  events, and the rewrite changes how many times a loop body runs (e.g. adding an early
  exit, batching, or a guard clause that used to be unconditional).
- **hardcoded seeds or indices** — a test asserts against a specific RNG seed's output, or
  indexes into a fixed-position element of a sequence, and the rewrite changes ordering,
  draw count, or list position upstream of that assertion.

If a change you're authorizing in Scope has this shape, pre-authorize the existing test
file(s) it will mechanically break in the same Scope section — do not leave it for the
implementer or reviewer to notice the assertion no longer holds and litigate whether the
touch was intended.

## Checklist for the Scope section

- [ ] New files this issue may create, listed by path.
- [ ] Existing test files this issue authorizes editing, listed by path — or "none" if
      there are none.
- [ ] For any authorized production change that alters loop cardinality, hardcoded seeds,
      or hardcoded indices an existing test depends on: the existing test file(s) it will
      mechanically invalidate, named in Scope rather than left implicit.
