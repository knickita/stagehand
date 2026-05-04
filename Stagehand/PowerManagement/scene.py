from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import os

import bpy
from mathutils import Quaternion, Vector

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
    int(StagehandLinkType.POWER_IN_CEE63A_PENTA),
}

POWER_OUTPUT_TYPES = {
    int(StagehandLinkType.POWER_OUT_CEE16A_MONO),
    int(StagehandLinkType.POWER_OUT_POWERCON_BLUE),
    int(StagehandLinkType.POWER_OUT_POWERCON_WHITE),
    int(StagehandLinkType.POWER_OUT_POWERCONTRUE),
    int(StagehandLinkType.POWER_OUT_CEE63A_PENTA),
}


@dataclass
class PowerGenerationResult:
    solver: PowerSolver
    structure_vertex_count: int
    route_edge_count: int
    power_node_count: int
    starting_label: str
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
    if getattr(link, "anchorForCables", False):
        return True
    return bool(link.cylindricalType and link.length > 0.0)


def _link_local_structure_points(link, resolution):
    position = Vector(link.posDir[:3])
    direction = _link_rotation(link) @ Vector((0.0, 1.0, 0.0))
    direction *= resolution
    point_count = int(link.length / resolution) + 1
    return [position + (direction * index) for index in range(point_count)]


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


def _collect_structure_vertices(resolution):
    vertices_by_key = {}

    for obj in _iter_stagehand_objects():
        rotation = obj.matrix_world.to_quaternion()
        translation = obj.matrix_world.to_translation()
        for link in obj.stagehand.links:
            if not _link_anchor_for_cables(link):
                continue

            for local_point in _link_local_structure_points(link, resolution):
                world_point = translation + (rotation @ local_point)
                vertices_by_key[position_key(world_point)] = tuple(world_point)

    obstacles = list(_iter_power_obstacles())
    if obstacles:
        vertices_by_key = {
            key: point
            for key, point in vertices_by_key.items()
            if not any(_point_inside_obstacle(point, obstacle) for obstacle in obstacles)
        }

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
        for link_index, link in enumerate(obj.stagehand.links):
            if int(link.type) not in POWER_INPUT_TYPES:
                continue

            position, _rotation = _link_world_transform(obj, link)
            yield PowerInputNode(
                position=tuple(position),
                consumption=int(obj.stagehand.watt),
                label=_power_link_label(obj, link_index),
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


def generate_power_solution(
    context,
    edge_resolution=EDGE_RESOLUTION,
    structure_resolution=STRUCTURE_RESOLUTION,
    delta_excess=DELTA_EXCESS,
    max_power_for_line=MAX_POWER_FOR_LINE,
):
    warnings = []
    structure_vertices = _collect_structure_vertices(structure_resolution)
    if not structure_vertices:
        raise PowerSolverError("No cable anchor links were found. Add truss/pipe objects first.")

    route_edges = _calculate_structure_edges(structure_vertices, edge_resolution)

    starting_position, starting_label = _find_starting_power_node(context)
    power_nodes = [PowerInputNode(starting_position, 0, starting_label)]
    input_power_nodes = list(_iter_power_input_nodes())
    power_nodes.extend(input_power_nodes)

    if not input_power_nodes:
        warnings.append("No power input nodes were found; generated mesh may be empty.")

    route_edges.extend(_calculate_power_edges(structure_vertices, power_nodes))

    solver = PowerSolver(
        max_power_for_line=max_power_for_line,
        delta_excess=delta_excess,
    )
    solver.construct_indices(route_edges, power_nodes, starting_position)
    solver.solve()

    return PowerGenerationResult(
        solver=solver,
        structure_vertex_count=len(structure_vertices),
        route_edge_count=len(route_edges),
        power_node_count=len(power_nodes),
        starting_label=starting_label,
        warnings=warnings,
    )
