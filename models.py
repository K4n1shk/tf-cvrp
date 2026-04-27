from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


Coordinate = Tuple[float, float]


@dataclass(frozen=True)
class Job:
    """One delivery demand from the input data, kept immutable across planning steps."""

    job_id: str
    name: str
    lat: float
    lon: float
    weight: float
    area_id: str
    quantity: int = 1
    priority: Optional[int] = None
    delivery_preference: Optional[int] = None
    job_type: str = ""
    has_area_id: bool = True

    @property
    def location(self) -> Coordinate:
        return (self.lat, self.lon)

    @property
    def weight_int(self) -> int:
        return int(math.ceil(self.weight))


@dataclass
class Vehicle:
    """A mutable working vehicle record used while building candidate assignments."""

    vehicle_id: int
    capacity: int
    max_stops: int
    depot: Coordinate
    assigned_jobs: List[Job] = field(default_factory=list)
    remaining_capacity: float = field(init=False)
    remaining_stops: int = field(init=False)
    current_location: Coordinate = field(init=False)
    primary_areas: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.remaining_capacity = float(self.capacity)
        self.remaining_stops = self.max_stops
        self.current_location = self.depot

    def can_take(self, job: Job) -> bool:
        return self.remaining_capacity >= job.weight and self.remaining_stops > 0

    def assign(self, job: Job) -> None:
        self.assigned_jobs.append(job)
        self.remaining_capacity -= job.weight
        self.remaining_stops -= 1
        self.current_location = job.location


@dataclass(frozen=True)
class AreaSummary:
    """Precomputed rollup for an area so scoring logic does not recompute group stats."""

    area_id: str
    centroid: Coordinate
    total_weight: float
    total_jobs: int


@dataclass(frozen=True)
class RouteStop:
    """One stop in a solved route, shaped for CSV export and rendering."""

    sequence: int
    job_id: str
    name: str
    area_id: str
    lat: float
    lon: float
    weight: float


@dataclass(frozen=True)
class VehicleRoute:
    """Final route output for one vehicle after the solver chooses stop order."""

    vehicle_id: int
    distance_km: float
    load_kg: float
    stop_count: int
    stops: List[RouteStop]


@dataclass(frozen=True)
class RoutingVehicleState:
    """Capture the remaining routing resources for one solver pass."""

    vehicle_id: int
    capacity: int
    max_stops: int
    start_location: Coordinate
