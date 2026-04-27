from __future__ import annotations

from typing import Dict, Sequence

from models import Job, VehicleRoute
from tfopt.scoring import drop_penalty


def build_vehicle_diagnostics(
    routes: Sequence[VehicleRoute],
    vehicle_limits: Dict[int, Dict[str, float]],
) -> List[Dict[str, object]]:
    """Summarize how each vehicle used weight and stop budgets."""
    diagnostics: List[Dict[str, object]] = []

    for route in routes:
        limits = vehicle_limits.get(route.vehicle_id, {})
        capacity_kg = float(limits.get("capacity_kg", 0))
        max_stops = int(limits.get("max_stops", 0))
        used_capacity_kg = round(route.load_kg, 2)
        remaining_capacity_kg = round(max(0.0, capacity_kg - route.load_kg), 2)
        remaining_stops = max(0, max_stops - route.stop_count)
        capacity_utilization_pct = round((route.load_kg / capacity_kg) * 100, 1) if capacity_kg else 0.0
        stop_utilization_pct = round((route.stop_count / max_stops) * 100, 1) if max_stops else 0.0

        if route.stop_count == 0:
            utilization_status = "unused"
        elif stop_utilization_pct >= 95 and capacity_utilization_pct < 80:
            utilization_status = "stop_bound"
        elif capacity_utilization_pct >= 95 and stop_utilization_pct < 80:
            utilization_status = "weight_bound"
        elif capacity_utilization_pct < 85 and stop_utilization_pct < 85:
            utilization_status = "underutilized"
        else:
            utilization_status = "balanced"

        diagnostics.append(
            {
                "vehicle_id": route.vehicle_id,
                "capacity_kg": capacity_kg,
                "used_capacity_kg": used_capacity_kg,
                "remaining_capacity_kg": remaining_capacity_kg,
                "capacity_utilization_pct": capacity_utilization_pct,
                "max_stops": max_stops,
                "used_stops": route.stop_count,
                "remaining_stops": remaining_stops,
                "stop_utilization_pct": stop_utilization_pct,
                "distance_km": route.distance_km,
                "utilization_status": utilization_status,
            }
        )

    return diagnostics


def summarize_vehicle_diagnostics(
    vehicle_diagnostics: Sequence[Dict[str, object]],
) -> Dict[str, int]:
    """Count how many vehicles fall into each utilization bucket."""
    summary = {
        "unused": 0,
        "stop_bound": 0,
        "weight_bound": 0,
        "underutilized": 0,
        "balanced": 0,
    }

    for item in vehicle_diagnostics:
        status = str(item["utilization_status"])
        summary[status] = summary.get(status, 0) + 1

    return summary


def route_summary(
    routes: Sequence[VehicleRoute],
    unassigned: Sequence[Job],
    vehicle_limits: Dict[int, Dict[str, float]],
) -> Dict[str, object]:
    """Build the structured summary exported to optimized_routes.json."""
    vehicle_diagnostics = build_vehicle_diagnostics(routes, vehicle_limits)
    vehicle_diagnostic_summary = summarize_vehicle_diagnostics(vehicle_diagnostics)
    return {
        "vehicle_count_used": sum(1 for route in routes if route.stop_count > 0),
        "total_stops": sum(route.stop_count for route in routes),
        "total_distance_km": round(sum(route.distance_km for route in routes), 2),
        "unassigned_job_count": len(unassigned),
        "unassigned_total_weight_kg": round(sum(job.weight for job in unassigned), 2),
        "vehicle_diagnostics": vehicle_diagnostics,
        "vehicle_diagnostic_summary": vehicle_diagnostic_summary,
        "unassigned_jobs": [
            {
                "job_id": job.job_id,
                "name": job.name,
                "area_id": job.area_id,
                "weight": job.weight,
                "priority": job.priority,
                "delivery_preference": job.delivery_preference,
                "quantity": job.quantity,
                "job_type": job.job_type,
                "drop_penalty": drop_penalty(job),
            }
            for job in unassigned
        ],
        "routes": [
            {
                "vehicle_id": route.vehicle_id,
                "distance_km": route.distance_km,
                "load_kg": route.load_kg,
                "stop_count": route.stop_count,
                "stops": [stop.__dict__ for stop in route.stops],
            }
            for route in routes
        ],
    }
