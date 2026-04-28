#!/usr/bin/env python3
"""Compatibility CLI for the TF route optimization pipeline.
"""

from __future__ import annotations

from tfopt.cli import build_parser, main
from tfopt.fleet import VEHICLE_SPECS_DEFAULT, build_vehicles, parse_vehicle_specs
from tfopt.geo import haversine_km, road_cost_meters, route_distance_for_stops
from tfopt.io import load_jobs, normalize_columns, write_json
from tfopt.matrices import build_distance_matrix, build_multi_start_distance_matrix
from tfopt.postprocess import (
    apply_compaction_cleanup,
    build_underutilized_debug_report,
    build_stop_swap_candidates,
    build_stop_transfer_candidates,
    cleanup_final_routes,
    compact_routes_by_distance,
    debug_reason_rank,
    min_distance_to_route_points,
    optimize_stop_order,
    optimize_stop_order_exact,
    preferred_vehicle_ids_for_stop,
    rebuild_route,
    rebalance_nearby_stops,
    rebalance_move_penalty,
    route_reference_points,
    route_utilization_deficit,
    vehicle_move_penalty,
)
from tfopt.routing import merge_route_repairs, remaining_vehicle_states, solve_routes, solve_routes_once
from tfopt.scoring import drop_penalty
from tfopt.summary import build_vehicle_diagnostics, route_summary, summarize_vehicle_diagnostics
from tfopt.territory import (
    assign_missing_area_ids,
    assign_primary_areas,
    build_candidate_map,
    build_job_territory_map,
    build_vehicle_penalty_map,
    flexible_areas_for_job,
    insertion_detour_km,
    redistribute_area_ids,
    score_vehicle_for_job,
    summarize_areas,
)


if __name__ == "__main__":
    main()
