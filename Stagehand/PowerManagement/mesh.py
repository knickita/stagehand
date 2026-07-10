from collections import defaultdict, deque
import colorsys
from dataclasses import dataclass, field

import bpy
from mathutils import Vector

from .solver import position_key


POWER_LINES_OBJECT_NAME = "Stagehand Power Lines"
POWER_LINES_MESH_NAME = "Stagehand Power Lines Mesh"
POWER_LINES_MATERIAL_NAME = "Stagehand Cable Material"
THREEPHASE_POWER_LINES_OBJECT_NAME = "Stagehand Threephase Power Lines"
THREEPHASE_POWER_LINES_MESH_NAME = "Stagehand Threephase Power Lines Mesh"
THREEPHASE_POWER_LINES_MATERIAL_NAME = "Stagehand Threephase Cable Material"
POWER_LINES_COLOR_ATTRIBUTE = "PowerLineColor"
STAGEHAND_COLLECTION_NAME = "stagehand"

_CABLE_PROFILE_CACHE = {}
_CABLE_CENTER_CACHE = {}


@dataclass
class _RenderNode:
    node_id: int
    position: Vector
    scale: float
    links: list = field(default_factory=list)


@dataclass
class _RenderLink:
    a: _RenderNode
    b: _RenderNode
    line_ids: tuple
    rotation: object
    direction: Vector
    length: float

    @property
    def lines(self):
        return len(self.line_ids)


@dataclass(frozen=True)
class _SocketTransform:
    endpoint: tuple
    rotation: object
    scale: tuple
    center: tuple
    line_id: int

    def multiply_point(self, point):
        local_point = Vector((
            (self.center[0] + point[0]) * self.scale[0],
            (self.center[1] + point[1]) * self.scale[1],
            (self.center[2] + point[2]) * self.scale[2],
        ))
        return Vector(self.endpoint) + (self.rotation @ local_point)


def calculate_node_scale(lines):
    step = 0
    increment = 1
    dimension = 0
    while step < lines:
        step += increment
        dimension += 1
        increment = int(dimension) * 6
    return dimension * 0.86 * 0.1


def _calculate_vertices(dimension):
    centers = [Vector((0.0, 0.0, 0.0)) for _index in range(dimension)]
    points = []
    center = Vector((0.0, 0.0, 0.0))

    hexagon = [
        Vector((-0.5, 0.0, -0.5)),
        Vector((-0.25, 0.43, -0.5)),
        Vector((0.25, 0.43, -0.5)),
        Vector((0.5, 0.0, -0.5)),
        Vector((0.25, -0.43, -0.5)),
        Vector((-0.25, -0.43, -0.5)),
    ]

    for index in range(6):
        points.append(center + hexagon[index])

    step = 1
    inner_step = 0
    number_of_inner_hexagons = 6

    if centers:
        centers[0] = Vector((0.0, 0.0, 0.0))

    while step < dimension:
        if inner_step == 0:
            center += Vector((0.0, 0.86, 0.0))
            for index in range(4):
                points.insert(index + 2, hexagon[index] + center)
        elif inner_step <= number_of_inner_hexagons / 6:
            center += Vector((0.75, -0.43, 0.0))
            points.pop(4 + inner_step * 2)
            if step > 6 and inner_step < number_of_inner_hexagons / 6:
                points.pop(4 + inner_step * 2)
            points.insert(4 + inner_step * 2, hexagon[2] + center)
            points.insert(5 + inner_step * 2, hexagon[3] + center)
            if inner_step == number_of_inner_hexagons / 6:
                points.insert(6 + inner_step * 2, hexagon[4] + center)
        elif inner_step <= number_of_inner_hexagons / 3:
            center += Vector((0.0, -0.86, 0.0))
            points.pop(5 + inner_step * 2)
            if step > 6 and inner_step < number_of_inner_hexagons / 3:
                points.pop(5 + inner_step * 2)
            points.insert(5 + inner_step * 2, hexagon[3] + center)
            points.insert(6 + inner_step * 2, hexagon[4] + center)
            if inner_step == number_of_inner_hexagons / 3:
                points.insert(7 + inner_step * 2, hexagon[5] + center)
        elif inner_step <= number_of_inner_hexagons / 2:
            center += Vector((-0.75, -0.43, 0.0))
            points.pop(6 + inner_step * 2)
            if step > 6 and inner_step < number_of_inner_hexagons / 2:
                points.pop(6 + inner_step * 2)
            points.insert(6 + inner_step * 2, hexagon[4] + center)
            points.insert(7 + inner_step * 2, hexagon[5] + center)
            if inner_step == number_of_inner_hexagons / 2:
                points.insert(8 + inner_step * 2, hexagon[0] + center)
        elif inner_step <= number_of_inner_hexagons * 4 / 6:
            center += Vector((-0.75, 0.43, 0.0))
            points.pop(7 + inner_step * 2)
            if step > 6 and inner_step < number_of_inner_hexagons * 4 / 6:
                points.pop(7 + inner_step * 2)
            points.insert(7 + inner_step * 2, hexagon[5] + center)
            points.insert(8 + inner_step * 2, hexagon[0] + center)
            if inner_step == number_of_inner_hexagons * 4 / 6:
                points.insert(9 + inner_step * 2, hexagon[1] + center)
        elif step != 6 and inner_step <= number_of_inner_hexagons * 5 / 6:
            center += Vector((0.0, 0.86, 0.0))
            points.pop(8 + inner_step * 2)
            if step > 6 and inner_step < number_of_inner_hexagons * 5 / 6:
                points.pop(8 + inner_step * 2)
            points.insert(8 + inner_step * 2, hexagon[0] + center)
            points.insert(9 + inner_step * 2, hexagon[1] + center)
            if inner_step == number_of_inner_hexagons * 5 / 6:
                points.insert(10 + inner_step * 2, hexagon[2] + center)
        elif step != 6 and inner_step < number_of_inner_hexagons - 1:
            center += Vector((0.75, 0.43, 0.0))
            points.pop(9 + inner_step * 2)
            if step > 6 and inner_step < number_of_inner_hexagons:
                points.pop(9 + inner_step * 2)
            points.insert(9 + inner_step * 2, hexagon[1] + center)
            points.insert(10 + inner_step * 2, hexagon[2] + center)
        elif step == 6:
            center += Vector((0.0, 0.86, 0.0))
            points.pop(0)
            points.pop(0)
            points.insert(len(points), hexagon[0] + center)
            points.insert(len(points), hexagon[1] + center)
        elif inner_step == number_of_inner_hexagons - 1:
            center += Vector((0.75, 0.43, 0.0))
            points.pop(0)
            points.pop(0)
            points.pop(len(points) - 1)
            points.insert(len(points), hexagon[1] + center)

        centers[step] = center.copy()

        inner_step += 1
        step += 1
        if inner_step == number_of_inner_hexagons:
            center += Vector((0.75, 0.43, 0.0))
            inner_step = 0
            number_of_inner_hexagons += 6

    point_count = len(points)
    for index in range(point_count):
        points.append(points[index] + Vector((0.0, 0.0, 1.0)))

    return points, centers


def _cable_profile(dimension):
    dimension = max(1, int(dimension))
    if dimension in _CABLE_PROFILE_CACHE:
        return _CABLE_PROFILE_CACHE[dimension]

    vertices, centers = _calculate_vertices(dimension)
    half_vertices = len(vertices) // 2
    faces = []

    for index in range(half_vertices - 1):
        faces.append((index, index + half_vertices, index + half_vertices + 1))
        faces.append((index, index + half_vertices + 1, index + 1))

    faces.append((len(vertices) - half_vertices - 1, len(vertices) - 1, half_vertices))
    faces.append((len(vertices) - half_vertices - 1, half_vertices, 0))

    _CABLE_PROFILE_CACHE[dimension] = (vertices, faces)
    _CABLE_CENTER_CACHE[dimension] = centers
    return vertices, faces


def _cable_centers(dimension):
    dimension = max(1, int(dimension))
    if dimension not in _CABLE_CENTER_CACHE:
        _cable_profile(dimension)
    return _CABLE_CENTER_CACHE[dimension]


def _cable_profile_radius(dimension, cable_radius_scale=1.0):
    profile_vertices, _profile_faces = _cable_profile(max(1, int(dimension)))
    half_vertices = len(profile_vertices) // 2
    return max((profile_vertices[index].x ** 2 + profile_vertices[index].y ** 2) ** 0.5 for index in range(half_vertices)) * 0.02 * cable_radius_scale


def _look_rotation(direction):
    if direction.length_squared == 0.0:
        return Vector((0.0, 0.0, 1.0)).to_track_quat("Z", "Y")
    return direction.normalized().to_track_quat("Z", "Y")


def _transformed_cable_point(point, midpoint, rotation, length, cable_radius_scale=1.0):
    radius = 0.02 * cable_radius_scale
    local_point = Vector((point.x * radius, point.y * radius, point.z * length))
    return midpoint + (rotation @ local_point)


def _scene_cable_draw_faces(context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return 'VISIBLE'
    return getattr(scene, "stagehand_cable_draw_faces", 'VISIBLE')


def _scene_cable_color_mode(context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return 'POWERLINES'
    mode = getattr(scene, "stagehand_cable_color", 'POWERLINES')
    return mode if mode in {'BLACK', 'POWERLINES'} else 'POWERLINES'


def _nearest_center_index(point, centers):
    return min(
        range(len(centers)),
        key=lambda index: (
            ((point.x - centers[index].x) * (point.x - centers[index].x))
            + ((point.y - centers[index].y) * (point.y - centers[index].y))
        ),
    )


def _profile_face_line_ids(profile_vertices, centers, line_ids):
    half_vertices = len(profile_vertices) // 2
    face_line_ids = []

    for index in range(half_vertices - 1):
        midpoint = (profile_vertices[index] + profile_vertices[index + 1]) * 0.5
        line_id = line_ids[_nearest_center_index(midpoint, centers)]
        face_line_ids.extend((line_id, line_id))

    midpoint = (profile_vertices[half_vertices - 1] + profile_vertices[0]) * 0.5
    line_id = line_ids[_nearest_center_index(midpoint, centers)]
    face_line_ids.extend((line_id, line_id))
    return face_line_ids


def _append_cable_mesh(vertices, faces, face_line_ids, render_link, draw_internal_faces=False, cable_radius_scale=1.0):
    if render_link.lines <= 0 or render_link.length <= 0.0:
        return

    pos_a = render_link.a.position + (render_link.direction * render_link.a.scale * 0.5)
    pos_b = render_link.b.position - (render_link.direction * render_link.b.scale * 0.5)
    length = (pos_b - pos_a).length
    if length <= 0.0:
        return

    midpoint = (pos_a + pos_b) * 0.5
    if draw_internal_faces:
        profile_vertices, profile_faces = _cable_profile(1)
        for center, line_id in zip(_cable_centers(render_link.lines), render_link.line_ids):
            base_index = len(vertices)
            vertices.extend(
                tuple(_transformed_cable_point(
                    Vector((point.x + center.x, point.y + center.y, point.z)),
                    midpoint,
                    render_link.rotation,
                    length,
                    cable_radius_scale,
                ))
                for point in profile_vertices
            )
            faces.extend(
                (base_index + face[0], base_index + face[1], base_index + face[2])
                for face in profile_faces
            )
            face_line_ids.extend(line_id for _face in profile_faces)
        return

    profile_vertices, profile_faces = _cable_profile(render_link.lines)
    centers = _cable_centers(render_link.lines)
    base_index = len(vertices)
    vertices.extend(
        tuple(_transformed_cable_point(point, midpoint, render_link.rotation, length, cable_radius_scale))
        for point in profile_vertices
    )
    faces.extend(
        (base_index + face[0], base_index + face[1], base_index + face[2])
        for face in profile_faces
    )
    face_line_ids.extend(_profile_face_line_ids(profile_vertices, centers, render_link.line_ids))


def _socket_transform_for_link(render_node, render_link, is_output, cable_radius_scale=1.0):
    endpoint_offset = render_link.direction * render_node.scale * 0.5
    if is_output:
        endpoint = render_node.position + endpoint_offset
    else:
        endpoint = render_node.position - endpoint_offset

    radius = 0.02 * cable_radius_scale
    return endpoint, render_link.rotation, (radius, radius, max(render_link.length, 0.0001))


def _socket_position(endpoint, rotation, scale, center):
    return _SocketTransform(tuple(endpoint), rotation, scale, tuple(center), -1).multiply_point(
        (0.0, 0.0, 0.0)
    )


def _link_socket_positions(render_node, render_link, is_output, cable_radius_scale=1.0):
    endpoint, rotation, scale = _socket_transform_for_link(render_node, render_link, is_output, cable_radius_scale)
    positions = {}
    for line_id, center in zip(render_link.line_ids, _cable_centers(render_link.lines)):
        positions[line_id] = _socket_position(endpoint, rotation, scale, center)
    return positions


def _segment_distance_squared(start_a, end_a, start_b, end_b):
    segment_a = end_a - start_a
    segment_b = end_b - start_b
    offset = start_a - start_b

    dot_aa = segment_a.dot(segment_a)
    dot_ab = segment_a.dot(segment_b)
    dot_bb = segment_b.dot(segment_b)
    dot_ao = segment_a.dot(offset)
    dot_bo = segment_b.dot(offset)

    denominator = (dot_aa * dot_bb) - (dot_ab * dot_ab)
    s_denominator = denominator
    t_denominator = denominator

    if denominator < 0.00000001:
        s_numerator = 0.0
        s_denominator = 1.0
        t_numerator = dot_bo
        t_denominator = dot_bb
    else:
        s_numerator = (dot_ab * dot_bo) - (dot_bb * dot_ao)
        t_numerator = (dot_aa * dot_bo) - (dot_ab * dot_ao)
        if s_numerator < 0.0:
            s_numerator = 0.0
            t_numerator = dot_bo
            t_denominator = dot_bb
        elif s_numerator > s_denominator:
            s_numerator = s_denominator
            t_numerator = dot_bo + dot_ab
            t_denominator = dot_bb

    if t_numerator < 0.0:
        t_numerator = 0.0
        if -dot_ao < 0.0:
            s_numerator = 0.0
        elif -dot_ao > dot_aa:
            s_numerator = s_denominator
        else:
            s_numerator = -dot_ao
            s_denominator = dot_aa
    elif t_numerator > t_denominator:
        t_numerator = t_denominator
        if (-dot_ao + dot_ab) < 0.0:
            s_numerator = 0.0
        elif (-dot_ao + dot_ab) > dot_aa:
            s_numerator = s_denominator
        else:
            s_numerator = -dot_ao + dot_ab
            s_denominator = dot_aa

    sc = 0.0 if abs(s_numerator) < 0.00000001 else s_numerator / s_denominator
    tc = 0.0 if abs(t_numerator) < 0.00000001 else t_numerator / t_denominator
    closest = offset + (segment_a * sc) - (segment_b * tc)
    return closest.length_squared


def _line_order_cost(ordered_line_ids, incoming_positions, outgoing_positions):
    cost = 0.0
    cable_clearance_squared = 0.035 * 0.035

    for slot_index, line_id in enumerate(ordered_line_ids):
        incoming_position = incoming_positions.get(line_id)
        if incoming_position is None:
            continue
        cost += (incoming_position - outgoing_positions[slot_index]).length_squared

    for slot_a, line_id_a in enumerate(ordered_line_ids):
        incoming_a = incoming_positions.get(line_id_a)
        if incoming_a is None:
            continue
        outgoing_a = outgoing_positions[slot_a]
        for slot_b in range(slot_a + 1, len(ordered_line_ids)):
            line_id_b = ordered_line_ids[slot_b]
            incoming_b = incoming_positions.get(line_id_b)
            if incoming_b is None:
                continue
            outgoing_b = outgoing_positions[slot_b]
            distance_squared = _segment_distance_squared(
                incoming_a,
                outgoing_a,
                incoming_b,
                outgoing_b,
            )
            if distance_squared < cable_clearance_squared:
                cost += (cable_clearance_squared - distance_squared) * 10000.0

    return cost


def _optimize_line_order(ordered_line_ids, incoming_positions, outgoing_positions):
    if len(ordered_line_ids) < 2:
        return tuple(ordered_line_ids)

    ordered_line_ids = list(ordered_line_ids)
    best_cost = _line_order_cost(ordered_line_ids, incoming_positions, outgoing_positions)

    improved = True
    while improved:
        improved = False
        for index_a in range(len(ordered_line_ids) - 1):
            for index_b in range(index_a + 1, len(ordered_line_ids)):
                ordered_line_ids[index_a], ordered_line_ids[index_b] = (
                    ordered_line_ids[index_b],
                    ordered_line_ids[index_a],
                )
                candidate_cost = _line_order_cost(
                    ordered_line_ids,
                    incoming_positions,
                    outgoing_positions,
                )
                if candidate_cost + 0.00000001 < best_cost:
                    best_cost = candidate_cost
                    improved = True
                    continue

                ordered_line_ids[index_a], ordered_line_ids[index_b] = (
                    ordered_line_ids[index_b],
                    ordered_line_ids[index_a],
                )

    return tuple(ordered_line_ids)


def _ordered_line_ids_for_link(line_ids, node_a, link_direction, rotation, length, incoming_link, cable_radius_scale=1.0):
    line_ids = tuple(sorted(line_ids))
    if not line_ids or incoming_link is None:
        return line_ids

    incoming_positions = _link_socket_positions(
        node_a,
        incoming_link,
        is_output=incoming_link.a is node_a,
        cable_radius_scale=cable_radius_scale,
    )
    if not incoming_positions:
        return line_ids

    endpoint = node_a.position + (link_direction * node_a.scale * 0.5)
    radius = 0.02 * cable_radius_scale
    scale = (radius, radius, max(length, 0.0001))
    available_slots = [
        (
            center_index,
            _socket_position(endpoint, rotation, scale, center),
        )
        for center_index, center in enumerate(_cable_centers(len(line_ids)))
    ]
    outgoing_positions = [position for _center_index, position in available_slots]

    ordered_line_ids = [None] * len(line_ids)
    for line_id in sorted(line_ids, key=lambda item: incoming_link.line_ids.index(item) if item in incoming_link.line_ids else len(incoming_link.line_ids)):
        target_position = incoming_positions.get(line_id)
        if target_position is None:
            continue

        best_available_index, best_slot = min(
            enumerate(available_slots),
            key=lambda item: (item[1][1] - target_position).length_squared,
        )
        center_index, _position = best_slot
        ordered_line_ids[center_index] = line_id
        available_slots.pop(best_available_index)

    remaining_line_ids = iter(line_id for line_id in line_ids if line_id not in ordered_line_ids)
    for index, line_id in enumerate(ordered_line_ids):
        if line_id is None:
            ordered_line_ids[index] = next(remaining_line_ids)

    return _optimize_line_order(ordered_line_ids, incoming_positions, outgoing_positions)


def _append_node_intersection_mesh(vertices, faces, face_line_ids, render_node, cable_radius_scale=1.0):
    if not render_node.links or render_node.scale <= 0.0:
        return

    inputs = []
    outputs = []

    for render_link in render_node.links:
        is_output = render_link.a is render_node
        endpoint, rotation, scale = _socket_transform_for_link(render_node, render_link, is_output, cable_radius_scale)
        target = outputs if is_output else inputs
        for line_id, center in zip(render_link.line_ids, _cable_centers(render_link.lines)):
            target.append(_SocketTransform(tuple(endpoint), rotation, scale, tuple(center), line_id))

    if not inputs:
        return

    _append_intersection_mesh(vertices, faces, face_line_ids, inputs, outputs)


def _transform_position(socket_transform):
    return socket_transform.multiply_point((0.0, 0.0, 0.0))


def _append_intersection_mesh(vertices, faces, face_line_ids, inputs_original, outputs_original):
    hexagon = [
        (-0.5, 0.0, 0.0),
        (-0.25, 0.43, 0.0),
        (0.25, 0.43, 0.0),
        (0.5, 0.0, 0.0),
        (0.25, -0.43, 0.0),
        (-0.25, -0.43, 0.0),
    ]

    in_positions = [_transform_position(socket) for socket in inputs_original]
    out_positions = [_transform_position(socket) for socket in outputs_original]

    distances = []
    for input_index, input_position in enumerate(in_positions):
        for output_index, output_position in enumerate(out_positions):
            distance = (input_position - output_position).length_squared
            distances.append((input_index, output_index, distance))
    distances.sort(key=lambda item: item[2])

    input_used = set()
    output_used = set()
    inputs = []
    outputs = []

    for input_index, output_index, _distance in distances:
        if inputs_original[input_index].line_id != outputs_original[output_index].line_id:
            continue
        if input_index in input_used or output_index in output_used:
            continue
        inputs.append(inputs_original[input_index])
        outputs.append(outputs_original[output_index])
        input_used.add(input_index)
        output_used.add(output_index)

    for input_index, output_index, _distance in distances:
        if input_index in input_used or output_index in output_used:
            continue
        inputs.append(inputs_original[input_index])
        outputs.append(outputs_original[output_index])
        input_used.add(input_index)
        output_used.add(output_index)

    if len(output_used) < len(outputs_original) and inputs_original:
        for output_index, output in enumerate(outputs_original):
            if output_index in output_used:
                continue

            matching_inputs = [
                input_socket
                for input_socket in inputs_original
                if input_socket.line_id == output.line_id
            ]
            if matching_inputs:
                output_position = _transform_position(output)
                input_socket = min(
                    matching_inputs,
                    key=lambda socket: (_transform_position(socket) - output_position).length_squared,
                )
            else:
                input_socket = inputs_original[output_index % len(inputs_original)]

            inputs.append(input_socket)
            outputs.append(output)

    pair_count = min(len(inputs), len(outputs))
    for pair_index in range(pair_count):
        pair_vertices = []
        for hex_point in hexagon:
            pair_vertices.append(inputs[pair_index].multiply_point(hex_point))
        for hex_point in hexagon:
            pair_vertices.append(outputs[pair_index].multiply_point(hex_point))

        start_index = 0
        min_distance = float("inf")
        for input_vertex_index in range(6):
            for output_vertex_index in range(6, 12):
                distance = (
                    pair_vertices[input_vertex_index]
                    - pair_vertices[output_vertex_index]
                ).length_squared
                if distance < min_distance:
                    min_distance = distance
                    start_index = output_vertex_index - input_vertex_index

        base_index = len(vertices)
        vertices.extend(tuple(vertex) for vertex in pair_vertices)
        for side_index in range(6):
            next_side = (side_index + 1) % 6
            output_a = ((start_index + side_index) % 6) + 6
            output_b = ((start_index + next_side) % 6) + 6
            faces.append((
                base_index + side_index,
                base_index + output_a,
                base_index + output_b,
            ))
            face_line_ids.append(outputs[pair_index].line_id)
            faces.append((
                base_index + output_b,
                base_index + next_side,
                base_index + side_index,
            ))
            face_line_ids.append(outputs[pair_index].line_id)


def _same_direction(a, b):
    return (a - b).length_squared <= 0.0000000001


def _node_position(solver, node_id):
    return Vector(solver.nodes[node_id])


def _offset_node_position(position, line_count, cable_anchor_offsets, cable_radius_scale=1.0):
    if not cable_anchor_offsets:
        return position

    offset_data = cable_anchor_offsets.get(position_key(position))
    if offset_data is None:
        return position

    direction = Vector(offset_data.get("direction", (0.0, 0.0, 0.0)))
    if direction.length_squared <= 0.0000000001:
        return position
    direction.normalize()

    display_radius = max(0.0, float(offset_data.get("display_radius", 0.0)))
    clearance = display_radius + _cable_profile_radius(max(1, int(line_count)), cable_radius_scale) + 0.005
    return position + (direction * clearance)


def _first_child(children):
    return next(iter(children))


def _build_render_graph(solver, cable_anchor_offsets=None, cable_radius_scale=1.0):
    render_nodes = {}
    render_links = []
    incoming_links_by_node = {}

    def ensure_node(node_id, scale=None):
        line_count = len(solver.power_lines_per_node.get(node_id, ()))
        if node_id not in render_nodes:
            render_nodes[node_id] = _RenderNode(
                node_id=node_id,
                position=_offset_node_position(
                    _node_position(solver, node_id),
                    line_count,
                    cable_anchor_offsets,
                    cable_radius_scale,
                ),
                scale=calculate_node_scale(line_count),
            )
        if scale is not None:
            render_nodes[node_id].scale = scale
        return render_nodes[node_id]

    ensure_node(solver.start_node, scale=0.0)
    to_explore = deque([(solver.start_node, solver.start_node)])

    while to_explore:
        start_node, actual_node = to_explore.popleft()
        before_actual = start_node
        direction = _node_position(solver, actual_node) - _node_position(solver, before_actual)
        actual_direction = direction.normalized() if direction.length_squared else Vector((0.0, 0.0, 0.0))

        while len(solver.steiner_children.get(actual_node, ())) == 1:
            before_actual = actual_node
            actual_node = _first_child(solver.steiner_children[actual_node])

            old_direction = actual_direction.copy()
            direction = _node_position(solver, actual_node) - _node_position(solver, before_actual)
            actual_direction = direction.normalized() if direction.length_squared else Vector((0.0, 0.0, 0.0))
            if not _same_direction(actual_direction, old_direction):
                actual_node = before_actual
                break

        if actual_node != start_node:
            line_ids = tuple(sorted(solver.power_lines_per_node.get(actual_node, ())))
            line_count = len(line_ids)
            node_a = ensure_node(start_node)
            node_b = ensure_node(actual_node, scale=calculate_node_scale(line_count))
            link_direction = node_b.position - node_a.position
            if line_count > 0 and link_direction.length_squared > 0.0:
                link_direction.normalize()
                length = (node_b.position - node_a.position).length
                rotation = _look_rotation(link_direction)
                line_ids = _ordered_line_ids_for_link(
                    line_ids,
                    node_a,
                    link_direction,
                    rotation,
                    length,
                    incoming_links_by_node.get(start_node),
                    cable_radius_scale,
                )
                render_link = _RenderLink(
                    a=node_a,
                    b=node_b,
                    line_ids=line_ids,
                    rotation=rotation,
                    direction=link_direction,
                    length=length,
                )
                node_a.links.append(render_link)
                node_b.links.append(render_link)
                render_links.append(render_link)
                incoming_links_by_node[actual_node] = render_link

        for child in solver.steiner_children.get(actual_node, ()):
            to_explore.append((actual_node, child))

    return list(render_nodes.values()), render_links


def _build_render_graph_from_routes(solver, power_line_routes, power_line_roots=None, cable_anchor_offsets=None, cable_radius_scale=1.0):
    edge_line_ids = defaultdict(set)
    edge_directions = {}
    node_line_ids = defaultdict(set)
    adjacency = defaultdict(list)
    render_nodes = {}
    render_links = []
    incoming_links_by_node = {}
    root_nodes = set((power_line_roots or {}).values())

    for line_id, route_edges in power_line_routes.items():
        for node_a_id, node_b_id in route_edges:
            if node_a_id == node_b_id:
                continue
            edge_key = tuple(sorted((node_a_id, node_b_id)))
            edge_line_ids[edge_key].add(line_id)
            edge_directions.setdefault(edge_key, (node_a_id, node_b_id))
            node_line_ids[node_a_id].add(line_id)
            node_line_ids[node_b_id].add(line_id)

    for edge_key in edge_line_ids:
        node_a_id, node_b_id = edge_key
        adjacency[node_a_id].append(node_b_id)
        adjacency[node_b_id].append(node_a_id)

    for line_id, root_node_id in (power_line_roots or {}).items():
        node_line_ids[root_node_id].add(line_id)

    def ensure_node(node_id, scale=None):
        line_count = len(node_line_ids.get(node_id, ()))
        if node_id not in render_nodes:
            render_nodes[node_id] = _RenderNode(
                node_id=node_id,
                position=_offset_node_position(
                    _node_position(solver, node_id),
                    line_count,
                    cable_anchor_offsets,
                    cable_radius_scale,
                ),
                scale=calculate_node_scale(line_count),
            )
        if scale is not None:
            render_nodes[node_id].scale = scale
        return render_nodes[node_id]

    def edge_key_for(node_a_id, node_b_id):
        return tuple(sorted((node_a_id, node_b_id)))

    def next_collinear_node(previous_node_id, current_node_id, line_id_set, blocked_edges):
        if current_node_id in root_nodes:
            return None
        if node_line_ids.get(current_node_id, set()) != line_id_set:
            return None

        incoming_direction = _node_position(solver, current_node_id) - _node_position(solver, previous_node_id)
        if incoming_direction.length_squared <= 0.0:
            return None
        incoming_direction.normalize()

        candidates = []
        for next_node_id in adjacency.get(current_node_id, ()):
            if next_node_id == previous_node_id:
                continue
            candidate_key = edge_key_for(current_node_id, next_node_id)
            if candidate_key in blocked_edges:
                continue
            if edge_line_ids.get(candidate_key, set()) != line_id_set:
                continue

            outgoing_direction = _node_position(solver, next_node_id) - _node_position(solver, current_node_id)
            if outgoing_direction.length_squared <= 0.0:
                continue
            outgoing_direction.normalize()
            if not _same_direction(incoming_direction, outgoing_direction):
                continue
            candidates.append(next_node_id)

        if len(candidates) != 1:
            return None
        return candidates[0]

    def extend_segment(start_node_id, end_node_id, line_id_set, segment_edges):
        previous_node_id = start_node_id
        current_node_id = end_node_id
        while True:
            next_node_id = next_collinear_node(
                previous_node_id,
                current_node_id,
                line_id_set,
                segment_edges,
            )
            if next_node_id is None:
                return current_node_id
            segment_edges.add(edge_key_for(current_node_id, next_node_id))
            previous_node_id, current_node_id = current_node_id, next_node_id

    def append_render_link(node_a_id, node_b_id, line_ids):
        line_count = len(line_ids)
        node_a = ensure_node(node_a_id, scale=0.0 if node_a_id in root_nodes else None)
        node_b = ensure_node(
            node_b_id,
            scale=0.0 if node_b_id in root_nodes else calculate_node_scale(line_count),
        )
        link_direction = node_b.position - node_a.position
        if line_count <= 0 or link_direction.length_squared <= 0.0:
            return

        link_direction.normalize()
        length = (node_b.position - node_a.position).length
        rotation = _look_rotation(link_direction)
        ordered_line_ids = _ordered_line_ids_for_link(
            line_ids,
            node_a,
            link_direction,
            rotation,
            length,
            incoming_links_by_node.get(node_a_id),
            cable_radius_scale,
        )
        render_link = _RenderLink(
            a=node_a,
            b=node_b,
            line_ids=ordered_line_ids,
            rotation=rotation,
            direction=link_direction,
            length=length,
        )
        node_a.links.append(render_link)
        node_b.links.append(render_link)
        render_links.append(render_link)
        incoming_links_by_node.setdefault(node_b_id, render_link)

    for root_node_id in root_nodes:
        ensure_node(root_node_id, scale=0.0)

    visited_edges = set()
    for edge_key in sorted(edge_line_ids):
        if edge_key in visited_edges:
            continue

        original_start_id, original_end_id = edge_directions.get(edge_key, edge_key)
        line_id_set = set(edge_line_ids[edge_key])
        segment_edges = {edge_key}
        end_node_id = extend_segment(original_start_id, original_end_id, line_id_set, segment_edges)
        start_node_id = extend_segment(original_end_id, original_start_id, line_id_set, segment_edges)
        visited_edges.update(segment_edges)
        append_render_link(start_node_id, end_node_id, tuple(sorted(line_id_set)))

    return list(render_nodes.values()), render_links


def _power_line_color(line_id, color_mode='POWERLINES'):
    if color_mode == 'BLACK':
        return 0.0, 0.0, 0.0, 1.0

    hue = (float(line_id) * 0.618033988749895) % 1.0
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.72, 0.95)
    return red, green, blue, 1.0


def _apply_power_line_colors(mesh, face_line_ids, color_mode='POWERLINES'):
    if not face_line_ids:
        return

    color_attributes = getattr(mesh, "color_attributes", None)
    if color_attributes is None:
        return

    color_attribute = color_attributes.new(
        name=POWER_LINES_COLOR_ATTRIBUTE,
        type='BYTE_COLOR',
        domain='CORNER',
    )

    for polygon, line_id in zip(mesh.polygons, face_line_ids):
        color = _power_line_color(line_id, color_mode)
        for loop_index in polygon.loop_indices:
            color_attribute.data[loop_index].color = color


def _material(context=None, material_name=POWER_LINES_MATERIAL_NAME):
    material = bpy.data.materials.get(material_name)
    if material is None:
        material = bpy.data.materials.new(material_name)
    material.diffuse_color = (
        (0.0, 0.0, 0.0, 1.0)
        if _scene_cable_color_mode(context) == 'BLACK'
        else (0.95, 0.95, 0.95, 1.0)
    )

    material.use_nodes = True
    node_tree = material.node_tree
    if node_tree is None:
        return material

    nodes = node_tree.nodes
    principled = nodes.get("Principled BSDF")
    if principled is None:
        return material

    attribute_node = nodes.get("Stagehand Power Line Color")
    if attribute_node is None:
        attribute_node = nodes.new(type="ShaderNodeAttribute")
        attribute_node.name = "Stagehand Power Line Color"
        attribute_node.label = "Power Line Color"
    attribute_node.attribute_name = POWER_LINES_COLOR_ATTRIBUTE

    if not any(link.to_node is principled and link.to_socket == principled.inputs.get("Base Color") for link in node_tree.links):
        base_color_input = principled.inputs.get("Base Color")
        color_output = attribute_node.outputs.get("Color")
        if base_color_input is not None and color_output is not None:
            node_tree.links.new(color_output, base_color_input)

    return material


def _stagehand_collection(context):
    collection = bpy.data.collections.get(STAGEHAND_COLLECTION_NAME)
    if collection is None:
        collection = bpy.data.collections.new(STAGEHAND_COLLECTION_NAME)

    scene = context.scene if context is not None else bpy.context.scene
    if scene is not None and all(child != collection for child in scene.collection.children):
        scene.collection.children.link(collection)

    return collection


def _move_to_stagehand_collection(obj, context):
    collection = _stagehand_collection(context)
    if all(existing != obj for existing in collection.objects):
        collection.objects.link(obj)

    for user_collection in list(obj.users_collection):
        if user_collection != collection:
            user_collection.objects.unlink(obj)


def _remove_existing_power_lines_object(object_name=POWER_LINES_OBJECT_NAME):
    existing = bpy.data.objects.get(object_name)
    if existing is None:
        return

    existing_mesh = existing.data
    bpy.data.objects.remove(existing, do_unlink=True)
    if existing_mesh is not None and existing_mesh.users == 0:
        bpy.data.meshes.remove(existing_mesh)


def build_power_lines_mesh(
    context,
    solver,
    cable_anchor_offsets=None,
    power_line_routes=None,
    power_line_roots=None,
    object_name=POWER_LINES_OBJECT_NAME,
    mesh_name=POWER_LINES_MESH_NAME,
    material_name=POWER_LINES_MATERIAL_NAME,
    cable_radius_scale=1.0,
):
    if power_line_routes is None:
        render_nodes, render_links = _build_render_graph(solver, cable_anchor_offsets, cable_radius_scale)
    else:
        render_nodes, render_links = _build_render_graph_from_routes(
            solver,
            power_line_routes,
            power_line_roots,
            cable_anchor_offsets,
            cable_radius_scale,
        )
    vertices = []
    faces = []
    face_line_ids = []
    draw_internal_faces = _scene_cable_draw_faces(context) == 'ALL'
    cable_color_mode = _scene_cable_color_mode(context)

    for render_link in render_links:
        _append_cable_mesh(vertices, faces, face_line_ids, render_link, draw_internal_faces, cable_radius_scale)

    for render_node in render_nodes:
        _append_node_intersection_mesh(vertices, faces, face_line_ids, render_node, cable_radius_scale)

    _remove_existing_power_lines_object(object_name)

    mesh = bpy.data.meshes.new(mesh_name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update(calc_edges=True)
    _apply_power_line_colors(mesh, face_line_ids, cable_color_mode)

    power_lines_object = bpy.data.objects.new(object_name, mesh)
    power_lines_object["stagehand_generated_power_lines"] = True
    power_lines_object.hide_select = True
    power_lines_object.data.materials.append(_material(context, material_name))
    _move_to_stagehand_collection(power_lines_object, context)

    return power_lines_object, len(render_links), len(render_nodes), len(vertices), len(faces)
