# Coding Conventions

## Composite dict keys: use tuples, not delimiter-joined strings

When a dict key must represent two or more identifiers together, use a
tuple key (or a dataclass) rather than joining the identifiers into a
single string with a delimiter — unless the delimiter is provably
excluded from valid id characters at the boundary (e.g. enforced by id
validation).

Delimiter-joined string keys are lossy: if any identifier can itself
contain the delimiter, encoding and decoding do not round-trip.

**Precedent:** `worldstate._pair_key` / `worldstate._split_pair_key`
encode an edge pair key as `f"{from_id}->{to_id}"` and decode it via
`pair_key.split("->", 1)`. Two independent reviewers on PR #36 flagged
this independently: an id like `from="a->b", to="c"` decodes back as
`from="a", to="b->c"`, and `character_edges`/`edges_at_or_above` would
report wrong endpoints for such ids. The bug is latent only because
current ids are slugs that cannot contain `->`.

**Rule for future code:** any new delimiter-joined key must ship with a
round-trip test (encode then decode) using an id that contains the
delimiter character as a test case.
