from collections import deque
import colorsys
from dataclasses import dataclass, field

import bpy
from mathutils import Vector


POWER_LINES_OBJECT_NAME = "Stagehand Power Lines"
POWER_LINES_MESH_NAME = "Stagehand Power Lines Mesh"
POWER_LINES_MATERIAL_NAME = "Stagehand Cable Material"
POWER_LINES_COLOR_ATTRIBUTE = "PowerLineColor"

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


def _look_rotation(direction):
    if direction.length_squared == 0.0:
        return Vector((0.0, 0.0, 1.0)).to_track_quat("Z", "Y")
    return direction.normalized().to_track_quat("Z", "Y")


def _transformed_cable_point(point, midpoint, rotation, length):
    local_point = Vector((point.x * 0.02, point.y * 0.02, point.z * length))
    return midpoint + (rotation @ local_point)


def _single_cable_profile(center):
    hexagon = [
        Vector((-0.5, 0.0, -0.5)),
        Vector((-0.25, 0.43, -0.5)),
        Vector((0.25, 0.43, -0.5)),
        Vector((0.5, 0.0, -0.5)),
        Vector((0.25, -0.43, -0.5)),
        Vector((-0.25, -0.43, -0.5)),
    ]
    profile_vertices = [
        Vector((center.x + point.x, center.y + point.y, point.z))
        for point in hexagon
    ]
    profile_vertices.extend(
        Vector((center.x + point.x, center.y + point.y, point.z + 1.0))
        for point in hexagon
    )

    profile_faces = []
    for side_index in range(6):
        next_side = (side_index + 1) % 6
        profile_faces.append((side_index, side_index + 6, next_side + 6))
        profile_faces.append((side_index, next_side + 6, next_side))

    return profile_vertices, profile_faces


def _append_cable_mesh(vertices, faces, face_line_ids, render_link):
    if render_link.lines <= 0 or render_link.length <= 0.0:
        return

    pos_a = render_link.a.position + (render_link.direction * render_link.a.scale * 0.5)
    pos_b = render_link.b.position - (render_link.direction * render_link.b.scale * 0.5)
    length = (pos_b - pos_a).length
    if length <= 0.0:
        return

    midpoint = (pos_a + pos_b) * 0.5
    centers = _cable_centers(render_link.lines)

    for line_id, center in zip(render_link.line_ids, centers):
        profile_vertices, profile_faces = _single_cable_profile(center)
        base_index = len(vertices)
        vertices.extend(
            tuple(_transformed_cable_point(point, midpoint, render_link.rotation, length))
            for point in profile_vertices
        )
        faces.extend(
            (base_index + face[0], base_index + face[1], base_index + face[2])
            for face in profile_faces
        )
        face_line_ids.extend([line_id] * len(profile_faces))


def _socket_transform_for_link(render_node, render_link, is_output):
    endpoint_offset = render_link.direction * render_node.scale * 0.5
    if is_output:
        endpoint = render_node.position + endpoint_offset
    else:
        endpoint = render_node.position - endpoint_offset

    return endpoint, render_link.rotation, (0.02, 0.02, max(render_link.length, 0.0001))


def _socket_position(endpoint, rotation, scale, center):
    return _SocketTransform(tuple(endpoint), rotation, scale, tuple(center), -1).multiply_point(
        (0.0, 0.0, 0.0)
    )


def _link_socket_positions(render_node, render_link, is_output):
    endpoint, rotation, scale = _socket_transform_for_link(render_node, render_link, is_output)
    positions = {}
    for line_id, center in zip(render_link.line_ids, _cable_centers(render_link.lines)):
        positions[line_id] = _socket_position(endpoint, rotation, scale, center)
    return positions


def _ordered_line_ids_for_link(line_ids, node_a, link_direction, rotation, length, incoming_link):
    line_ids = tuple(sorted(line_ids))
    if not line_ids or incoming_link is None:
        return line_ids

    incoming_positions = _link_socket_positions(
        node_a,
        incoming_link,
        is_output=incoming_link.a is node_a,
    )
    if not incoming_positions:
        return line_ids

    endpoint = node_a.position + (link_direction * node_a.scale * 0.5)
    scale = (0.02, 0.02, max(length, 0.0001))
    available_slots = [
        (
            center_index,
            _socket_position(endpoint, rotation, scale, center),
        )
        for center_index, center in enumerate(_cable_centers(len(line_ids)))
    ]

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

    return tuple(ordered_line_ids)


def _append_node_intersection_mesh(vertices, faces, face_line_ids, render_node):
    if not render_node.links or render_node.scale <= 0.0:
        return

    inputs = []
    outputs = []

    for render_link in render_node.links:
        is_output = render_link.a is render_node
        endpoint, rotation, scale = _socket_transform_for_link(render_node, render_link, is_output)
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


def _first_child(children):
    return next(iter(children))


def _build_render_graph(solver):
    render_nodes = {}
    render_links = []
    incoming_links_by_node = {}

    def ensure_node(node_id, scale=None):
        if node_id not in render_nodes:
            render_nodes[node_id] = _RenderNode(
                node_id=node_id,
                position=_node_position(solver, node_id),
                scale=calculate_node_scale(len(solver.power_lines_per_node.get(node_id, ()))),
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


def _power_line_color(line_id):
    hue = (float(line_id) * 0.618033988749895) % 1.0
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.72, 0.95)
    return red, green, blue, 1.0


def _apply_power_line_colors(mesh, face_line_ids):
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
        color = _power_line_color(line_id)
        for loop_index in polygon.loop_indices:
            color_attribute.data[loop_index].color = color


def _material():
    material = bpy.data.materials.get(POWER_LINES_MATERIAL_NAME)
    if material is None:
        material = bpy.data.materials.new(POWER_LINES_MATERIAL_NAME)
    material.diffuse_color = (0.95, 0.95, 0.95, 1.0)

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


def _remove_existing_power_lines_object():
    existing = bpy.data.objects.get(POWER_LINES_OBJECT_NAME)
    if existing is None:
        return

    existing_mesh = existing.data
    bpy.data.objects.remove(existing, do_unlink=True)
    if existing_mesh is not None and existing_mesh.users == 0:
        bpy.data.meshes.remove(existing_mesh)


def build_power_lines_mesh(context, solver):
    render_nodes, render_links = _build_render_graph(solver)
    vertices = []
    faces = []
    face_line_ids = []

    for render_link in render_links:
        _append_cable_mesh(vertices, faces, face_line_ids, render_link)

    for render_node in render_nodes:
        _append_node_intersection_mesh(vertices, faces, face_line_ids, render_node)

    _remove_existing_power_lines_object()

    mesh = bpy.data.meshes.new(POWER_LINES_MESH_NAME)
    mesh.from_pydata(vertices, [], faces)
    mesh.update(calc_edges=True)
    _apply_power_line_colors(mesh, face_line_ids)

    power_lines_object = bpy.data.objects.new(POWER_LINES_OBJECT_NAME, mesh)
    power_lines_object["stagehand_generated_power_lines"] = True
    power_lines_object.data.materials.append(_material())
    context.collection.objects.link(power_lines_object)

    return power_lines_object, len(render_links), len(render_nodes), len(vertices), len(faces)
