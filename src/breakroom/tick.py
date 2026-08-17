from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from breakroom import jsonio, norms, storylets, worldstate
from breakroom.events import append_event
from breakroom.narrator import render_scene
from breakroom.resolution.incidents import evaluate_tick, load_incident_table
from breakroom.resolution.rng import RollLog

QUIET_DAY_PROSE = "No incident fired today. The tower kept to itself."


class TickError(ValueError):
    pass


def tick_world(world: Path) -> None:
    state_path = world / "state" / "tower.json"
    loaded = worldstate.load_world(world)
    state = loaded.state
    day = state["day"] + 1

    roll_log = RollLog()
    table = load_incident_table(world)
    resolution = evaluate_tick(table, state=state, seed=state["seed"], tick=day, log=roll_log)

    fired_ids = sorted(
        {
            member["incident_id"]
            for cascade in resolution.cascades
            for member in cascade["members"]
        }
    )
    details: dict[str, dict[str, Any]] = {}
    for event in resolution.events:
        if event.get("type") == "incident_detail":
            details.setdefault(event["incident_id"], event)

    # The cleanup owner is the acting character, so the cast has to be resolved before
    # any incident event is emitted. Per-incident casting is storylet work (#75/#76).
    character = loaded.characters[state["characters"][0]]
    registry = _load_registry(world)
    storylet_registry = storylets.load_registry(world)

    incidents: dict[str, dict[str, Any]] = {}
    for incident_id in fired_ids:
        detail = details[incident_id]
        incident = {
            "id": incident_id,
            "name": detail["name"],
            "room": detail["room"],
            "morale_delta": detail["morale_delta"],
            "norm_tags": detail["norm_tags"],
            "needs_cleanup": detail["needs_cleanup"],
        }
        incidents[incident_id] = {
            **incident,
            "cleanup_owner": character["id"] if incident["needs_cleanup"] else None,
            "resolved": False,
        }

    # Drift is a mechanic, not presentation: every fired incident's violations feed it,
    # not just the spotlight one's. Accumulated in sorted incident-id order so the
    # scene event's integrity_drift is stable for a given seed.
    norm_violations: list[dict[str, Any]] = []
    incident_events: list[dict[str, Any]] = []
    for incident_id in fired_ids:
        emitted_incident = incidents[incident_id]
        incident_event: dict[str, Any] = {
            "type": "incident",
            "day": day,
            "incident": emitted_incident,
        }
        if registry is not None:
            tags = norms.tag_record(registry, incident_event)
            norm_violations.extend(tags["norm_violations"])
            incident_event.update(tags)
        incident_events.append(incident_event)
        append_event(world, incident_event)
        state = worldstate.apply_event(state, incident_event)

    world_state = {
        "budget": state["budget"],
        "morale": state["morale"],
        "reputation": state["reputation"],
    }
    brief: dict[str, Any] = {
        "day": day,
        "character": character,
        "room": None,
        "incident": None,
        "storylet": None,
        "state": world_state,
    }
    prose: str | None = None

    # `base_rate` is a bernoulli probability, so a day where nothing fires is a legal
    # outcome, not an error. It gets no spotlight draw and no scene event: there is
    # nothing to narrate, and the chronicle renderer (#17) is specified to accept a
    # missing scene and emit a digest-only episode.
    if fired_ids:
        context = storylets.EngineContext(
            tick=day,
            state=state,
            characters=loaded.characters,
            incident_events=incident_events,
        )
        selection = storylets.select_storylet(
            storylet_registry, context=context, seed=state["seed"], log=roll_log
        )
        if selection is None:
            # An empty spotlight is a legitimate outcome: no eligible storylet does not
            # mean no incidents fired, but the day still needs its receipts recorded.
            append_event(world, {"type": "quiet_day", "day": day, "rolls": roll_log.records})
        else:
            character_ids: list[str] = []
            for slot in selection.storylet.participants:
                for participant_id in selection.participants.get(slot.slot, []):
                    if participant_id not in character_ids:
                        character_ids.append(participant_id)

            try:
                spotlight_character_id = next(
                    ids[0]
                    for slot in selection.storylet.participants
                    if (ids := selection.participants.get(slot.slot))
                )
            except StopIteration:
                raise TickError(
                    f"storylet {selection.storylet.id!r} was selected but drew no "
                    "participants in any slot"
                ) from None
            spotlight_character = loaded.characters[spotlight_character_id]

            spotlight_incident_id = selection.storylet.eligibility.incident_ids[0]
            spotlight_incident = incidents[spotlight_incident_id]

            try:
                room = next(
                    room for room in state["rooms"] if room["id"] == spotlight_incident["room"]
                )
            except StopIteration:
                raise TickError(
                    f"incident {spotlight_incident['id']!r} references room "
                    f"{spotlight_incident['room']!r}, which is missing from tower state"
                ) from None
            brief |= {
                "character": spotlight_character,
                "room": room,
                "incident": spotlight_incident,
                "storylet": {
                    "id": selection.storylet.id,
                    "title": selection.storylet.title,
                    "premise": selection.storylet.premise,
                },
            }
            prose = render_scene(brief)
            scene_event: dict[str, Any] = {
                "type": "scene",
                "day": day,
                "character_id": spotlight_character_id,
                "storylet_id": selection.storylet.id,
                "character_ids": character_ids,
                "brief": brief,
                "rolls": roll_log.records,
                "prose": prose,
            }
            if registry is not None:
                scene_event["integrity_drift"] = norms.integrity_drift(
                    spotlight_character, registry, norm_violations
                )
            append_event(world, scene_event)
            state = worldstate.apply_event(state, scene_event)
    else:
        # The scene event is the only carrier of the roll log on an ordinary day, so a
        # quiet day needs its own record: otherwise the tick appends nothing at all and
        # the day is missing from the event chronology along with its receipts.
        append_event(world, {"type": "quiet_day", "day": day, "rolls": roll_log.records})

    if not fired_ids:
        # No incident fired, so the `fired_ids` loop above never ran `apply_event` to
        # advance `state["day"]`. This is the only case where that's still needed.
        state["day"] = day
    jsonio.write_pretty_json(state_path, state)
    write_chronicle(world, day=day, brief=brief, prose=prose)


def _load_registry(world: Path) -> norms.Registry | None:
    """The registry is optional: a world initialized before norms existed still ticks.

    Mirrors the graceful skip in `secrets._tag_with_norms`. Scaffolding the file is
    `init_world`'s job, never the tick loop's.
    """
    if not (world / "data" / "norms.toml").exists():
        return None
    return norms.load_registry(world)


def write_chronicle(world: Path, day: int, brief: dict[str, Any], prose: str | None) -> None:
    """Write the day's episode. `prose` is None on a quiet day (no spotlight scene).

    The episode is written either way: every workday ends in a chronicle, so a quiet
    day has to leave a file behind rather than look like a tick that never ran.
    """
    chronicle = world / "chronicles" / f"day-{day:04d}.md"
    chronicle.write_text(
        f"# Day {day:04d}\n\n{QUIET_DAY_PROSE if prose is None else prose}\n\n"
        "## Trace\n\n"
        f"brief:\n```json\n{json.dumps(brief, indent=2, sort_keys=True)}\n```\n",
        encoding="utf-8",
    )
