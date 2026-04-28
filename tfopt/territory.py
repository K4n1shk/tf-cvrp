from __future__ import annotations

from dataclasses import replace
import math
from typing import Dict, Iterable, List, Sequence

from models import AreaSummary, Coordinate, Job, Vehicle
from tfopt.geo import haversine_km


def redistribute_area_ids(jobs: Sequence[Job], area_ids: Sequence[str]) -> List[Job]:
    """
    Reassign problematic area ids to the nearest existing non-problematic area.

    This keeps source CSVs unchanged while preventing tiny or overlapping area codes
    from becoming their own territory in the optimization model.
    """
    area_ids_to_replace = {str(area_id).strip() for area_id in area_ids if str(area_id).strip()}
    if not area_ids_to_replace:
        return list(jobs)

    donor_jobs = [
        job
        for job in jobs
        if job.area_id and job.area_id not in area_ids_to_replace
    ]
    if not donor_jobs:
        return list(jobs)

    normalized_jobs: List[Job] = []
    for job in jobs:
        if job.area_id not in area_ids_to_replace:
            normalized_jobs.append(job)
            continue

        nearest_job = min(
            donor_jobs,
            key=lambda donor: haversine_km(job.location, donor.location),
        )
        normalized_jobs.append(
            replace(
                job,
                area_id=nearest_job.area_id,
                has_area_id=True,
            )
        )

    return normalized_jobs


def assign_missing_area_ids(jobs: Sequence[Job]) -> List[Job]:
    """
    Replace blank area ids with the nearest existing area's centroid.
    """
    area_points: Dict[str, List[Job]] = {}
    missing_jobs: List[Job] = []

    for job in jobs:
        if job.area_id:
            area_points.setdefault(job.area_id, []).append(job)
        else:
            missing_jobs.append(job)

    if not missing_jobs or not area_points:
        return list(jobs)

    area_centroids: Dict[str, Coordinate] = {}
    for area_id, area_jobs in area_points.items():
        area_centroids[area_id] = (
            sum(job.lat for job in area_jobs) / len(area_jobs),
            sum(job.lon for job in area_jobs) / len(area_jobs),
        )

    normalized_jobs: List[Job] = []
    for job in jobs:
        if job.area_id:
            normalized_jobs.append(job)
            continue

        nearest_area_id = min(
            area_centroids,
            key=lambda area_id: haversine_km(job.location, area_centroids[area_id]),
        )
        normalized_jobs.append(
            Job(
                job_id=job.job_id,
                name=job.name,
                lat=job.lat,
                lon=job.lon,
                weight=job.weight,
                area_id=nearest_area_id,
                quantity=job.quantity,
                priority=job.priority,
                delivery_preference=job.delivery_preference,
                job_type=job.job_type,
                has_area_id=True,
            )
        )

    return normalized_jobs


def summarize_areas(jobs: Iterable[Job]) -> Dict[str, AreaSummary]:
    """Group jobs by area and compute centroid/weight/count summaries."""
    grouped: Dict[str, List[Job]] = {}
    for job in jobs:
        grouped.setdefault(job.area_id, []).append(job)

    summaries: Dict[str, AreaSummary] = {}
    for area_id, area_jobs in grouped.items():
        summaries[area_id] = AreaSummary(
            area_id=area_id,
            centroid=(
                sum(job.lat for job in area_jobs) / len(area_jobs),
                sum(job.lon for job in area_jobs) / len(area_jobs),
            ),
            total_weight=sum(job.weight for job in area_jobs),
            total_jobs=len(area_jobs),
        )
    return summaries


def assign_primary_areas(
    vehicles: Sequence[Vehicle],
    area_summaries: Dict[str, AreaSummary],
    depot: Coordinate,
) -> Dict[str, int]:
    """Assign each area a preferred vehicle owner without reserving capacity."""
    area_to_vehicle: Dict[str, int] = {}
    ownership_count: Dict[int, int] = {vehicle.vehicle_id: 0 for vehicle in vehicles}

    for area in sorted(
        area_summaries.values(),
        key=lambda item: (item.total_weight, item.total_jobs),
        reverse=True,
    ):
        ranked = sorted(
            vehicles,
            key=lambda vehicle: (
                ownership_count[vehicle.vehicle_id],
                haversine_km(depot, area.centroid),
                -vehicle.capacity,
            ),
        )
        chosen = ranked[0]
        area_to_vehicle[area.area_id] = chosen.vehicle_id
        chosen.primary_areas.append(area.area_id)
        ownership_count[chosen.vehicle_id] += 1

    return area_to_vehicle


def flexible_areas_for_job(
    job: Job,
    area_summaries: Dict[str, AreaSummary],
    threshold_km: float,
) -> List[str]:
    """Return nearby areas that are close enough to allow spillover for a job."""
    nearby: List[str] = []
    for area_id, summary in area_summaries.items():
        if area_id == job.area_id:
            continue
        if haversine_km(job.location, summary.centroid) <= threshold_km:
            nearby.append(area_id)
    return nearby


def insertion_detour_km(
    vehicle: Vehicle,
    job: Job,
    depot: Coordinate,
) -> float:
    """Estimate extra distance if this vehicle serves the job next on an open route."""
    current = vehicle.current_location
    return haversine_km(current, job.location)


def score_vehicle_for_job(
    vehicle: Vehicle,
    job: Job,
    area_summaries: Dict[str, AreaSummary],
    primary_vehicle_id: int,
    nearby_vehicle_ids: Sequence[int],
    spillover_penalty: float,
    non_flexible_penalty: float,
    consistency_bonus: float,
    depot: Coordinate,
) -> float:
    """Score how suitable a vehicle is for a job during candidate generation."""
    if not vehicle.can_take(job):
        return float("inf")

    area_centroid = area_summaries[job.area_id].centroid
    insertion_detour = insertion_detour_km(vehicle, job, depot)
    centroid_detour = haversine_km(job.location, area_centroid)
    capacity_pressure = 10.0 * (job.weight / max(vehicle.remaining_capacity, 1.0))
    stop_pressure = 4.0 * (1.0 / max(vehicle.remaining_stops, 1))
    # Mix spatial efficiency with resource pressure so we do not greedily overpack a vehicle.
    score = insertion_detour + 0.35 * centroid_detour + capacity_pressure + stop_pressure

    if vehicle.vehicle_id == primary_vehicle_id:
        score -= consistency_bonus
    elif vehicle.vehicle_id in nearby_vehicle_ids:
        score += spillover_penalty
    else:
        score += non_flexible_penalty

    return score


def build_candidate_map(
    jobs: Sequence[Job],
    vehicles: Sequence[Vehicle],
    area_summaries: Dict[str, AreaSummary],
    area_to_vehicle: Dict[str, int],
    depot: Coordinate,
    threshold_km: float,
    spillover_penalty: float,
    non_flexible_penalty: float,
    consistency_bonus: float,
    max_candidates: int,
) -> Dict[str, List[int]]:
    """Rank the full fleet for each job so the solver can use soft territory preferences."""
    del max_candidates
    candidate_map: Dict[str, List[int]] = {}

    ordered_jobs = sorted(
        jobs,
        key=lambda job: (
            -job.weight,
            area_summaries[job.area_id].total_weight,
        ),
        reverse=True,
    )

    for job in ordered_jobs:
        primary_vehicle_id = area_to_vehicle[job.area_id]
        nearby_areas = flexible_areas_for_job(job, area_summaries, threshold_km)
        nearby_vehicle_ids = {
            area_to_vehicle[area_id]
            for area_id in nearby_areas
            if area_id in area_to_vehicle
        }

        for area_id in nearby_areas:
            nearby_vehicle_ids.add(area_to_vehicle[area_id])

        scored: List[Tuple[float, int]] = []
        for vehicle in vehicles:
            score = score_vehicle_for_job(
                vehicle=vehicle,
                job=job,
                area_summaries=area_summaries,
                primary_vehicle_id=primary_vehicle_id,
                nearby_vehicle_ids=sorted(nearby_vehicle_ids),
                spillover_penalty=spillover_penalty,
                non_flexible_penalty=non_flexible_penalty,
                consistency_bonus=consistency_bonus,
                depot=depot,
            )
            if math.isfinite(score):
                scored.append((score, vehicle.vehicle_id))

        scored.sort(key=lambda item: item[0])
        candidate_map[job.job_id] = [vehicle_id for _, vehicle_id in scored]

    return candidate_map


def build_job_territory_map(
    jobs: Sequence[Job],
    vehicles: Sequence[Vehicle],
    area_summaries: Dict[str, AreaSummary],
    area_to_vehicle: Dict[str, int],
    threshold_km: float,
) -> Dict[str, Dict[str, object]]:
    """Classify every vehicle for every job as primary, nearby, or unrelated."""
    all_vehicle_ids = {vehicle.vehicle_id for vehicle in vehicles}
    territory_map: Dict[str, Dict[str, object]] = {}

    for job in jobs:
        primary_vehicle_id = area_to_vehicle[job.area_id]
        nearby_areas = flexible_areas_for_job(job, area_summaries, threshold_km)
        nearby_vehicle_ids = {
            area_to_vehicle[area_id]
            for area_id in nearby_areas
            if area_id in area_to_vehicle and area_to_vehicle[area_id] != primary_vehicle_id
        }
        unrelated_vehicle_ids = sorted(
            all_vehicle_ids.difference(nearby_vehicle_ids).difference({primary_vehicle_id})
        )
        territory_map[job.job_id] = {
            "primary_vehicle_id": primary_vehicle_id,
            "nearby_vehicle_ids": sorted(nearby_vehicle_ids),
            "unrelated_vehicle_ids": unrelated_vehicle_ids,
        }

    return territory_map


def build_vehicle_penalty_map(
    territory_map: Dict[str, Dict[str, object]],
    spillover_penalty: float,
    non_flexible_penalty: float,
) -> Dict[str, Dict[int, int]]:
    """Convert territory classes into per-job soft penalties used by the solver objective."""
    vehicle_penalty_map: Dict[str, Dict[int, int]] = {}

    for job_id, job_territory in territory_map.items():
        primary_vehicle_id = int(job_territory["primary_vehicle_id"])
        nearby_vehicle_ids = [int(vehicle_id) for vehicle_id in job_territory["nearby_vehicle_ids"]]
        unrelated_vehicle_ids = [int(vehicle_id) for vehicle_id in job_territory["unrelated_vehicle_ids"]]
        penalties_for_job: Dict[int, int] = {}
        penalties_for_job[primary_vehicle_id] = 0
        for vehicle_id in nearby_vehicle_ids:
            penalties_for_job[vehicle_id] = int(round(spillover_penalty * 1000))
        for vehicle_id in unrelated_vehicle_ids:
            penalties_for_job[vehicle_id] = int(round(non_flexible_penalty * 1000))
        vehicle_penalty_map[job_id] = penalties_for_job

    return vehicle_penalty_map
