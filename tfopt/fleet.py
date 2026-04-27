from __future__ import annotations

from typing import List, Sequence, Tuple

from models import Coordinate, Vehicle


VEHICLE_SPECS_DEFAULT: List[Tuple[int, int, int]] = [
    (2400, 1, 15),
    (2200, 1, 15),
    (1300, 2, 10),
    (2500, 3, 18),
    (2700, 4, 20),
    (3200, 7, 25),
    (3000, 7, 25),
]


def build_vehicles(
    depot: Coordinate,
    vehicle_specs: Sequence[Tuple[int, int, int]],
) -> List[Vehicle]:
    """Expand fleet specs into concrete Vehicle objects with unique ids."""
    vehicles: List[Vehicle] = []
    next_id = 1
    for capacity, count, max_stops in vehicle_specs:
        for _ in range(count):
            vehicles.append(
                Vehicle(
                    vehicle_id=next_id,
                    capacity=capacity,
                    max_stops=max_stops,
                    depot=depot,
                )
            )
            next_id += 1
    return vehicles


def parse_vehicle_specs(raw_specs: Sequence[str]) -> List[Tuple[int, int, int]]:
    """Parse CLI vehicle overrides in capacity:count:max_stops format."""
    parsed: List[Tuple[int, int, int]] = []
    for raw in raw_specs:
        try:
            capacity, count, max_stops = raw.split(":")
            parsed.append((int(capacity), int(count), int(max_stops)))
        except ValueError as exc:
            raise ValueError(
                f"Invalid vehicle spec '{raw}'. Expected format capacity:count:max_stops."
            ) from exc
    return parsed
