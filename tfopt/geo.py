from __future__ import annotations

import math
from typing import Sequence

from models import Coordinate, RouteStop


def haversine_km(a: Coordinate, b: Coordinate) -> float:
    """Return straight-line geographic distance between two lat/lon points."""
    lat1, lon1 = a
    lat2, lon2 = b
    radius_km = 6371.0

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    term = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius_km * math.atan2(math.sqrt(term), math.sqrt(1 - term))


def road_cost_meters(a: Coordinate, b: Coordinate, road_factor: float) -> int:
    """Approximate road distance from haversine distance for the solver matrix."""
    return max(1, int(round(haversine_km(a, b) * road_factor * 1000)))


def route_distance_for_stops(
    stops: Sequence[RouteStop],
    start_location: Coordinate,
    road_factor: float,
) -> float:
    """Compute open-route distance for an ordered stop sequence."""
    route_distance = 0.0
    current_location = start_location
    for stop in stops:
        route_distance += haversine_km(current_location, (stop.lat, stop.lon)) * road_factor
        current_location = (stop.lat, stop.lon)
    return route_distance
