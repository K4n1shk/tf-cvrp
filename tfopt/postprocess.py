from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from models import Coordinate, RouteStop, Vehicle, VehicleRoute
from tfopt.geo import haversine_km, route_distance_for_stops


def optimize_stop_order_exact(
    stops: Sequence[RouteStop],
    start_location: Coordinate,
    road_factor: float,
) -> List[RouteStop]:
    """Find the shortest open-route order for a small stop set."""
    stop_count = len(stops)
    locations = [(stop.lat, stop.lon) for stop in stops]
    start_distances = [
        haversine_km(start_location, location) * road_factor
        for location in locations
    ]
    stop_distances = [
        [
            haversine_km(from_location, to_location) * road_factor
            for to_location in locations
        ]
        for from_location in locations
    ]

    best_distance: Dict[Tuple[int, int], float] = {}
    parent_stop: Dict[Tuple[int, int], int] = {}
    for stop_index, start_distance in enumerate(start_distances):
        best_distance[(1 << stop_index, stop_index)] = start_distance

    for visited_mask in range(1, 1 << stop_count):
        for last_stop_index in range(stop_count):
            current_key = (visited_mask, last_stop_index)
            if current_key not in best_distance:
                continue
            for next_stop_index in range(stop_count):
                if visited_mask & (1 << next_stop_index):
                    continue
                next_mask = visited_mask | (1 << next_stop_index)
                next_key = (next_mask, next_stop_index)
                candidate_distance = (
                    best_distance[current_key]
                    + stop_distances[last_stop_index][next_stop_index]
                )
                if candidate_distance < best_distance.get(next_key, float("inf")):
                    best_distance[next_key] = candidate_distance
                    parent_stop[next_key] = last_stop_index

    full_mask = (1 << stop_count) - 1
    last_stop_index = min(
        range(stop_count),
        key=lambda stop_index: best_distance.get((full_mask, stop_index), float("inf")),
    )
    ordered_indices: List[int] = []
    visited_mask = full_mask
    while True:
        ordered_indices.append(last_stop_index)
        current_key = (visited_mask, last_stop_index)
        if current_key not in parent_stop:
            break
        previous_stop_index = parent_stop[current_key]
        visited_mask &= ~(1 << last_stop_index)
        last_stop_index = previous_stop_index

    return [stops[index] for index in reversed(ordered_indices)]


def optimize_stop_order(
    stops: Sequence[RouteStop],
    start_location: Coordinate,
    road_factor: float,
) -> List[RouteStop]:
    """Build a compact open-route order after local stop transfers or swaps."""
    remaining = list(stops)
    if len(remaining) <= 2:
        return remaining
    if len(remaining) <= 12:
        return optimize_stop_order_exact(remaining, start_location, road_factor)

    ordered: List[RouteStop] = []
    current_location = start_location
    while remaining:
        next_stop = min(
            remaining,
            key=lambda stop: haversine_km(current_location, (stop.lat, stop.lon)),
        )
        ordered.append(next_stop)
        remaining.remove(next_stop)
        current_location = (next_stop.lat, next_stop.lon)

    improved = True
    while improved:
        improved = False
        best_distance = route_distance_for_stops(ordered, start_location, road_factor)
        for start_index in range(len(ordered) - 1):
            for end_index in range(start_index + 2, len(ordered) + 1):
                candidate = (
                    ordered[:start_index]
                    + list(reversed(ordered[start_index:end_index]))
                    + ordered[end_index:]
                )
                candidate_distance = route_distance_for_stops(candidate, start_location, road_factor)
                if candidate_distance + 0.001 < best_distance:
                    ordered = candidate
                    best_distance = candidate_distance
                    improved = True
                    break
            if improved:
                break

    return ordered


def rebuild_route(
    vehicle_id: int,
    stops: Sequence[RouteStop],
    start_location: Coordinate,
    road_factor: float,
    optimize_order: bool = True,
) -> VehicleRoute:
    """Rebuild a route after local stop moves, resequencing and recomputing totals."""
    ordered_stops = (
        optimize_stop_order(stops, start_location, road_factor)
        if optimize_order
        else list(stops)
    )
    resequenced_stops = [
        RouteStop(
            sequence=index + 1,
            job_id=stop.job_id,
            name=stop.name,
            area_id=stop.area_id,
            lat=stop.lat,
            lon=stop.lon,
            weight=stop.weight,
        )
        for index, stop in enumerate(ordered_stops)
    ]
    route_distance = route_distance_for_stops(resequenced_stops, start_location, road_factor)

    return VehicleRoute(
        vehicle_id=vehicle_id,
        distance_km=round(route_distance, 2),
        load_kg=round(sum(stop.weight for stop in resequenced_stops), 2),
        stop_count=len(resequenced_stops),
        stops=resequenced_stops,
    )


def cleanup_final_routes(
    routes: Sequence[VehicleRoute],
    depot: Coordinate,
    road_factor: float,
) -> List[VehicleRoute]:
    """Resequence every final route and recompute totals before export."""
    return [
        rebuild_route(
            vehicle_id=route.vehicle_id,
            stops=route.stops,
            start_location=depot,
            road_factor=road_factor,
        )
        for route in routes
    ]


def preferred_vehicle_ids_for_stop(
    stop: RouteStop,
    territory_map: Dict[str, Dict[str, object]],
) -> List[int]:
    """Return primary and nearby vehicles for a stop, preserving priority order."""
    territory = territory_map.get(stop.job_id)
    if territory is None:
        return []

    preferred_ids = [int(territory["primary_vehicle_id"])]
    preferred_ids.extend(int(vehicle_id) for vehicle_id in territory["nearby_vehicle_ids"])
    return preferred_ids


def compact_routes_by_distance(
    routes: Sequence[VehicleRoute],
    vehicles: Sequence[Vehicle],
    depot: Coordinate,
    territory_map: Dict[str, Dict[str, object]],
    road_factor: float,
    nearby_threshold_km: float,
    max_iterations: int = 25,
    min_improvement_km: float = 0.25,
) -> Tuple[List[VehicleRoute], int]:
    """Greedily move or swap stops when preferred-vehicle changes shorten routes."""
    vehicle_by_id = {vehicle.vehicle_id: vehicle for vehicle in vehicles}
    route_by_vehicle_id = {route.vehicle_id: route for route in routes}
    moved_stop_count = 0

    for _ in range(max_iterations):
        best_move: Optional[Tuple[str, float, int, int, object]] = None
        route_points_by_vehicle_id = {
            route.vehicle_id: route_reference_points(route, depot)
            for route in route_by_vehicle_id.values()
        }

        for source_route in route_by_vehicle_id.values():
            if not source_route.stops:
                continue

            for source_index, source_stop in enumerate(source_route.stops):
                preferred_vehicle_ids = set(preferred_vehicle_ids_for_stop(source_stop, territory_map))
                nearby_vehicle_ids = {
                    route.vehicle_id
                    for route in route_by_vehicle_id.values()
                    if route.vehicle_id != source_route.vehicle_id
                    and route.stops
                    and min_distance_to_route_points(
                        route_points_by_vehicle_id[route.vehicle_id],
                        (source_stop.lat, source_stop.lon),
                    )
                    <= nearby_threshold_km
                }

                for target_vehicle_id in sorted(preferred_vehicle_ids | nearby_vehicle_ids):
                    if target_vehicle_id == source_route.vehicle_id:
                        continue

                    target_route = route_by_vehicle_id.get(target_vehicle_id)
                    target_vehicle = vehicle_by_id.get(target_vehicle_id)
                    source_vehicle = vehicle_by_id[source_route.vehicle_id]
                    if target_route is None or target_vehicle is None:
                        continue

                    source_stops_after_move = [
                        stop for index, stop in enumerate(source_route.stops) if index != source_index
                    ]
                    if (
                        target_route.load_kg + source_stop.weight <= target_vehicle.capacity
                        and target_route.stop_count + 1 <= target_vehicle.max_stops
                    ):
                        new_source_route = rebuild_route(
                            source_route.vehicle_id,
                            source_stops_after_move,
                            depot,
                            road_factor,
                            optimize_order=False,
                        )
                        new_target_route = rebuild_route(
                            target_vehicle_id,
                            list(target_route.stops) + [source_stop],
                            depot,
                            road_factor,
                            optimize_order=False,
                        )
                        distance_delta = (
                            new_source_route.distance_km
                            + new_target_route.distance_km
                            - source_route.distance_km
                            - target_route.distance_km
                        )
                        if distance_delta < -min_improvement_km and (
                            best_move is None or distance_delta < best_move[1]
                        ):
                            best_move = (
                                "transfer",
                                distance_delta,
                                source_route.vehicle_id,
                                target_vehicle_id,
                                source_index,
                            )

                    for target_index, target_stop in enumerate(target_route.stops):
                        if (
                            source_route.load_kg - source_stop.weight + target_stop.weight
                            > source_vehicle.capacity
                        ):
                            continue
                        if (
                            target_route.load_kg - target_stop.weight + source_stop.weight
                            > target_vehicle.capacity
                        ):
                            continue

                        new_source_stops = list(source_route.stops)
                        new_target_stops = list(target_route.stops)
                        new_source_stops[source_index] = target_stop
                        new_target_stops[target_index] = source_stop
                        new_source_route = rebuild_route(
                            source_route.vehicle_id,
                            new_source_stops,
                            depot,
                            road_factor,
                            optimize_order=False,
                        )
                        new_target_route = rebuild_route(
                            target_vehicle_id,
                            new_target_stops,
                            depot,
                            road_factor,
                            optimize_order=False,
                        )
                        distance_delta = (
                            new_source_route.distance_km
                            + new_target_route.distance_km
                            - source_route.distance_km
                            - target_route.distance_km
                        )
                        if distance_delta < -min_improvement_km and (
                            best_move is None or distance_delta < best_move[1]
                        ):
                            best_move = (
                                "swap",
                                distance_delta,
                                source_route.vehicle_id,
                                target_vehicle_id,
                                (source_index, target_index),
                            )

        if best_move is None:
            break

        move_type, _, source_vehicle_id, target_vehicle_id, move_payload = best_move
        source_route = route_by_vehicle_id[source_vehicle_id]
        target_route = route_by_vehicle_id[target_vehicle_id]
        if move_type == "transfer":
            source_index = int(move_payload)
            moved_stop = source_route.stops[source_index]
            route_by_vehicle_id[source_vehicle_id] = rebuild_route(
                source_vehicle_id,
                [
                    stop
                    for index, stop in enumerate(source_route.stops)
                    if index != source_index
                ],
                depot,
                road_factor,
            )
            route_by_vehicle_id[target_vehicle_id] = rebuild_route(
                target_vehicle_id,
                list(target_route.stops) + [moved_stop],
                depot,
                road_factor,
            )
            moved_stop_count += 1
        else:
            source_index, target_index = move_payload
            source_stops = list(source_route.stops)
            target_stops = list(target_route.stops)
            source_stops[source_index], target_stops[target_index] = (
                target_stops[target_index],
                source_stops[source_index],
            )
            route_by_vehicle_id[source_vehicle_id] = rebuild_route(
                source_vehicle_id,
                source_stops,
                depot,
                road_factor,
            )
            route_by_vehicle_id[target_vehicle_id] = rebuild_route(
                target_vehicle_id,
                target_stops,
                depot,
                road_factor,
            )
            moved_stop_count += 2

    return [route_by_vehicle_id[route.vehicle_id] for route in routes], moved_stop_count


def apply_compaction_cleanup(
    routes: Sequence[VehicleRoute],
    vehicles: Sequence[Vehicle],
    depot: Coordinate,
    territory_map: Dict[str, Dict[str, object]],
    road_factor: float,
    nearby_threshold_km: float,
    diagnostic_flags: Dict[str, object],
) -> List[VehicleRoute]:
    """Apply final distance compaction and record whether it changed assignments."""
    compacted_routes, compacted_stop_count = compact_routes_by_distance(
        routes,
        vehicles,
        depot,
        territory_map,
        road_factor,
        nearby_threshold_km,
    )
    if compacted_stop_count > 0:
        diagnostic_flags["used_compaction_pass"] = True
        diagnostic_flags["compacted_stop_count"] = (
            int(diagnostic_flags["compacted_stop_count"]) + compacted_stop_count
        )
    return compacted_routes


def route_utilization_deficit(route: VehicleRoute, vehicle: Vehicle) -> float:
    """Score how far a route is from the current underutilization thresholds."""
    capacity_utilization_pct = (route.load_kg / vehicle.capacity) * 100 if vehicle.capacity else 0.0
    stop_utilization_pct = (route.stop_count / vehicle.max_stops) * 100 if vehicle.max_stops else 0.0
    return max(0.0, 85.0 - capacity_utilization_pct) + max(0.0, 85.0 - stop_utilization_pct)


def vehicle_move_penalty(
    job_id: str,
    vehicle_id: int,
    territory_map: Dict[str, Dict[str, object]],
    spillover_penalty: float,
    non_flexible_penalty: float,
) -> float:
    """Return the territory penalty for assigning a given job to a vehicle."""
    territory = territory_map.get(job_id)
    if territory is None:
        return non_flexible_penalty
    if vehicle_id == int(territory["primary_vehicle_id"]):
        return 0.0
    if vehicle_id in {int(item) for item in territory["nearby_vehicle_ids"]}:
        return spillover_penalty
    return non_flexible_penalty


def rebalance_move_penalty(
    job_id: str,
    vehicle_id: int,
    territory_map: Dict[str, Dict[str, object]],
    spillover_penalty: float,
    non_flexible_penalty: float,
) -> float:
    """Ignore territory penalties during local utilization rebalancing."""
    del job_id, vehicle_id, territory_map, spillover_penalty, non_flexible_penalty
    return 0.0


def route_reference_points(route: VehicleRoute, depot: Coordinate) -> List[Coordinate]:
    """Return depot plus all stop coordinates so proximity can consider the whole route footprint."""
    return [depot] + [(stop.lat, stop.lon) for stop in route.stops]


def min_distance_to_route_points(route_points: Sequence[Coordinate], point: Coordinate) -> float:
    """Return the minimum haversine distance from a point to any route reference point."""
    return min(haversine_km(route_point, point) for route_point in route_points)


def debug_reason_rank(reason: str) -> int:
    """Order debug examples by actionability before distance."""
    rank_by_reason = {
        "transfer_feasible": 0,
        "swap_feasible": 0,
        "territory_penalty_too_high": 1,
        "no_transfer_distance_gain": 2,
        "no_swap_distance_gain": 2,
        "no_transfer_deficit_gain": 3,
        "no_swap_deficit_gain": 3,
        "too_heavy": 2,
        "swap_weight_infeasible": 2,
        "too_far": 4,
    }
    return rank_by_reason.get(reason, 4)


def build_stop_transfer_candidates(
    donor_route: VehicleRoute,
    remaining_capacity: float,
    remaining_stops: int,
    receiver_points: Sequence[Coordinate],
    receiver_vehicle_id: int,
    territory_map: Dict[str, Dict[str, object]],
    spillover_penalty: float,
    non_flexible_penalty: float,
    nearby_threshold_km: float,
) -> List[Tuple[List[int], List[RouteStop], float]]:
    """Build one-stop and small multi-stop transfer bundles from a donor route."""
    nearby_candidates: List[Tuple[int, RouteStop, float, float]] = []

    for stop_index, stop in enumerate(donor_route.stops):
        territory_penalty = rebalance_move_penalty(
            stop.job_id,
            receiver_vehicle_id,
            territory_map,
            spillover_penalty,
            non_flexible_penalty,
        )

        distance_to_receiver = min_distance_to_route_points(receiver_points, (stop.lat, stop.lon))
        if distance_to_receiver > nearby_threshold_km:
            continue

        nearby_candidates.append((stop_index, stop, territory_penalty, distance_to_receiver))

    transfer_candidates: List[Tuple[List[int], List[RouteStop], float]] = []
    for stop_index, stop, territory_penalty, _ in nearby_candidates:
        if stop.weight <= remaining_capacity and remaining_stops >= 1:
            transfer_candidates.append(([stop_index], [stop], territory_penalty))

    if remaining_stops < 2:
        return transfer_candidates

    ordered_nearby = sorted(nearby_candidates, key=lambda item: item[3])
    for first_pos in range(len(ordered_nearby)):
        first_index, first_stop, first_penalty, _ = ordered_nearby[first_pos]
        for second_pos in range(first_pos + 1, len(ordered_nearby)):
            second_index, second_stop, second_penalty, _ = ordered_nearby[second_pos]
            combined_weight = first_stop.weight + second_stop.weight
            if combined_weight > remaining_capacity:
                continue
            pair_distance = haversine_km(
                (first_stop.lat, first_stop.lon),
                (second_stop.lat, second_stop.lon),
            )
            if pair_distance > nearby_threshold_km:
                continue
            transfer_candidates.append(
                (
                    sorted([first_index, second_index]),
                    [first_stop, second_stop],
                    first_penalty + second_penalty,
                )
            )

    return transfer_candidates


def build_stop_swap_candidates(
    receiver_route: VehicleRoute,
    donor_route: VehicleRoute,
    receiver_vehicle: Vehicle,
    donor_vehicle: Vehicle,
    receiver_points: Sequence[Coordinate],
    donor_points: Sequence[Coordinate],
    territory_map: Dict[str, Dict[str, object]],
    spillover_penalty: float,
    non_flexible_penalty: float,
    nearby_threshold_km: float,
) -> List[Tuple[int, int, RouteStop, RouteStop, float]]:
    """Build feasible one-for-one stop swaps between two routes."""
    swap_candidates: List[Tuple[int, int, RouteStop, RouteStop, float]] = []

    for receiver_index, receiver_stop in enumerate(receiver_route.stops):
        for donor_index, donor_stop in enumerate(donor_route.stops):
            if min_distance_to_route_points(receiver_points, (donor_stop.lat, donor_stop.lon)) > nearby_threshold_km:
                continue
            if min_distance_to_route_points(donor_points, (receiver_stop.lat, receiver_stop.lon)) > nearby_threshold_km:
                continue

            new_receiver_load = receiver_route.load_kg - receiver_stop.weight + donor_stop.weight
            new_donor_load = donor_route.load_kg - donor_stop.weight + receiver_stop.weight
            if new_receiver_load > receiver_vehicle.capacity or new_donor_load > donor_vehicle.capacity:
                continue

            receiver_penalty = rebalance_move_penalty(
                donor_stop.job_id,
                receiver_route.vehicle_id,
                territory_map,
                spillover_penalty,
                non_flexible_penalty,
            )
            donor_penalty = rebalance_move_penalty(
                receiver_stop.job_id,
                donor_route.vehicle_id,
                territory_map,
                spillover_penalty,
                non_flexible_penalty,
            )

            swap_candidates.append(
                (
                    receiver_index,
                    donor_index,
                    receiver_stop,
                    donor_stop,
                    receiver_penalty + donor_penalty,
                )
            )

    return swap_candidates


def rebalance_nearby_stops(
    routes: Sequence[VehicleRoute],
    vehicles: Sequence[Vehicle],
    depot: Coordinate,
    territory_map: Dict[str, Dict[str, object]],
    spillover_penalty: float,
    non_flexible_penalty: float,
    road_factor: float,
    nearby_threshold_km: float,
) -> Tuple[List[VehicleRoute], int]:
    """Greedily move nearby assigned stops or stop bundles onto underutilized vehicles."""
    vehicle_by_id = {vehicle.vehicle_id: vehicle for vehicle in vehicles}
    route_by_vehicle_id = {route.vehicle_id: route for route in routes}
    moved_stop_count = 0

    while True:
        best_move: Optional[Tuple[str, float, int, int, object]] = None

        for receiver_route in route_by_vehicle_id.values():
            receiver_vehicle = vehicle_by_id[receiver_route.vehicle_id]
            receiver_deficit = route_utilization_deficit(receiver_route, receiver_vehicle)
            if receiver_deficit <= 0:
                continue

            remaining_capacity = receiver_vehicle.capacity - receiver_route.load_kg
            remaining_stops = receiver_vehicle.max_stops - receiver_route.stop_count
            if remaining_capacity <= 0 or remaining_stops <= 0:
                continue

            receiver_points = route_reference_points(receiver_route, depot)

            for donor_route in route_by_vehicle_id.values():
                if donor_route.vehicle_id == receiver_route.vehicle_id or not donor_route.stops:
                    continue

                donor_vehicle = vehicle_by_id[donor_route.vehicle_id]
                donor_deficit = route_utilization_deficit(donor_route, donor_vehicle)
                donor_points = route_reference_points(donor_route, depot)
                transfer_candidates = build_stop_transfer_candidates(
                    donor_route,
                    remaining_capacity,
                    remaining_stops,
                    receiver_points,
                    receiver_route.vehicle_id,
                    territory_map,
                    spillover_penalty,
                    non_flexible_penalty,
                    nearby_threshold_km,
                )

                for stop_indexes, moved_stops, receiver_penalty in transfer_candidates:
                    donor_stops = [
                        stop for index, stop in enumerate(donor_route.stops) if index not in set(stop_indexes)
                    ]
                    receiver_stops = list(receiver_route.stops) + moved_stops
                    new_donor_route = rebuild_route(
                        donor_route.vehicle_id,
                        donor_stops,
                        depot,
                        road_factor,
                        optimize_order=False,
                    )
                    new_receiver_route = rebuild_route(
                        receiver_route.vehicle_id,
                        receiver_stops,
                        depot,
                        road_factor,
                        optimize_order=False,
                    )

                    new_receiver_deficit = route_utilization_deficit(new_receiver_route, receiver_vehicle)
                    new_donor_deficit = route_utilization_deficit(new_donor_route, donor_vehicle)
                    deficit_improvement = (
                        (receiver_deficit + donor_deficit) - (new_receiver_deficit + new_donor_deficit)
                    )
                    distance_delta = (
                        new_receiver_route.distance_km
                        + new_donor_route.distance_km
                        - receiver_route.distance_km
                        - donor_route.distance_km
                    )
                    move_score = deficit_improvement - distance_delta - receiver_penalty

                    if deficit_improvement <= 0 or move_score <= 0:
                        continue
                    if best_move is None or move_score > best_move[1]:
                        best_move = (
                            "transfer",
                            move_score,
                            receiver_route.vehicle_id,
                            donor_route.vehicle_id,
                            stop_indexes,
                        )

                swap_candidates = build_stop_swap_candidates(
                    receiver_route,
                    donor_route,
                    receiver_vehicle,
                    donor_vehicle,
                    receiver_points,
                    donor_points,
                    territory_map,
                    spillover_penalty,
                    non_flexible_penalty,
                    nearby_threshold_km,
                )

                for receiver_index, donor_index, receiver_stop, donor_stop, swap_penalty in swap_candidates:
                    receiver_stops = list(receiver_route.stops)
                    donor_stops = list(donor_route.stops)
                    receiver_stops[receiver_index] = donor_stop
                    donor_stops[donor_index] = receiver_stop
                    new_receiver_route = rebuild_route(
                        receiver_route.vehicle_id,
                        receiver_stops,
                        depot,
                        road_factor,
                        optimize_order=False,
                    )
                    new_donor_route = rebuild_route(
                        donor_route.vehicle_id,
                        donor_stops,
                        depot,
                        road_factor,
                        optimize_order=False,
                    )

                    new_receiver_deficit = route_utilization_deficit(new_receiver_route, receiver_vehicle)
                    new_donor_deficit = route_utilization_deficit(new_donor_route, donor_vehicle)
                    deficit_improvement = (
                        (receiver_deficit + donor_deficit) - (new_receiver_deficit + new_donor_deficit)
                    )
                    distance_delta = (
                        new_receiver_route.distance_km
                        + new_donor_route.distance_km
                        - receiver_route.distance_km
                        - donor_route.distance_km
                    )
                    move_score = deficit_improvement - distance_delta - swap_penalty

                    if deficit_improvement <= 0 or move_score <= 0:
                        continue
                    if best_move is None or move_score > best_move[1]:
                        best_move = (
                            "swap",
                            move_score,
                            receiver_route.vehicle_id,
                            donor_route.vehicle_id,
                            (receiver_index, donor_index),
                        )

        if best_move is None:
            break

        move_type, _, receiver_vehicle_id, donor_vehicle_id, move_payload = best_move
        receiver_route = route_by_vehicle_id[receiver_vehicle_id]
        donor_route = route_by_vehicle_id[donor_vehicle_id]
        if move_type == "transfer":
            stop_indexes = move_payload
            stop_index_set = set(stop_indexes)
            moved_stops = [
                stop for index, stop in enumerate(donor_route.stops) if index in stop_index_set
            ]
            donor_stops = [
                stop for index, stop in enumerate(donor_route.stops) if index not in stop_index_set
            ]
            receiver_stops = list(receiver_route.stops) + moved_stops
            route_by_vehicle_id[receiver_vehicle_id] = rebuild_route(
                receiver_vehicle_id,
                receiver_stops,
                depot,
                road_factor,
            )
            route_by_vehicle_id[donor_vehicle_id] = rebuild_route(
                donor_vehicle_id,
                donor_stops,
                depot,
                road_factor,
            )
            moved_stop_count += len(moved_stops)
        else:
            receiver_index, donor_index = move_payload
            receiver_stops = list(receiver_route.stops)
            donor_stops = list(donor_route.stops)
            receiver_stops[receiver_index], donor_stops[donor_index] = (
                donor_stops[donor_index],
                receiver_stops[receiver_index],
            )
            route_by_vehicle_id[receiver_vehicle_id] = rebuild_route(
                receiver_vehicle_id,
                receiver_stops,
                depot,
                road_factor,
            )
            route_by_vehicle_id[donor_vehicle_id] = rebuild_route(
                donor_vehicle_id,
                donor_stops,
                depot,
                road_factor,
            )
            moved_stop_count += 2

    return [route_by_vehicle_id[route.vehicle_id] for route in routes], moved_stop_count


def build_underutilized_debug_report(
    routes: Sequence[VehicleRoute],
    vehicles: Sequence[Vehicle],
    depot: Coordinate,
    territory_map: Dict[str, Dict[str, object]],
    spillover_penalty: float,
    non_flexible_penalty: float,
    road_factor: float,
    nearby_threshold_km: float,
    max_examples_per_vehicle: int = 10,
) -> Dict[int, List[Dict[str, object]]]:
    """Explain why nearby reassignment candidates were rejected for underutilized vehicles."""
    vehicle_by_id = {vehicle.vehicle_id: vehicle for vehicle in vehicles}
    debug_report: Dict[int, List[Dict[str, object]]] = {}

    for receiver_route in routes:
        receiver_vehicle = vehicle_by_id[receiver_route.vehicle_id]
        receiver_capacity_pct = (
            (receiver_route.load_kg / receiver_vehicle.capacity) * 100 if receiver_vehicle.capacity else 0.0
        )
        receiver_stops_pct = (
            (receiver_route.stop_count / receiver_vehicle.max_stops) * 100 if receiver_vehicle.max_stops else 0.0
        )
        if not (receiver_capacity_pct < 85 and receiver_stops_pct < 85):
            continue

        receiver_points = route_reference_points(receiver_route, depot)
        remaining_capacity = receiver_vehicle.capacity - receiver_route.load_kg
        remaining_stops = receiver_vehicle.max_stops - receiver_route.stop_count
        explanations: List[Dict[str, object]] = []

        for donor_route in routes:
            if donor_route.vehicle_id == receiver_route.vehicle_id:
                continue
            donor_points = route_reference_points(donor_route, depot)

            for donor_index, donor_stop in enumerate(donor_route.stops):
                reason = ""
                receiver_penalty: Optional[float] = None
                deficit_improvement: Optional[float] = None
                distance_delta: Optional[float] = None
                move_score: Optional[float] = None
                distance_to_receiver = min_distance_to_route_points(receiver_points, (donor_stop.lat, donor_stop.lon))
                if distance_to_receiver > nearby_threshold_km:
                    reason = "too_far"
                elif donor_stop.weight > remaining_capacity:
                    reason = "too_heavy"
                else:
                    receiver_penalty = rebalance_move_penalty(
                        donor_stop.job_id,
                        receiver_route.vehicle_id,
                        territory_map,
                        spillover_penalty,
                        non_flexible_penalty,
                    )
                    donor_stops = [
                        stop for index, stop in enumerate(donor_route.stops) if index != donor_index
                    ]
                    receiver_stops = list(receiver_route.stops) + [donor_stop]
                    new_receiver_route = rebuild_route(
                        receiver_route.vehicle_id,
                        receiver_stops,
                        depot,
                        road_factor,
                        optimize_order=False,
                    )
                    new_donor_route = rebuild_route(
                        donor_route.vehicle_id,
                        donor_stops,
                        depot,
                        road_factor,
                        optimize_order=False,
                    )
                    deficit_improvement = (
                        route_utilization_deficit(receiver_route, receiver_vehicle)
                        + route_utilization_deficit(donor_route, vehicle_by_id[donor_route.vehicle_id])
                        - route_utilization_deficit(new_receiver_route, receiver_vehicle)
                        - route_utilization_deficit(new_donor_route, vehicle_by_id[donor_route.vehicle_id])
                    )
                    distance_delta = (
                        new_receiver_route.distance_km
                        + new_donor_route.distance_km
                        - receiver_route.distance_km
                        - donor_route.distance_km
                    )
                    raw_move_score = deficit_improvement - distance_delta
                    move_score = deficit_improvement - distance_delta - receiver_penalty
                    if deficit_improvement <= 0:
                        reason = "no_transfer_deficit_gain"
                    elif raw_move_score <= 0:
                        reason = "no_transfer_distance_gain"
                    elif move_score <= 0:
                        reason = "territory_penalty_too_high"
                    else:
                        reason = "transfer_feasible"

                explanations.append(
                    {
                        "candidate_type": "transfer",
                        "from_vehicle_id": donor_route.vehicle_id,
                        "job_id": donor_stop.job_id,
                        "job_name": donor_stop.name,
                        "weight": donor_stop.weight,
                        "distance_km": round(distance_to_receiver, 2),
                        "reason": reason,
                        "territory_penalty": receiver_penalty,
                        "deficit_improvement": round(deficit_improvement, 2)
                        if deficit_improvement is not None
                        else None,
                        "distance_delta": round(distance_delta, 2) if distance_delta is not None else None,
                        "move_score": round(move_score, 2) if move_score is not None else None,
                    }
                )

        for donor_route in routes:
            if donor_route.vehicle_id == receiver_route.vehicle_id:
                continue

            donor_vehicle = vehicle_by_id[donor_route.vehicle_id]
            donor_points = route_reference_points(donor_route, depot)
            for receiver_index, receiver_stop in enumerate(receiver_route.stops):
                for donor_index, donor_stop in enumerate(donor_route.stops):

                    receiver_penalty = None
                    donor_penalty = None
                    total_penalty = None
                    deficit_improvement = None
                    distance_delta = None
                    move_score = None
                    distance_to_receiver = min_distance_to_route_points(receiver_points, (donor_stop.lat, donor_stop.lon))
                    distance_to_donor = min_distance_to_route_points(donor_points, (receiver_stop.lat, receiver_stop.lon))
                    if distance_to_receiver > nearby_threshold_km or distance_to_donor > nearby_threshold_km:
                        reason = "too_far"
                    else:
                        new_receiver_load = receiver_route.load_kg - receiver_stop.weight + donor_stop.weight
                        new_donor_load = donor_route.load_kg - donor_stop.weight + receiver_stop.weight
                        if new_receiver_load > receiver_vehicle.capacity or new_donor_load > donor_vehicle.capacity:
                            reason = "swap_weight_infeasible"
                        else:
                            receiver_penalty = rebalance_move_penalty(
                                donor_stop.job_id,
                                receiver_route.vehicle_id,
                                territory_map,
                                spillover_penalty,
                                non_flexible_penalty,
                            )
                            donor_penalty = rebalance_move_penalty(
                                receiver_stop.job_id,
                                donor_route.vehicle_id,
                                territory_map,
                                spillover_penalty,
                                non_flexible_penalty,
                            )
                            total_penalty = receiver_penalty + donor_penalty
                            receiver_stops = list(receiver_route.stops)
                            donor_stops = list(donor_route.stops)
                            receiver_stops[receiver_index] = donor_stop
                            donor_stops[donor_index] = receiver_stop
                            new_receiver_route = rebuild_route(
                                receiver_route.vehicle_id,
                                receiver_stops,
                                depot,
                                road_factor,
                                optimize_order=False,
                            )
                            new_donor_route = rebuild_route(
                                donor_route.vehicle_id,
                                donor_stops,
                                depot,
                                road_factor,
                                optimize_order=False,
                            )
                            deficit_improvement = (
                                route_utilization_deficit(receiver_route, receiver_vehicle)
                                + route_utilization_deficit(donor_route, donor_vehicle)
                                - route_utilization_deficit(new_receiver_route, receiver_vehicle)
                                - route_utilization_deficit(new_donor_route, donor_vehicle)
                            )
                            distance_delta = (
                                new_receiver_route.distance_km
                                + new_donor_route.distance_km
                                - receiver_route.distance_km
                                - donor_route.distance_km
                            )
                            raw_move_score = deficit_improvement - distance_delta
                            move_score = deficit_improvement - distance_delta - total_penalty
                            if deficit_improvement <= 0:
                                reason = "no_swap_deficit_gain"
                            elif raw_move_score <= 0:
                                reason = "no_swap_distance_gain"
                            elif move_score <= 0:
                                reason = "territory_penalty_too_high"
                            else:
                                reason = "swap_feasible"

                    explanations.append(
                        {
                            "candidate_type": "swap",
                            "from_vehicle_id": donor_route.vehicle_id,
                            "out_job_id": receiver_stop.job_id,
                            "out_job_name": receiver_stop.name,
                            "in_job_id": donor_stop.job_id,
                            "in_job_name": donor_stop.name,
                            "distance_km": round(distance_to_receiver, 2),
                            "reason": reason,
                            "territory_penalty": total_penalty,
                            "deficit_improvement": round(deficit_improvement, 2)
                            if deficit_improvement is not None
                            else None,
                            "distance_delta": round(distance_delta, 2) if distance_delta is not None else None,
                            "move_score": round(move_score, 2) if move_score is not None else None,
                        }
                    )

        explanations.sort(
            key=lambda item: (
                debug_reason_rank(str(item["reason"])),
                -(float(item["move_score"]) if item["move_score"] is not None else -float("inf")),
                item["distance_km"],
                item["from_vehicle_id"],
            )
        )
        debug_report[receiver_route.vehicle_id] = explanations[:max_examples_per_vehicle]

    return debug_report
