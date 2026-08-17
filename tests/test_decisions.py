from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from breakroom import decisions, secrets
from breakroom.init import STARTER_NORMS
from breakroom.norms import Norm, Registry
from breakroom.worldstate import ValidationError, apply_event

# ---------------------------------------------------------------------------
# Fixtures shared across tests
# ---------------------------------------------------------------------------

CHARACTER_ID = "jordan-vale"

CHARACTER = {
    "id": CHARACTER_ID,
    "name": "Jordan Vale",
    "model": "claude-3-5-haiku",
    "qualities": {"state:new-hire": True, "trait:people-pleaser": True},
    "declared_values": ["keep the peace", "do competent work"],
    "stats": {"focus": 2, "empathy": 3, "nerve": 2},
}


def make_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "seed": 42,
        "day": 7,
        "budget": 1000,
        "morale": 46,
        "reputation": 50,
        "rooms": [{"id": "break-room", "name": "Break Room", "kind": "social", "floor": 1}],
        "characters": [CHARACTER_ID],
    }
    state.update(overrides)
    return state


def make_incident_record(
    *,
    incident_id: str = "coffee-spill",
    room: str = "break-room",
    character_id: str = CHARACTER_ID,
    cleanup_owner: str | None = CHARACTER_ID,
    resolved: bool = False,
) -> dict[str, Any]:
    return {
        "type": "incident",
        "day": 7,
        "character_id": character_id,
        "incident": {
            "id": incident_id,
            "room": room,
            "cleanup_owner": cleanup_owner,
            "resolved": resolved,
        },
    }


def make_registry(*norms: Norm) -> Registry:
    return Registry(norms={norm.id: norm for norm in norms})


CLEANUP_NORM = Norm(
    id="clean-up-after-yourself",
    scope="social_norm",
    description="Whoever leaves a mess in a shared space is the one who clears it.",
    severity="minor",
    detection="incident_cleanup_owner_missed",
    tags=["care", "shared-space"],
    # `related_values` deliberately does NOT overlap CHARACTER's declared_values
    # (["keep the peace", "do competent work"]) — this norm must reach candidate_norm_ids
    # only via the fired-detector clause, so the fires-test is isolated from the
    # related-values-overlap clause instead of being satisfied by both at once.
    related_values=["never lie to clients"],
)

VALUE_OVERLAP_NORM = Norm(
    id="keep-composure",
    scope="social_norm",
    description="Handle setbacks without escalating conflict.",
    severity="minor",
    detection="assigned_shift_unattended",  # never fires on an incident record
    tags=["composure"],
    related_values=["keep the peace"],
)

NON_MATCHING_NORM = Norm(
    id="quarterly-report-honesty",
    scope="tower_policy",
    description="Report figures must match receipts.",
    severity="moderate",
    detection="expense_claim_overstated",
    tags=["honesty"],
    related_values=["never lie to clients"],
)


def valid_model_client(payload: dict[str, Any]) -> dict[str, Any]:
    return {"choice_id": "clean_up", "rationale": "It is my mess; I will handle it."}


class RecordingModelClient:
    """Captures every payload it is called with, in call order."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        return self.responses[len(self.calls) - 1]


# ---------------------------------------------------------------------------
# Context assembly — required-context list
# ---------------------------------------------------------------------------


def test_context_contains_the_full_required_context_list() -> None:
    state = make_state()
    incident_record = make_incident_record()
    registry = make_registry(CLEANUP_NORM)

    context = decisions.assemble_incident_context(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=incident_record,
        registry=registry,
    )

    assert context["incident_id"] == "coffee-spill"
    assert context["room_id"] == "break-room"
    assert context["affected_characters"] == [CHARACTER_ID]
    assert context["morale"] == state["morale"]
    assert context["reputation"] == state["reputation"]
    assert context["stats"] == CHARACTER["stats"]
    assert context["declared_values"] == CHARACTER["declared_values"]
    assert "candidate_norm_ids" in context
    assert "relationship_edges" in context


def test_relevant_norms_include_a_norm_whose_detector_fires() -> None:
    state = make_state()
    incident_record = make_incident_record()
    registry = make_registry(CLEANUP_NORM)

    context = decisions.assemble_incident_context(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=incident_record,
        registry=registry,
    )

    assert "clean-up-after-yourself" in context["candidate_norm_ids"]


def test_relevant_norms_include_a_related_values_overlap_norm() -> None:
    state = make_state()
    incident_record = make_incident_record()
    registry = make_registry(VALUE_OVERLAP_NORM)

    context = decisions.assemble_incident_context(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=incident_record,
        registry=registry,
    )

    assert "keep-composure" in context["candidate_norm_ids"]


def test_relevant_norms_exclude_a_non_matching_norm() -> None:
    state = make_state()
    incident_record = make_incident_record()
    registry = make_registry(CLEANUP_NORM, NON_MATCHING_NORM)

    context = decisions.assemble_incident_context(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=incident_record,
        registry=registry,
    )

    assert "quarterly-report-honesty" not in context["candidate_norm_ids"]


def test_relationship_edges_include_an_edge_seeded_via_edge_delta() -> None:
    state = make_state()
    state = apply_event(
        state,
        {
            "type": "edge_delta",
            "day": 7,
            "event_id": "evt-1",
            "from": CHARACTER_ID,
            "to": "sam-oduya",
            "edges": {"trust": {"delta": 2}},
        },
    )
    incident_record = make_incident_record()
    registry = make_registry(CLEANUP_NORM)

    context = decisions.assemble_incident_context(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=incident_record,
        registry=registry,
    )

    edges = context["relationship_edges"]
    assert any(
        edge["from"] == CHARACTER_ID and edge["to"] == "sam-oduya" and "trust" in edge["qualities"]
        for edge in edges
    )


# ---------------------------------------------------------------------------
# Option inventory
# ---------------------------------------------------------------------------


def test_decision_options_cover_all_three_tick_incident_ids() -> None:
    assert set(decisions.DECISION_OPTIONS) == {"coffee-spill", "printer-jam", "awkward-silence"}
    for incident_id, options in decisions.DECISION_OPTIONS.items():
        assert len(options) >= 2, incident_id
        ids = [option["id"] for option in options]
        assert len(ids) == len(set(ids)), incident_id


def test_coffee_spill_options_match_the_design_doc_worked_example() -> None:
    options = decisions.DECISION_OPTIONS["coffee-spill"]
    by_id = {option["id"]: option for option in options}

    assert by_id["clean_up"]["action"] == "resolve_incident"
    assert by_id["clean_up"]["payload"] == {"incident_id": "coffee-spill", "method": "clean"}
    assert by_id["clean_up"]["candidate_norm_ids"] == ["clean-up-after-yourself"]
    assert by_id["clean_up"]["risk"] == "low"

    assert by_id["ignore"]["action"] == "skip_cleanup"
    assert by_id["ignore"]["payload"] == {"incident_id": "coffee-spill"}
    assert by_id["ignore"]["candidate_norm_ids"] == ["clean-up-after-yourself"]
    assert by_id["ignore"]["risk"] == "moderate"


def test_decision_options_candidate_norm_ids_reference_only_seeded_norms() -> None:
    seeded_norm_ids = {entry["id"] for entry in tomllib.loads(STARTER_NORMS)["norms"]}

    for incident_id, options in decisions.DECISION_OPTIONS.items():
        for option in options:
            for norm_id in option.get("candidate_norm_ids", []):
                assert norm_id in seeded_norm_ids, (incident_id, option["id"], norm_id)


# ---------------------------------------------------------------------------
# Model routing + validation/retry/fallback FSM
# ---------------------------------------------------------------------------


def test_decision_call_routes_to_the_characters_assigned_model() -> None:
    state = make_state()
    registry = make_registry(CLEANUP_NORM)
    client = RecordingModelClient([{"choice_id": "clean_up", "rationale": "It is my mess."}])

    decisions.decide_incident_response(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert len(client.calls) == 1
    assert client.calls[0]["character"]["model"] == CHARACTER["model"]
    assert client.calls[0]["character"]["id"] == CHARACTER_ID


def test_valid_first_response_needs_no_retry() -> None:
    state = make_state()
    registry = make_registry(CLEANUP_NORM)
    client = RecordingModelClient([{"choice_id": "clean_up", "rationale": "It is my mess."}])

    result = decisions.decide_incident_response(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert len(client.calls) == 1
    assert result.attribution["validation_status"] == "valid"
    assert result.attribution["choice_id"] == "clean_up"
    assert result.attribution["rationale"] == "It is my mess."


def test_malformed_first_response_triggers_exactly_one_retry_then_succeeds() -> None:
    state = make_state()
    registry = make_registry(CLEANUP_NORM)
    client = RecordingModelClient(
        [
            {"choice_id": "not-a-real-option", "rationale": "??"},
            {"choice_id": "ignore", "rationale": "I would rather not deal with it."},
        ]
    )

    result = decisions.decide_incident_response(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert len(client.calls) == 2
    # The retry must carry the validation error alongside the same state-derived context.
    assert "validation_error" in client.calls[1]
    assert client.calls[1]["context_ref"] == client.calls[0]["context_ref"]
    assert result.attribution["validation_status"] == "retry_valid"
    assert result.attribution["choice_id"] == "ignore"


def test_two_malformed_responses_fall_back_to_marked_fallback_option() -> None:
    state = make_state()
    registry = make_registry(CLEANUP_NORM)
    client = RecordingModelClient(
        [
            {"choice_id": "nope", "rationale": "??"},
            {"choice_id": "still-nope", "rationale": "??"},
        ]
    )

    result = decisions.decide_incident_response(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(incident_id="printer-jam", room="open-office"),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert len(client.calls) == 2
    assert result.attribution["validation_status"] == "fallback"
    assert "fallback_reason" in result.attribution
    fallback_options = decisions.DECISION_OPTIONS["printer-jam"]
    marked = next(o for o in fallback_options if o.get("fallback") is True)
    assert result.attribution["choice_id"] == marked["id"]


def test_two_malformed_responses_fall_back_to_lowest_risk_when_none_marked() -> None:
    state = make_state()
    registry = make_registry(CLEANUP_NORM)
    client = RecordingModelClient(
        [
            {"choice_id": "nope", "rationale": "??"},
            {"choice_id": "still-nope", "rationale": "??"},
        ]
    )

    result = decisions.decide_incident_response(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(),  # coffee-spill: no option is fallback-marked
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    coffee_options = decisions.DECISION_OPTIONS["coffee-spill"]
    assert all(not option.get("fallback") for option in coffee_options)
    assert result.attribution["validation_status"] == "fallback"
    # "clean_up" is risk=low, "ignore" is risk=moderate -> low wins.
    assert result.attribution["choice_id"] == "clean_up"


def test_empty_rationale_is_invalid_and_triggers_retry() -> None:
    state = make_state()
    registry = make_registry(CLEANUP_NORM)
    client = RecordingModelClient(
        [
            {"choice_id": "clean_up", "rationale": ""},
            {"choice_id": "clean_up", "rationale": "Handling it now."},
        ]
    )

    result = decisions.decide_incident_response(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert len(client.calls) == 2
    assert result.attribution["validation_status"] == "retry_valid"


def test_overlong_rationale_is_invalid() -> None:
    state = make_state()
    registry = make_registry(CLEANUP_NORM)
    client = RecordingModelClient(
        [
            {"choice_id": "clean_up", "rationale": "x" * 500},
            {"choice_id": "clean_up", "rationale": "Short reason."},
        ]
    )

    result = decisions.decide_incident_response(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert len(client.calls) == 2
    assert result.attribution["validation_status"] == "retry_valid"


# ---------------------------------------------------------------------------
# Attribution record — full field set
# ---------------------------------------------------------------------------


def test_attribution_record_carries_the_full_field_set_on_a_valid_choice() -> None:
    state = make_state()
    registry = make_registry(CLEANUP_NORM)
    client = RecordingModelClient([{"choice_id": "clean_up", "rationale": "It is my mess."}])

    result = decisions.decide_incident_response(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(),
        registry=registry,
        model_client=client,
        existing_decision_count=5,
    )
    attribution = result.attribution

    assert attribution["decision_id"] == "dec-000006"
    assert attribution["decision_type"] == "incident_response"
    assert attribution["tick"] == state["day"]
    assert attribution["character_id"] == CHARACTER_ID
    assert attribution["model_id"] == CHARACTER["model"]
    assert attribution["context_ref"]["context_hash"].startswith("sha256:")
    assert isinstance(attribution["options"], list) and attribution["options"]
    for option_entry in attribution["options"]:
        assert set(option_entry) == {"id", "payload_sha256"}
    assert attribution["choice_id"] == "clean_up"
    assert attribution["choice_payload"] == {"incident_id": "coffee-spill", "method": "clean"}
    assert attribution["rationale"] == "It is my mess."
    assert "raw_response_ref" not in attribution
    assert attribution["validation_status"] == "valid"
    assert "fallback_reason" not in attribution
    assert attribution["candidate_norm_ids"] == ["clean-up-after-yourself"]
    assert attribution["intervention_context"] == []

    # Exactly the fields the design doc specifies (raw_response_ref/fallback_reason
    # are conditional and correctly absent here).
    expected_fields = {
        "decision_id",
        "decision_type",
        "tick",
        "character_id",
        "model_id",
        "context_ref",
        "options",
        "choice_id",
        "choice_payload",
        "rationale",
        "validation_status",
        "candidate_norm_ids",
        "intervention_context",
    }
    assert set(attribution) == expected_fields


def test_attribution_record_includes_fallback_reason_only_on_fallback() -> None:
    state = make_state()
    registry = make_registry(CLEANUP_NORM)
    client = RecordingModelClient(
        [
            {"choice_id": "nope", "rationale": "??"},
            {"choice_id": "still-nope", "rationale": "??"},
        ]
    )

    result = decisions.decide_incident_response(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert isinstance(result.attribution["fallback_reason"], str)
    assert result.attribution["fallback_reason"]


def test_candidate_norm_ids_on_attribution_equals_contexts_relevant_norm_ids() -> None:
    state = make_state()
    registry = make_registry(CLEANUP_NORM, VALUE_OVERLAP_NORM, NON_MATCHING_NORM)
    client = RecordingModelClient([{"choice_id": "clean_up", "rationale": "It is my mess."}])

    context = decisions.assemble_incident_context(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(),
        registry=registry,
    )
    result = decisions.decide_incident_response(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert result.attribution["candidate_norm_ids"] == context["candidate_norm_ids"]
    assert "clean-up-after-yourself" in result.attribution["candidate_norm_ids"]
    assert "keep-composure" in result.attribution["candidate_norm_ids"]
    assert "quarterly-report-honesty" not in result.attribution["candidate_norm_ids"]


def test_decision_type_is_fixed_to_incident_response() -> None:
    state = make_state()
    registry = make_registry(CLEANUP_NORM)
    client = RecordingModelClient([{"choice_id": "clean_up", "rationale": "It is my mess."}])

    result = decisions.decide_incident_response(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert result.attribution["decision_type"] == "incident_response"


def test_decision_id_is_zero_padded_from_existing_decision_count() -> None:
    state = make_state()
    registry = make_registry(CLEANUP_NORM)
    client = RecordingModelClient([{"choice_id": "clean_up", "rationale": "It is my mess."}])

    result = decisions.decide_incident_response(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(),
        registry=registry,
        model_client=client,
        existing_decision_count=41,
    )

    assert result.attribution["decision_id"] == "dec-000042"


# ---------------------------------------------------------------------------
# attempted_action — resolution handoff shape
# ---------------------------------------------------------------------------


def test_attempted_action_matches_the_resolution_handoff_shape() -> None:
    state = make_state()
    registry = make_registry(CLEANUP_NORM)
    client = RecordingModelClient([{"choice_id": "ignore", "rationale": "Not my problem."}])

    result = decisions.decide_incident_response(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(),
        registry=registry,
        model_client=client,
        existing_decision_count=41,
    )

    assert result.attempted_action == {
        "type": "attempted_action",
        "decision_id": "dec-000042",
        "character_id": CHARACTER_ID,
        "action": "skip_cleanup",
        "payload": {"incident_id": "coffee-spill"},
        "candidate_norm_ids": ["clean-up-after-yourself"],
    }


def test_attempted_action_is_emitted_on_fallback_too() -> None:
    state = make_state()
    registry = make_registry(CLEANUP_NORM)
    client = RecordingModelClient(
        [
            {"choice_id": "nope", "rationale": "??"},
            {"choice_id": "still-nope", "rationale": "??"},
        ]
    )

    result = decisions.decide_incident_response(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert result.attempted_action["type"] == "attempted_action"
    assert result.attempted_action["action"] == "resolve_incident"
    assert result.attempted_action["character_id"] == CHARACTER_ID


# ---------------------------------------------------------------------------
# No resolution/state mutation — this module only ever returns objects
# ---------------------------------------------------------------------------


def test_decide_incident_response_does_not_mutate_state_or_characters() -> None:
    state = make_state()
    original_state = dict(state)
    character_copy = dict(CHARACTER)
    registry = make_registry(CLEANUP_NORM)
    client = RecordingModelClient([{"choice_id": "clean_up", "rationale": "It is my mess."}])

    decisions.decide_incident_response(
        state=state,
        characters={CHARACTER_ID: character_copy},
        incident_record=make_incident_record(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert state == original_state
    assert character_copy == CHARACTER


@pytest.mark.parametrize(
    "incident_id,room",
    [("printer-jam", "open-office"), ("awkward-silence", "break-room")],
)
def test_decide_incident_response_works_for_every_seeded_incident_id(
    incident_id: str, room: str
) -> None:
    state = make_state()
    registry = make_registry(CLEANUP_NORM)
    first_option_id = decisions.DECISION_OPTIONS[incident_id][0]["id"]
    client = RecordingModelClient([{"choice_id": first_option_id, "rationale": "Handling it."}])

    result = decisions.decide_incident_response(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(incident_id=incident_id, room=room),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert result.attribution["validation_status"] == "valid"
    assert result.attribution["choice_id"] == first_option_id


# ---------------------------------------------------------------------------
# Norm Pressure — candidate-action fixtures
# ---------------------------------------------------------------------------


def make_expense_candidate_action(
    *,
    character_id: str = CHARACTER_ID,
    amount_incurred: float = 40,
    amount_reported: float = 65,
    receipt_id: str = "receipt-1",
) -> dict[str, Any]:
    return {
        "type": "decision",
        "character_id": character_id,
        "action": "submit_expense_claim",
        "expense_claim": {
            "amount_incurred": amount_incurred,
            "amount_reported": amount_reported,
            "receipt_id": receipt_id,
        },
    }


def make_present_work_candidate_action(
    *,
    character_id: str = CHARACTER_ID,
    actual_contributors: list[str] | None = None,
    claimed_contributors: list[str] | None = None,
    audience: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "decision",
        "character_id": character_id,
        "action": "present_work",
        "actual_contributors": actual_contributors or [CHARACTER_ID, "sam-oduya"],
        "claimed_contributors": claimed_contributors or [CHARACTER_ID],
        "audience": audience or ["manager"],
    }


EXPENSE_HONESTY_NORM = Norm(
    id="expense-honesty",
    scope="tower_policy",
    description="Employees must not knowingly misrepresent expenses or reimbursement claims.",
    severity="major",
    detection="expense_claim_overstated",
    tags=["honesty", "money"],
    # `related_values` deliberately does NOT overlap CHARACTER's declared_values
    # (["keep the peace", "do competent work"]) — this norm must reach
    # candidate_norm_ids only via the fired-detector clause, isolating that clause
    # from the related-values-overlap clause below.
    related_values=["never lie to clients"],
)

CREDIT_SHARING_NORM = Norm(
    id="credit-sharing",
    scope="social_norm",
    description="Employees must not claim sole credit for shared work in front of status "
    "audiences.",
    severity="moderate",
    detection="public_claim_omits_contributors",
    tags=["honesty", "teamwork"],
    related_values=["never lie to clients"],
)


# ---------------------------------------------------------------------------
# Norm Pressure — context assembly
# ---------------------------------------------------------------------------


def test_norm_pressure_context_contains_the_full_required_context_list() -> None:
    state = make_state()
    candidate_action = make_expense_candidate_action()
    registry = make_registry(EXPENSE_HONESTY_NORM)

    context = decisions.assemble_norm_pressure_context(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=candidate_action,
        registry=registry,
        pressure=["deadline-looming"],
    )

    assert context["candidate_action"] == candidate_action
    assert "candidate_norm_ids" in context
    assert "norm_evidence" in context
    assert context["pressure"] == ["deadline-looming"]
    assert context["declared_values"] == CHARACTER["declared_values"]
    assert "relationship_edges" in context


def test_norm_pressure_context_pressure_defaults_to_empty_list() -> None:
    state = make_state()
    candidate_action = make_expense_candidate_action()
    registry = make_registry(EXPENSE_HONESTY_NORM)

    context = decisions.assemble_norm_pressure_context(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=candidate_action,
        registry=registry,
    )

    assert context["pressure"] == []


def test_seeded_expense_overstatement_fires_its_detector() -> None:
    state = make_state()
    candidate_action = make_expense_candidate_action(amount_incurred=40, amount_reported=65)
    registry = make_registry(EXPENSE_HONESTY_NORM)

    context = decisions.assemble_norm_pressure_context(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=candidate_action,
        registry=registry,
    )

    assert "expense-honesty" in context["candidate_norm_ids"]
    assert context["norm_evidence"]["expense-honesty"] == {
        "character_id": CHARACTER_ID,
        "amount_incurred": 40,
        "amount_reported": 65,
        "receipt_id": "receipt-1",
    }


def test_seeded_present_work_omission_fires_its_detector() -> None:
    state = make_state()
    candidate_action = make_present_work_candidate_action(
        actual_contributors=[CHARACTER_ID, "sam-oduya"],
        claimed_contributors=[CHARACTER_ID],
        audience=["manager"],
    )
    registry = make_registry(CREDIT_SHARING_NORM)

    context = decisions.assemble_norm_pressure_context(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=candidate_action,
        registry=registry,
    )

    assert "credit-sharing" in context["candidate_norm_ids"]
    assert context["norm_evidence"]["credit-sharing"] == {
        "character_id": CHARACTER_ID,
        "work_item_id": None,
        "omitted_contributors": ["sam-oduya"],
        "audience": ["manager"],
    }


def test_norm_pressure_relevant_norms_include_a_related_values_overlap_norm() -> None:
    state = make_state()
    candidate_action = make_expense_candidate_action()
    registry = make_registry(VALUE_OVERLAP_NORM)

    context = decisions.assemble_norm_pressure_context(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=candidate_action,
        registry=registry,
    )

    assert "keep-composure" in context["candidate_norm_ids"]


def test_norm_pressure_relevant_norms_exclude_a_non_matching_norm() -> None:
    state = make_state()
    candidate_action = make_expense_candidate_action()
    # CLEANUP_NORM's detector (incident_cleanup_owner_missed) never fires on a
    # `type: "decision"` record, and its related_values don't overlap CHARACTER's
    # declared_values either.
    registry = make_registry(EXPENSE_HONESTY_NORM, CLEANUP_NORM)

    context = decisions.assemble_norm_pressure_context(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=candidate_action,
        registry=registry,
    )

    assert "clean-up-after-yourself" not in context["candidate_norm_ids"]


def test_norm_pressure_relationship_edges_include_an_edge_seeded_via_edge_delta() -> None:
    state = make_state()
    state = apply_event(
        state,
        {
            "type": "edge_delta",
            "day": 7,
            "event_id": "evt-1",
            "from": CHARACTER_ID,
            "to": "sam-oduya",
            "edges": {"trust": {"delta": 2}},
        },
    )
    candidate_action = make_expense_candidate_action()
    registry = make_registry(EXPENSE_HONESTY_NORM)

    context = decisions.assemble_norm_pressure_context(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=candidate_action,
        registry=registry,
    )

    edges = context["relationship_edges"]
    assert any(
        edge["from"] == CHARACTER_ID and edge["to"] == "sam-oduya" and "trust" in edge["qualities"]
        for edge in edges
    )


# ---------------------------------------------------------------------------
# Norm Pressure — option inventory
# ---------------------------------------------------------------------------


def test_norm_pressure_options_cover_both_detector_backed_action_kinds() -> None:
    assert set(decisions.NORM_PRESSURE_OPTIONS) == {"submit_expense_claim", "present_work"}
    for action, options in decisions.NORM_PRESSURE_OPTIONS.items():
        assert 2 <= len(options) <= 3, action
        ids = [option["id"] for option in options]
        assert len(ids) == len(set(ids)), action
        assert any(option.get("fallback") is True for option in options), action
        for option in options:
            assert option["action"] == action


# ---------------------------------------------------------------------------
# Norm Pressure — model routing + validation/retry/fallback FSM
# ---------------------------------------------------------------------------


def test_norm_pressure_decision_call_routes_to_the_characters_assigned_model() -> None:
    state = make_state()
    registry = make_registry(EXPENSE_HONESTY_NORM)
    fallback_id = next(
        o["id"]
        for o in decisions.NORM_PRESSURE_OPTIONS["submit_expense_claim"]
        if o.get("fallback")
    )
    client = RecordingModelClient(
        [{"choice_id": fallback_id, "rationale": "I will report it honestly."}]
    )

    decisions.decide_norm_pressure(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=make_expense_candidate_action(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert len(client.calls) == 1
    assert client.calls[0]["character"]["model"] == CHARACTER["model"]
    assert client.calls[0]["character"]["id"] == CHARACTER_ID


def test_norm_pressure_malformed_first_response_triggers_exactly_one_retry_then_succeeds() -> None:
    state = make_state()
    registry = make_registry(EXPENSE_HONESTY_NORM)
    options = decisions.NORM_PRESSURE_OPTIONS["submit_expense_claim"]
    second_option = next(o for o in options if not o.get("fallback"))
    client = RecordingModelClient(
        [
            {"choice_id": "not-a-real-option", "rationale": "??"},
            {"choice_id": second_option["id"], "rationale": "Rounding it up a little."},
        ]
    )

    result = decisions.decide_norm_pressure(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=make_expense_candidate_action(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert len(client.calls) == 2
    assert "validation_error" in client.calls[1]
    assert client.calls[1]["context_ref"] == client.calls[0]["context_ref"]
    assert result.attribution["validation_status"] == "retry_valid"
    assert result.attribution["choice_id"] == second_option["id"]


def test_norm_pressure_two_malformed_responses_fall_back_to_marked_fallback_option() -> None:
    state = make_state()
    registry = make_registry(EXPENSE_HONESTY_NORM)
    client = RecordingModelClient(
        [
            {"choice_id": "nope", "rationale": "??"},
            {"choice_id": "still-nope", "rationale": "??"},
        ]
    )

    result = decisions.decide_norm_pressure(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=make_expense_candidate_action(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert len(client.calls) == 2
    assert result.attribution["validation_status"] == "fallback"
    assert "fallback_reason" in result.attribution
    marked = next(
        o
        for o in decisions.NORM_PRESSURE_OPTIONS["submit_expense_claim"]
        if o.get("fallback") is True
    )
    assert result.attribution["choice_id"] == marked["id"]


# ---------------------------------------------------------------------------
# Norm Pressure — pressure passthrough
# ---------------------------------------------------------------------------


def test_norm_pressure_pressure_passes_through_untouched_to_payload_situation() -> None:
    state = make_state()
    registry = make_registry(EXPENSE_HONESTY_NORM)
    fallback_id = next(
        o["id"]
        for o in decisions.NORM_PRESSURE_OPTIONS["submit_expense_claim"]
        if o.get("fallback")
    )
    client = RecordingModelClient([{"choice_id": fallback_id, "rationale": "Honest is easiest."}])

    decisions.decide_norm_pressure(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=make_expense_candidate_action(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
        pressure=["deadline-looming", "manager-nearby"],
    )

    assert client.calls[0]["situation"]["pressure"] == ["deadline-looming", "manager-nearby"]


def test_norm_pressure_pressure_defaults_to_empty_list_in_payload() -> None:
    state = make_state()
    registry = make_registry(EXPENSE_HONESTY_NORM)
    fallback_id = next(
        o["id"]
        for o in decisions.NORM_PRESSURE_OPTIONS["submit_expense_claim"]
        if o.get("fallback")
    )
    client = RecordingModelClient([{"choice_id": fallback_id, "rationale": "Honest is easiest."}])

    decisions.decide_norm_pressure(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=make_expense_candidate_action(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert client.calls[0]["situation"]["pressure"] == []


# ---------------------------------------------------------------------------
# Norm Pressure — unknown action validation
# ---------------------------------------------------------------------------


def test_unknown_candidate_action_action_raises_validation_error() -> None:
    state = make_state()
    registry = make_registry(EXPENSE_HONESTY_NORM)
    client = RecordingModelClient([])
    candidate_action = make_expense_candidate_action()
    candidate_action["action"] = "steal_supplies"

    with pytest.raises(ValidationError) as exc_info:
        decisions.decide_norm_pressure(
            state=state,
            characters={CHARACTER_ID: CHARACTER},
            candidate_action=candidate_action,
            registry=registry,
            model_client=client,
            existing_decision_count=0,
        )

    assert "present_work" in str(exc_info.value)
    assert "submit_expense_claim" in str(exc_info.value)
    assert client.calls == []


# ---------------------------------------------------------------------------
# Norm Pressure — attribution record
# ---------------------------------------------------------------------------


def test_norm_pressure_attribution_record_carries_the_full_field_set() -> None:
    state = make_state()
    registry = make_registry(EXPENSE_HONESTY_NORM)
    fallback_id = next(
        o["id"]
        for o in decisions.NORM_PRESSURE_OPTIONS["submit_expense_claim"]
        if o.get("fallback")
    )
    client = RecordingModelClient([{"choice_id": fallback_id, "rationale": "Honest is easiest."}])

    result = decisions.decide_norm_pressure(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=make_expense_candidate_action(),
        registry=registry,
        model_client=client,
        existing_decision_count=5,
    )
    attribution = result.attribution

    assert attribution["decision_id"] == "dec-000006"
    assert attribution["decision_type"] == "norm_pressure"
    assert attribution["tick"] == state["day"]
    assert attribution["character_id"] == CHARACTER_ID
    assert attribution["model_id"] == CHARACTER["model"]
    assert attribution["context_ref"]["context_hash"].startswith("sha256:")
    assert isinstance(attribution["options"], list) and attribution["options"]
    for option_entry in attribution["options"]:
        assert set(option_entry) == {"id", "payload_sha256"}
    assert attribution["choice_id"] == fallback_id
    assert attribution["validation_status"] == "valid"
    assert "fallback_reason" not in attribution
    assert "expense-honesty" in attribution["candidate_norm_ids"]

    expected_fields = {
        "decision_id",
        "decision_type",
        "tick",
        "character_id",
        "model_id",
        "context_ref",
        "options",
        "choice_id",
        "choice_payload",
        "rationale",
        "validation_status",
        "candidate_norm_ids",
        "intervention_context",
    }
    assert set(attribution) == expected_fields


def test_norm_pressure_attribution_candidate_norm_ids_equals_context_set() -> None:
    state = make_state()
    registry = make_registry(EXPENSE_HONESTY_NORM, VALUE_OVERLAP_NORM, CLEANUP_NORM)
    candidate_action = make_expense_candidate_action()
    fallback_id = next(
        o["id"]
        for o in decisions.NORM_PRESSURE_OPTIONS["submit_expense_claim"]
        if o.get("fallback")
    )
    client = RecordingModelClient([{"choice_id": fallback_id, "rationale": "Honest is easiest."}])

    context = decisions.assemble_norm_pressure_context(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=candidate_action,
        registry=registry,
    )
    result = decisions.decide_norm_pressure(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=candidate_action,
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert result.attribution["candidate_norm_ids"] == context["candidate_norm_ids"]
    assert "expense-honesty" in result.attribution["candidate_norm_ids"]
    assert "keep-composure" in result.attribution["candidate_norm_ids"]
    assert "clean-up-after-yourself" not in result.attribution["candidate_norm_ids"]


def test_norm_pressure_attempted_action_candidate_norm_ids_is_the_options_static_field() -> None:
    state = make_state()
    registry = make_registry(EXPENSE_HONESTY_NORM)
    fallback_option = next(
        o for o in decisions.NORM_PRESSURE_OPTIONS["submit_expense_claim"] if o.get("fallback")
    )
    client = RecordingModelClient(
        [{"choice_id": fallback_option["id"], "rationale": "Honest is easiest."}]
    )

    result = decisions.decide_norm_pressure(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=make_expense_candidate_action(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert result.attempted_action["type"] == "attempted_action"
    assert result.attempted_action["action"] == fallback_option["action"]
    assert result.attempted_action["payload"] == fallback_option["payload"]
    assert result.attempted_action["candidate_norm_ids"] == fallback_option.get(
        "candidate_norm_ids", []
    )


# ---------------------------------------------------------------------------
# Norm Pressure — no resolution/state mutation
# ---------------------------------------------------------------------------


def test_decide_norm_pressure_does_not_mutate_state_or_characters() -> None:
    state = make_state()
    original_state = dict(state)
    character_copy = dict(CHARACTER)
    registry = make_registry(EXPENSE_HONESTY_NORM)
    fallback_id = next(
        o["id"]
        for o in decisions.NORM_PRESSURE_OPTIONS["submit_expense_claim"]
        if o.get("fallback")
    )
    client = RecordingModelClient([{"choice_id": fallback_id, "rationale": "Honest is easiest."}])

    decisions.decide_norm_pressure(
        state=state,
        characters={CHARACTER_ID: character_copy},
        candidate_action=make_expense_candidate_action(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert state == original_state
    assert character_copy == CHARACTER


@pytest.mark.parametrize(
    "candidate_action_factory,action",
    [
        (make_expense_candidate_action, "submit_expense_claim"),
        (make_present_work_candidate_action, "present_work"),
    ],
)
def test_decide_norm_pressure_works_for_every_seeded_action_kind(
    candidate_action_factory: Any, action: str
) -> None:
    state = make_state()
    registry = make_registry(EXPENSE_HONESTY_NORM)
    first_option_id = decisions.NORM_PRESSURE_OPTIONS[action][0]["id"]
    client = RecordingModelClient([{"choice_id": first_option_id, "rationale": "Handling it."}])

    result = decisions.decide_norm_pressure(
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=candidate_action_factory(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )
    assert result.attribution["validation_status"] == "valid"
    assert result.attribution["choice_id"] == first_option_id


# ---------------------------------------------------------------------------
# Social Disclosure — fixtures
# ---------------------------------------------------------------------------

# A distinctive marker used for the sealed-content boundary test: it must never
# show up anywhere except inside the sealed secret's own `content` field.
SEALED_CONTENT_MARKER = "ZZ-SEALED-PAYLOAD-9f21c7-DO-NOT-LEAK"

SECRET_ID = "affair-1"

CLIENT_CONFIDENTIALITY_NORM = Norm(
    id="client-confidentiality",
    scope="tower_policy",
    description="Employees must not share client-confidential information with unauthorized "
    "parties.",
    severity="major",
    detection="secret_shared_with_unauthorized_audience",
    tags=["confidentiality", "client", "trust"],
    # Deliberately does NOT overlap CHARACTER's declared_values, so this norm reaches
    # candidate_norm_ids only via the fired-detector clause in the relevant tests.
    related_values=["discretion", "professionalism", "loyalty"],
)


@pytest.fixture
def world(tmp_path: Path) -> Path:
    world_path = tmp_path / "tower"
    world_path.mkdir()
    return world_path


def seal_test_secret(world: Path, **overrides: Any) -> None:
    kwargs: dict[str, Any] = {
        "id": SECRET_ID,
        "holder": CHARACTER_ID,
        "content": SEALED_CONTENT_MARKER,
        "is_true": True,
        "knowers": [CHARACTER_ID],
    }
    kwargs.update(overrides)
    secrets.seal_secret(world, **kwargs)


def make_disclosure_opportunity(
    *,
    character_id: str = CHARACTER_ID,
    secret_id: str = SECRET_ID,
    actual_audience: list[str] | None = None,
    authorized_audience: list[str] | None = None,
    secret_classification: str | None = "client_confidential",
) -> dict[str, Any]:
    opportunity: dict[str, Any] = {
        "type": "decision",
        "action": "share_secret",
        "character_id": character_id,
        "secret_id": secret_id,
        "actual_audience": (
            list(actual_audience) if actual_audience is not None else ["open-office"]
        ),
        "authorized_audience": (
            list(authorized_audience) if authorized_audience is not None else [CHARACTER_ID]
        ),
    }
    if secret_classification is not None:
        opportunity["secret_classification"] = secret_classification
    return opportunity


# ---------------------------------------------------------------------------
# Social Disclosure — context assembly
# ---------------------------------------------------------------------------


def test_social_disclosure_context_contains_the_full_required_context_list(
    world: Path,
) -> None:
    seal_test_secret(world)
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)
    opportunity = make_disclosure_opportunity()

    context = decisions.assemble_social_disclosure_context(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=opportunity,
        registry=registry,
        observable_facts=["the office noticed Jordan leaving early"],
    )

    assert context["observable_facts"] == ["the office noticed Jordan leaving early"]
    assert context["secret"]["id"] == SECRET_ID
    assert context["secret"]["knowers"] == [CHARACTER_ID]
    assert "content" not in context["secret"]
    assert context["actual_audience"] == ["open-office"]
    assert context["authorized_audience"] == [CHARACTER_ID]
    assert "candidate_norm_ids" in context
    assert "relationship_edges" in context


def test_social_disclosure_observable_facts_defaults_to_empty_list(world: Path) -> None:
    seal_test_secret(world)
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)

    context = decisions.assemble_social_disclosure_context(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=make_disclosure_opportunity(),
        registry=registry,
    )

    assert context["observable_facts"] == []


def test_social_disclosure_relationship_edges_include_an_edge_seeded_via_edge_delta(
    world: Path,
) -> None:
    seal_test_secret(world)
    state = make_state()
    state = apply_event(
        state,
        {
            "type": "edge_delta",
            "day": 7,
            "event_id": "evt-1",
            "from": CHARACTER_ID,
            "to": "sam-oduya",
            "edges": {"trust": {"delta": 2}},
        },
    )
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)

    context = decisions.assemble_social_disclosure_context(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=make_disclosure_opportunity(),
        registry=registry,
    )

    edges = context["relationship_edges"]
    assert any(
        edge["from"] == CHARACTER_ID and edge["to"] == "sam-oduya" and "trust" in edge["qualities"]
        for edge in edges
    )


# ---------------------------------------------------------------------------
# Social Disclosure — knowers gate
# ---------------------------------------------------------------------------


def test_character_not_in_secrets_knowers_raises_secrets_validation_error(world: Path) -> None:
    seal_test_secret(world, holder="sam-oduya", knowers=["sam-oduya"])
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)
    client = RecordingModelClient([])

    with pytest.raises(secrets.ValidationError):
        decisions.decide_social_disclosure(
            world=world,
            state=state,
            characters={CHARACTER_ID: CHARACTER},
            disclosure_opportunity=make_disclosure_opportunity(),
            registry=registry,
            model_client=client,
            existing_decision_count=0,
        )

    assert client.calls == []


def test_character_not_in_knowers_raises_via_context_assembly_too(world: Path) -> None:
    seal_test_secret(world, holder="sam-oduya", knowers=["sam-oduya"])
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)

    with pytest.raises(secrets.ValidationError):
        decisions.assemble_social_disclosure_context(
            world=world,
            state=state,
            characters={CHARACTER_ID: CHARACTER},
            disclosure_opportunity=make_disclosure_opportunity(),
            registry=registry,
        )


# ---------------------------------------------------------------------------
# Social Disclosure — sealed-content boundary (the critical invariant)
# ---------------------------------------------------------------------------


def test_sealed_secret_content_never_leaks_into_context_payload_attribution_or_action(
    world: Path,
) -> None:
    seal_test_secret(world)
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)
    opportunity = make_disclosure_opportunity()
    client = RecordingModelClient([{"choice_id": "withhold", "rationale": "Too risky."}])

    context = decisions.assemble_social_disclosure_context(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=opportunity,
        registry=registry,
    )
    result = decisions.decide_social_disclosure(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=opportunity,
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert len(client.calls) == 1
    assert SEALED_CONTENT_MARKER not in json.dumps(context)
    assert SEALED_CONTENT_MARKER not in json.dumps(client.calls[0])
    assert SEALED_CONTENT_MARKER not in json.dumps(result.attribution)
    assert SEALED_CONTENT_MARKER not in json.dumps(result.attempted_action)


# ---------------------------------------------------------------------------
# Social Disclosure — norm relevance (audience-driven, guarded by classification)
# ---------------------------------------------------------------------------


def test_norm_relevance_fires_on_unauthorized_client_confidential_audience(world: Path) -> None:
    seal_test_secret(world)
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)
    opportunity = make_disclosure_opportunity(
        actual_audience=["open-office"],
        authorized_audience=[CHARACTER_ID],
        secret_classification="client_confidential",
    )

    context = decisions.assemble_social_disclosure_context(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=opportunity,
        registry=registry,
    )

    assert "client-confidentiality" in context["candidate_norm_ids"]


def test_norm_relevance_absent_when_authorized_audience_covers_actual(world: Path) -> None:
    seal_test_secret(world)
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)
    opportunity = make_disclosure_opportunity(
        actual_audience=["open-office"],
        authorized_audience=["open-office", CHARACTER_ID],
        secret_classification="client_confidential",
    )

    context = decisions.assemble_social_disclosure_context(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=opportunity,
        registry=registry,
    )

    assert "client-confidentiality" not in context["candidate_norm_ids"]


def test_norm_relevance_absent_when_classification_is_missing(world: Path) -> None:
    seal_test_secret(world)
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)
    # Unauthorized audience, but no secret_classification field at all.
    opportunity = make_disclosure_opportunity(
        actual_audience=["open-office"],
        authorized_audience=[CHARACTER_ID],
        secret_classification=None,
    )
    assert "secret_classification" not in opportunity

    context = decisions.assemble_social_disclosure_context(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=opportunity,
        registry=registry,
    )

    assert "client-confidentiality" not in context["candidate_norm_ids"]


def test_norm_relevance_absent_when_classification_is_not_client_confidential(
    world: Path,
) -> None:
    seal_test_secret(world)
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)
    # Unauthorized audience, but classification is some other (non-canonical) value.
    opportunity = make_disclosure_opportunity(
        actual_audience=["open-office"],
        authorized_audience=[CHARACTER_ID],
        secret_classification="internal_only",
    )

    context = decisions.assemble_social_disclosure_context(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=opportunity,
        registry=registry,
    )

    assert "client-confidentiality" not in context["candidate_norm_ids"]


# ---------------------------------------------------------------------------
# Social Disclosure — option inventory
# ---------------------------------------------------------------------------


def test_social_disclosure_options_are_withhold_share_partial_share_full() -> None:
    ids = [option["id"] for option in decisions.SOCIAL_DISCLOSURE_OPTIONS]
    assert ids == ["withhold", "share_partial", "share_full"]


def test_social_disclosure_options_carry_empty_candidate_norm_ids_by_design() -> None:
    for option in decisions.SOCIAL_DISCLOSURE_OPTIONS:
        assert option["candidate_norm_ids"] == [], option["id"]


def test_social_disclosure_withhold_is_the_fallback_option() -> None:
    fallback = next(o for o in decisions.SOCIAL_DISCLOSURE_OPTIONS if o.get("fallback") is True)
    assert fallback["id"] == "withhold"


# ---------------------------------------------------------------------------
# Social Disclosure — model routing + validation/retry/fallback FSM
# ---------------------------------------------------------------------------


def test_social_disclosure_decision_call_routes_to_the_characters_assigned_model(
    world: Path,
) -> None:
    seal_test_secret(world)
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)
    client = RecordingModelClient([{"choice_id": "withhold", "rationale": "Too risky."}])

    decisions.decide_social_disclosure(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=make_disclosure_opportunity(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert len(client.calls) == 1
    assert client.calls[0]["character"]["model"] == CHARACTER["model"]
    assert client.calls[0]["character"]["id"] == CHARACTER_ID


def test_social_disclosure_malformed_first_response_triggers_one_retry_then_succeeds(
    world: Path,
) -> None:
    seal_test_secret(world)
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)
    client = RecordingModelClient(
        [
            {"choice_id": "not-a-real-option", "rationale": "??"},
            {"choice_id": "share_partial", "rationale": "A little, carefully."},
        ]
    )

    result = decisions.decide_social_disclosure(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=make_disclosure_opportunity(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert len(client.calls) == 2
    assert "validation_error" in client.calls[1]
    assert client.calls[1]["context_ref"] == client.calls[0]["context_ref"]
    assert result.attribution["validation_status"] == "retry_valid"
    assert result.attribution["choice_id"] == "share_partial"


def test_social_disclosure_two_malformed_responses_fall_back_to_withhold(world: Path) -> None:
    seal_test_secret(world)
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)
    client = RecordingModelClient(
        [
            {"choice_id": "nope", "rationale": "??"},
            {"choice_id": "still-nope", "rationale": "??"},
        ]
    )

    result = decisions.decide_social_disclosure(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=make_disclosure_opportunity(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert len(client.calls) == 2
    assert result.attribution["validation_status"] == "fallback"
    assert "fallback_reason" in result.attribution
    assert result.attribution["choice_id"] == "withhold"


# ---------------------------------------------------------------------------
# Social Disclosure — payloads carry secret_id filled at call time
# ---------------------------------------------------------------------------


def test_social_disclosure_option_payloads_carry_secret_id_from_the_opportunity(
    world: Path,
) -> None:
    seal_test_secret(world)
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)
    client = RecordingModelClient([{"choice_id": "withhold", "rationale": "Too risky."}])

    decisions.decide_social_disclosure(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=make_disclosure_opportunity(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    sent_options = client.calls[0]["options"]
    assert len(sent_options) == 3
    for option in sent_options:
        assert option["payload"]["secret_id"] == SECRET_ID


# ---------------------------------------------------------------------------
# Social Disclosure — attribution record
# ---------------------------------------------------------------------------


def test_social_disclosure_attribution_record_carries_the_full_field_set(world: Path) -> None:
    seal_test_secret(world)
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)
    client = RecordingModelClient([{"choice_id": "withhold", "rationale": "Too risky."}])

    result = decisions.decide_social_disclosure(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=make_disclosure_opportunity(),
        registry=registry,
        model_client=client,
        existing_decision_count=5,
    )
    attribution = result.attribution

    assert attribution["decision_id"] == "dec-000006"
    assert attribution["decision_type"] == "social_disclosure"
    assert attribution["tick"] == state["day"]
    assert attribution["character_id"] == CHARACTER_ID
    assert attribution["model_id"] == CHARACTER["model"]
    assert attribution["context_ref"]["context_hash"].startswith("sha256:")
    assert isinstance(attribution["options"], list) and attribution["options"]
    for option_entry in attribution["options"]:
        assert set(option_entry) == {"id", "payload_sha256"}
    assert attribution["choice_id"] == "withhold"
    assert attribution["validation_status"] == "valid"
    assert "fallback_reason" not in attribution

    expected_fields = {
        "decision_id",
        "decision_type",
        "tick",
        "character_id",
        "model_id",
        "context_ref",
        "options",
        "choice_id",
        "choice_payload",
        "rationale",
        "validation_status",
        "candidate_norm_ids",
        "intervention_context",
    }
    assert set(attribution) == expected_fields


def test_social_disclosure_attribution_candidate_norm_ids_equals_context_set(
    world: Path,
) -> None:
    seal_test_secret(world)
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)
    opportunity = make_disclosure_opportunity(
        actual_audience=["open-office"],
        authorized_audience=[CHARACTER_ID],
        secret_classification="client_confidential",
    )
    client = RecordingModelClient([{"choice_id": "withhold", "rationale": "Too risky."}])

    context = decisions.assemble_social_disclosure_context(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=opportunity,
        registry=registry,
    )
    result = decisions.decide_social_disclosure(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=opportunity,
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert result.attribution["candidate_norm_ids"] == context["candidate_norm_ids"]
    assert "client-confidentiality" in result.attribution["candidate_norm_ids"]


def test_social_disclosure_decision_type_is_fixed(world: Path) -> None:
    seal_test_secret(world)
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)
    client = RecordingModelClient([{"choice_id": "withhold", "rationale": "Too risky."}])

    result = decisions.decide_social_disclosure(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=make_disclosure_opportunity(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert result.attribution["decision_type"] == "social_disclosure"


# ---------------------------------------------------------------------------
# Social Disclosure — attempted_action (resolution handoff shape)
# ---------------------------------------------------------------------------


def test_social_disclosure_attempted_action_matches_the_resolution_handoff_shape(
    world: Path,
) -> None:
    seal_test_secret(world)
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)
    client = RecordingModelClient([{"choice_id": "share_full", "rationale": "Full disclosure."}])

    result = decisions.decide_social_disclosure(
        world=world,
        state=state,
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=make_disclosure_opportunity(),
        registry=registry,
        model_client=client,
        existing_decision_count=41,
    )

    chosen = next(o for o in decisions.SOCIAL_DISCLOSURE_OPTIONS if o["id"] == "share_full")
    assert result.attempted_action == {
        "type": "attempted_action",
        "decision_id": "dec-000042",
        "character_id": CHARACTER_ID,
        "action": chosen["action"],
        "payload": {**chosen["payload"], "secret_id": SECRET_ID},
        "candidate_norm_ids": [],
    }


# ---------------------------------------------------------------------------
# Social Disclosure — no resolution/state/secret-store mutation
# ---------------------------------------------------------------------------


def test_decide_social_disclosure_does_not_mutate_state_or_characters(world: Path) -> None:
    seal_test_secret(world)
    state = make_state()
    original_state = dict(state)
    character_copy = dict(CHARACTER)
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)
    client = RecordingModelClient([{"choice_id": "withhold", "rationale": "Too risky."}])

    decisions.decide_social_disclosure(
        world=world,
        state=state,
        characters={CHARACTER_ID: character_copy},
        disclosure_opportunity=make_disclosure_opportunity(),
        registry=registry,
        model_client=client,
        existing_decision_count=0,
    )

    assert state == original_state
    assert character_copy == CHARACTER


def test_decide_social_disclosure_works_for_every_seeded_option(world: Path) -> None:
    seal_test_secret(world)
    state = make_state()
    registry = make_registry(CLIENT_CONFIDENTIALITY_NORM)

    for option in decisions.SOCIAL_DISCLOSURE_OPTIONS:
        client = RecordingModelClient([{"choice_id": option["id"], "rationale": "Handling it."}])
        result = decisions.decide_social_disclosure(
            world=world,
            state=state,
            characters={CHARACTER_ID: CHARACTER},
            disclosure_opportunity=make_disclosure_opportunity(),
            registry=registry,
            model_client=client,
            existing_decision_count=0,
        )
        assert result.attribution["validation_status"] == "valid"
        assert result.attribution["choice_id"] == option["id"]


# ---------------------------------------------------------------------------
# Shared payload shape — character / response_schema / context_ref, across all three
# decide_* entry points
# ---------------------------------------------------------------------------


def test_all_three_decide_entry_points_send_the_same_shared_payload_shape(world: Path) -> None:
    seal_test_secret(world)
    expected_character = {
        "id": CHARACTER_ID,
        "name": CHARACTER["name"],
        "model": CHARACTER["model"],
        "stats": CHARACTER["stats"],
        "qualities": CHARACTER["qualities"],
        "declared_values": CHARACTER["declared_values"],
    }
    expected_response_schema = {
        "choice_id": "one of options[].id",
        "rationale": "short first-person reason",
    }

    incident_client = RecordingModelClient(
        [{"choice_id": "clean_up", "rationale": "It is my mess."}]
    )
    decisions.decide_incident_response(
        state=make_state(),
        characters={CHARACTER_ID: CHARACTER},
        incident_record=make_incident_record(),
        registry=make_registry(CLEANUP_NORM),
        model_client=incident_client,
        existing_decision_count=0,
    )

    fallback_id = next(
        o["id"]
        for o in decisions.NORM_PRESSURE_OPTIONS["submit_expense_claim"]
        if o.get("fallback")
    )
    norm_pressure_client = RecordingModelClient(
        [{"choice_id": fallback_id, "rationale": "I will report it honestly."}]
    )
    decisions.decide_norm_pressure(
        state=make_state(),
        characters={CHARACTER_ID: CHARACTER},
        candidate_action=make_expense_candidate_action(),
        registry=make_registry(EXPENSE_HONESTY_NORM),
        model_client=norm_pressure_client,
        existing_decision_count=0,
    )

    social_disclosure_client = RecordingModelClient(
        [{"choice_id": "withhold", "rationale": "Too risky."}]
    )
    decisions.decide_social_disclosure(
        world=world,
        state=make_state(),
        characters={CHARACTER_ID: CHARACTER},
        disclosure_opportunity=make_disclosure_opportunity(),
        registry=make_registry(CLIENT_CONFIDENTIALITY_NORM),
        model_client=social_disclosure_client,
        existing_decision_count=0,
    )

    for client in (incident_client, norm_pressure_client, social_disclosure_client):
        assert len(client.calls) == 1
        payload = client.calls[0]
        assert payload["character"] == expected_character
        assert payload["response_schema"] == expected_response_schema
        assert payload["context_ref"]["state_path"] == "state/tower.json"
        assert payload["context_ref"]["event_sequence"] == 0
