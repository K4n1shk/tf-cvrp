from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Tuple

import rendering
from tfopt.fleet import VEHICLE_SPECS_DEFAULT, build_vehicles, parse_vehicle_specs
from tfopt.io import load_jobs, write_json
from tfopt.postprocess import build_underutilized_debug_report, cleanup_final_routes
from tfopt.routing import solve_routes
from tfopt.scoring import drop_penalty
from tfopt.summary import route_summary
from tfopt.territory import (
    assign_missing_area_ids,
    assign_primary_areas,
    build_candidate_map,
    build_job_territory_map,
    redistribute_area_ids,
    summarize_areas,
)


def build_parser() -> argparse.ArgumentParser:
    """Define the CLI used to run the full routing pipeline."""
    parser = argparse.ArgumentParser(description="Assign jobs, optimize routes, and export artifacts.")
    parser.add_argument(
        "input_csv",
        nargs="?",
        default="drop_points.csv",
        help="Input CSV path. Defaults to drop_points.csv in the current directory.",
    )
    parser.add_argument("--output-dir", default="output", help="Directory for generated artifacts.")
    parser.add_argument("--depot-lat", type=float, default=13.019211191241423, help="Depot latitude.")
    parser.add_argument("--depot-lon", type=float, default=77.54907673395722, help="Depot longitude.")
    parser.add_argument(
        "--flexible-distance-km",
        type=float,
        default=2.5,
        help="Area spillover threshold in kilometers.",
    )
    parser.add_argument(
        "--spillover-penalty",
        type=float,
        default=8.0,
        help="Penalty for flexible jobs assigned outside the primary vehicle.",
    )
    parser.add_argument(
        "--non-flexible-penalty",
        type=float,
        default=1000.0,
        help="Penalty for non-flexible jobs assigned outside the primary vehicle.",
    )
    parser.add_argument(
        "--consistency-bonus",
        type=float,
        default=5.0,
        help="Bonus for keeping jobs on the primary area vehicle.",
    )
    parser.add_argument(
        "--road-factor",
        type=float,
        default=1.23,
        help="Multiplier used to approximate road distance from haversine distance.",
    )
    parser.add_argument(
        "--candidate-vehicles",
        type=int,
        default=4,
        help="Preferred vehicle count before soft territory penalties become stronger.",
    )
    parser.add_argument(
        "--time-limit-seconds",
        type=int,
        default=120,
        help="Routing solver time limit in seconds.",
    )
    parser.add_argument(
        "--vehicle-spec",
        action="append",
        default=[],
        help="Vehicle spec in capacity:count:max_stops format. Can be repeated.",
    )
    parser.add_argument(
        "--redistribute-area-id",
        action="append",
        default=None,
        help=(
            "Area id to dissolve into the nearest existing area before routing. "
            "Defaults to 1111 when omitted. Can be repeated."
        ),
    )
    return parser


def main() -> None:
    """Run the full pipeline from load -> preprocess -> assign -> route -> export."""
    args = build_parser().parse_args()
    input_csv = Path(args.input_csv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    depot = (args.depot_lat, args.depot_lon)

    redistribute_area_ids_arg = [
        area_id
        for area_id in (args.redistribute_area_id or ["1111"])
        if str(area_id).strip()
    ]

    # Normalize configured problem areas and blank-area jobs so every downstream stage
    # works with routeable territories without mutating the source CSV.
    jobs = redistribute_area_ids(load_jobs(input_csv), redistribute_area_ids_arg)
    jobs = assign_missing_area_ids(jobs)
    vehicle_specs = parse_vehicle_specs(args.vehicle_spec) if args.vehicle_spec else VEHICLE_SPECS_DEFAULT
    fleet_source = "cli_or_default"
    area_summaries = summarize_areas(jobs)
    vehicles_for_assignment = build_vehicles(depot, vehicle_specs)
    area_to_vehicle = assign_primary_areas(
        vehicles_for_assignment,
        area_summaries,
        depot,
    )
    candidate_map = build_candidate_map(
        jobs=jobs,
        vehicles=vehicles_for_assignment,
        area_summaries=area_summaries,
        area_to_vehicle=area_to_vehicle,
        depot=depot,
        threshold_km=args.flexible_distance_km,
        spillover_penalty=args.spillover_penalty,
        non_flexible_penalty=args.non_flexible_penalty,
        consistency_bonus=args.consistency_bonus,
        max_candidates=args.candidate_vehicles,
    )
    territory_map = build_job_territory_map(
        jobs=jobs,
        vehicles=vehicles_for_assignment,
        area_summaries=area_summaries,
        area_to_vehicle=area_to_vehicle,
        threshold_km=args.flexible_distance_km,
    )

    vehicles_for_routing = build_vehicles(depot, vehicle_specs)
    vehicle_limits = {
        vehicle.vehicle_id: {
            "capacity_kg": vehicle.capacity,
            "max_stops": vehicle.max_stops,
        }
        for vehicle in vehicles_for_routing
    }
    # Solve the constrained fleet plan and capture any overflow jobs the fleet cannot serve.
    routes, unassigned, solver_diagnostics = solve_routes(
        jobs=jobs,
        vehicles=vehicles_for_routing,
        depot=depot,
        candidate_map=candidate_map,
        territory_map=territory_map,
        spillover_penalty=args.spillover_penalty,
        non_flexible_penalty=args.non_flexible_penalty,
        road_factor=args.road_factor,
        flexible_distance_km=args.flexible_distance_km,
        time_limit_seconds=args.time_limit_seconds,
    )
    routes = cleanup_final_routes(routes, depot, args.road_factor)

    preassignment_summary = {
        "depot": {"lat": depot[0], "lon": depot[1]},
        "fleet_source": fleet_source,
        "job_count": len(jobs),
        "area_count": len(area_summaries),
        "total_job_weight_kg": round(sum(job.weight for job in jobs), 2),
        "total_fleet_capacity_kg": sum(vehicle.capacity for vehicle in vehicles_for_routing),
        "vehicle_specs": vehicle_specs,
        "redistributed_area_ids": redistribute_area_ids_arg,
        "area_to_vehicle": area_to_vehicle,
        "candidate_vehicle_map": candidate_map,
        "job_territory_map": territory_map,
        **solver_diagnostics,
    }
    optimized_summary = route_summary(routes, unassigned, vehicle_limits)
    optimized_summary["underutilized_debug_report"] = build_underutilized_debug_report(
        routes,
        vehicles_for_routing,
        depot,
        territory_map,
        args.spillover_penalty,
        args.non_flexible_penalty,
        args.road_factor,
        args.flexible_distance_km,
    )
    output_warnings: List[str] = []

    # Export machine-readable artifacts first, then human-facing CSV/maps.
    write_json(preassignment_summary, output_dir / "preassignment_summary.json")
    write_json(optimized_summary, output_dir / "optimized_routes.json")

    try:
        rendering.write_routes_csv(routes, output_dir / "optimized_routes.csv")
    except Exception as exc:
        output_warnings.append(f"Failed to write optimized_routes.csv: {exc}")

    try:
        rendering.write_unassigned_csv(unassigned, output_dir / "unassigned_jobs.csv", drop_penalty)
    except Exception as exc:
        output_warnings.append(f"Failed to write unassigned_jobs.csv: {exc}")

    try:
        rendering.render_routes_png(routes, depot, unassigned, output_dir / "routes.png")
    except Exception as exc:
        output_warnings.append(f"Failed to render routes.png: {exc}")

    try:
        rendering.render_leaflet_routes_html(
            jobs=jobs,
            routes=routes,
            depot=depot,
            unassigned=unassigned,
            area_summaries=area_summaries,
            output_path=output_dir / "routes_map.html",
            drop_penalty_fn=drop_penalty,
            vehicle_limits=vehicle_limits,
            vehicle_diagnostics=optimized_summary["vehicle_diagnostics"],
            vehicle_diagnostic_summary=optimized_summary["vehicle_diagnostic_summary"],
            underutilized_debug_report=optimized_summary["underutilized_debug_report"],
        )
    except Exception as exc:
        output_warnings.append(f"Failed to render routes_map.html: {exc}")

    try:
        rendering.render_leaflet_unassigned_html(
            unassigned=unassigned,
            depot=depot,
            area_summaries=area_summaries,
            output_path=output_dir / "unassigned_map.html",
            drop_penalty_fn=drop_penalty,
        )
    except Exception as exc:
        output_warnings.append(f"Failed to render unassigned_map.html: {exc}")

    if output_warnings:
        preassignment_summary["output_warnings"] = output_warnings
        write_json(preassignment_summary, output_dir / "preassignment_summary.json")

    print(f"Input jobs: {len(jobs)}")
    print(f"Areas: {len(area_summaries)}")
    print(f"Vehicles: {len(vehicles_for_routing)}")
    print(f"Vehicles used: {optimized_summary['vehicle_count_used']}")
    print(f"Total stops: {optimized_summary['total_stops']}")
    print(f"Total distance km: {optimized_summary['total_distance_km']}")
    print(f"Unassigned jobs: {optimized_summary['unassigned_job_count']}")
    print(f"Unassigned weight kg: {optimized_summary['unassigned_total_weight_kg']}")
    summary = optimized_summary["vehicle_diagnostic_summary"]
    print(
        "Vehicle utilization summary: "
        f"balanced={summary['balanced']}, "
        f"stop_bound={summary['stop_bound']}, "
        f"weight_bound={summary['weight_bound']}, "
        f"underutilized={summary['underutilized']}, "
        f"unused={summary['unused']}"
    )
    print("Vehicle diagnostics:")
    for item in optimized_summary["vehicle_diagnostics"]:
        print(
            "  "
            f"V{item['vehicle_id']}: "
            f"load {item['used_capacity_kg']}/{item['capacity_kg']} kg "
            f"({item['capacity_utilization_pct']}%), "
            f"stops {item['used_stops']}/{item['max_stops']} "
            f"({item['stop_utilization_pct']}%), "
            f"status={item['utilization_status']}"
        )
    if optimized_summary["underutilized_debug_report"]:
        print("Underutilized debug report:")
        for vehicle_id, examples in optimized_summary["underutilized_debug_report"].items():
            print(f"  V{vehicle_id}:")
            for example in examples:
                if example["candidate_type"] == "transfer":
                    print(
                        "    "
                        f"transfer from V{example['from_vehicle_id']} "
                        f"job {example['job_id']} ({example['job_name']}), "
                        f"weight={example['weight']} kg, "
                        f"distance={example['distance_km']} km, "
                        f"penalty={example['territory_penalty']}, "
                        f"score={example['move_score']}, "
                        f"reason={example['reason']}"
                    )
                else:
                    print(
                        "    "
                        f"swap with V{example['from_vehicle_id']} "
                        f"out {example['out_job_id']} ({example['out_job_name']}), "
                        f"in {example['in_job_id']} ({example['in_job_name']}), "
                        f"distance={example['distance_km']} km, "
                        f"penalty={example['territory_penalty']}, "
                        f"score={example['move_score']}, "
                        f"reason={example['reason']}"
                    )
    if output_warnings:
        print("Output warnings:")
        for warning in output_warnings:
            print(f"- {warning}")
    print(f"Artifacts written to: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
