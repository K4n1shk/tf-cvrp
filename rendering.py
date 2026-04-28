#!/usr/bin/env python3
"""Rendering and export helpers for the routing pipeline."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Sequence, Tuple

if TYPE_CHECKING:
    from models import AreaSummary, Coordinate, Job, VehicleRoute


def color_for_index(index: int) -> str:
    """Pick a stable high-contrast categorical color for maps and legends."""
    palette = [
        "#e6194b",
        "#3cb44b",
        "#4363d8",
        "#f58231",
        "#911eb4",
        "#46f0f0",
        "#f032e6",
        "#bcf60c",
        "#fabebe",
        "#008080",
        "#e6beff",
        "#9a6324",
        "#fffac8",
        "#800000",
        "#aaffc3",
        "#000075",
        "#808000",
        "#ffd8b1",
        "#000000",
        "#42d4f4",
        "#bfef45",
        "#469990",
        "#dcbeff",
        "#ffe119",
        "#a9a9a9",
        "#ff7f00",
        "#6a3d9a",
        "#b15928",
        "#17becf",
        "#ff4d6d",
    ]
    return palette[index % len(palette)]


def write_routes_csv(routes: Sequence["VehicleRoute"], output_path: Path) -> None:
    """Write routed stops in flat CSV form for spreadsheet-style review."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["vehicle_id", "sequence", "job_id", "name", "area_id", "latitude", "longitude", "weight"]
        )
        for route in routes:
            for stop in route.stops:
                writer.writerow(
                    [
                        route.vehicle_id,
                        stop.sequence,
                        stop.job_id,
                        stop.name,
                        stop.area_id,
                        stop.lat,
                        stop.lon,
                        stop.weight,
                    ]
                )


def write_unassigned_csv(
    unassigned: Sequence["Job"],
    output_path: Path,
    drop_penalty_fn: Callable[["Job"], int],
) -> None:
    """Write dropped jobs and their business fields for overflow analysis."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "job_id",
                "name",
                "area_id",
                "latitude",
                "longitude",
                "weight",
                "quantity",
                "priority",
                "delivery_preference",
                "job_type",
                "drop_penalty",
            ]
        )
        for job in unassigned:
            writer.writerow(
                [
                    job.job_id,
                    job.name,
                    job.area_id,
                    job.lat,
                    job.lon,
                    job.weight,
                    job.quantity,
                    job.priority,
                    job.delivery_preference,
                    job.job_type,
                    drop_penalty_fn(job),
                ]
            )


def display_area_id(area_id: str) -> str:
    """Return a user-facing label for an area id."""
    return area_id if area_id else "NO_AREA"


def build_area_color_map(area_summaries: Dict[str, "AreaSummary"]) -> Dict[str, str]:
    """Assign a consistent color to every area shown in the maps."""
    ordered_area_ids = sorted(area_summaries.keys(), key=lambda area_id: (area_id == "", display_area_id(area_id)))
    return {area_id: color_for_index(index) for index, area_id in enumerate(ordered_area_ids)}


def build_area_overlay_data(
    jobs: Sequence["Job"],
    area_summaries: Dict[str, "AreaSummary"],
    area_color_map: Dict[str, str],
) -> List[Dict[str, object]]:
    """Prepare polygon and summary data used by the Leaflet area overlays."""
    jobs_by_area: Dict[str, List["Job"]] = {}
    for job in jobs:
        jobs_by_area.setdefault(job.area_id, []).append(job)

    overlays: List[Dict[str, object]] = []
    for area_id, summary in area_summaries.items():
        area_jobs = jobs_by_area.get(area_id, [])
        if not area_jobs:
            continue
        polygon_points = build_area_polygon_points(area_jobs)
        overlays.append(
            {
                "area_id": area_id,
                "label": display_area_id(area_id),
                "color": area_color_map[area_id],
                "centroid": {"lat": summary.centroid[0], "lon": summary.centroid[1]},
                "polygon": polygon_points,
                "job_count": summary.total_jobs,
                "total_weight": round(summary.total_weight, 2),
            }
        )

    return overlays


def cross_product(o: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Return the signed cross product used by convex hull construction."""
    return (a[1] - o[1]) * (b[0] - o[0]) - (a[0] - o[0]) * (b[1] - o[1])


def convex_hull(points: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Compute a convex hull around a set of lat/lon points."""
    unique_points = sorted(set(points))
    if len(unique_points) <= 1:
        return list(unique_points)

    lower: List[Tuple[float, float]] = []
    for point in unique_points:
        while len(lower) >= 2 and cross_product(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)

    upper: List[Tuple[float, float]] = []
    for point in reversed(unique_points):
        while len(upper) >= 2 and cross_product(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)

    return lower[:-1] + upper[:-1]


def build_area_polygon_points(area_jobs: Sequence["Job"]) -> List[Dict[str, float]]:
    """Build polygon-ready area boundaries from the outermost stop points."""
    points = [(job.lat, job.lon) for job in area_jobs]
    hull = convex_hull(points)

    if len(hull) == 1:
        lat, lon = hull[0]
        delta = 0.001
        hull = [
            (lat - delta, lon - delta),
            (lat - delta, lon + delta),
            (lat + delta, lon + delta),
            (lat + delta, lon - delta),
        ]
    elif len(hull) == 2:
        (lat1, lon1), (lat2, lon2) = hull
        lat_delta = max(abs(lat2 - lat1) * 0.1, 0.001)
        lon_delta = max(abs(lon2 - lon1) * 0.1, 0.001)
        hull = [
            (lat1 - lat_delta, lon1 - lon_delta),
            (lat1 + lat_delta, lon1 + lon_delta),
            (lat2 + lat_delta, lon2 + lon_delta),
            (lat2 - lat_delta, lon2 - lon_delta),
        ]

    return [{"lat": lat, "lon": lon} for lat, lon in hull]


def render_routes_png(
    routes: Sequence["VehicleRoute"],
    depot: "Coordinate",
    unassigned: Sequence["Job"],
    output_path: Path,
) -> None:
    """Render a static PNG overview of the routed and dropped work."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(output_path.parent / ".mplconfig"))
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 10))
    ax.scatter(depot[1], depot[0], c="black", marker="*", s=180, label="Depot")

    for index, route in enumerate(routes):
        if not route.stops:
            continue
        color = color_for_index(index)
        lons = [depot[1]] + [stop.lon for stop in route.stops]
        lats = [depot[0]] + [stop.lat for stop in route.stops]
        ax.plot(lons, lats, color=color, linewidth=1.5, alpha=0.9, label=f"Vehicle {route.vehicle_id}")
        ax.scatter(lons[1:], lats[1:], color=color, s=18)

    for job in unassigned:
        ax.scatter(job.lon, job.lat, color="#dc2626", marker="x", s=45)
        ax.annotate(
            f"{job.name[:18]} ({job.weight:.0f}kg)",
            (job.lon, job.lat),
            textcoords="offset points",
            xytext=(4, 4),
            fontsize=6,
            color="#991b1b",
        )

    ax.set_title("Optimized Vehicle Routes")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.legend(loc="best", fontsize=8, ncol=2)
    ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.4)
    fig.tight_layout()
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def render_leaflet_routes_html(
    jobs: Sequence["Job"],
    routes: Sequence["VehicleRoute"],
    depot: "Coordinate",
    unassigned: Sequence["Job"],
    area_summaries: Dict[str, "AreaSummary"],
    output_path: Path,
    drop_penalty_fn: Callable[["Job"], int],
    vehicle_limits: Dict[int, Dict[str, float]] | None = None,
    vehicle_diagnostics: Sequence[Dict[str, object]] | None = None,
    vehicle_diagnostic_summary: Dict[str, int] | None = None,
    underutilized_debug_report: Dict[int, List[Dict[str, object]]] | None = None,
) -> None:
    """Render the main interactive map with routes, polygons, and route inspector."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    area_color_map = build_area_color_map(area_summaries)
    area_overlays = build_area_overlay_data(jobs, area_summaries, area_color_map)
    total_job_weight = round(sum(job.weight for job in jobs), 2)
    assigned_job_count = sum(route.stop_count for route in routes)
    assigned_weight = round(sum(route.load_kg for route in routes), 2)
    dropped_job_count = len(unassigned)
    dropped_weight = round(sum(job.weight for job in unassigned), 2)
    used_routes = [route for route in routes if route.stop_count > 0]
    progress_summary = {
        "total_jobs": len(jobs),
        "assigned_jobs": assigned_job_count,
        "dropped_jobs": dropped_job_count,
        "assigned_weight": assigned_weight,
        "dropped_weight": dropped_weight,
        "total_weight": total_job_weight,
        "vehicles_used": len(used_routes),
        "vehicle_total": len(routes),
        "completion_stops_pct": round((assigned_job_count / len(jobs)) * 100, 1) if jobs else 0.0,
        "completion_weight_pct": round((assigned_weight / total_job_weight) * 100, 1) if total_job_weight else 0.0,
        "total_distance_km": round(sum(route.distance_km for route in routes), 2),
    }
    vehicle_diagnostics_payload = list(vehicle_diagnostics or [])
    vehicle_diagnostic_summary_payload = vehicle_diagnostic_summary or {}
    underutilized_debug_payload = {
        str(vehicle_id): examples
        for vehicle_id, examples in (underutilized_debug_report or {}).items()
    }

    route_payload = []
    for index, route in enumerate(routes):
        if not route.stops:
            continue
        route_limits = vehicle_limits.get(route.vehicle_id, {}) if vehicle_limits else {}
        capacity_kg = route_limits.get("capacity_kg")
        max_stops = route_limits.get("max_stops")
        unused_capacity_kg = (
            round(max(0.0, float(capacity_kg) - route.load_kg), 2)
            if capacity_kg is not None
            else None
        )
        capacity_fill_pct = (
            round((route.load_kg / float(capacity_kg)) * 100, 1)
            if capacity_kg not in (None, 0)
            else None
        )
        stops_fill_pct = (
            round((route.stop_count / float(max_stops)) * 100, 1)
            if max_stops not in (None, 0)
            else None
        )
        area_breakdown: Dict[str, int] = {}
        for stop in route.stops:
            label = display_area_id(stop.area_id)
            area_breakdown[label] = area_breakdown.get(label, 0) + 1
        route_payload.append(
            {
                "vehicle_id": route.vehicle_id,
                "color": color_for_index(index),
                "distance_km": route.distance_km,
                "load_kg": route.load_kg,
                "stop_count": route.stop_count,
                "capacity_kg": capacity_kg,
                "max_stops": max_stops,
                "unused_capacity_kg": unused_capacity_kg,
                "capacity_fill_pct": capacity_fill_pct,
                "stops_fill_pct": stops_fill_pct,
                "area_breakdown": area_breakdown,
                "points": (
                    [{"lat": depot[0], "lon": depot[1], "label": "Depot"}]
                    + [
                        {
                            "lat": stop.lat,
                            "lon": stop.lon,
                            "job_id": stop.job_id,
                            "name": stop.name,
                            "area_id": stop.area_id,
                            "area_label": display_area_id(stop.area_id),
                            "area_color": area_color_map.get(stop.area_id, "#6b7280"),
                            "sequence": stop.sequence,
                            "weight": stop.weight,
                        }
                        for stop in route.stops
                    ]
                ),
            }
        )

    unassigned_payload = [
        {
            "job_id": job.job_id,
            "name": job.name,
            "lat": job.lat,
            "lon": job.lon,
            "area_id": job.area_id,
            "area_label": display_area_id(job.area_id),
            "area_color": area_color_map.get(job.area_id, "#dc2626"),
            "weight": job.weight,
            "priority": job.priority,
            "drop_penalty": drop_penalty_fn(job),
        }
        for job in unassigned
    ]

    area_legend = [
        {"area_label": display_area_id(area_id), "color": color}
        for area_id, color in sorted(area_color_map.items(), key=lambda item: display_area_id(item[0]))
    ]

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Routes Map</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <style>
    * {{ box-sizing: border-box; }}
    body {{ --sidebar-width: 390px; margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f8fafc; color: #0f172a; }}
    .layout {{ display: grid; grid-template-columns: minmax(0, 1fr) 6px minmax(300px, var(--sidebar-width)); height: 100vh; transition: grid-template-columns 180ms ease; }}
    .layout.sidebar-collapsed {{ grid-template-columns: minmax(0, 1fr) 0 0; }}
    #map {{ height: 100vh; width: 100%; min-width: 0; }}
    .sidebar-resizer {{ cursor: col-resize; background: #e2e8f0; border-left: 1px solid #cbd5e1; border-right: 1px solid #cbd5e1; transition: background 120ms ease; z-index: 900; }}
    .sidebar-resizer:hover, body.resizing-sidebar .sidebar-resizer {{ background: #94a3b8; }}
    .layout.sidebar-collapsed .sidebar-resizer {{ border: 0; overflow: hidden; }}
    .panel {{ min-width: 0; overflow: auto; background: #f8fafc; border-left: 1px solid #d0d7de; padding: 16px; transition: padding 180ms ease, border-color 180ms ease; }}
    .layout.sidebar-collapsed .panel {{ border-left: 0; overflow: hidden; padding-left: 0; padding-right: 0; visibility: hidden; }}
    .sidebar-toggle {{ position: fixed; top: 12px; left: 56px; z-index: 2000; border: 1px solid #cbd5e1; border-radius: 8px; background: #fff; color: #0f172a; cursor: pointer; font-size: 13px; font-weight: 800; line-height: 1; padding: 10px 12px; box-shadow: 0 4px 14px rgba(15, 23, 42, 0.16); }}
    .sidebar-toggle:hover {{ background: #f8fafc; }}
    .panel h2, .panel h3 {{ margin-top: 0; }}
    .panel h2 {{ font-size: 18px; line-height: 1.2; margin-bottom: 14px; letter-spacing: 0; }}
    .section-title {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 0 0 10px; font-size: 13px; font-weight: 800; color: #0f172a; text-transform: uppercase; letter-spacing: 0.04em; }}
    .legend-item {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 13px; }}
    .swatch {{ width: 12px; height: 12px; border-radius: 3px; display: inline-block; }}
    .leaflet-tooltip.area-label {{ background: rgba(255,255,255,0.9); border: 1px solid #cbd5e1; color: #111827; box-shadow: none; }}
    .metric {{ margin-bottom: 8px; font-size: 14px; }}
    .map-label {{ background: rgba(255,255,255,0.92); border: 1px solid #cbd5e1; color: #111827; padding: 1px 4px; border-radius: 4px; font-size: 11px; white-space: nowrap; }}
    .control {{ width: 100%; padding: 8px; font-size: 14px; margin-bottom: 12px; }}
    .card {{ border: 1px solid #d7dee8; border-radius: 8px; padding: 12px; margin-bottom: 12px; background: #ffffff; box-shadow: 0 2px 8px rgba(15, 23, 42, 0.05); }}
    .grid2 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; }}
    .small {{ font-size: 12px; color: #4b5563; }}
    .summary-hero {{ border: 1px solid #d7dee8; border-radius: 8px; background: #ffffff; padding: 12px; margin-bottom: 12px; box-shadow: 0 2px 8px rgba(15, 23, 42, 0.05); }}
    .summary-hero-top {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; margin-bottom: 10px; }}
    .summary-kicker {{ font-size: 11px; font-weight: 800; color: #64748b; text-transform: uppercase; letter-spacing: 0.04em; }}
    .summary-headline {{ font-size: 22px; font-weight: 800; line-height: 1.05; color: #0f172a; margin-top: 3px; }}
    .summary-subline {{ font-size: 12px; color: #475569; margin-top: 4px; }}
    .summary-pill {{ display: inline-flex; align-items: center; min-height: 24px; padding: 0 8px; border-radius: 999px; border: 1px solid #bbf7d0; background: #f0fdf4; color: #166534; font-size: 11px; font-weight: 800; white-space: nowrap; }}
    .summary-stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 8px; margin-top: 10px; }}
    .summary-stat {{ border: 1px solid #e2e8f0; border-radius: 8px; background: #ffffff; padding: 9px; min-width: 0; }}
    .summary-stat-label {{ font-size: 11px; color: #64748b; margin-bottom: 4px; }}
    .summary-stat-value {{ font-size: 16px; font-weight: 800; color: #0f172a; line-height: 1.1; overflow-wrap: anywhere; }}
    .summary-stat-sub {{ font-size: 11px; color: #64748b; margin-top: 3px; }}
    .summary-progress {{ margin-top: 10px; }}
    .summary-band {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 8px; margin-bottom: 12px; }}
    .global-list {{ display: grid; gap: 8px; }}
    .global-row {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; border: 1px solid #e2e8f0; border-radius: 8px; background: #ffffff; padding: 9px; font-size: 12px; }}
    .global-row span:first-child {{ color: #64748b; }}
    .global-row strong {{ color: #0f172a; font-size: 13px; text-align: right; overflow-wrap: anywhere; }}
    .legend-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 6px 10px; }}
    .legend-grid .legend-item {{ margin-bottom: 0; min-width: 0; }}
    .legend-grid .legend-item span:last-child {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .util-summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; }}
    .util-chip {{ border: 1px solid #e2e8f0; border-radius: 8px; background: #ffffff; padding: 9px; min-width: 0; }}
    .util-chip-label {{ display: flex; align-items: center; gap: 6px; font-size: 11px; color: #64748b; margin-bottom: 4px; }}
    .util-chip-value {{ font-size: 18px; font-weight: 800; color: #0f172a; line-height: 1; }}
    .status-dot {{ width: 8px; height: 8px; border-radius: 999px; display: inline-block; background: #94a3b8; }}
    .status-dot.balanced {{ background: #22c55e; }}
    .status-dot.weight_bound {{ background: #0ea5e9; }}
    .status-dot.stop_bound {{ background: #a855f7; }}
    .status-dot.underutilized {{ background: #f97316; }}
    .status-dot.unused {{ background: #94a3b8; }}
    .diag-list {{ display: grid; gap: 8px; max-height: 360px; overflow: auto; padding-right: 2px; }}
    .diag-row {{ border: 1px solid #e2e8f0; border-radius: 8px; background: #ffffff; padding: 9px; }}
    .diag-row-head {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 7px; }}
    .diag-title {{ font-size: 13px; font-weight: 800; color: #0f172a; }}
    .status-badge {{ border-radius: 999px; padding: 3px 7px; font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.03em; white-space: nowrap; }}
    .status-badge.balanced {{ background: #dcfce7; color: #166534; }}
    .status-badge.weight_bound {{ background: #dbeafe; color: #1d4ed8; }}
    .status-badge.stop_bound {{ background: #f3e8ff; color: #7e22ce; }}
    .status-badge.underutilized {{ background: #ffedd5; color: #c2410c; }}
    .status-badge.unused {{ background: #f1f5f9; color: #475569; }}
    .diag-meta {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 6px; margin-top: 7px; font-size: 11px; color: #64748b; }}
    .debug-list {{ display: grid; gap: 10px; max-height: 360px; overflow: auto; padding-right: 2px; }}
    .debug-group {{ border: 1px solid #e2e8f0; border-radius: 8px; background: #ffffff; padding: 9px; }}
    .debug-group-head {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }}
    .debug-title {{ font-size: 13px; font-weight: 800; color: #0f172a; }}
    .debug-count {{ font-size: 11px; color: #64748b; white-space: nowrap; }}
    .debug-item {{ border-top: 1px solid #f1f5f9; padding: 7px 0 0; margin-top: 7px; }}
    .debug-item:first-of-type {{ border-top: 0; padding-top: 0; margin-top: 0; }}
    .debug-primary {{ font-size: 12px; font-weight: 700; color: #0f172a; line-height: 1.25; }}
    .debug-meta {{ display: flex; flex-wrap: wrap; gap: 5px; margin-top: 5px; }}
    .debug-tag {{ border: 1px solid #e2e8f0; border-radius: 999px; background: #f8fafc; padding: 2px 6px; font-size: 10px; color: #475569; }}
    .empty-state {{ border: 1px dashed #cbd5e1; border-radius: 8px; padding: 10px; color: #64748b; font-size: 12px; background: #ffffff; }}
    .route-stop-list {{ max-height: 260px; overflow: auto; border-top: 1px solid #e5e7eb; margin-top: 8px; padding-top: 8px; }}
    .route-stop-item {{ font-size: 13px; padding: 4px 0; border-bottom: 1px solid #f1f5f9; }}
    .route-summary-grid {{ display: grid; gap: 10px; margin-top: 10px; }}
    .vehicle-summary-card {{ border: 1px solid #d7dee8; border-radius: 10px; background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%); padding: 10px; box-shadow: 0 2px 8px rgba(15, 23, 42, 0.05); }}
    .vehicle-summary-head {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; }}
    .vehicle-summary-title {{ display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 700; color: #0f172a; }}
    .vehicle-summary-meta {{ font-size: 11px; color: #475569; text-align: right; }}
    .vehicle-summary-metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; margin-bottom: 8px; }}
    .vehicle-metric {{ border: 1px solid #e2e8f0; border-radius: 8px; background: rgba(255,255,255,0.9); padding: 8px; }}
    .vehicle-metric-label {{ font-size: 11px; color: #64748b; margin-bottom: 3px; }}
    .vehicle-metric-value {{ font-size: 14px; font-weight: 700; color: #0f172a; }}
    .progress-block {{ margin-top: 6px; }}
    .progress-label {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 11px; color: #475569; margin-bottom: 4px; }}
    .progress-track {{ height: 9px; border-radius: 999px; background: #e2e8f0; overflow: hidden; }}
    .progress-fill {{ height: 100%; border-radius: 999px; }}
    .progress-fill.capacity {{ background: linear-gradient(90deg, #0ea5e9 0%, #2563eb 100%); }}
    .progress-fill.stops {{ background: linear-gradient(90deg, #22c55e 0%, #16a34a 100%); }}
    .progress-fill.dropped {{ background: linear-gradient(90deg, #f97316 0%, #dc2626 100%); }}
    .selection-summary {{ display: grid; gap: 8px; }}
    .route-filter-list {{ max-height: 260px; overflow: auto; border: 1px solid #d0d7de; border-radius: 8px; background: #fff; padding: 8px 10px; margin-bottom: 12px; }}
    .route-filter-item {{ display: flex; align-items: center; gap: 8px; font-size: 13px; padding: 4px 0; }}
    .route-filter-item input {{ margin: 0; }}
    .route-filter-item.select-all {{ padding-bottom: 8px; margin-bottom: 8px; border-bottom: 1px solid #e5e7eb; font-weight: 700; }}
    .route-swatch {{ width: 12px; height: 12px; border-radius: 3px; display: inline-block; border: 1px solid #cbd5e1; }}
    .leaflet-tile-pane {{ filter: grayscale(100%) contrast(115%) brightness(88%); }}
    @media (max-width: 900px) {{
      .layout {{ grid-template-columns: minmax(0, 1fr); grid-template-rows: 65vh auto; height: auto; min-height: 100vh; }}
      .layout.sidebar-collapsed {{ grid-template-columns: minmax(0, 1fr); grid-template-rows: 100vh 0 0; }}
      #map {{ height: 65vh; }}
      .layout.sidebar-collapsed #map {{ height: 100vh; }}
      .sidebar-resizer {{ display: none; }}
      .panel {{ border-left: 0; border-top: 1px solid #d0d7de; }}
      .layout.sidebar-collapsed .panel {{ border-top: 0; height: 0; padding-top: 0; padding-bottom: 0; }}
      .sidebar-toggle {{ left: 56px; top: 10px; }}
    }}
  </style>
</head>
<body>
  <button id="sidebar-toggle" class="sidebar-toggle" type="button" aria-controls="sidebar-panel" aria-expanded="true">Hide panel</button>
  <div class="layout" id="app-layout">
    <div id="map"></div>
    <div class="sidebar-resizer" id="sidebar-resizer" role="separator" aria-label="Resize sidebar" aria-controls="sidebar-panel" aria-orientation="vertical" tabindex="0"></div>
    <div class="panel" id="sidebar-panel">
      <h2>Routes And Area Overlap</h2>
      <div class="summary-hero">
        <div class="summary-hero-top">
          <div>
            <div class="summary-kicker">Fleet plan</div>
            <div class="summary-headline">{progress_summary["completion_stops_pct"]}% stops assigned</div>
            <div class="summary-subline">{progress_summary["assigned_jobs"]} of {progress_summary["total_jobs"]} stops routed across {progress_summary["vehicles_used"]} vehicles.</div>
          </div>
          <div class="summary-pill">{progress_summary["dropped_jobs"]} dropped</div>
        </div>
        <div class="summary-progress">
          <div class="progress-label">
            <span>Stop completion</span>
            <span>{progress_summary["completion_stops_pct"]}%</span>
          </div>
          <div class="progress-track">
            <div class="progress-fill stops" style="width:{progress_summary["completion_stops_pct"]}%"></div>
          </div>
        </div>
        <div class="summary-progress">
          <div class="progress-label">
            <span>Weight completion</span>
            <span>{progress_summary["completion_weight_pct"]}%</span>
          </div>
          <div class="progress-track">
            <div class="progress-fill capacity" style="width:{progress_summary["completion_weight_pct"]}%"></div>
          </div>
        </div>
        <div class="summary-stat-grid">
          <div class="summary-stat">
            <div class="summary-stat-label">Assigned stops</div>
            <div class="summary-stat-value"><span id="assigned-jobs">{progress_summary["assigned_jobs"]}</span> / {progress_summary["total_jobs"]}</div>
            <div class="summary-stat-sub">{progress_summary["completion_stops_pct"]}% complete</div>
          </div>
          <div class="summary-stat">
            <div class="summary-stat-label">Assigned weight</div>
            <div class="summary-stat-value"><span id="assigned-weight">{progress_summary["assigned_weight"]}</span> kg</div>
            <div class="summary-stat-sub">{progress_summary["completion_weight_pct"]}% of demand</div>
          </div>
          <div class="summary-stat">
            <div class="summary-stat-label">Dropped work</div>
            <div class="summary-stat-value"><span id="dropped-jobs">{progress_summary["dropped_jobs"]}</span> stops</div>
            <div class="summary-stat-sub"><span id="dropped-weight">{progress_summary["dropped_weight"]}</span> kg</div>
          </div>
          <div class="summary-stat">
            <div class="summary-stat-label">Total distance</div>
            <div class="summary-stat-value">{progress_summary["total_distance_km"]} km</div>
            <div class="summary-stat-sub">{progress_summary["vehicles_used"]} / {progress_summary["vehicle_total"]} vehicles used</div>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="section-title">Select Routes</div>
        <div id="route-select" class="route-filter-list">
          <label class="route-filter-item select-all">
            <input type="checkbox" id="route-select-all" checked>
            <span>Select all vehicles</span>
          </label>
        </div>
        <div class="small">Use the checkboxes to show only the vehicles you want on the map.</div>
      </div>
      <div class="card">
        <div class="section-title">Current Route</div>
        <div id="route-summary" class="selection-summary small">Showing all routes.</div>
        <div id="route-stops" class="route-stop-list small"></div>
      </div>
      <div class="card">
        <div class="section-title">Utilization Summary</div>
        <div id="utilization-summary" class="util-summary-grid"></div>
      </div>
      <div class="card">
        <div class="section-title">Vehicle Diagnostics</div>
        <div id="vehicle-diagnostics" class="diag-list"></div>
      </div>
      <div class="card">
        <div class="section-title">Underutilized Debug</div>
        <div id="underutilized-debug" class="debug-list"></div>
      </div>
      <div class="card">
        <div class="section-title">Global Info</div>
        <div class="global-list">
          <div class="global-row"><span>Depot</span><strong>{depot[0]}, {depot[1]}</strong></div>
          <div class="global-row"><span>Vehicles used</span><strong>{sum(1 for route in routes if route.stops)}</strong></div>
          <div class="global-row"><span>Total routed stops</span><strong>{sum(route.stop_count for route in routes)}</strong></div>
          <div class="global-row"><span>Unassigned</span><strong>{len(unassigned)}</strong></div>
        </div>
      </div>
      <div class="card">
        <div class="section-title">Area Colors</div>
        <div class="legend-grid">
          {''.join(f'<div class="legend-item"><span class="swatch" style="background:{item["color"]}"></span><span>{item["area_label"]}</span></div>' for item in area_legend)}
        </div>
      </div>
    </div>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const depot = {json.dumps({"lat": depot[0], "lon": depot[1]})};
    const routes = {json.dumps(route_payload)};
    const unassigned = {json.dumps(unassigned_payload)};
    const areaOverlays = {json.dumps(area_overlays)};
    const progressSummary = {json.dumps(progress_summary)};
    const vehicleDiagnostics = {json.dumps(vehicle_diagnostics_payload)};
    const vehicleDiagnosticSummary = {json.dumps(vehicle_diagnostic_summary_payload)};
    const underutilizedDebugReport = {json.dumps(underutilized_debug_payload)};

    const map = L.map('map');
    L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors'
    }}).addTo(map);

    const bounds = [];
    const areaLayer = L.layerGroup().addTo(map);
    const routeLayer = L.layerGroup().addTo(map);
    const stopLayer = L.layerGroup().addTo(map);
    const allAssignedStopsLayer = L.layerGroup();
    const assignedLabelLayer = L.layerGroup();
    const allAssignedLabelLayer = L.layerGroup();
    const routeSelect = document.getElementById('route-select');
    const routeSelectAll = document.getElementById('route-select-all');
    const routeSummaryEl = document.getElementById('route-summary');
    const routeStopsEl = document.getElementById('route-stops');
    const utilizationSummaryEl = document.getElementById('utilization-summary');
    const vehicleDiagnosticsEl = document.getElementById('vehicle-diagnostics');
    const underutilizedDebugEl = document.getElementById('underutilized-debug');
    const layoutEl = document.getElementById('app-layout');
    const sidebarToggle = document.getElementById('sidebar-toggle');
    const sidebarResizer = document.getElementById('sidebar-resizer');
    const routeLayers = new Map();
    const routeStopLayers = new Map();
    const routeLabelLayers = new Map();

    function invalidateMapSizeSoon() {{
      window.requestAnimationFrame(() => {{
        window.setTimeout(() => map.invalidateSize(), 220);
      }});
    }}

    sidebarToggle.addEventListener('click', () => {{
      const collapsed = layoutEl.classList.toggle('sidebar-collapsed');
      sidebarToggle.setAttribute('aria-expanded', String(!collapsed));
      sidebarToggle.textContent = collapsed ? 'Show panel' : 'Hide panel';
      invalidateMapSizeSoon();
    }});

    window.addEventListener('resize', () => map.invalidateSize());

    function setSidebarWidth(width) {{
      const maxWidth = Math.min(760, Math.max(300, window.innerWidth - 320));
      const nextWidth = Math.min(Math.max(width, 300), maxWidth);
      document.body.style.setProperty('--sidebar-width', `${{nextWidth}}px`);
      map.invalidateSize();
    }}

    sidebarResizer.addEventListener('pointerdown', (event) => {{
      if (window.matchMedia('(max-width: 900px)').matches || layoutEl.classList.contains('sidebar-collapsed')) return;
      event.preventDefault();
      sidebarResizer.setPointerCapture(event.pointerId);
      document.body.classList.add('resizing-sidebar');
    }});

    sidebarResizer.addEventListener('pointermove', (event) => {{
      if (!document.body.classList.contains('resizing-sidebar')) return;
      setSidebarWidth(window.innerWidth - event.clientX);
    }});

    function stopSidebarResize(event) {{
      if (!document.body.classList.contains('resizing-sidebar')) return;
      document.body.classList.remove('resizing-sidebar');
      if (sidebarResizer.hasPointerCapture(event.pointerId)) sidebarResizer.releasePointerCapture(event.pointerId);
      invalidateMapSizeSoon();
    }}

    sidebarResizer.addEventListener('pointerup', stopSidebarResize);
    sidebarResizer.addEventListener('pointercancel', stopSidebarResize);

    areaOverlays.forEach((area) => {{
      const polygonLatLngs = area.polygon.map((point) => [point.lat, point.lon]);
      polygonLatLngs.forEach((latlng) => bounds.push(latlng));
      L.polygon(polygonLatLngs, {{
        color: area.color,
        fillColor: area.color,
        fillOpacity: 0.11,
        opacity: 0.9,
        weight: 2.5
      }}).bindTooltip(
        `<strong>${{area.label}}</strong><br>Jobs: ${{area.job_count}}<br>Weight: ${{area.total_weight}} kg`,
        {{permanent: false, direction: 'top', className: 'area-label'}}
      ).addTo(areaLayer);
    }});

    L.marker([depot.lat, depot.lon]).bindPopup('Common depot / start point').addTo(map);
    bounds.push([depot.lat, depot.lon]);

    routes.forEach((route) => {{
      const routePolylineLayer = L.layerGroup().addTo(routeLayer);
      const routeStopGroup = L.layerGroup().addTo(stopLayer);
      const routeLabelGroup = L.layerGroup();
      routeLayers.set(String(route.vehicle_id), routePolylineLayer);
      routeStopLayers.set(String(route.vehicle_id), routeStopGroup);
      routeLabelLayers.set(String(route.vehicle_id), routeLabelGroup);
      routeSelect.insertAdjacentHTML(
        'beforeend',
        `<label class="route-filter-item" title="Vehicle ${route.vehicle_id}">
          <input type="checkbox" class="route-checkbox" value="${{route.vehicle_id}}" checked>
          <span class="route-swatch" style="background:${{route.color}}"></span>
          <span>Vehicle ${{route.vehicle_id}}</span>
        </label>`
      );
      const latlngs = route.points.map((point) => [point.lat, point.lon]);
      latlngs.forEach((latlng) => bounds.push(latlng));
      L.polyline(latlngs, {{
        color: '#0f172a',
        weight: 7,
        opacity: 0.42
      }}).addTo(routePolylineLayer);
      L.polyline(latlngs, {{
        color: route.color,
        weight: 4,
        opacity: 0.95
      }}).bindPopup(`Vehicle ${{route.vehicle_id}}<br>Stops: ${{route.stop_count}}<br>Distance: ${{route.distance_km}} km`).addTo(routePolylineLayer);

      route.points.forEach((point) => {{
        if (!point.job_id) {{
          return;
        }}
        const marker = L.circleMarker([point.lat, point.lon], {{
          radius: 6,
          color: '#334155',
          fillColor: point.area_color,
          fillOpacity: 0.95,
          weight: 1.5
        }});
        marker.bindPopup(
          `<strong>${{point.name}}</strong><br>` +
          `Vehicle: ${{route.vehicle_id}}<br>` +
          `Stop: ${{point.sequence}}<br>` +
          `Area: ${{point.area_label}}<br>` +
          `Weight: ${{point.weight}} kg`
        );
        marker.addTo(routeStopGroup);
        L.circleMarker([point.lat, point.lon], {{
          radius: 6,
          color: '#334155',
          fillColor: point.area_color,
          fillOpacity: 0.95,
          weight: 1.5
        }}).bindPopup(
          `<strong>${{point.name}}</strong><br>` +
          `Vehicle: ${{route.vehicle_id}}<br>` +
          `Stop: ${{point.sequence}}<br>` +
          `Area: ${{point.area_label}}<br>` +
          `Weight: ${{point.weight}} kg`
        ).addTo(allAssignedStopsLayer);
        const labelIcon = L.divIcon({{
          className: 'map-label',
          html: `<span style="color:${{point.area_color}};font-weight:700">${{point.area_label}}</span> ${{point.sequence}}. ${{point.name}}`,
          iconSize: null
        }});
        L.marker([point.lat, point.lon], {{icon: labelIcon, interactive: false}}).addTo(routeLabelGroup);
        L.marker([point.lat, point.lon], {{icon: labelIcon, interactive: false}}).addTo(allAssignedLabelLayer);
      }});
    }});

    L.control.layers(null, {{
      "Area overlays": areaLayer,
      "Vehicle routes": routeLayer,
      "Assigned stops (selected route)": stopLayer,
      "Assigned stops (all routes)": allAssignedStopsLayer,
      "Assigned labels (selected route)": assignedLabelLayer,
      "Assigned labels (all stops)": allAssignedLabelLayer
    }}, {{collapsed: false}}).addTo(map);

    function getRouteCheckboxes() {{
      return Array.from(document.querySelectorAll('.route-checkbox'));
    }}

    function getSelectedRouteIds() {{
      return getRouteCheckboxes()
        .filter((checkbox) => checkbox.checked)
        .map((checkbox) => checkbox.value);
    }}

    function syncSelectAllState() {{
      const checkboxes = getRouteCheckboxes();
      const checkedCount = checkboxes.filter((checkbox) => checkbox.checked).length;
      routeSelectAll.checked = checkedCount === checkboxes.length && checkboxes.length > 0;
      routeSelectAll.indeterminate = checkedCount > 0 && checkedCount < checkboxes.length;
    }}

    function formatMetricValue(value, suffix = '') {{
      if (value === null || value === undefined) {{
        return 'NA';
      }}
      return `${{value}}${{suffix}}`;
    }}

    function statusLabel(status) {{
      return String(status || 'unknown').replaceAll('_', ' ');
    }}

    function statusClass(status) {{
      return String(status || 'unused');
    }}

    function buildVehicleSummaryCard(route) {{
      const capacityKnown = route.capacity_kg !== null && route.capacity_kg !== undefined;
      const stopsKnown = route.max_stops !== null && route.max_stops !== undefined;
      const capacityFill = capacityKnown ? Math.max(0, Math.min(100, route.capacity_fill_pct ?? 0)) : 0;
      const stopsFill = stopsKnown ? Math.max(0, Math.min(100, route.stops_fill_pct ?? 0)) : 0;
      const deliveryRunout = route.load_kg > 0 ? '0 kg after final stop' : 'No load assigned';
      return `
        <div class="vehicle-summary-card">
          <div class="vehicle-summary-head">
            <div class="vehicle-summary-title">
              <span class="route-swatch" style="background:${{route.color}}"></span>
              <span>Vehicle ${{route.vehicle_id}}</span>
            </div>
            <div class="vehicle-summary-meta">${{route.distance_km}} km<br>${{route.stop_count}} stops</div>
          </div>
          <div class="vehicle-summary-metrics">
            <div class="vehicle-metric">
              <div class="vehicle-metric-label">Vehicle capacity</div>
              <div class="vehicle-metric-value">${{formatMetricValue(route.capacity_kg, ' kg')}}</div>
            </div>
            <div class="vehicle-metric">
              <div class="vehicle-metric-label">Allowed stops</div>
              <div class="vehicle-metric-value">${{formatMetricValue(route.max_stops)}}</div>
            </div>
            <div class="vehicle-metric">
              <div class="vehicle-metric-label">Planned load at depot</div>
              <div class="vehicle-metric-value">${{route.load_kg.toFixed(2)}} kg</div>
            </div>
            <div class="vehicle-metric">
              <div class="vehicle-metric-label">Unused capacity</div>
              <div class="vehicle-metric-value">${{capacityKnown ? `${{route.unused_capacity_kg.toFixed(2)}} kg` : 'NA'}}</div>
            </div>
          </div>
          <div class="progress-block">
            <div class="progress-label">
              <span>Capacity utilization</span>
              <span>${{capacityKnown ? `${{capacityFill.toFixed(1)}}% used` : 'Not available'}}</span>
            </div>
            <div class="progress-track">
              <div class="progress-fill capacity" style="width:${{capacityKnown ? capacityFill : 0}}%"></div>
            </div>
          </div>
          <div class="progress-block">
            <div class="progress-label">
              <span>Stop utilization</span>
              <span>${{stopsKnown ? `${{stopsFill.toFixed(1)}}% used` : 'Not available'}}</span>
            </div>
            <div class="progress-track">
              <div class="progress-fill stops" style="width:${{stopsKnown ? stopsFill : 0}}%"></div>
            </div>
          </div>
          <div class="small" style="margin-top:8px;">Delivery flow: starts with <strong>${{route.load_kg.toFixed(2)}} kg</strong> on the vehicle, then unloads along the route until <strong>${{deliveryRunout}}</strong>.</div>
        </div>
      `;
    }}

    function buildSelectionSummary(title, routesForSummary) {{
      const totalStops = routesForSummary.reduce((sum, route) => sum + route.stop_count, 0);
      const totalLoad = routesForSummary.reduce((sum, route) => sum + route.load_kg, 0);
      const totalDistance = routesForSummary.reduce((sum, route) => sum + route.distance_km, 0);
      const capacityKnownRoutes = routesForSummary.filter((route) => route.capacity_kg !== null && route.capacity_kg !== undefined);
      const totalCapacity = capacityKnownRoutes.reduce((sum, route) => sum + route.capacity_kg, 0);
      const capacityPct = totalCapacity > 0 ? (totalLoad / totalCapacity) * 100 : null;
      const vehicleLabel = routesForSummary.length === 1 ? 'vehicle' : 'vehicles';
      return `
        <div class="summary-stat-grid">
          <div class="summary-stat">
            <div class="summary-stat-label">${{title}}</div>
            <div class="summary-stat-value">${{routesForSummary.length}} ${{vehicleLabel}}</div>
            <div class="summary-stat-sub">${{totalStops}} stops selected</div>
          </div>
          <div class="summary-stat">
            <div class="summary-stat-label">Load</div>
            <div class="summary-stat-value">${{totalLoad.toFixed(2)}} kg</div>
            <div class="summary-stat-sub">${{capacityPct === null ? 'Capacity not available' : `${{capacityPct.toFixed(1)}}% of selected capacity`}}</div>
          </div>
          <div class="summary-stat">
            <div class="summary-stat-label">Distance</div>
            <div class="summary-stat-value">${{totalDistance.toFixed(2)}} km</div>
            <div class="summary-stat-sub">Open-route total</div>
          </div>
          <div class="summary-stat">
            <div class="summary-stat-label">Dropped stops</div>
            <div class="summary-stat-value">${{progressSummary.dropped_jobs}}</div>
            <div class="summary-stat-sub">${{progressSummary.dropped_weight}} kg</div>
          </div>
        </div>
      `;
    }}

    function renderUtilizationSummary() {{
      const statusOrder = ['balanced', 'weight_bound', 'stop_bound', 'underutilized', 'unused'];
      utilizationSummaryEl.innerHTML = statusOrder
        .map((status) => `
          <div class="util-chip">
            <div class="util-chip-label">
              <span class="status-dot ${{status}}"></span>
              <span>${{statusLabel(status)}}</span>
            </div>
            <div class="util-chip-value">${{vehicleDiagnosticSummary[status] || 0}}</div>
          </div>
        `)
        .join('');
    }}

    function renderVehicleDiagnostics() {{
      if (!vehicleDiagnostics.length) {{
        vehicleDiagnosticsEl.innerHTML = '<div class="empty-state">No vehicle diagnostics available.</div>';
        return;
      }}

      vehicleDiagnosticsEl.innerHTML = vehicleDiagnostics
        .map((item) => {{
          const capacityPct = Math.max(0, Math.min(100, Number(item.capacity_utilization_pct || 0)));
          const stopPct = Math.max(0, Math.min(100, Number(item.stop_utilization_pct || 0)));
          const status = statusClass(item.utilization_status);
          return `
            <div class="diag-row">
              <div class="diag-row-head">
                <div class="diag-title">Vehicle ${{item.vehicle_id}}</div>
                <div class="status-badge ${{status}}">${{statusLabel(status)}}</div>
              </div>
              <div class="progress-block">
                <div class="progress-label">
                  <span>Capacity</span>
                  <span>${{item.used_capacity_kg}} / ${{item.capacity_kg}} kg (${{item.capacity_utilization_pct}}%)</span>
                </div>
                <div class="progress-track">
                  <div class="progress-fill capacity" style="width:${{capacityPct}}%"></div>
                </div>
              </div>
              <div class="progress-block">
                <div class="progress-label">
                  <span>Stops</span>
                  <span>${{item.used_stops}} / ${{item.max_stops}} (${{item.stop_utilization_pct}}%)</span>
                </div>
                <div class="progress-track">
                  <div class="progress-fill stops" style="width:${{stopPct}}%"></div>
                </div>
              </div>
              <div class="diag-meta">
                <span>Remaining: ${{item.remaining_capacity_kg}} kg</span>
                <span>${{item.remaining_stops}} stops left</span>
                <span>Distance: ${{item.distance_km}} km</span>
                <span>Status: ${{statusLabel(status)}}</span>
              </div>
            </div>
          `;
        }})
        .join('');
    }}

    function debugItemTitle(example) {{
      if (example.candidate_type === 'transfer') {{
        return `Transfer ${{example.job_id}} from V${{example.from_vehicle_id}}`;
      }}
      return `Swap with V${{example.from_vehicle_id}}`;
    }}

    function debugItemSubtitle(example) {{
      if (example.candidate_type === 'transfer') {{
        return example.job_name || 'Unknown stop';
      }}
      return `Out ${{example.out_job_id}} - in ${{example.in_job_id}}`;
    }}

    function renderUnderutilizedDebug() {{
      const entries = Object.entries(underutilizedDebugReport);
      if (!entries.length) {{
        underutilizedDebugEl.innerHTML = '<div class="empty-state">No underutilized routes after rebalancing.</div>';
        return;
      }}

      underutilizedDebugEl.innerHTML = entries
        .map(([vehicleId, examples]) => `
          <div class="debug-group">
            <div class="debug-group-head">
              <div class="debug-title">Vehicle ${{vehicleId}}</div>
              <div class="debug-count">${{examples.length}} examples</div>
            </div>
            ${{examples.map((example) => `
              <div class="debug-item">
                <div class="debug-primary">${{debugItemTitle(example)}}</div>
                <div class="small">${{debugItemSubtitle(example)}}</div>
                <div class="debug-meta">
                  <span class="debug-tag">${{String(example.reason || '').replaceAll('_', ' ')}}</span>
                  <span class="debug-tag">${{example.distance_km}} km</span>
                  <span class="debug-tag">score ${{example.move_score}}</span>
                  <span class="debug-tag">penalty ${{example.territory_penalty}}</span>
                </div>
              </div>
            `).join('')}}
          </div>
        `)
        .join('');
    }}

    function setRouteView(selectedRouteIds) {{
      const selectedSet = new Set(selectedRouteIds);
      const showingNone = selectedSet.size === 0;
      const showingAll = selectedSet.size === routes.length;
      routes.forEach((route) => {{
        const key = String(route.vehicle_id);
        const show = selectedSet.has(key);
        const polyLayer = routeLayers.get(key);
        const stopGroup = routeStopLayers.get(key);
        const labelGroup = routeLabelLayers.get(key);
        if (show) {{
          routeLayer.addLayer(polyLayer);
          stopLayer.addLayer(stopGroup);
          if (map.hasLayer(assignedLabelLayer)) {{
            assignedLabelLayer.addLayer(labelGroup);
          }}
        }} else {{
          routeLayer.removeLayer(polyLayer);
          stopLayer.removeLayer(stopGroup);
          assignedLabelLayer.removeLayer(labelGroup);
        }}
      }});

      if (showingNone) {{
        routeSummaryEl.innerHTML = `
          <div class="empty-state">No routes selected.</div>
          <div class="global-row" style="margin-top:8px;"><span>Vehicles</span><strong>None</strong></div>
          <div class="global-row" style="margin-top:8px;"><span>Map routes</span><strong>Hidden</strong></div>
        `;
        routeStopsEl.innerHTML = '';
        return;
      }}

      if (showingAll) {{
        routeSummaryEl.innerHTML = buildSelectionSummary('Showing all routes', routes);
        routeStopsEl.innerHTML = routes
          .map((route) => buildVehicleSummaryCard(route))
          .join('');
        if (bounds.length > 0) {{
          map.fitBounds(bounds, {{padding: [30, 30]}});
        }}
        return;
      }}

      const selectedRoutes = routes.filter((item) => selectedSet.has(String(item.vehicle_id)));
      const areaCounts = new Map();
      selectedRoutes.forEach((route) => {{
        Object.entries(route.area_breakdown).forEach(([area, count]) => {{
          areaCounts.set(area, (areaCounts.get(area) || 0) + count);
        }});
      }});
      const areaSummary = Array.from(areaCounts.entries())
        .map(([area, count]) => `${{area}} (${{count}})`)
        .join(', ');
      const selectionTitle = selectedRoutes.length === 1
        ? `Vehicle ${{selectedRoutes[0].vehicle_id}}`
        : `${{selectedRoutes.length}} selected routes`;
      routeSummaryEl.innerHTML =
        buildSelectionSummary(selectionTitle, selectedRoutes) +
        `<div class="global-row" style="margin-top:8px;"><span>Vehicles</span><strong>${{selectedRoutes.map((route) => route.vehicle_id).join(', ')}}</strong></div>` +
        `<div class="global-row" style="margin-top:8px;"><span>Areas</span><strong>${{areaSummary || 'NA'}}</strong></div>`;
      routeStopsEl.innerHTML = selectedRoutes
        .map((route) => {{
          const summaryCard = buildVehicleSummaryCard(route);
          const items = route.points
            .filter((point) => point.job_id)
            .map((point) => `<div class="route-stop-item"><strong>V${{route.vehicle_id}} - ${{point.sequence}}.</strong> ${{point.name}}<br><span class="small">Area: ${{point.area_label}} | Weight: ${{point.weight}} kg</span></div>`)
            .join('');
          return `<div style="margin-bottom:10px;">${{summaryCard}}<div>${{items}}</div></div>`;
        }})
        .join('');
      const routeBounds = selectedRoutes.flatMap((route) => route.points.map((point) => [point.lat, point.lon]));
      map.fitBounds(routeBounds, {{padding: [30, 30]}});
    }}

    routeSelectAll.addEventListener('change', () => {{
      getRouteCheckboxes().forEach((checkbox) => {{
        checkbox.checked = routeSelectAll.checked;
      }});
      syncSelectAllState();
      setRouteView(getSelectedRouteIds());
    }});

    routeSelect.addEventListener('change', (event) => {{
      if (!event.target.classList.contains('route-checkbox')) {{
        return;
      }}
      syncSelectAllState();
      setRouteView(getSelectedRouteIds());
    }});

    map.on('overlayadd', (event) => {{
      if (event.layer === assignedLabelLayer) {{
        const selectedRouteIds = getSelectedRouteIds();
        selectedRouteIds.forEach((routeId) => {{
          const layer = routeLabelLayers.get(routeId);
          if (layer) {{
            assignedLabelLayer.addLayer(layer);
          }}
        }});
      }}
    }});

    map.on('overlayremove', (event) => {{
      if (event.layer === assignedLabelLayer) {{
        routes.forEach((route) => assignedLabelLayer.removeLayer(routeLabelLayers.get(String(route.vehicle_id))));
      }}
    }});

    syncSelectAllState();
    renderUtilizationSummary();
    renderVehicleDiagnostics();
    renderUnderutilizedDebug();
    setRouteView(getSelectedRouteIds());

    if (bounds.length === 0) {{
      map.setView([depot.lat, depot.lon], 11);
    }}
  </script>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")


def render_leaflet_unassigned_html(
    unassigned: Sequence["Job"],
    depot: "Coordinate",
    area_summaries: Dict[str, "AreaSummary"],
    output_path: Path,
    drop_penalty_fn: Callable[["Job"], int],
) -> None:
    """Render a separate interactive map focused only on dropped jobs."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    area_color_map = build_area_color_map(area_summaries)
    unassigned_area_summaries = {
        area_id: summary for area_id, summary in area_summaries.items()
        if any(job.area_id == area_id for job in unassigned)
    }
    area_overlays = build_area_overlay_data(unassigned, unassigned_area_summaries, area_color_map) if unassigned else []
    unassigned_payload = [
        {
            "job_id": job.job_id,
            "name": job.name,
            "lat": job.lat,
            "lon": job.lon,
            "area_label": display_area_id(job.area_id),
            "area_color": area_color_map.get(job.area_id, "#dc2626"),
            "weight": job.weight,
            "priority": job.priority,
            "drop_penalty": drop_penalty_fn(job),
        }
        for job in unassigned
    ]
    area_legend = sorted(
        {(item["label"], item["color"]) for item in area_overlays},
        key=lambda item: item[0],
    )

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Unassigned Map</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; background: #f8fafc; }}
    .layout {{ display: grid; grid-template-columns: minmax(0, 1fr) 340px; height: 100vh; }}
    #map {{ height: 100vh; width: 100%; }}
    .panel {{ overflow: auto; background: white; border-left: 1px solid #d0d7de; padding: 16px; }}
    .legend-item {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 13px; }}
    .swatch {{ width: 12px; height: 12px; border-radius: 3px; display: inline-block; }}
    .map-label {{ background: rgba(255,255,255,0.92); border: 1px solid #cbd5e1; color: #111827; padding: 1px 4px; border-radius: 4px; font-size: 11px; white-space: nowrap; }}
    .leaflet-tile-pane {{ filter: grayscale(100%) contrast(115%) brightness(88%); }}
  </style>
</head>
<body>
  <div class="layout">
    <div id="map"></div>
    <div class="panel">
      <h2>Unassigned Stores</h2>
      <div><strong>Count:</strong> {len(unassigned)}</div>
      <div><strong>Depot:</strong> {depot[0]}, {depot[1]}</div>
      <h3>Area Colors</h3>
      {''.join(f'<div class="legend-item"><span class="swatch" style="background:{color}"></span>{label}</div>' for label, color in area_legend)}
    </div>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const depot = {json.dumps({"lat": depot[0], "lon": depot[1]})};
    const unassigned = {json.dumps(unassigned_payload)};
    const areaOverlays = {json.dumps(area_overlays)};
    const map = L.map('map');
    L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors'
    }}).addTo(map);

    const bounds = [[depot.lat, depot.lon]];
    L.marker([depot.lat, depot.lon]).bindPopup('Common depot / start point').addTo(map);

    areaOverlays.forEach((area) => {{
      const polygonLatLngs = area.polygon.map((point) => [point.lat, point.lon]);
      polygonLatLngs.forEach((latlng) => bounds.push(latlng));
      L.polygon(polygonLatLngs, {{
        color: area.color,
        fillColor: area.color,
        fillOpacity: 0.12,
        opacity: 0.9,
        weight: 2.5
      }}).bindPopup(`Area ${{area.label}}<br>Unassigned jobs: ${{area.job_count}}`).addTo(map);
    }});

    const unassignedLabelLayer = L.layerGroup();
    unassigned.forEach((job) => {{
      const latlng = [job.lat, job.lon];
      bounds.push(latlng);
      const marker = L.circleMarker(latlng, {{
        radius: 7,
        color: '#991b1b',
        fillColor: job.area_color,
        fillOpacity: 0.95,
        weight: 2
      }});
      marker.bindPopup(
        `<strong>${{job.name}}</strong><br>` +
        `Area: ${{job.area_label}}<br>` +
        `Weight: ${{job.weight}} kg<br>` +
        `Priority: ${{job.priority ?? 'NA'}}<br>` +
        `Drop penalty: ${{job.drop_penalty}}`
      );
      marker.addTo(map);
      const labelIcon = L.divIcon({{
        className: 'map-label',
        html: `<span style="color:${{job.area_color}};font-weight:700">${{job.area_label}}</span> ${{job.name}}`,
        iconSize: null
      }});
      L.marker([job.lat, job.lon], {{icon: labelIcon, interactive: false}}).addTo(unassignedLabelLayer);
    }});

    L.control.layers(null, {{
      "Unassigned labels": unassignedLabelLayer
    }}, {{collapsed: false}}).addTo(map);

    map.fitBounds(bounds, {{padding: [30, 30]}});
  </script>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
