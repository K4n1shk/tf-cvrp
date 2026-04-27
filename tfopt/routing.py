from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from models import Coordinate, Job, RouteStop, RoutingVehicleState, Vehicle, VehicleRoute
from tfopt.matrices import build_multi_start_distance_matrix
from tfopt.postprocess import apply_compaction_cleanup, rebalance_nearby_stops
from tfopt.scoring import drop_penalty
from tfopt.territory import build_vehicle_penalty_map


def solve_routes_once(
    jobs: Sequence[Job],
    vehicle_states: Sequence[RoutingVehicleState],
    territory_map: Dict[str, Dict[str, object]],
    spillover_penalty: float,
    non_flexible_penalty: float,
    road_factor: float,
    time_limit_seconds: int,
    allow_unrestricted_fallback: bool,
) -> Tuple[Optional[List[VehicleRoute]], List[Job], bool]:
    """Solve one open-route VRP pass for the given vehicle states."""
    if not jobs:
        return ([
            VehicleRoute(
                vehicle_id=vehicle_state.vehicle_id,
                distance_km=0.0,
                load_kg=0.0,
                stop_count=0,
                stops=[],
            )
            for vehicle_state in vehicle_states
        ], [], False)

    starts = list(range(len(vehicle_states)))
    first_job_node = len(vehicle_states)
    first_end_node = first_job_node + len(jobs)
    ends = [first_end_node + idx for idx, _ in enumerate(vehicle_states)]
    manager = pywrapcp.RoutingIndexManager(
        len(vehicle_states) + len(jobs) + len(vehicle_states),
        len(vehicle_states),
        starts,
        ends,
    )
    routing = pywrapcp.RoutingModel(manager)
    matrix = build_multi_start_distance_matrix(
        [vehicle_state.start_location for vehicle_state in vehicle_states],
        jobs,
        road_factor,
        end_count=len(vehicle_states),
    )
    capacities = [vehicle_state.capacity for vehicle_state in vehicle_states]
    max_stops = [vehicle_state.max_stops for vehicle_state in vehicle_states]
    demands = [0 for _ in vehicle_states] + [job.weight_int for job in jobs] + [0 for _ in vehicle_states]
    stop_demands = [0 for _ in vehicle_states] + [1 for _ in jobs] + [0 for _ in vehicle_states]
    vehicle_penalty_map = build_vehicle_penalty_map(
        territory_map=territory_map,
        spillover_penalty=spillover_penalty,
        non_flexible_penalty=non_flexible_penalty,
    )

    def make_transit_callback(vehicle_id: int):
        def transit_callback(from_index: int, to_index: int) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            cost = matrix[from_node][to_node]
            if first_job_node <= to_node < first_end_node:
                job = jobs[to_node - first_job_node]
                cost += vehicle_penalty_map.get(job.job_id, {}).get(vehicle_id, 0)
            return cost

        return transit_callback

    transit_callbacks = []
    for vehicle_idx, vehicle_state in enumerate(vehicle_states):
        transit_callback = make_transit_callback(vehicle_state.vehicle_id)
        transit_callbacks.append(transit_callback)
        transit_index = routing.RegisterTransitCallback(transit_callback)
        routing.SetArcCostEvaluatorOfVehicle(transit_index, vehicle_idx)

    def demand_callback(from_index: int) -> int:
        return demands[manager.IndexToNode(from_index)]

    demand_index = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(demand_index, 0, capacities, True, "Capacity")

    def stop_callback(from_index: int) -> int:
        return stop_demands[manager.IndexToNode(from_index)]

    stop_index = routing.RegisterUnaryTransitCallback(stop_callback)
    routing.AddDimensionWithVehicleCapacity(stop_index, 0, max_stops, True, "Stops")

    for job_offset, job in enumerate(jobs):
        index = manager.NodeToIndex(first_job_node + job_offset)
        routing.AddDisjunction([index], drop_penalty(job))

    search = pywrapcp.DefaultRoutingSearchParameters()
    search.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    search.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search.time_limit.seconds = time_limit_seconds
    solution = routing.SolveWithParameters(search)
    used_unrestricted_fallback = False

    if solution is None and allow_unrestricted_fallback:
        used_unrestricted_fallback = True
        routing = pywrapcp.RoutingModel(manager)
        transit_callbacks = []
        for vehicle_idx, vehicle_state in enumerate(vehicle_states):
            transit_callback = make_transit_callback(vehicle_state.vehicle_id)
            transit_callbacks.append(transit_callback)
            transit_index = routing.RegisterTransitCallback(transit_callback)
            routing.SetArcCostEvaluatorOfVehicle(transit_index, vehicle_idx)
        demand_index = routing.RegisterUnaryTransitCallback(demand_callback)
        routing.AddDimensionWithVehicleCapacity(demand_index, 0, capacities, True, "Capacity")
        stop_index = routing.RegisterUnaryTransitCallback(stop_callback)
        routing.AddDimensionWithVehicleCapacity(stop_index, 0, max_stops, True, "Stops")
        for job_offset, job in enumerate(jobs):
            index = manager.NodeToIndex(first_job_node + job_offset)
            routing.AddDisjunction([index], drop_penalty(job))
        search = pywrapcp.DefaultRoutingSearchParameters()
        search.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
        search.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        search.time_limit.seconds = time_limit_seconds
        solution = routing.SolveWithParameters(search)

    if solution is None:
        return None, list(jobs), used_unrestricted_fallback

    routes: List[VehicleRoute] = []
    unassigned: List[Job] = []
    for vehicle_idx, vehicle_state in enumerate(vehicle_states):
        index = routing.Start(vehicle_idx)
        stops: List[RouteStop] = []
        route_distance = 0
        route_load = 0.0
        sequence = 1

        while not routing.IsEnd(index):
            next_index = solution.Value(routing.NextVar(index))
            from_node = manager.IndexToNode(index)
            to_node = manager.IndexToNode(next_index)
            route_distance += matrix[from_node][to_node]
            if first_job_node <= to_node < first_end_node:
                job = jobs[to_node - first_job_node]
                route_load += job.weight
                stops.append(
                    RouteStop(
                        sequence=sequence,
                        job_id=job.job_id,
                        name=job.name,
                        area_id=job.area_id,
                        lat=job.lat,
                        lon=job.lon,
                        weight=job.weight,
                    )
                )
                sequence += 1
            index = next_index

        routes.append(
            VehicleRoute(
                vehicle_id=vehicle_state.vehicle_id,
                distance_km=round(route_distance / 1000.0, 2),
                load_kg=round(route_load, 2),
                stop_count=len(stops),
                stops=stops,
            )
        )

    for job_offset, job in enumerate(jobs):
        index = manager.NodeToIndex(first_job_node + job_offset)
        if solution.Value(routing.NextVar(index)) == index:
            unassigned.append(job)

    return routes, unassigned, used_unrestricted_fallback


def remaining_vehicle_states(
    vehicles: Sequence[Vehicle],
    routes: Sequence[VehicleRoute],
    depot: Coordinate,
) -> List[RoutingVehicleState]:
    """Convert routed work into residual capacity/stop budgets for repair solving."""
    vehicle_by_id = {vehicle.vehicle_id: vehicle for vehicle in vehicles}
    residual_states: List[RoutingVehicleState] = []

    for route in routes:
        vehicle = vehicle_by_id[route.vehicle_id]
        used_capacity = sum(int(math.ceil(stop.weight)) for stop in route.stops)
        remaining_capacity = max(0, vehicle.capacity - used_capacity)
        remaining_stops = max(0, vehicle.max_stops - route.stop_count)
        if remaining_capacity <= 0 or remaining_stops <= 0:
            continue
        start_location = (route.stops[-1].lat, route.stops[-1].lon) if route.stops else depot
        residual_states.append(
            RoutingVehicleState(
                vehicle_id=vehicle.vehicle_id,
                capacity=remaining_capacity,
                max_stops=remaining_stops,
                start_location=start_location,
            )
        )

    return residual_states


def merge_route_repairs(
    base_routes: Sequence[VehicleRoute],
    repair_routes: Sequence[VehicleRoute],
) -> List[VehicleRoute]:
    """Append repair-pass stops to the first-pass routes without reordering the base plan."""
    repair_by_vehicle_id = {route.vehicle_id: route for route in repair_routes}
    merged_routes: List[VehicleRoute] = []

    for base_route in base_routes:
        repair_route = repair_by_vehicle_id.get(base_route.vehicle_id)
        if not repair_route or not repair_route.stops:
            merged_routes.append(base_route)
            continue

        next_sequence = base_route.stop_count + 1
        appended_stops = [
            RouteStop(
                sequence=next_sequence + offset,
                job_id=stop.job_id,
                name=stop.name,
                area_id=stop.area_id,
                lat=stop.lat,
                lon=stop.lon,
                weight=stop.weight,
            )
            for offset, stop in enumerate(repair_route.stops)
        ]
        merged_routes.append(
            VehicleRoute(
                vehicle_id=base_route.vehicle_id,
                distance_km=round(base_route.distance_km + repair_route.distance_km, 2),
                load_kg=round(base_route.load_kg + repair_route.load_kg, 2),
                stop_count=base_route.stop_count + repair_route.stop_count,
                stops=base_route.stops + appended_stops,
            )
        )

    return merged_routes


def cleanup_solved_routes(
    routes: Sequence[VehicleRoute],
    vehicles: Sequence[Vehicle],
    depot: Coordinate,
    territory_map: Dict[str, Dict[str, object]],
    spillover_penalty: float,
    non_flexible_penalty: float,
    road_factor: float,
    flexible_distance_km: float,
    diagnostic_flags: Dict[str, object],
    max_iterations: int = 10,
) -> List[VehicleRoute]:
    """Alternate utilization rebalancing and distance compaction until stable."""
    cleaned_routes = list(routes)

    for _ in range(max_iterations):
        previous_compacted_stop_count = int(diagnostic_flags["compacted_stop_count"])
        compacted_routes = apply_compaction_cleanup(
            cleaned_routes,
            vehicles,
            depot,
            territory_map,
            road_factor,
            flexible_distance_km,
            diagnostic_flags,
        )
        compacted_stop_count = (
            int(diagnostic_flags["compacted_stop_count"]) - previous_compacted_stop_count
        )

        rebalanced_routes, moved_stop_count = rebalance_nearby_stops(
            compacted_routes,
            vehicles,
            depot,
            territory_map,
            spillover_penalty,
            non_flexible_penalty,
            road_factor,
            flexible_distance_km,
        )
        if moved_stop_count > 0:
            diagnostic_flags["used_rebalance_pass"] = True
            diagnostic_flags["rebalanced_stop_count"] = (
                int(diagnostic_flags["rebalanced_stop_count"]) + moved_stop_count
            )

        cleaned_routes = rebalanced_routes

        if moved_stop_count == 0 and compacted_stop_count == 0:
            break

    return cleaned_routes


def solve_routes(
    jobs: Sequence[Job],
    vehicles: Sequence[Vehicle],
    depot: Coordinate,
    candidate_map: Dict[str, List[int]],
    territory_map: Dict[str, Dict[str, object]],
    spillover_penalty: float,
    non_flexible_penalty: float,
    road_factor: float,
    flexible_distance_km: float,
    time_limit_seconds: int,
) -> Tuple[List[VehicleRoute], List[Job], Dict[str, object]]:
    """Solve in two passes: a territory-aware pass, then a leftover-only repair pass."""
    del candidate_map
    initial_states = [
        RoutingVehicleState(
            vehicle_id=vehicle.vehicle_id,
            capacity=vehicle.capacity,
            max_stops=vehicle.max_stops,
            start_location=depot,
        )
        for vehicle in vehicles
    ]
    diagnostic_flags = {
        "used_relaxed_candidates_in_solver": False,
        "used_unrestricted_fallback_in_solver": False,
        "used_repair_pass": False,
        "used_unrestricted_repair": False,
        "used_rebalance_pass": False,
        "rebalanced_stop_count": 0,
        "used_compaction_pass": False,
        "compacted_stop_count": 0,
    }

    first_pass_routes, first_pass_unassigned, used_unrestricted = solve_routes_once(
        jobs=jobs,
        vehicle_states=initial_states,
        territory_map=territory_map,
        spillover_penalty=spillover_penalty,
        non_flexible_penalty=non_flexible_penalty,
        road_factor=road_factor,
        time_limit_seconds=time_limit_seconds,
        allow_unrestricted_fallback=False,
    )
    if first_pass_routes is None:
        diagnostic_flags["used_relaxed_candidates_in_solver"] = True
        first_pass_routes, first_pass_unassigned, used_unrestricted = solve_routes_once(
            jobs=jobs,
            vehicle_states=initial_states,
            territory_map=territory_map,
            spillover_penalty=spillover_penalty,
            non_flexible_penalty=non_flexible_penalty,
            road_factor=road_factor,
            time_limit_seconds=time_limit_seconds,
            allow_unrestricted_fallback=False,
        )

    if first_pass_routes is None:
        diagnostic_flags["used_unrestricted_fallback_in_solver"] = True
        first_pass_routes, first_pass_unassigned, used_unrestricted = solve_routes_once(
            jobs=jobs,
            vehicle_states=initial_states,
            territory_map=territory_map,
            spillover_penalty=spillover_penalty,
            non_flexible_penalty=non_flexible_penalty,
            road_factor=road_factor,
            time_limit_seconds=time_limit_seconds,
            allow_unrestricted_fallback=True,
        )

    if first_pass_routes is None:
        raise RuntimeError("No feasible route solution found for the provided jobs and vehicles.")

    if used_unrestricted:
        diagnostic_flags["used_unrestricted_fallback_in_solver"] = True

    if not first_pass_unassigned:
        cleaned_routes = cleanup_solved_routes(
            first_pass_routes,
            vehicles,
            depot,
            territory_map,
            spillover_penalty,
            non_flexible_penalty,
            road_factor,
            flexible_distance_km,
            diagnostic_flags,
        )
        return cleaned_routes, [], diagnostic_flags

    residual_states = remaining_vehicle_states(vehicles, first_pass_routes, depot)
    if not residual_states:
        cleaned_routes = cleanup_solved_routes(
            first_pass_routes,
            vehicles,
            depot,
            territory_map,
            spillover_penalty,
            non_flexible_penalty,
            road_factor,
            flexible_distance_km,
            diagnostic_flags,
        )
        return cleaned_routes, first_pass_unassigned, diagnostic_flags

    diagnostic_flags["used_repair_pass"] = True
    repair_routes, repair_unassigned, used_unrestricted_repair = solve_routes_once(
        jobs=first_pass_unassigned,
        vehicle_states=residual_states,
        territory_map=territory_map,
        spillover_penalty=spillover_penalty,
        non_flexible_penalty=non_flexible_penalty,
        road_factor=road_factor,
        time_limit_seconds=max(10, time_limit_seconds // 2),
        allow_unrestricted_fallback=False,
    )

    if repair_routes is None:
        diagnostic_flags["used_unrestricted_repair"] = True
        repair_routes, repair_unassigned, used_unrestricted_repair = solve_routes_once(
            jobs=first_pass_unassigned,
            vehicle_states=residual_states,
            territory_map=territory_map,
            spillover_penalty=spillover_penalty,
            non_flexible_penalty=non_flexible_penalty,
            road_factor=road_factor,
            time_limit_seconds=max(10, time_limit_seconds // 2),
            allow_unrestricted_fallback=True,
        )

    if repair_routes is None:
        cleaned_routes = cleanup_solved_routes(
            first_pass_routes,
            vehicles,
            depot,
            territory_map,
            spillover_penalty,
            non_flexible_penalty,
            road_factor,
            flexible_distance_km,
            diagnostic_flags,
        )
        return cleaned_routes, first_pass_unassigned, diagnostic_flags

    if used_unrestricted_repair:
        diagnostic_flags["used_unrestricted_repair"] = True

    merged_routes = merge_route_repairs(first_pass_routes, repair_routes)
    cleaned_routes = cleanup_solved_routes(
        merged_routes,
        vehicles,
        depot,
        territory_map,
        spillover_penalty,
        non_flexible_penalty,
        road_factor,
        flexible_distance_km,
        diagnostic_flags,
    )
    return cleaned_routes, repair_unassigned, diagnostic_flags
