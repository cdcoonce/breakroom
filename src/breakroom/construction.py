from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REQUIRED_ROOM_TYPE_FIELDS = ("id", "name", "capacity", "assignable", "slots", "storylet_hooks")

SAME_ROOM_WEIGHT = 1.0
SAME_FLOOR_WEIGHT = 0.4
ADJACENT_FLOOR_WEIGHT = 0.1
NO_PROXIMITY_WEIGHT = 0.0


class ConstructionError(ValueError):
    pass


@dataclass(frozen=True)
class RoomType:
    id: str
    name: str
    capacity: int
    assignable: bool
    slots: int
    storylet_hooks: tuple[str, ...]


@dataclass(frozen=True)
class Floor:
    number: int
    max_slots: int


@dataclass(frozen=True)
class Placement:
    floor_number: int
    slot_id: str
    room_type_id: str
    slots: int


@dataclass(frozen=True)
class Tower:
    floors: dict[int, Floor]
    placements: tuple[Placement, ...] = field(default_factory=tuple)

    def occupied_slots(self, floor_number: int) -> int:
        return sum(
            placement.slots
            for placement in self.placements
            if placement.floor_number == floor_number
        )


def load_room_types(data_dir: Path) -> dict[str, RoomType]:
    room_types: dict[str, RoomType] = {}
    for path in sorted(data_dir.glob("*.toml")):
        raw = tomllib.loads(path.read_text())
        room_type = _parse_room_type(path, raw)
        if room_type.id in room_types:
            raise ConstructionError(f"{path.name}: duplicate room type id {room_type.id}")
        room_types[room_type.id] = room_type
    return room_types


def place_room(tower: Tower, floor_number: int, slot_id: str, room_type: RoomType) -> Tower:
    floor = tower.floors.get(floor_number)
    if floor is None:
        raise ConstructionError(f"floor {floor_number}: not defined")
    for placement in tower.placements:
        if placement.floor_number == floor_number and placement.slot_id == slot_id:
            raise ConstructionError(f"floor {floor_number}: slot {slot_id} already occupied")

    projected = tower.occupied_slots(floor_number) + room_type.slots
    if projected > floor.max_slots:
        raise ConstructionError(
            f"floor {floor_number}: placing {room_type.id} exceeds max_slots "
            f"({projected} > {floor.max_slots})"
        )

    placement = Placement(
        floor_number=floor_number,
        slot_id=slot_id,
        room_type_id=room_type.id,
        slots=room_type.slots,
    )
    return Tower(floors=tower.floors, placements=tower.placements + (placement,))


def encounter_odds(room_a: str, floor_a: int, room_b: str, floor_b: int) -> float:
    if room_a == room_b:
        return SAME_ROOM_WEIGHT
    if floor_a == floor_b:
        return SAME_FLOOR_WEIGHT
    if abs(floor_a - floor_b) == 1:
        return ADJACENT_FLOOR_WEIGHT
    return NO_PROXIMITY_WEIGHT


def _parse_room_type(path: Path, raw: dict[str, Any]) -> RoomType:
    for field_name in REQUIRED_ROOM_TYPE_FIELDS:
        if field_name not in raw:
            raise ConstructionError(f"{path.name}: missing {field_name}")
    hooks = raw["storylet_hooks"]
    if not isinstance(hooks, list) or not hooks:
        raise ConstructionError(f"{path.name}: storylet_hooks must be a non-empty list")
    return RoomType(
        id=raw["id"],
        name=raw["name"],
        capacity=raw["capacity"],
        assignable=raw["assignable"],
        slots=raw["slots"],
        storylet_hooks=tuple(hooks),
    )
