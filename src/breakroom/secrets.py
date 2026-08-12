from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from breakroom import jsonio
from breakroom.events import append_event
from breakroom.resolution.rng import RngStream

REVEAL_THRESHOLD = 0.8
NEAR_EXPOSURE_THRESHOLD = 0.6

_REQUIRED_RECORD_FIELDS = (
    "id",
    "holder",
    "content",
    "is_true",
    "exposure_risk",
    "knowers",
    "state",
    "revealed_by",
)
_VALID_STATES = {"sealed", "observable"}


class ValidationError(ValueError):
    pass


@dataclass
class Secret:
    id: str
    holder: str
    content: str
    is_true: bool
    exposure_risk: float
    knowers: list[str]
    state: str
    revealed_by: dict[str, Any] | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "holder": self.holder,
            "content": self.content,
            "is_true": self.is_true,
            "exposure_risk": self.exposure_risk,
            "knowers": list(self.knowers),
            "state": self.state,
            "revealed_by": self.revealed_by,
        }

    def public_view(self) -> dict[str, Any]:
        """The record as it may leave this module: sealed content stays sealed."""
        record = self.to_record()
        if self.state == "sealed":
            record.pop("content")
        return record


def seal_secret(
    world: Path,
    *,
    id: str,
    holder: str,
    content: str,
    is_true: bool,
    exposure_risk: float = 0.0,
    knowers: list[str] | None = None,
) -> Secret:
    """Create a sealed secret in the gitignored per-tower store."""
    store = _load_store(world)
    if id in store:
        raise ValidationError(f"secret already sealed: {id}")
    secret = Secret(
        id=id,
        holder=holder,
        content=content,
        is_true=is_true,
        exposure_risk=_clamp(exposure_risk),
        knowers=list(knowers) if knowers is not None else [holder],
        state="sealed",
        revealed_by=None,
    )
    store[id] = secret.to_record()
    _save_store(world, store)
    return secret


def advance_exposure(
    world: Path,
    secret: Secret,
    *,
    seed: int,
    tick: int,
    deltas: list[float],
) -> Secret:
    """Advance exposure risk by caller-supplied per-tick deltas.

    Each delta is the ceiling of pressure that event applies; the realized
    fraction is drawn from ``RngStream(stream="exposure", tick=tick)``, so risk
    only ever moves when the caller supplies a delta, and the same
    ``(seed, tick, deltas)`` always yields the same risk. Result is clamped to
    ``[0.0, 1.0]``.
    """
    store = _load_store(world)
    if secret.id not in store:
        raise ValidationError(f"unknown secret: {secret.id}")

    current = store[secret.id]
    rng = RngStream(seed=seed, stream="exposure", tick=tick)
    risk = secret.exposure_risk
    for index, delta in enumerate(deltas):
        risk = _clamp(risk + delta * rng.uniform(f"delta:{index}"))

    updated = replace(
        secret,
        exposure_risk=risk,
        state=current["state"],
        knowers=list(current["knowers"]),
        revealed_by=current["revealed_by"],
    )
    store[secret.id] = updated.to_record()
    _save_store(world, store)
    return updated


def maybe_reveal(
    world: Path,
    secret: Secret,
    *,
    day: int,
    rng: RngStream,
    observed_by: list[str] | None = None,
) -> Secret:
    """Reveal a sealed secret once its exposure risk crosses the threshold.

    No draw is taken below ``REVEAL_THRESHOLD``. On a successful draw the
    secret transitions sealed -> observable, gains the observers as knowers, is
    linked to the emitted ``secret_reveal`` event through ``revealed_by``, and
    that event is appended to the world's event log.
    """
    if secret.state != "sealed" or secret.exposure_risk < REVEAL_THRESHOLD:
        return secret
    if not rng.bernoulli("reveal", probability=secret.exposure_risk):
        return secret

    store = _load_store(world)
    if secret.id not in store:
        raise ValidationError(f"unknown secret: {secret.id}")

    current = store[secret.id]
    knowers = list(current["knowers"])
    for character_id in observed_by if observed_by is not None else []:
        if character_id not in knowers:
            knowers.append(character_id)

    provenance: dict[str, Any] = {
        "trigger": "exposure_threshold",
        "exposure_risk": secret.exposure_risk,
        "tick": rng.tick,
        "stream": rng.stream,
        "holder": secret.holder,
        "is_true": secret.is_true,
        "content": secret.content,
    }
    tagging = _tag_with_norms(
        world,
        {
            "type": "secret_reveal",
            "day": day,
            "secret_id": secret.id,
            "knowers": knowers,
            "provenance": provenance,
        },
    )
    if tagging is not None:
        provenance["norm_tags"] = tagging["norm_tags"]
        provenance["norm_violations"] = tagging["norm_violations"]

    event = append_event(
        world,
        {
            "type": "secret_reveal",
            "day": day,
            "secret_id": secret.id,
            "knowers": knowers,
            "provenance": provenance,
        },
    )

    revealed = replace(
        secret,
        state="observable",
        knowers=knowers,
        revealed_by={"type": event["type"], "sequence": event["sequence"], "day": event["day"]},
        exposure_risk=current["exposure_risk"],
    )
    store[secret.id] = revealed.to_record()
    _save_store(world, store)
    return revealed


def read_secret(world: Path, secret_id: str) -> dict[str, Any]:
    """Read a stored secret as public state; sealed content is never returned."""
    store = _load_store(world)
    record = store.get(secret_id)
    if record is None:
        raise ValidationError(f"unknown secret: {secret_id}")
    return _secret_from_record(_store_path(world), secret_id, record).public_view()


def _tag_with_norms(world: Path, record: dict[str, Any]) -> dict[str, Any] | None:
    try:
        from breakroom import norms
    except ImportError:
        return None
    if not (world / "data" / "norms.toml").exists():
        return None
    return norms.tag_record(norms.load_registry(world), record)


def _store_path(world: Path) -> Path:
    return world / ".secrets" / world.name / "secrets.json"


def _load_store(world: Path) -> dict[str, dict[str, Any]]:
    path = _store_path(world)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{path.name}: invalid JSON") from exc
    if not isinstance(data, dict):
        raise ValidationError(f"{path.name}: store must be a JSON object")
    for secret_id, record in data.items():
        _validate_record(path, secret_id, record)
    return data


def _save_store(world: Path, store: dict[str, dict[str, Any]]) -> None:
    path = _store_path(world)
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_pretty_json(path, store)


def _secret_from_record(path: Path, secret_id: str, record: Any) -> Secret:
    _validate_record(path, secret_id, record)
    return Secret(
        id=record["id"],
        holder=record["holder"],
        content=record["content"],
        is_true=record["is_true"],
        exposure_risk=record["exposure_risk"],
        knowers=list(record["knowers"]),
        state=record["state"],
        revealed_by=record["revealed_by"],
    )


def _validate_record(path: Path, secret_id: str, record: Any) -> None:
    if not isinstance(record, dict):
        raise ValidationError(f"{path.name}: record for {secret_id} must be an object")
    for field_name in _REQUIRED_RECORD_FIELDS:
        if field_name not in record:
            raise ValidationError(f"{path.name}: missing {field_name} for {secret_id}")
    for field_name in ("id", "holder", "content"):
        if not isinstance(record[field_name], str):
            raise ValidationError(f"{path.name}: {field_name} must be a string for {secret_id}")
    if not isinstance(record["is_true"], bool):
        raise ValidationError(f"{path.name}: is_true must be a bool for {secret_id}")
    risk = record["exposure_risk"]
    if isinstance(risk, bool) or not isinstance(risk, (int, float)):
        raise ValidationError(f"{path.name}: exposure_risk must be a number for {secret_id}")
    if risk < 0.0 or risk > 1.0:
        raise ValidationError(f"{path.name}: exposure_risk out of range for {secret_id}")
    if not isinstance(record["knowers"], list):
        raise ValidationError(f"{path.name}: knowers must be a list for {secret_id}")
    if record["state"] not in _VALID_STATES:
        raise ValidationError(f"{path.name}: invalid state {record['state']!r} for {secret_id}")
    if record["revealed_by"] is not None and not isinstance(record["revealed_by"], dict):
        raise ValidationError(f"{path.name}: revealed_by must be an object or null for {secret_id}")


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
