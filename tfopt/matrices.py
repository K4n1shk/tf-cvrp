from __future__ import annotations

from typing import List, Sequence

from models import Coordinate, Job
from tfopt.geo import road_cost_meters


def build_distance_matrix(
    depot: Coordinate,
    jobs: Sequence[Job],
    road_factor: float,
    end_count: int = 0,
) -> List[List[int]]:
    """Build the depot+jobs pairwise travel-cost matrix used by OR-Tools."""
    points = [depot] + [job.location for job in jobs]
    base_count = len(points)
    total_count = base_count + end_count
    matrix: List[List[int]] = [[0 for _ in range(total_count)] for _ in range(total_count)]

    for from_idx in range(base_count):
        for to_idx in range(base_count):
            matrix[from_idx][to_idx] = road_cost_meters(points[from_idx], points[to_idx], road_factor)

    # Dummy end nodes allow vehicles to finish anywhere without paying a depot return cost.
    for from_idx in range(base_count):
        for end_idx in range(base_count, total_count):
            matrix[from_idx][end_idx] = 0

    return matrix


def build_multi_start_distance_matrix(
    start_locations: Sequence[Coordinate],
    jobs: Sequence[Job],
    road_factor: float,
    end_count: int = 0,
) -> List[List[int]]:
    """Build a distance matrix for open routes with one start node per vehicle."""
    points = list(start_locations) + [job.location for job in jobs]
    base_count = len(points)
    total_count = base_count + end_count
    matrix: List[List[int]] = [[0 for _ in range(total_count)] for _ in range(total_count)]

    for from_idx in range(base_count):
        for to_idx in range(base_count):
            matrix[from_idx][to_idx] = road_cost_meters(points[from_idx], points[to_idx], road_factor)

    # Dummy end nodes keep routes open so a vehicle can finish at its last job.
    for from_idx in range(base_count):
        for end_idx in range(base_count, total_count):
            matrix[from_idx][end_idx] = 0

    return matrix
