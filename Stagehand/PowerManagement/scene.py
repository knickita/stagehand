from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import os

import bpy
from mathutils import Quaternion, Vector

from ..AddStagehandObject import ensure_stagehand_link_uid, ensure_stagehand_uid
from ..LinkTypes import StagehandLinkType
from .solver import PowerInputNode, PowerSolver, PowerSolverError, position_key

try:
    import numpy as np
except ImportError:
    np = None


STRUCTURE_RESOLUTION = 0.25
EDGE_RESOLUTION = 0.3
DELTA_EXCESS = 10
MAX_POWER_FOR_LINE = 3000


POWER_INPUT_TYPES = {
    int(StagehandLinkType.POWER_IN_CEE16A_MONO),
    int(StagehandLinkType.POWER_IN_POWERCON_BLUE),
    int(StagehandLinkType.POWER_IN_POWERCON_WHITE),
    int(StagehandLinkType.POWER_IN_POWERCONTRUE),
}

POWER_OUTPUT_TYPES = {
    int(StagehandLinkType.POWER_OUT_CEE16A_MONO),
    int(StagehandLinkType.POWER_OUT_POWERCON_BLUE),
    int(StagehandLinkType.POWER_OUT_POWERCON_WHITE),
    int(StagehandLinkType.POWER_OUT_POWERCONTRUE),
}

POWER_16A_OUTPUT_TYPE = int(StagehandLinkType.POWER_OUT_CEE16A_MONO)
THREEPHASE_POWER_INPUT_TYPE = int(StagehandLinkType.POWER_IN_CEE63A_PENTA)
THREEPHASE_POWER_OUTPUT_TYPE = int(StagehandLinkType.POWER_OUT_CEE63A_PENTA)


@dataclass(frozen=True)
class PowerOutputNode:
    position: tuple
    label: str = ""
    object_uid: str = ""
    link_uid: str = ""
    object_name: str = ""
    link_index: int = -1
    node_id: int = -1


@dataclass(frozen=True)
class PowerLineOutputAssignment:
    line_id: int
    output_index: int
    output_node_id: int
    output_label: str
    output_position: tuple
    output_object_uid: str = ""
    output_link_uid: str = ""
    destination_node_ids: tuple = ()


@dataclass
class PowerGenerationResult:
    solver: PowerSolver
    structure_vertex_count: int
    route_edge_count: int
    power_node_count: int
    starting_label: str
    required_power_lines: int = 0
    available_16a_outputs: int = 0
    missing_16a_outputs: int = 0
    power_line_output_assignments: dict = field(default_factory=dict)
    power_line_routes: dict = field(default_factory=dict)
    power_line_roots: dict = field(default_factory=dict)
    required_threephase_lines: int = 0
    available_threephase_outputs: int = 0
    missing_threephase_outputs: int = 0
    threephase_output_assignments: dict = field(default_factory=dict)
    threephase_routes: dict = field(default_factory=dict)
    threephase_roots: dict = field(default_factory=dict)
    generated_powerline_connections: dict = field(default_factory=dict)
    cable_anchor_offsets: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


def _worker_count(item_count):
    if item_count <= 1:
        return 1
    return max(1, min(os.cpu_count() or 1, item_count))


def _is_stagehand_object(obj):
    stagehand = getattr(obj, "stagehand", None)
    return stagehand is not None and stagehand.is_stagehand_object


def _is_visible_scene_object(obj):
    return obj is not None and not obj.hide_get() and not obj.hide_viewport


def _iter_stagehand_objects():
    for obj in bpy.data.objects:
        if _is_stagehand_object(obj) and _is_visible_scene_object(obj):
            yield obj


def _has_tag(obj, tag):
    target = tag.lower()
    return any(str(tag_item.value).lower() == target for tag_item in obj.stagehand.tags)


def _link_rotation(link):
    return Quaternion((
        link.posDir[6],
        link.posDir[3],
        link.posDir[4],
        link.posDir[5],
    ))


def _link_world_transform(obj, link):
    local_position = Vector(link.posDir[:3])
    local_rotation = _link_rotation(link)
    world_rotation = obj.matrix_world.to_quaternion()
    center = obj.matrix_world.to_translation() + (world_rotation @ local_position)
    rotation = world_rotation @ local_rotation
    return center, rotation


def _link_anchor_for_cables(link):
    return bool(getattr(link, "anchorForCables", False))


def _dimension_offsets(dimension, resolution):
    dimension = max(0.0, float(dimension))
    if dimension <= 0.0:
        return (0.0,)

    offsets = [index * resolution for index in range(int(dimension / resolution) + 1)]
    if not offsets or abs(offsets[-1] - dimension) > 0.000001:
        offsets.append(dimension)
    return tuple(offsets)


def _link_local_structure_points(link, resolution):
    position = Vector(link.posDir[:3])
    rotation = _link_rotation(link)

    if getattr(link, "planeType", False):
        width_direction = rotation @ Vector((1.0, 0.0, 0.0))
        length_direction = rotation @ Vector((0.0, 1.0, 0.0))
        width_offsets = _dimension_offsets(getattr(link, "width", 0.0), resolution)
        length_offsets = _dimension_offsets(getattr(link, "length", 0.0), resolution)
        return [
            position + (width_direction * width_offset) + (length_direction * length_offset)
            for width_offset in width_offsets
            for length_offset in length_offsets
        ]

    direction = rotation @ Vector((0.0, 1.0, 0.0))
    direction *= resolution
    point_count = int(link.length / resolution) + 1
    return [position + (direction * index) for index in range(point_count)]


def _link_outward_direction(obj, link, local_point):
    link_rotation = _link_rotation(link)
    if getattr(link, "planeType", False):
        outward = link_rotation @ Vector((0.0, 0.0, 1.0))
        if outward.length_squared <= 0.0000000001:
            outward = Vector((0.0, 0.0, 1.0))
        outward.normalize()
        return obj.matrix_world.to_quaternion() @ outward

    local_position = Vector(link.posDir[:3])
    local_direction = link_rotation @ Vector((0.0, 1.0, 0.0))
    if local_direction.length_squared > 0.0:
        local_direction.normalize()

    outward = Vector(local_point)
    outward -= local_direction * outward.dot(local_direction)
    if outward.length_squared <= 0.0000000001:
        outward = link_rotation @ Vector((1.0, 0.0, 0.0))
    if outward.length_squared <= 0.0000000001:
        outward = Vector(local_position)
    if outward.length_squared <= 0.0000000001:
        outward = Vector((1.0, 0.0, 0.0))

    outward.normalize()
    return obj.matrix_world.to_quaternion() @ outward


def _record_cable_anchor_offset(offsets_by_key, key, outward_direction, display_radius):
    existing = offsets_by_key.get(key)
    offset_data = {
        "direction": tuple(outward_direction.normalized()),
        "display_radius": max(0.0, float(display_radius)),
    }

    if existing is None or offset_data["display_radius"] > existing["display_radius"]:
        offsets_by_key[key] = offset_data


def _iter_power_obstacles():
    for obj in bpy.data.objects:
        if not _is_visible_scene_object(obj):
            continue
        name = obj.name.lower()
        if (
            obj.get("stagehand_power_obstacle")
            or name.startswith("power obstacle")
            or name.startswith("cable obstacle")
        ):
            yield obj


def _point_inside_obstacle(point, obstacle):
    try:
        local_point = obstacle.matrix_world.inverted() @ Vector(point)
    except ValueError:
        return False
    return (
        abs(local_point.x) <= 0.5
        and abs(local_point.y) <= 0.5
        and abs(local_point.z) <= 0.5
    )


def _collect_structure_vertices(resolution, collect_offsets=False):
    vertices_by_key = {}
    offsets_by_key = {}

    for obj in _iter_stagehand_objects():
        rotation = obj.matrix_world.to_quaternion()
        translation = obj.matrix_world.to_translation()
        for link in obj.stagehand.links:
            if not _link_anchor_for_cables(link):
                continue

            for local_point in _link_local_structure_points(link, resolution):
                world_point = translation + (rotation @ local_point)
                key = position_key(world_point)
                vertices_by_key[key] = tuple(world_point)
                if collect_offsets:
                    _record_cable_anchor_offset(
                        offsets_by_key,
                        key,
                        _link_outward_direction(obj, link, local_point),
                        getattr(link, "displayRadius", 0.0),
                    )

    obstacles = list(_iter_power_obstacles())
    if obstacles:
        blocked_keys = {
            key
            for key, point in vertices_by_key.items()
            if any(_point_inside_obstacle(point, obstacle) for obstacle in obstacles)
        }
        vertices_by_key = {
            key: point
            for key, point in vertices_by_key.items()
            if key not in blocked_keys
        }
        if collect_offsets:
            offsets_by_key = {
                key: offset
                for key, offset in offsets_by_key.items()
                if key not in blocked_keys
            }

    if collect_offsets:
        return list(vertices_by_key.values()), offsets_by_key

    return list(vertices_by_key.values())


def collect_cable_anchor_points(resolution=STRUCTURE_RESOLUTION):
    return _collect_structure_vertices(resolution)


def _bsp_key(point):
    return int(point[0]), int(point[1]), int(point[2])


def _build_bsp_tree(vertices):
    bsp_tree = defaultdict(list)
    for index, vertex in enumerate(vertices):
        bsp_tree[_bsp_key(vertex)].append(index)
    return bsp_tree


def _edge_chunk(args):
    vertices, bsp_tree, squared_edge_resolution, start_index, end_index = args
    edges = []

    for index_a in range(start_index, end_index):
        vertex_a = vertices[index_a]
        base_key = _bsp_key(vertex_a)

        for x_offset in (-1, 0, 1):
            for y_offset in (-1, 0, 1):
                for z_offset in (-1, 0, 1):
                    key = (
                        base_key[0] + x_offset,
                        base_key[1] + y_offset,
                        base_key[2] + z_offset,
                    )
                    for index_b in bsp_tree.get(key, ()):
                        if index_a <= index_b:
                            continue

                        vertex_b = vertices[index_b]
                        dx = vertex_a[0] - vertex_b[0]
                        dy = vertex_a[1] - vertex_b[1]
                        dz = vertex_a[2] - vertex_b[2]
                        if (dx * dx) + (dy * dy) + (dz * dz) <= squared_edge_resolution:
                            edges.append((vertex_a, vertex_b))

    return edges


def _calculate_structure_edges(vertices, edge_resolution):
    if not vertices:
        return []

    bsp_tree = _build_bsp_tree(vertices)
    squared_edge_resolution = edge_resolution * edge_resolution
    worker_count = _worker_count(len(vertices))
    chunk_size = max(1, (len(vertices) + worker_count - 1) // worker_count)
    chunks = [
        (vertices, bsp_tree, squared_edge_resolution, start, min(len(vertices), start + chunk_size))
        for start in range(0, len(vertices), chunk_size)
    ]

    if len(chunks) <= 1:
        return _edge_chunk(chunks[0])

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        edge_chunks = list(executor.map(_edge_chunk, chunks))

    edges = []
    for chunk_edges in edge_chunks:
        edges.extend(chunk_edges)
    return edges


def _power_link_label(obj, link_index):
    return f"{obj.name_full} link {link_index + 1}"


def _iter_power_input_nodes():
    for obj in _iter_stagehand_objects():
        object_uid = ensure_stagehand_uid(obj)
        for link_index, link in enumerate(obj.stagehand.links):
            if int(link.type) not in POWER_INPUT_TYPES:
                continue

            position, _rotation = _link_world_transform(obj, link)
            yield PowerInputNode(
                position=tuple(position),
                consumption=int(obj.stagehand.watt),
                label=_power_link_label(obj, link_index),
                object_uid=object_uid,
                link_uid=ensure_stagehand_link_uid(link),
                object_name=obj.name_full,
                link_index=link_index,
            )


def _iter_power_output_nodes(objects):
    for obj in objects:
        if not _is_stagehand_object(obj) or not _is_visible_scene_object(obj):
            continue
        for link_index, link in enumerate(obj.stagehand.links):
            if int(link.type) not in POWER_OUTPUT_TYPES:
                continue
            position, _rotation = _link_world_transform(obj, link)
            yield tuple(position), _power_link_label(obj, link_index)


def _iter_power_16a_output_nodes():
    for obj in _iter_stagehand_objects():
        object_uid = ensure_stagehand_uid(obj)
        for link_index, link in enumerate(obj.stagehand.links):
            if int(link.type) != POWER_16A_OUTPUT_TYPE:
                continue
            position, _rotation = _link_world_transform(obj, link)
            yield PowerOutputNode(
                position=tuple(position),
                label=_power_link_label(obj, link_index),
                object_uid=object_uid,
                link_uid=ensure_stagehand_link_uid(link),
                object_name=obj.name_full,
                link_index=link_index,
            )


def _iter_threephase_input_nodes():
    for obj in _iter_stagehand_objects():
        object_uid = ensure_stagehand_uid(obj)
        for link_index, link in enumerate(obj.stagehand.links):
            if int(link.type) != THREEPHASE_POWER_INPUT_TYPE:
                continue

            position, _rotation = _link_world_transform(obj, link)
            yield PowerInputNode(
                position=tuple(position),
                consumption=0,
                label=_power_link_label(obj, link_index),
                object_uid=object_uid,
                link_uid=ensure_stagehand_link_uid(link),
                object_name=obj.name_full,
                link_index=link_index,
            )


def _iter_threephase_output_nodes():
    for obj in _iter_stagehand_objects():
        object_uid = ensure_stagehand_uid(obj)
        for link_index, link in enumerate(obj.stagehand.links):
            if int(link.type) != THREEPHASE_POWER_OUTPUT_TYPE:
                continue
            position, _rotation = _link_world_transform(obj, link)
            yield PowerOutputNode(
                position=tuple(position),
                label=_power_link_label(obj, link_index),
                object_uid=object_uid,
                link_uid=ensure_stagehand_link_uid(link),
                object_name=obj.name_full,
                link_index=link_index,
            )


def _selected_stagehand_objects(context):
    return [
        obj
        for obj in getattr(context, "selected_objects", ())
        if _is_stagehand_object(obj) and _is_visible_scene_object(obj)
    ]


def _find_starting_power_node(context):
    selected_objects = _selected_stagehand_objects(context)
    for position, label in _iter_power_output_nodes(selected_objects):
        return position, label

    stagehand_objects = list(_iter_stagehand_objects())
    power_supply_objects = [obj for obj in stagehand_objects if _has_tag(obj, "powersupply")]
    for position, label in _iter_power_output_nodes(power_supply_objects):
        return position, label

    for position, label in _iter_power_output_nodes(stagehand_objects):
        return position, label

    return (0.0, 0.0, 0.0), "default origin"


def _closest_structure_index_numpy(vertex_array, power_position):
    position_array = np.asarray(power_position, dtype=np.float64)
    deltas = vertex_array - position_array
    distances = np.einsum("ij,ij->i", deltas, deltas)
    return int(np.argmin(distances))


def _closest_structure_index_python(structure_vertices, power_position):
    closest_index = 0
    min_distance = float("inf")

    for index, vertex in enumerate(structure_vertices):
        dx = power_position[0] - vertex[0]
        dy = power_position[1] - vertex[1]
        dz = power_position[2] - vertex[2]
        distance = (dx * dx) + (dy * dy) + (dz * dz)
        if distance < min_distance:
            min_distance = distance
            closest_index = index

    return closest_index


def _calculate_power_edges(structure_vertices, power_nodes):
    if not structure_vertices:
        raise PowerSolverError("No cable anchor vertices were found in the scene.")

    edges = []
    if np is not None:
        vertex_array = np.asarray(structure_vertices, dtype=np.float64)
        for power_node in power_nodes:
            closest_vertex = structure_vertices[_closest_structure_index_numpy(vertex_array, power_node.position)]
            edges.append((power_node.position, closest_vertex))
    else:
        for power_node in power_nodes:
            closest_vertex = structure_vertices[_closest_structure_index_python(structure_vertices, power_node.position)]
            edges.append((power_node.position, closest_vertex))
    return edges


def _deduplicate_route_edges(route_edges):
    deduplicated_edges = []
    seen_edges = set()

    for start_position, end_position in route_edges:
        start_key = position_key(start_position)
        end_key = position_key(end_position)
        edge_key = tuple(sorted((start_key, end_key)))
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)
        deduplicated_edges.append((start_position, end_position))

    return deduplicated_edges


def _node_id_for_position(solver, position):
    key = position_key(position)
    node_lookup = getattr(solver, "node_lookup", None)
    if node_lookup is not None:
        node_id = node_lookup.get(key)
        if node_id is not None:
            return node_id

    for node_id, node_position in enumerate(solver.nodes):
        if position_key(node_position) == key:
            return node_id
    return None


def _resolve_power_output_nodes(solver, output_nodes, output_description="16A output"):
    resolved_outputs = []
    for output_node in output_nodes:
        node_id = _node_id_for_position(solver, output_node.position)
        if node_id is None:
            raise PowerSolverError(f"{output_description} is not present in graph: {output_node.label}")
        resolved_outputs.append(PowerOutputNode(
            position=output_node.position,
            label=output_node.label,
            object_uid=output_node.object_uid,
            link_uid=output_node.link_uid,
            object_name=output_node.object_name,
            link_index=output_node.link_index,
            node_id=node_id,
        ))
    return resolved_outputs


def _iter_solver_neighbors(solver, node_id):
    start = solver.edges_index[node_id]
    end = solver.edges_index[node_id + 1]
    yielded = set()
    for edge_index in range(start, end):
        neighbor = solver.edges[edge_index]
        if neighbor in yielded:
            continue
        yielded.add(neighbor)
        yield neighbor


def _bfs_from_node(solver, root_node):
    distances = {root_node: 0}
    parents = {}
    to_explore = deque([root_node])

    while to_explore:
        actual_node = to_explore.popleft()
        next_distance = distances[actual_node] + 1
        for neighbor in _iter_solver_neighbors(solver, actual_node):
            if neighbor in distances:
                continue
            distances[neighbor] = next_distance
            parents[neighbor] = actual_node
            to_explore.append(neighbor)

    return distances, parents


def _power_input_link_uids_by_node(solver, input_power_nodes):
    input_link_uids_by_node = {}
    for power_node in input_power_nodes:
        if not power_node.link_uid:
            continue
        node_id = _node_id_for_position(solver, power_node.position)
        if node_id is not None:
            input_link_uids_by_node[node_id] = power_node.link_uid
    return input_link_uids_by_node


def _generated_powerline_connections(solver, required_line_ids, assignments, input_link_uids_by_node):
    generated_connections = {}
    for line_id in required_line_ids:
        assignment = assignments.get(line_id)
        if assignment is None or not assignment.output_link_uid:
            continue
        destination_nodes = assignment.destination_node_ids or _line_destination_nodes(solver, line_id)
        for destination in destination_nodes:
            input_link_uid = input_link_uids_by_node.get(destination)
            if input_link_uid:
                generated_connections[input_link_uid] = assignment.output_link_uid
    return generated_connections


def _generated_direct_powerline_connections(input_nodes, assignments):
    generated_connections = {}
    for input_index, assignment in assignments.items():
        if input_index >= len(input_nodes) or not assignment.output_link_uid:
            continue
        input_link_uid = input_nodes[input_index].link_uid
        if input_link_uid:
            generated_connections[input_link_uid] = assignment.output_link_uid
    return generated_connections


def _format_power_node_labels(power_nodes, limit=5):
    labels = []
    for power_node in tuple(power_nodes)[:limit]:
        labels.append(power_node.label or power_node.object_name or power_node.link_uid or "unknown input")
    remaining = len(power_nodes) - len(labels)
    if remaining > 0:
        labels.append(f"and {remaining} more")
    return ", ".join(labels)

def _line_destination_nodes(solver, line_id):
    return tuple(sorted(
        node
        for node in solver.power_lines_path.get(line_id, ())
        if node != solver.start_node and node in solver.power_node_consumptions
    ))


def _required_power_line_ids(solver):
    return tuple(sorted(
        line_id
        for line_id, consumption in solver.power_lines_consumption.items()
        if int(consumption) > 0 and _line_destination_nodes(solver, line_id)
    ))


def _line_representative_position(solver, line_id):
    destinations = _line_destination_nodes(solver, line_id)
    if not destinations:
        return Vector((0.0, 0.0, 0.0))

    position = Vector((0.0, 0.0, 0.0))
    for node_id in destinations:
        position += Vector(solver.nodes[node_id])
    position /= len(destinations)
    return position


def _route_edges_from_parent_tree(destination, root_node, distances, parents, route_label):
    if destination not in distances:
        raise PowerSolverError(f"{route_label} cannot reach its assigned output.")

    route_edges = set()
    actual_node = destination
    while actual_node != root_node:
        parent_node = parents.get(actual_node)
        if parent_node is None:
            raise PowerSolverError(f"{route_label} has an incomplete generated route.")
        route_edges.add((parent_node, actual_node))
        actual_node = parent_node
    return route_edges


def _build_power_line_routes(solver, required_line_ids, assignments, bfs_cache):
    power_line_roots = {}
    power_line_routes = {}

    for line_id in required_line_ids:
        assignment = assignments[line_id]
        distances, parents = bfs_cache[assignment.output_index]
        root_node = assignment.output_node_id
        route_edges = set()
        power_line_roots[line_id] = root_node

        destination_nodes = assignment.destination_node_ids or _line_destination_nodes(solver, line_id)
        for destination in destination_nodes:
            route_edges.update(_route_edges_from_parent_tree(
                destination,
                root_node,
                distances,
                parents,
                f"Powerline {line_id}",
            ))

        power_line_routes[line_id] = route_edges

    return power_line_roots, power_line_routes


def _build_direct_power_line_routes(solver, input_node_ids, assignments, bfs_cache):
    power_line_roots = {}
    power_line_routes = {}

    for line_id, assignment in assignments.items():
        destination = input_node_ids.get(line_id)
        if destination is None:
            continue
        distances, parents = bfs_cache[assignment.output_index]
        root_node = assignment.output_node_id
        power_line_roots[line_id] = root_node
        power_line_routes[line_id] = _route_edges_from_parent_tree(
            destination,
            root_node,
            distances,
            parents,
            f"Threephase powerline {line_id}",
        )

    return power_line_roots, power_line_routes


def _assign_power_lines_to_outputs(solver, required_line_ids, output_nodes):
    if not required_line_ids:
        return {}, {}, {}, ()

    resolved_outputs = _resolve_power_output_nodes(solver, output_nodes)
    representatives = {
        line_id: _line_representative_position(solver, line_id)
        for line_id in required_line_ids
    }
    bfs_cache = {}
    candidates = []

    for output_index, output_node in enumerate(resolved_outputs):
        distances, parents = _bfs_from_node(solver, output_node.node_id)
        bfs_cache[output_index] = (distances, parents)
        output_position = Vector(output_node.position)

        for line_id in required_line_ids:
            destinations = _line_destination_nodes(solver, line_id)
            reachable_destinations = tuple(destination for destination in destinations if destination in distances)
            if not reachable_destinations:
                continue
            graph_distance = sum(distances[destination] for destination in reachable_destinations)
            euclidean_distance = (output_position - representatives[line_id]).length_squared
            candidates.append((-len(reachable_destinations), graph_distance, euclidean_distance, line_id, output_index, reachable_destinations))

    assignments = {}
    assigned_lines = set()
    used_outputs = set()

    for _reachable_count, _graph_distance, _euclidean_distance, line_id, output_index, reachable_destinations in sorted(candidates):
        if line_id in assigned_lines or output_index in used_outputs:
            continue

        output_node = resolved_outputs[output_index]
        assignments[line_id] = PowerLineOutputAssignment(
            line_id=line_id,
            output_index=output_index,
            output_node_id=output_node.node_id,
            output_label=output_node.label,
            output_position=output_node.position,
            output_object_uid=output_node.object_uid,
            output_link_uid=output_node.link_uid,
            destination_node_ids=reachable_destinations,
        )
        assigned_lines.add(line_id)
        used_outputs.add(output_index)

    missing_lines = tuple(line_id for line_id in required_line_ids if line_id not in assignments)
    assigned_line_ids = tuple(line_id for line_id in required_line_ids if line_id in assignments)
    power_line_roots, power_line_routes = _build_power_line_routes(
        solver,
        assigned_line_ids,
        assignments,
        bfs_cache,
    )
    return assignments, power_line_roots, power_line_routes, missing_lines


def _assign_threephase_inputs_to_outputs(solver, input_nodes, output_nodes):
    if not input_nodes:
        return {}, {}, {}, ()

    input_node_ids = {}
    for input_index, input_node in enumerate(input_nodes):
        node_id = _node_id_for_position(solver, input_node.position)
        if node_id is not None:
            input_node_ids[input_index] = node_id

    resolved_outputs = _resolve_power_output_nodes(
        solver,
        output_nodes,
        output_description="threephase output",
    )
    bfs_cache = {}
    candidates = []

    for output_index, output_node in enumerate(resolved_outputs):
        distances, parents = _bfs_from_node(solver, output_node.node_id)
        bfs_cache[output_index] = (distances, parents)
        output_position = Vector(output_node.position)

        for input_index, input_node in enumerate(input_nodes):
            destination = input_node_ids.get(input_index)
            if destination is None or destination not in distances:
                continue
            euclidean_distance = (output_position - Vector(input_node.position)).length_squared
            candidates.append((distances[destination], euclidean_distance, input_index, output_index))

    assignments = {}
    assigned_inputs = set()
    used_outputs = set()

    for _graph_distance, _euclidean_distance, input_index, output_index in sorted(candidates):
        if input_index in assigned_inputs or output_index in used_outputs:
            continue

        output_node = resolved_outputs[output_index]
        assignments[input_index] = PowerLineOutputAssignment(
            line_id=input_index,
            output_index=output_index,
            output_node_id=output_node.node_id,
            output_label=output_node.label,
            output_position=output_node.position,
            output_object_uid=output_node.object_uid,
            output_link_uid=output_node.link_uid,
        )
        assigned_inputs.add(input_index)
        used_outputs.add(output_index)

    missing_inputs = tuple(input_index for input_index in range(len(input_nodes)) if input_index not in assignments)
    threephase_roots, threephase_routes = _build_direct_power_line_routes(
        solver,
        input_node_ids,
        assignments,
        bfs_cache,
    )
    return assignments, threephase_roots, threephase_routes, missing_inputs


def generate_power_solution(
    context,
    edge_resolution=EDGE_RESOLUTION,
    structure_resolution=STRUCTURE_RESOLUTION,
    delta_excess=DELTA_EXCESS,
    max_power_for_line=MAX_POWER_FOR_LINE,
    profiler=None,
):
    warnings = []
    if profiler is not None:
        profiler.step("generate solution begin")

    structure_vertices, cable_anchor_offsets = _collect_structure_vertices(
        structure_resolution,
        collect_offsets=True,
    )
    if profiler is not None:
        profiler.step(
            "collect structure vertices",
            vertices=len(structure_vertices),
            anchor_offsets=len(cable_anchor_offsets),
        )
    if not structure_vertices:
        raise PowerSolverError("No cable anchor links were found. Add truss/pipe objects first.")

    route_edges = _calculate_structure_edges(structure_vertices, edge_resolution)
    if profiler is not None:
        profiler.step("calculate structure edges", edges=len(route_edges))

    power_16a_outputs = list(_iter_power_16a_output_nodes())
    threephase_outputs = list(_iter_threephase_output_nodes())
    threephase_input_nodes = list(_iter_threephase_input_nodes())
    starting_position, starting_label = _find_starting_power_node(context)
    power_nodes = [PowerInputNode(starting_position, 0, starting_label)]
    input_power_nodes = list(_iter_power_input_nodes())
    power_nodes.extend(input_power_nodes)
    if profiler is not None:
        profiler.step(
            "collect power nodes",
            monophase_inputs=len(input_power_nodes),
            threephase_inputs=len(threephase_input_nodes),
            outputs_16a=len(power_16a_outputs),
            threephase_outputs=len(threephase_outputs),
            start=starting_label,
        )

    if not input_power_nodes and not threephase_input_nodes:
        warnings.append("No power input nodes were found; generated mesh may be empty.")

    graph_attachment_nodes = list(power_nodes)
    graph_attachment_nodes.extend(power_16a_outputs)
    graph_attachment_nodes.extend(threephase_input_nodes)
    graph_attachment_nodes.extend(threephase_outputs)
    power_edges = _calculate_power_edges(structure_vertices, graph_attachment_nodes)
    route_edges.extend(power_edges)
    if profiler is not None:
        profiler.step(
            "attach power nodes to graph",
            attachment_nodes=len(graph_attachment_nodes),
            power_edges=len(power_edges),
            route_edges=len(route_edges),
        )
    route_edges = _deduplicate_route_edges(route_edges)
    if profiler is not None:
        profiler.step("deduplicate route edges", route_edges=len(route_edges))

    solver = PowerSolver(
        max_power_for_line=max_power_for_line,
        delta_excess=delta_excess,
    )
    solver.construct_indices(route_edges, power_nodes, starting_position)
    if profiler is not None:
        profiler.step(
            "construct solver graph",
            solver_nodes=len(solver.nodes),
            solver_edges=len(solver.edges),
            solver_power_nodes=len(solver.power_nodes),
        )
    solver.solve(profiler=profiler)

    required_line_ids = _required_power_line_ids(solver)
    if profiler is not None:
        profiler.step("collect required monophase lines", required_lines=len(required_line_ids))
    assignments, power_line_roots, power_line_routes, missing_line_ids = _assign_power_lines_to_outputs(
        solver,
        required_line_ids,
        power_16a_outputs,
    )
    if profiler is not None:
        profiler.step(
            "assign monophase outputs",
            assigned=len(assignments),
            missing=len(missing_line_ids),
            routes=len(power_line_routes),
        )
    threephase_assignments, threephase_roots, threephase_routes, missing_threephase_inputs = _assign_threephase_inputs_to_outputs(
        solver,
        threephase_input_nodes,
        threephase_outputs,
    )
    if profiler is not None:
        profiler.step(
            "assign threephase outputs",
            assigned=len(threephase_assignments),
            missing=len(missing_threephase_inputs),
            routes=len(threephase_routes),
        )

    missing_16a_outputs = len(missing_line_ids)
    missing_threephase_outputs = len(missing_threephase_inputs)
    if missing_16a_outputs:
        warnings.append(
            f"Missing {missing_16a_outputs} 16A output plug(s); "
            f"plugged {len(assignments)} of {len(required_line_ids)} monophase power line(s)."
        )
    if missing_threephase_outputs:
        warnings.append(
            f"Missing {missing_threephase_outputs} threephase output plug(s); "
            f"plugged {len(threephase_assignments)} of {len(threephase_input_nodes)} threephase input(s)."
        )

    generated_powerline_connections = _generated_powerline_connections(
        solver,
        required_line_ids,
        assignments,
        _power_input_link_uids_by_node(solver, input_power_nodes),
    )
    generated_powerline_connections.update(_generated_direct_powerline_connections(
        threephase_input_nodes,
        threephase_assignments,
    ))
    positive_monophase_inputs = tuple(
        power_node
        for power_node in input_power_nodes
        if power_node.link_uid and int(power_node.consumption) > 0
    )
    zero_watt_monophase_inputs = tuple(
        power_node
        for power_node in input_power_nodes
        if power_node.link_uid and int(power_node.consumption) <= 0
    )
    connected_monophase_inputs = tuple(
        power_node
        for power_node in input_power_nodes
        if power_node.link_uid and power_node.link_uid in generated_powerline_connections
    )
    missing_positive_monophase_inputs = tuple(
        power_node
        for power_node in positive_monophase_inputs
        if power_node.link_uid not in generated_powerline_connections
    )
    missing_zero_watt_monophase_inputs = tuple(
        power_node
        for power_node in zero_watt_monophase_inputs
        if power_node.link_uid not in generated_powerline_connections
    )
    if profiler is not None:
        profiler.step(
            "build generated connection map",
            generated_connections=len(generated_powerline_connections),
        )
        profiler.step(
            "monophase generated connection coverage",
            inputs=len(input_power_nodes),
            positive_inputs=len(positive_monophase_inputs),
            zero_watt_inputs=len(zero_watt_monophase_inputs),
            connected_inputs=len(connected_monophase_inputs),
            missing_positive_inputs=len(missing_positive_monophase_inputs),
            missing_zero_watt_inputs=len(missing_zero_watt_monophase_inputs),
        )
    if missing_positive_monophase_inputs:
        warnings.append(
            f"{len(missing_positive_monophase_inputs)} consuming monophase input(s) were not connected by generated power lines: "
            f"{_format_power_node_labels(missing_positive_monophase_inputs)}."
        )
    if missing_zero_watt_monophase_inputs:
        warnings.append(
            f"Skipped {len(missing_zero_watt_monophase_inputs)} monophase input(s) with watt 0: "
            f"{_format_power_node_labels(missing_zero_watt_monophase_inputs)}."
        )

    return PowerGenerationResult(
        solver=solver,
        structure_vertex_count=len(structure_vertices),
        route_edge_count=len(route_edges),
        power_node_count=len(power_nodes),
        starting_label=starting_label,
        required_power_lines=len(required_line_ids),
        available_16a_outputs=len(power_16a_outputs),
        missing_16a_outputs=missing_16a_outputs,
        power_line_output_assignments=assignments,
        power_line_routes=power_line_routes,
        power_line_roots=power_line_roots,
        required_threephase_lines=len(threephase_input_nodes),
        available_threephase_outputs=len(threephase_outputs),
        missing_threephase_outputs=missing_threephase_outputs,
        threephase_output_assignments=threephase_assignments,
        threephase_routes=threephase_routes,
        threephase_roots=threephase_roots,
        generated_powerline_connections=generated_powerline_connections,
        cable_anchor_offsets=cable_anchor_offsets,
        warnings=warnings,
    )
