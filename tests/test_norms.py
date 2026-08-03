from pathlib import Path

import pytest

from breakroom.norms import integrity_drift, load_registry, tag_record
from breakroom.worldstate import ValidationError

NORMS_TOML = """\
[[norms]]
id = "expense-honesty"
scope = "tower_policy"
description = "Employees must not knowingly misrepresent expenses or reimbursement claims."
severity = "major"
detection = "expense_claim_overstated"
tags = ["honesty", "money", "trust"]
related_values = ["honesty", "fairness", "stewardship"]

[[norms]]
id = "credit-sharing"
scope = "social_norm"
description = "Employees must not claim sole credit for shared work in front of status audiences."
severity = "moderate"
detection = "public_claim_omits_contributors"
tags = ["credit", "status", "trust"]
related_values = ["fairness", "honesty", "teamwork"]

[[norms]]
id = "assigned-shift-duty"
scope = "character_duty"
description = "Employees must attend their assigned shifts."
severity = "moderate"
detection = "assigned_shift_unattended"
tags = ["duty", "reliability", "operations"]
related_values = ["reliability", "responsibility"]

[[norms]]
id = "client-confidentiality"
scope = "tower_policy"
description = "Employees must not share client-confidential information with unauthorized parties."
severity = "major"
detection = "secret_shared_with_unauthorized_audience"
tags = ["confidentiality", "client", "trust"]
related_values = ["discretion", "professionalism", "loyalty"]

[[norms]]
id = "shared-space-care"
scope = "social_norm"
description = "Employees who own shared-space incident cleanup must see it through."
severity = "minor"
detection = "incident_cleanup_owner_missed"
tags = ["care", "shared-space", "morale"]
related_values = ["care", "teamwork", "responsibility"]
"""

EXPENSE_FRAUD_DECISION = {
    "type": "decision",
    "character_id": "mara-chen",
    "action": "submit_expense_claim",
    "expense_claim": {
        "amount_incurred": 42,
        "amount_reported": 120,
        "receipt_id": "receipt-0007",
    },
}

EXPENSE_HONEST_DECISION = {
    "type": "decision",
    "character_id": "mara-chen",
    "action": "submit_expense_claim",
    "expense_claim": {
        "amount_incurred": 42,
        "amount_reported": 42,
        "receipt_id": "receipt-0008",
    },
}

CREDIT_STEALING_DECISION = {
    "type": "decision",
    "character_id": "eli-ramos",
    "action": "present_work",
    "work_item_id": "contract-alpha-slide-deck",
    "actual_contributors": ["eli-ramos", "jordan-vale"],
    "claimed_contributors": ["eli-ramos"],
    "audience": ["manager"],
}

CREDIT_SHARING_PRIVATE_NOTE = {
    "type": "decision",
    "character_id": "eli-ramos",
    "action": "present_work",
    "work_item_id": "contract-alpha-slide-deck",
    "actual_contributors": ["eli-ramos", "jordan-vale"],
    "claimed_contributors": ["eli-ramos"],
    "audience": [],
}

SHIFT_ASSIGNMENTS_STATE = {
    "assignments": [
        {
            "character_id": "jordan-vale",
            "duty_id": "front-desk-close",
            "day": 9,
            "required_room": "front-desk",
        }
    ]
}

SHIFT_SKIPPED_OBSERVATION = {
    "type": "location_observation",
    "day": 9,
    "character_id": "jordan-vale",
    "room_id": "break-room",
    "during_duty_id": "front-desk-close",
}

SHIFT_ATTENDED_EVENT = {
    "type": "duty_attended",
    "day": 9,
    "character_id": "jordan-vale",
    "duty_id": "front-desk-close",
}

SHIFT_ATTENDED_PRIOR_DAY_EVENT = {
    "type": "duty_attended",
    "day": 8,
    "character_id": "jordan-vale",
    "duty_id": "front-desk-close",
}

CONFIDENTIALITY_LEAK_DECISION = {
    "type": "decision",
    "character_id": "nina-patel",
    "action": "share_secret",
    "secret_id": "client-merger-rumor",
    "secret_classification": "client_confidential",
    "authorized_audience": ["exec-team"],
    "actual_audience": ["open-office"],
}

CONFIDENTIALITY_AUTHORIZED_DECISION = {
    "type": "decision",
    "character_id": "nina-patel",
    "action": "share_secret",
    "secret_id": "client-merger-rumor",
    "secret_classification": "client_confidential",
    "authorized_audience": ["exec-team"],
    "actual_audience": ["exec-team"],
}

SHARED_SPACE_NEGLECT_INCIDENT = {
    "type": "incident",
    "incident": {
        "id": "coffee-spill",
        "room": "break-room",
        "cleanup_owner": "jordan-vale",
        "resolved": False,
    },
}

SHARED_SPACE_RESOLVED_INCIDENT = {
    "type": "incident",
    "incident": {
        "id": "coffee-spill",
        "room": "break-room",
        "cleanup_owner": "jordan-vale",
        "resolved": True,
    },
}

IRRELEVANT_SCENE_EVENT = {
    "type": "scene",
    "day": 3,
    "character_id": "jordan-vale",
    "incident_id": "coffee-spill",
}


def write_registry(world: Path, toml_text: str = NORMS_TOML) -> None:
    (world / "data").mkdir(parents=True, exist_ok=True)
    (world / "data" / "norms.toml").write_text(toml_text, encoding="utf-8")


def test_load_registry_parses_all_worked_example_norms(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(world)

    registry = load_registry(world)

    assert set(registry.norms) == {
        "expense-honesty",
        "credit-sharing",
        "assigned-shift-duty",
        "client-confidentiality",
        "shared-space-care",
    }
    assert registry.norms["expense-honesty"].severity == "major"
    assert registry.norms["expense-honesty"].related_values == [
        "honesty",
        "fairness",
        "stewardship",
    ]


def test_load_registry_requires_file(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    world.mkdir()

    with pytest.raises(ValidationError, match="data/norms.toml: missing file"):
        load_registry(world)


def test_load_registry_rejects_norm_missing_required_field(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(
        world,
        '[[norms]]\nid = "expense-honesty"\nscope = "tower_policy"\n',
    )

    with pytest.raises(ValidationError, match="data/norms.toml: missing description"):
        load_registry(world)


def test_load_registry_rejects_invalid_scope(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(
        world,
        "\n".join(
            [
                '[[norms]]',
                'id = "expense-honesty"',
                'scope = "made-up-scope"',
                'description = "desc"',
                'severity = "major"',
                'detection = "expense_claim_overstated"',
                'tags = ["honesty"]',
                'related_values = ["honesty"]',
            ]
        ),
    )

    with pytest.raises(ValidationError, match="data/norms.toml: invalid scope"):
        load_registry(world)


def test_expense_fraud_detected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(world)
    registry = load_registry(world)

    result = tag_record(registry, EXPENSE_FRAUD_DECISION)

    assert result["norm_tags"] == ["honesty", "money", "trust"]
    assert result["norm_violations"] == [
        {
            "norm_id": "expense-honesty",
            "severity": "major",
            "detected_by": "expense_claim_overstated",
            "evidence": {
                "character_id": "mara-chen",
                "amount_incurred": 42,
                "amount_reported": 120,
                "receipt_id": "receipt-0007",
            },
        }
    ]


def test_expense_claim_with_equal_amounts_not_flagged(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(world)
    registry = load_registry(world)

    result = tag_record(registry, EXPENSE_HONEST_DECISION)

    assert result == {"norm_tags": [], "norm_violations": []}


def test_credit_stealing_detected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(world)
    registry = load_registry(world)

    result = tag_record(registry, CREDIT_STEALING_DECISION)

    assert result["norm_tags"] == ["credit", "status", "trust"]
    assert result["norm_violations"] == [
        {
            "norm_id": "credit-sharing",
            "severity": "moderate",
            "detected_by": "public_claim_omits_contributors",
            "evidence": {
                "character_id": "eli-ramos",
                "work_item_id": "contract-alpha-slide-deck",
                "omitted_contributors": ["jordan-vale"],
                "audience": ["manager"],
            },
        }
    ]


def test_credit_sharing_private_note_not_flagged(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(world)
    registry = load_registry(world)

    result = tag_record(registry, CREDIT_SHARING_PRIVATE_NOTE)

    assert result == {"norm_tags": [], "norm_violations": []}


def test_assigned_shift_unattended_detected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(world)
    registry = load_registry(world)

    result = tag_record(
        registry,
        SHIFT_SKIPPED_OBSERVATION,
        state=SHIFT_ASSIGNMENTS_STATE,
        events=[],
    )

    assert result["norm_tags"] == ["duty", "reliability", "operations"]
    assert result["norm_violations"] == [
        {
            "norm_id": "assigned-shift-duty",
            "severity": "moderate",
            "detected_by": "assigned_shift_unattended",
            "evidence": {
                "character_id": "jordan-vale",
                "duty_id": "front-desk-close",
                "day": 9,
                "required_room": "front-desk",
                "observed_room": "break-room",
            },
        }
    ]


def test_assigned_shift_attended_not_flagged(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(world)
    registry = load_registry(world)

    result = tag_record(
        registry,
        SHIFT_SKIPPED_OBSERVATION,
        state=SHIFT_ASSIGNMENTS_STATE,
        events=[SHIFT_ATTENDED_EVENT],
    )

    assert result == {"norm_tags": [], "norm_violations": []}


def test_assigned_shift_attendance_on_another_day_does_not_suppress(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(world)
    registry = load_registry(world)

    result = tag_record(
        registry,
        SHIFT_SKIPPED_OBSERVATION,
        state=SHIFT_ASSIGNMENTS_STATE,
        events=[SHIFT_ATTENDED_PRIOR_DAY_EVENT],
    )

    assert result["norm_tags"] == ["duty", "reliability", "operations"]
    assert [violation["norm_id"] for violation in result["norm_violations"]] == [
        "assigned-shift-duty"
    ]


def test_assigned_shift_without_state_not_flagged(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(world)
    registry = load_registry(world)

    result = tag_record(registry, SHIFT_SKIPPED_OBSERVATION)

    assert result == {"norm_tags": [], "norm_violations": []}


def test_client_confidentiality_leak_detected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(world)
    registry = load_registry(world)

    result = tag_record(registry, CONFIDENTIALITY_LEAK_DECISION)

    assert result["norm_tags"] == ["confidentiality", "client", "trust"]
    assert result["norm_violations"] == [
        {
            "norm_id": "client-confidentiality",
            "severity": "major",
            "detected_by": "secret_shared_with_unauthorized_audience",
            "evidence": {
                "character_id": "nina-patel",
                "secret_id": "client-merger-rumor",
                "secret_classification": "client_confidential",
                "unauthorized_audience": ["open-office"],
            },
        }
    ]


def test_client_confidentiality_authorized_audience_not_flagged(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(world)
    registry = load_registry(world)

    result = tag_record(registry, CONFIDENTIALITY_AUTHORIZED_DECISION)

    assert result == {"norm_tags": [], "norm_violations": []}


def test_shared_space_neglect_detected(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(world)
    registry = load_registry(world)

    result = tag_record(registry, SHARED_SPACE_NEGLECT_INCIDENT)

    assert result["norm_tags"] == ["care", "shared-space", "morale"]
    assert result["norm_violations"] == [
        {
            "norm_id": "shared-space-care",
            "severity": "minor",
            "detected_by": "incident_cleanup_owner_missed",
            "evidence": {
                "incident_id": "coffee-spill",
                "room_id": "break-room",
                "cleanup_owner": "jordan-vale",
                "resolved": False,
            },
        }
    ]


def test_shared_space_resolved_incident_not_flagged(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(world)
    registry = load_registry(world)

    result = tag_record(registry, SHARED_SPACE_RESOLVED_INCIDENT)

    assert result == {"norm_tags": [], "norm_violations": []}


def test_irrelevant_event_carries_no_tags(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(world)
    registry = load_registry(world)

    result = tag_record(registry, IRRELEVANT_SCENE_EVENT)

    assert result == {"norm_tags": [], "norm_violations": []}


def test_integrity_drift_links_violation_to_declared_value(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(world)
    registry = load_registry(world)
    character = {"id": "mara-chen", "declared_values": ["honesty", "punctuality"]}

    result = tag_record(registry, EXPENSE_FRAUD_DECISION)
    drift = integrity_drift(character, registry, result["norm_violations"])

    assert drift == [
        {
            "character_id": "mara-chen",
            "norm_id": "expense-honesty",
            "declared_values": ["honesty"],
        }
    ]


def test_integrity_drift_no_match_when_declared_values_disjoint(tmp_path: Path) -> None:
    world = tmp_path / "tower"
    write_registry(world)
    registry = load_registry(world)
    character = {"id": "mara-chen", "declared_values": ["punctuality"]}

    result = tag_record(registry, EXPENSE_FRAUD_DECISION)
    drift = integrity_drift(character, registry, result["norm_violations"])

    assert drift == []
