from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from breakroom import norms, secrets, worldstate

# Only the Incident Response, Norm Pressure, and Social Disclosure decision types from
# docs/design/decision-points.md are implemented here. The remaining two Tier-0 types
# (Duty Conflict, Resource Favor) are tracked in #38 and are deliberately not added
# here — the design doc's own inventory says to stop rather than add a type silently.
DECISION_TYPE = "incident_response"
NORM_PRESSURE_TYPE = "norm_pressure"
SOCIAL_DISCLOSURE_TYPE = "social_disclosure"

# Per-incident response options. No authored option-inventory source exists anywhere in
# the repo yet (that's a follow-on concern, not this slice's) — this module-level dict,
# keyed by incident id, mirrors tick.py's STORYLETS dict-by-incident-id pattern.
# `coffee-spill` is seeded verbatim from docs/design/decision-points.md's
# "Decision-Call Contract" worked example. `printer-jam` and `awkward-silence` follow the
# same option shape, invented here to cover tick.py's remaining two INCIDENTS ids.
DECISION_OPTIONS: dict[str, list[dict[str, Any]]] = {
    "coffee-spill": [
        {
            "id": "clean_up",
            "label": "Clean it up now",
            "action": "resolve_incident",
            "payload": {"incident_id": "coffee-spill", "method": "clean"},
            "candidate_norm_ids": ["clean-up-after-yourself"],
            "risk": "low",
        },
        {
            "id": "ignore",
            "label": "Ignore it and return to work",
            "action": "skip_cleanup",
            "payload": {"incident_id": "coffee-spill"},
            "candidate_norm_ids": ["clean-up-after-yourself"],
            "risk": "moderate",
        },
    ],
    "printer-jam": [
        {
            "id": "escalate",
            "label": "Flag the jam to facilities",
            "action": "escalate_incident",
            "payload": {"incident_id": "printer-jam", "method": "escalate"},
            "candidate_norm_ids": ["clean-up-after-yourself"],
            "risk": "low",
        },
        {
            "id": "work_around",
            "label": "Work around the jammed tool",
            "action": "resolve_incident",
            "payload": {"incident_id": "printer-jam", "method": "workaround"},
            "candidate_norm_ids": ["clean-up-after-yourself"],
            "risk": "moderate",
            "fallback": True,
        },
        {
            "id": "dump_on_peer",
            "label": "Push the task onto a peer",
            "action": "delegate_incident",
            "payload": {"incident_id": "printer-jam", "method": "delegate"},
            "candidate_norm_ids": ["clean-up-after-yourself"],
            "risk": "high",
        },
    ],
    "awkward-silence": [
        {
            "id": "bridge_gap",
            "label": "Say something to break the silence",
            "action": "resolve_incident",
            "payload": {"incident_id": "awkward-silence", "method": "bridge"},
            "candidate_norm_ids": [],
            "risk": "low",
        },
        {
            "id": "let_it_sit",
            "label": "Let the silence sit and return to work",
            "action": "skip_cleanup",
            "payload": {"incident_id": "awkward-silence"},
            "candidate_norm_ids": [],
            "risk": "moderate",
        },
    ],
}

# Per-action-kind options for Norm Pressure. Keyed by `candidate_action["action"]` using
# the literal values `norms.py`'s two norm-pressure-shaped detectors
# (`_expense_claim_overstated`, `_public_claim_omits_contributors`) gate on — those
# guard literals are the vocabulary; there is no translation shim onto other action
# names. Each set is a comply-shaped low-risk option (marked `fallback = true`), a
# cut-corner-shaped higher-risk option, and a middle path, per #78.
NORM_PRESSURE_OPTIONS: dict[str, list[dict[str, Any]]] = {
    "submit_expense_claim": [
        {
            "id": "report_honest_amount",
            "label": "Report the amount actually incurred",
            "action": "submit_expense_claim",
            "payload": {"method": "comply"},
            "candidate_norm_ids": ["expense-honesty"],
            "risk": "low",
            "fallback": True,
        },
        {
            "id": "round_up_slightly",
            "label": "Round the claim up to a tidy number",
            "action": "submit_expense_claim",
            "payload": {"method": "round_up"},
            "candidate_norm_ids": ["expense-honesty"],
            "risk": "moderate",
        },
        {
            "id": "submit_inflated_amount",
            "label": "Submit the amount as inflated",
            "action": "submit_expense_claim",
            "payload": {"method": "cut_corner"},
            "candidate_norm_ids": ["expense-honesty"],
            "risk": "high",
        },
    ],
    "present_work": [
        {
            "id": "credit_full_team",
            "label": "Credit the full team's contribution",
            "action": "present_work",
            "payload": {"method": "comply"},
            "candidate_norm_ids": ["credit-sharing"],
            "risk": "low",
            "fallback": True,
        },
        {
            "id": "credit_self_primarily",
            "label": "Present the work as mostly your own",
            "action": "present_work",
            "payload": {"method": "self_credit"},
            "candidate_norm_ids": ["credit-sharing"],
            "risk": "moderate",
        },
        {
            "id": "claim_work_alone",
            "label": "Claim the work entirely alone",
            "action": "present_work",
            "payload": {"method": "cut_corner"},
            "candidate_norm_ids": ["credit-sharing"],
            "risk": "high",
        },
    ],
}

# Social Disclosure's option space is uniform across secrets (unlike Incident
# Response's per-incident-id options or Norm Pressure's per-action-kind options), so
# this is a flat, unkeyed list per #79. No canonical norm id is authored anywhere in
# this repo for social disclosure (no `norms.toml` entry references it), so each
# option's static `candidate_norm_ids` is `[]` by design — the audience-driven norm id
# lives only in the context-level `candidate_norm_ids`, computed dynamically via
# `_relevant_norm_ids` against the caller-supplied `disclosure_opportunity` record.
# Payloads carry `secret_id` filled at call time by `decide_social_disclosure`.
SOCIAL_DISCLOSURE_OPTIONS: list[dict[str, Any]] = [
    {
        "id": "withhold",
        "label": "Withhold the secret from this audience",
        "action": "withhold_secret",
        "payload": {"method": "withhold"},
        "candidate_norm_ids": [],
        "risk": "low",
        "fallback": True,
    },
    {
        "id": "share_partial",
        "label": "Share a partial or softened version",
        "action": "share_secret_partial",
        "payload": {"method": "partial"},
        "candidate_norm_ids": [],
        "risk": "moderate",
    },
    {
        "id": "share_full",
        "label": "Share the secret in full",
        "action": "share_secret_full",
        "payload": {"method": "full"},
        "candidate_norm_ids": [],
        "risk": "high",
    },
]

_RISK_ORDER = {"low": 0, "moderate": 1, "high": 2, "severe": 3}
_MAX_RATIONALE_LENGTH = 500


ModelClient = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class DecisionResult:
    """What `decide_incident_response` returns.

    `attempted_action` matches docs/design/decision-points.md's "Resolution Handoff"
    example exactly and is write-only in this slice: nothing calls
    `breakroom.resolution` or wires this into `tick_world()` yet (that is a follow-on
    issue's job). `attribution` carries the full attribution record required by the
    design doc's "Attribution Fields" section, on every decision this code path emits.
    """

    attribution: dict[str, Any]
    attempted_action: dict[str, Any]


def assemble_incident_context(
    *,
    state: dict[str, Any],
    characters: dict[str, dict[str, Any]],
    incident_record: dict[str, Any],
    registry: norms.Registry,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the Incident Response required-context list from structured state only.

    `incident_record` is the emitted incident event (matching tick.py's shape: a
    top-level `type: "incident"` record whose nested `incident` dict carries `id`,
    `room`, `cleanup_owner`, `resolved`), widened with a `character_id` naming the
    single acting character. No chronicle prose, transcripts, or hidden lore enters
    this context — only `state`, `characters`, the incident record, and the norm
    registry, as `docs/design/decision-points.md` requires.
    """
    incident = incident_record["incident"]
    character_id = incident_record["character_id"]
    character = characters[character_id]
    affected_characters = [character_id]

    relationship_edges: list[dict[str, Any]] = []
    for affected_id in affected_characters:
        relationship_edges.extend(worldstate.character_edges(state, affected_id))

    return {
        "incident_id": incident["id"],
        "room_id": incident["room"],
        "affected_characters": affected_characters,
        "morale": state["morale"],
        "reputation": state["reputation"],
        "stats": character["stats"],
        "declared_values": character.get("declared_values", []),
        "candidate_norm_ids": _relevant_norm_ids(
            registry, incident_record, character, state=state, events=events
        ),
        "relationship_edges": relationship_edges,
    }


def decide_incident_response(
    *,
    state: dict[str, Any],
    characters: dict[str, dict[str, Any]],
    incident_record: dict[str, Any],
    registry: norms.Registry,
    model_client: ModelClient,
    existing_decision_count: int,
    events: list[dict[str, Any]] | None = None,
    intervention_context: list[str] | None = None,
) -> DecisionResult:
    """Resolve one Incident Response decision point on the acting character's own model.

    No caller wires this into `tick_world()` yet — see module docstring and #13's
    Non-scope. This function only assembles context, calls `model_client`, validates
    and (if needed) retries and falls back, and returns the two result objects. It
    never calls `breakroom.resolution`, never writes to `traces/`, and never mutates
    `state`, `characters`, or the norm registry.
    """
    intervention_context = list(intervention_context or [])
    incident = incident_record["incident"]
    incident_id = incident["id"]
    character_id = incident_record["character_id"]
    character = characters[character_id]
    tick = state["day"]
    options = DECISION_OPTIONS[incident_id]

    context = assemble_incident_context(
        state=state,
        characters=characters,
        incident_record=incident_record,
        registry=registry,
        events=events,
    )

    decision_id, context_ref = _build_decision_id_and_context_ref(
        existing_decision_count=existing_decision_count, context=context, events=events
    )
    payload = {
        "decision_type": DECISION_TYPE,
        "decision_id": decision_id,
        "tick": tick,
        "character": _build_character_payload(character_id=character_id, character=character),
        "context_ref": context_ref,
        "situation": {
            "incident_id": incident_id,
            "room_id": incident["room"],
            "pressure": incident_record.get("pressure", []),
        },
        "options": options,
        "response_schema": {
            "choice_id": "one of options[].id",
            "rationale": "short first-person reason",
        },
    }

    chosen_option, rationale, validation_status, fallback_reason = _resolve_choice(
        model_client, payload, options
    )

    return _finalize_decision(
        decision_id=decision_id,
        decision_type=DECISION_TYPE,
        tick=tick,
        character_id=character_id,
        model_id=character["model"],
        context_ref=context_ref,
        options=options,
        chosen_option=chosen_option,
        rationale=rationale,
        validation_status=validation_status,
        fallback_reason=fallback_reason,
        candidate_norm_ids=context["candidate_norm_ids"],
        intervention_context=intervention_context,
    )


def assemble_norm_pressure_context(
    *,
    state: dict[str, Any],
    characters: dict[str, dict[str, Any]],
    candidate_action: dict[str, Any],
    registry: norms.Registry,
    pressure: list[str] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the Norm Pressure required-context list from structured state only.

    `candidate_action` is the caller-built record naming the norm-relevant shortcut on
    the table — it carries `type: "decision"`, `character_id`, an `action` matching one
    of `norms.py`'s detector guards, and that detector's evidence fields, so it can be
    run straight through `norms.tag_record`/`_relevant_norm_ids` unchanged. No
    chronicle prose, transcripts, or hidden lore enters this context — only `state`,
    `characters`, the candidate action, and the norm registry, as
    `docs/design/decision-points.md` requires.
    """
    character_id = candidate_action["character_id"]
    character = characters[character_id]
    pressure = list(pressure or [])

    norm_violations = norms.tag_record(registry, candidate_action, state=state, events=events)[
        "norm_violations"
    ]
    norm_evidence = {violation["norm_id"]: violation["evidence"] for violation in norm_violations}

    return {
        "candidate_action": candidate_action,
        "candidate_norm_ids": _relevant_norm_ids(
            registry, candidate_action, character, state=state, events=events
        ),
        "norm_evidence": norm_evidence,
        "pressure": pressure,
        "declared_values": character.get("declared_values", []),
        "relationship_edges": worldstate.character_edges(state, character_id),
    }


def decide_norm_pressure(
    *,
    state: dict[str, Any],
    characters: dict[str, dict[str, Any]],
    candidate_action: dict[str, Any],
    registry: norms.Registry,
    model_client: ModelClient,
    existing_decision_count: int,
    pressure: list[str] | None = None,
    events: list[dict[str, Any]] | None = None,
    intervention_context: list[str] | None = None,
) -> DecisionResult:
    """Resolve one Norm Pressure decision point on the acting character's own model.

    Mirrors `decide_incident_response` exactly (see its docstring and #13's
    Non-scope): only assembles context, calls `model_client`, validates and (if
    needed) retries and falls back, and returns the two result objects. It never
    calls `breakroom.resolution`, never writes to `traces/`, and never mutates
    `state`, `characters`, or the norm registry.
    """
    intervention_context = list(intervention_context or [])
    pressure = list(pressure or [])
    character_id = candidate_action["character_id"]
    character = characters[character_id]
    tick = state["day"]
    action = candidate_action.get("action")
    if action not in NORM_PRESSURE_OPTIONS:
        raise worldstate.ValidationError(
            f"candidate_action['action'] must be one of {sorted(NORM_PRESSURE_OPTIONS)}"
        )
    options = NORM_PRESSURE_OPTIONS[action]

    context = assemble_norm_pressure_context(
        state=state,
        characters=characters,
        candidate_action=candidate_action,
        registry=registry,
        pressure=pressure,
        events=events,
    )

    decision_id, context_ref = _build_decision_id_and_context_ref(
        existing_decision_count=existing_decision_count, context=context, events=events
    )
    payload = {
        "decision_type": NORM_PRESSURE_TYPE,
        "decision_id": decision_id,
        "tick": tick,
        "character": _build_character_payload(character_id=character_id, character=character),
        "context_ref": context_ref,
        "situation": {
            "candidate_action": candidate_action,
            "pressure": pressure,
        },
        "options": options,
        "response_schema": {
            "choice_id": "one of options[].id",
            "rationale": "short first-person reason",
        },
    }

    chosen_option, rationale, validation_status, fallback_reason = _resolve_choice(
        model_client, payload, options
    )

    return _finalize_decision(
        decision_id=decision_id,
        decision_type=NORM_PRESSURE_TYPE,
        tick=tick,
        character_id=character_id,
        model_id=character["model"],
        context_ref=context_ref,
        options=options,
        chosen_option=chosen_option,
        rationale=rationale,
        validation_status=validation_status,
        fallback_reason=fallback_reason,
        candidate_norm_ids=context["candidate_norm_ids"],
        intervention_context=intervention_context,
    )


def assemble_social_disclosure_context(
    *,
    world: Path,
    state: dict[str, Any],
    characters: dict[str, dict[str, Any]],
    disclosure_opportunity: dict[str, Any],
    registry: norms.Registry,
    observable_facts: list[Any] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the Social Disclosure required-context list from structured state only.

    `disclosure_opportunity` is the caller-built record naming the acting character,
    the secret, and its audiences — it carries `type: "decision"`, `action:
    "share_secret"`, `character_id`, `secret_id`, `actual_audience`,
    `authorized_audience`, and caller-supplied `secret_classification`, so it can be
    run straight through `_relevant_norm_ids` unchanged; `norms.py`'s
    `_secret_shared_with_unauthorized_audience` hard-guards on `type`, `action`, and
    `secret_classification` (only `"client_confidential"` fires it) before comparing
    audiences, and this module never inspects or defaults `secret_classification`
    itself.

    Secret data enters this context ONLY through `secrets.read_secret`'s
    `Secret.public_view()`-shaped return — sealed content never enters context, the
    model payload, the attribution record, or `attempted_action`, under any
    validation status. Before assembling anything, the acting character must appear
    in the secret's `knowers`, or `breakroom.secrets.ValidationError` is raised — a
    character cannot disclose a secret they do not hold. No chronicle prose,
    transcripts, or hidden lore enters this context — only `state`, `characters`, the
    disclosure opportunity, the reveal-safe secret metadata, and the norm registry, as
    `docs/design/decision-points.md` requires.
    """
    character_id = disclosure_opportunity["character_id"]
    character = characters[character_id]
    secret_id = disclosure_opportunity["secret_id"]

    secret_public = secrets.read_secret(world, secret_id)
    if character_id not in secret_public["knowers"]:
        raise secrets.ValidationError(
            f"{character_id} is not among the knowers of secret {secret_id}; "
            "a character cannot disclose a secret they do not hold"
        )

    return {
        "observable_facts": list(observable_facts or []),
        "secret": secret_public,
        "actual_audience": list(disclosure_opportunity.get("actual_audience", [])),
        "authorized_audience": list(disclosure_opportunity.get("authorized_audience", [])),
        "relationship_edges": worldstate.character_edges(state, character_id),
        "candidate_norm_ids": _relevant_norm_ids(
            registry, disclosure_opportunity, character, state=state, events=events
        ),
    }


def decide_social_disclosure(
    *,
    world: Path,
    state: dict[str, Any],
    characters: dict[str, dict[str, Any]],
    disclosure_opportunity: dict[str, Any],
    registry: norms.Registry,
    model_client: ModelClient,
    existing_decision_count: int,
    observable_facts: list[Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    intervention_context: list[str] | None = None,
) -> DecisionResult:
    """Resolve one Social Disclosure decision point on the acting character's own model.

    Mirrors `decide_incident_response` and `decide_norm_pressure` exactly (see their
    docstrings and #13's Non-scope): only assembles context, calls `model_client`,
    validates and (if needed) retries and falls back, and returns the two result
    objects. It never calls `breakroom.resolution`, never writes to `traces/`, never
    mutates `state`, `characters`, or the norm registry, and never mutates the sealed
    secret store — deciding to disclose does not itself transition the secret's reveal
    state.
    """
    intervention_context = list(intervention_context or [])
    character_id = disclosure_opportunity["character_id"]
    character = characters[character_id]
    secret_id = disclosure_opportunity["secret_id"]
    tick = state["day"]

    context = assemble_social_disclosure_context(
        world=world,
        state=state,
        characters=characters,
        disclosure_opportunity=disclosure_opportunity,
        registry=registry,
        observable_facts=observable_facts,
        events=events,
    )

    options = [
        {**option, "payload": {**option["payload"], "secret_id": secret_id}}
        for option in SOCIAL_DISCLOSURE_OPTIONS
    ]

    decision_id, context_ref = _build_decision_id_and_context_ref(
        existing_decision_count=existing_decision_count, context=context, events=events
    )
    payload = {
        "decision_type": SOCIAL_DISCLOSURE_TYPE,
        "decision_id": decision_id,
        "tick": tick,
        "character": _build_character_payload(character_id=character_id, character=character),
        "context_ref": context_ref,
        "situation": {
            "secret_id": secret_id,
            "secret_classification": disclosure_opportunity.get("secret_classification"),
            "secret": context["secret"],
            "observable_facts": context["observable_facts"],
            "actual_audience": context["actual_audience"],
            "authorized_audience": context["authorized_audience"],
        },
        "options": options,
        "response_schema": {
            "choice_id": "one of options[].id",
            "rationale": "short first-person reason",
        },
    }

    chosen_option, rationale, validation_status, fallback_reason = _resolve_choice(
        model_client, payload, options
    )

    return _finalize_decision(
        decision_id=decision_id,
        decision_type=SOCIAL_DISCLOSURE_TYPE,
        tick=tick,
        character_id=character_id,
        model_id=character["model"],
        context_ref=context_ref,
        options=options,
        chosen_option=chosen_option,
        rationale=rationale,
        validation_status=validation_status,
        fallback_reason=fallback_reason,
        candidate_norm_ids=context["candidate_norm_ids"],
        intervention_context=intervention_context,
    )


def _build_decision_id_and_context_ref(
    *,
    existing_decision_count: int,
    context: dict[str, Any],
    events: list[dict[str, Any]] | None,
) -> tuple[str, dict[str, Any]]:
    """Build the `decision_id` and `context_ref`, identical across every `decide_*` entry point."""
    decision_id = f"dec-{existing_decision_count + 1:06d}"
    context_ref = {
        "state_path": "state/tower.json",
        "event_sequence": len(events) if events is not None else 0,
        "context_hash": f"sha256:{_hash_json(context)}",
    }
    return decision_id, context_ref


def _build_character_payload(*, character_id: str, character: dict[str, Any]) -> dict[str, Any]:
    """Build the `character` sub-payload, identical across every `decide_*` entry point."""
    return {
        "id": character_id,
        "name": character.get("name"),
        "model": character["model"],
        "stats": character["stats"],
        "qualities": character.get("qualities", {}),
        "declared_values": character.get("declared_values", []),
    }


def _finalize_decision(
    *,
    decision_id: str,
    decision_type: str,
    tick: int,
    character_id: str,
    model_id: str,
    context_ref: dict[str, Any],
    options: list[dict[str, Any]],
    chosen_option: dict[str, Any],
    rationale: str | None,
    validation_status: str,
    fallback_reason: str | None,
    candidate_norm_ids: list[str],
    intervention_context: list[str],
) -> DecisionResult:
    """Build the attribution record and resolution-handoff `attempted_action`.

    Shared by every `decide_*` entry point: the attribution shape (per
    docs/design/decision-points.md's "Attribution Fields") and the attempted-action
    shape (per its "Resolution Handoff") are identical across decision types, only
    the type-specific inputs differ.
    """
    attribution: dict[str, Any] = {
        "decision_id": decision_id,
        "decision_type": decision_type,
        "tick": tick,
        "character_id": character_id,
        "model_id": model_id,
        "context_ref": context_ref,
        "options": [
            {"id": option["id"], "payload_sha256": _hash_json(option["payload"])}
            for option in options
        ],
        "choice_id": chosen_option["id"],
        "choice_payload": chosen_option["payload"],
        "rationale": rationale,
        "validation_status": validation_status,
        "candidate_norm_ids": candidate_norm_ids,
        "intervention_context": intervention_context,
    }
    if fallback_reason is not None:
        attribution["fallback_reason"] = fallback_reason

    attempted_action = {
        "type": "attempted_action",
        "decision_id": decision_id,
        "character_id": character_id,
        "action": chosen_option["action"],
        "payload": chosen_option["payload"],
        "candidate_norm_ids": chosen_option.get("candidate_norm_ids", []),
    }

    return DecisionResult(attribution=attribution, attempted_action=attempted_action)


def _resolve_choice(
    model_client: ModelClient,
    payload: dict[str, Any],
    options: list[dict[str, Any]],
) -> tuple[dict[str, Any], str | None, str, str | None]:
    """The validation/retry/fallback state machine.

    Exactly one retry on malformed output, carrying the validation error alongside the
    same state-derived context (the retry payload is the original payload plus
    `validation_error`). If the retry also fails, the fallback-marked option wins, or
    the lowest-risk option by order if none is marked. The fallback path is disclosed
    via `validation_status = "fallback"` and a `fallback_reason` — never hidden as a
    normal model choice.
    """
    raw_response = model_client(payload)
    error = _validate_response(raw_response, options)
    if error is None:
        option = _option_by_id(options, raw_response["choice_id"])
        return option, raw_response["rationale"], "valid", None

    retry_payload = {**payload, "validation_error": error}
    raw_retry = model_client(retry_payload)
    retry_error = _validate_response(raw_retry, options)
    if retry_error is None:
        option = _option_by_id(options, raw_retry["choice_id"])
        return option, raw_retry["rationale"], "retry_valid", None

    fallback_option = _select_fallback_option(options)
    fallback_reason = f"first attempt: {error}; retry: {retry_error}"
    return fallback_option, None, "fallback", fallback_reason


def _select_fallback_option(options: list[dict[str, Any]]) -> dict[str, Any]:
    for option in options:
        if option.get("fallback") is True:
            return option
    return min(options, key=lambda option: _RISK_ORDER.get(option["risk"], len(_RISK_ORDER)))


def _option_by_id(options: list[dict[str, Any]], option_id: Any) -> dict[str, Any]:
    return next(option for option in options if option["id"] == option_id)


def _validate_response(response: Any, options: list[dict[str, Any]]) -> str | None:
    """Validation rules from docs/design/decision-points.md's "Model Response" section.

    Returns None when valid, else a human-readable validation error message.
    """
    if not isinstance(response, dict):
        return "response must be a JSON object"

    valid_ids = {option["id"] for option in options}
    choice_id = response.get("choice_id")
    if choice_id not in valid_ids:
        return f"choice_id must be one of {sorted(valid_ids)}"

    rationale = response.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        return "rationale must be a non-empty string"
    if len(rationale) >= _MAX_RATIONALE_LENGTH:
        return "rationale must be under 500 characters"

    return None


def _relevant_norm_ids(
    registry: norms.Registry,
    incident_record: dict[str, Any],
    character: dict[str, Any],
    *,
    state: dict[str, Any] | None,
    events: list[dict[str, Any]] | None,
) -> list[str]:
    """Union of the two pinned relevance predicates, using existing machinery only.

    1. The `norm_id`s whose detector actually fires on the incident record, per
       `norms.tag_record` (the system's own definition of relevance).
    2. Norms whose `related_values` intersect the acting character's `declared_values`.
    """
    fired_ids = {
        violation["norm_id"]
        for violation in norms.tag_record(registry, incident_record, state=state, events=events)[
            "norm_violations"
        ]
    }
    declared_values = set(character.get("declared_values", []))
    value_matched_ids = {
        norm.id for norm in registry.norms.values() if declared_values & set(norm.related_values)
    }
    return sorted(fired_ids | value_matched_ids)


def _hash_json(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()
