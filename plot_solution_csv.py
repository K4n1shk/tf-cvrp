#!/usr/bin/env python3
"""Plot a routed CSV solution using the same visuals as the main pipeline."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Dict, List

from models import AreaSummary, Job, RouteStop, VehicleRoute
import rendering
from preassign import assign_missing_area_ids, drop_penalty, load_jobs, summarize_areas


def load_route_csv(route_csv: Path) -> List[VehicleRoute]:
    """Read the flat routed CSV export and rebuild ordered vehicle routes."""
    with route_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("Route CSV is missing a header row.")

        required = {
            "vehicle_id",
            "sequence",
            "job_id",
            "name",
            "area_id",
            "latitude",
            "longitude",
            "weight",
        }
        missing = required.difference(reader.fieldnames)
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise ValueError(f"Route CSV is missing required columns: {missing_list}")

        grouped: Dict[int, List[RouteStop]] = {}
        for row in reader:
            vehicle_id = int(row["vehicle_id"])
            grouped.setdefault(vehicle_id, []).append(
                RouteStop(
                    sequence=int(row["sequence"]),
                    job_id=str(row["job_id"]).strip(),
                    name=str(row["name"]).strip(),
                    area_id=str(row["area_id"]).strip(),
                    lat=float(row["latitude"]),
                    lon=float(row["longitude"]),
                    weight=float(row["weight"]),
                )
            )

    routes: List[VehicleRoute] = []
    for vehicle_id in sorted(grouped):
        stops = sorted(grouped[vehicle_id], key=lambda stop: stop.sequence)
        routes.append(
            VehicleRoute(
                vehicle_id=vehicle_id,
                distance_km=0.0,
                load_kg=round(sum(stop.weight for stop in stops), 2),
                stop_count=len(stops),
                stops=stops,
            )
        )
    return routes


def load_unassigned_csv(unassigned_csv: Path) -> List[Job]:
    """Read optional unassigned-jobs CSV exported by the main pipeline."""
    with unassigned_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("Unassigned CSV is missing a header row.")

        jobs: List[Job] = []
        for row in reader:
            jobs.append(
                Job(
                    job_id=str(row["job_id"]).strip(),
                    name=str(row["name"]).strip(),
                    lat=float(row["latitude"]),
                    lon=float(row["longitude"]),
                    weight=float(row["weight"]),
                    area_id=str(row["area_id"]).strip(),
                    quantity=int(float(row["quantity"])) if row.get("quantity") else 1,
                    priority=int(float(row["priority"])) if row.get("priority") else None,
                    delivery_preference=int(float(row["delivery_preference"]))
                    if row.get("delivery_preference")
                    else None,
                    job_type=str(row.get("job_type", "")).strip(),
                    has_area_id=bool(str(row["area_id"]).strip()),
                )
            )
    return jobs


def jobs_from_routes(routes: List[VehicleRoute]) -> List[Job]:
    """Convert routed stops into lightweight Job objects for polygon rendering."""
    jobs: List[Job] = []
    for route in routes:
        for stop in route.stops:
            jobs.append(
                Job(
                    job_id=stop.job_id,
                    name=stop.name,
                    lat=stop.lat,
                    lon=stop.lon,
                    weight=stop.weight,
                    area_id=stop.area_id,
                    has_area_id=bool(stop.area_id),
                )
            )
    return jobs


def combine_area_summaries(
    routed_jobs: List[Job],
    full_jobs: List[Job],
) -> Dict[str, AreaSummary]:
    """Prefer full-job area boundaries when available, otherwise fall back to routed jobs."""
    if full_jobs:
        return summarize_areas(full_jobs)
    return summarize_areas(routed_jobs)


def build_parser() -> argparse.ArgumentParser:
    """Define CLI arguments for plotting an already-solved route CSV."""
    parser = argparse.ArgumentParser(description="Plot an exported routing CSV.")
    parser.add_argument("route_csv", help="Path to a routed solution CSV like optimized_routes.csv.")
    parser.add_argument(
        "--jobs-csv",
        default="drop_points.csv",
        help="Optional jobs CSV used for full area overlays. Defaults to drop_points.csv.",
    )
    parser.add_argument(
        "--unassigned-csv",
        default=None,
        help="Optional unassigned_jobs.csv path to populate the unassigned map.",
    )
    parser.add_argument("--output-dir", default="plotted_solution", help="Directory for generated plot artifacts.")
    parser.add_argument("--depot-lat", type=float, default=13.019211191241423, help="Depot latitude.")
    parser.add_argument("--depot-lon", type=float, default=77.54907673395722, help="Depot longitude.")
    return parser


def main() -> None:
    """Read an exported route CSV and regenerate the map and PNG outputs."""
    args = build_parser().parse_args()
    route_csv = Path(args.route_csv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    depot = (args.depot_lat, args.depot_lon)

    if not route_csv.exists():
        raise FileNotFoundError(f"Route CSV not found: {route_csv}")

    routes = load_route_csv(route_csv)
    routed_jobs = jobs_from_routes(routes)

    jobs_csv = Path(args.jobs_csv)
    if jobs_csv.exists():
        full_jobs = assign_missing_area_ids(load_jobs(jobs_csv))
    else:
        full_jobs = []

    unassigned: List[Job] = []
    if args.unassigned_csv:
        unassigned_csv = Path(args.unassigned_csv)
        if not unassigned_csv.exists():
            raise FileNotFoundError(f"Unassigned CSV not found: {unassigned_csv}")
        unassigned = load_unassigned_csv(unassigned_csv)

    area_summaries = combine_area_summaries(routed_jobs, full_jobs)

    rendering.write_routes_csv(routes, output_dir / "optimized_routes.csv")
    rendering.write_unassigned_csv(unassigned, output_dir / "unassigned_jobs.csv", drop_penalty)
    rendering.render_routes_png(routes, depot, unassigned, output_dir / "routes.png")
    rendering.render_leaflet_routes_html(
        jobs=full_jobs or routed_jobs,
        routes=routes,
        depot=depot,
        unassigned=unassigned,
        area_summaries=area_summaries,
        output_path=output_dir / "routes_map.html",
        drop_penalty_fn=drop_penalty,
    )
    rendering.render_leaflet_unassigned_html(
        unassigned=unassigned,
        depot=depot,
        area_summaries=area_summaries,
        output_path=output_dir / "unassigned_map.html",
        drop_penalty_fn=drop_penalty,
    )

    print(f"Routes loaded: {len(routes)}")
    print(f"Total stops: {sum(route.stop_count for route in routes)}")
    print(f"Unassigned jobs: {len(unassigned)}")
    print(f"Artifacts written to: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
