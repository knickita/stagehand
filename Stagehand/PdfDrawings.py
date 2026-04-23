import math
import os
import tempfile
import zlib
from pathlib import Path

import bpy
from bpy_extras.io_utils import ExportHelper
from mathutils import Vector

from . import Connections


PAGE_WIDTH = 842.0
PAGE_HEIGHT = 595.0
PAGE_MARGIN = 36.0
PAGE_GUTTER = 18.0
TITLE_HEIGHT = 32.0
RENDER_WIDTH = 1200
RENDER_HEIGHT = 850
CAMERA_FIT_MARGIN = 1.65
DIMENSION_FIT_MARGIN = 1.28
WHITE = (1.0, 1.0, 1.0)
BLACK = (0.0, 0.0, 0.0)
OPAQUE_WHITE = (1.0, 1.0, 1.0, 1.0)


def _project_name():
    blend_path = bpy.data.filepath
    if blend_path:
        return Path(blend_path).stem

    return "Stagehand PDF Drawings"


def _pdf_filename_for_project(project_name):
    sanitized = "".join(
        character if character.isalnum() or character in (" ", "-", "_") else "_"
        for character in project_name
    ).strip()
    sanitized = "_".join(sanitized.split())
    return f"{sanitized or 'stagehand_pdf_drawings'}.pdf"


def _visible_mesh_objects(context):
    return [
        obj
        for obj in context.scene.objects
        if obj.type == 'MESH' and obj.visible_get()
    ]


def _object_tags(obj):
    stagehand = getattr(obj, "stagehand", None)
    if stagehand is None or not getattr(stagehand, "is_stagehand_object", False):
        return []

    return [tag.value.lower() for tag in stagehand.tags]


def _has_tag(obj, tag_name):
    tag_name = tag_name.lower()
    return any(tag_name in tag for tag in _object_tags(obj))


def _is_truss_object(obj):
    stagehand = getattr(obj, "stagehand", None)
    if stagehand is None or not getattr(stagehand, "is_stagehand_object", False):
        return False

    return _has_tag(obj, "truss")


def _is_trusscube_object(obj):
    return _has_tag(obj, "trusscube")


def _object_bounds(objects):
    min_corner = Vector((math.inf, math.inf, math.inf))
    max_corner = Vector((-math.inf, -math.inf, -math.inf))

    for obj in objects:
        for corner in obj.bound_box:
            world_corner = obj.matrix_world @ Vector(corner)
            min_corner.x = min(min_corner.x, world_corner.x)
            min_corner.y = min(min_corner.y, world_corner.y)
            min_corner.z = min(min_corner.z, world_corner.z)
            max_corner.x = max(max_corner.x, world_corner.x)
            max_corner.y = max(max_corner.y, world_corner.y)
            max_corner.z = max(max_corner.z, world_corner.z)

    center = (min_corner + max_corner) * 0.5
    dimensions = max_corner - min_corner
    return center, dimensions


def _world_box(objects):
    min_corner = Vector((math.inf, math.inf, math.inf))
    max_corner = Vector((-math.inf, -math.inf, -math.inf))

    for obj in objects:
        for corner in obj.bound_box:
            world_corner = obj.matrix_world @ Vector(corner)
            min_corner.x = min(min_corner.x, world_corner.x)
            min_corner.y = min(min_corner.y, world_corner.y)
            min_corner.z = min(min_corner.z, world_corner.z)
            max_corner.x = max(max_corner.x, world_corner.x)
            max_corner.y = max(max_corner.y, world_corner.y)
            max_corner.z = max(max_corner.z, world_corner.z)

    return min_corner, max_corner


def _segment_local_box(segment_objects, rotation=None):
    if rotation is None:
        primary_obj = max(
            segment_objects,
            key=lambda obj: max(obj.dimensions.x, obj.dimensions.y, obj.dimensions.z),
        )
        rotation = primary_obj.matrix_world.to_quaternion().to_matrix()
    inverse_rotation = rotation.inverted()
    origin = segment_objects[0].matrix_world.translation
    min_corner = Vector((math.inf, math.inf, math.inf))
    max_corner = Vector((-math.inf, -math.inf, -math.inf))

    for obj in segment_objects:
        for corner in obj.bound_box:
            world_corner = obj.matrix_world @ Vector(corner)
            local_corner = inverse_rotation @ (world_corner - origin)
            min_corner.x = min(min_corner.x, local_corner.x)
            min_corner.y = min(min_corner.y, local_corner.y)
            min_corner.z = min(min_corner.z, local_corner.z)
            max_corner.x = max(max_corner.x, local_corner.x)
            max_corner.y = max(max_corner.y, local_corner.y)
            max_corner.z = max(max_corner.z, local_corner.z)

    return {
        "dimensions": max_corner - min_corner,
        "rotation": rotation,
    }


def _structure_rotation(objects):
    primary_obj = max(
        objects,
        key=lambda obj: max(obj.dimensions.x, obj.dimensions.y, obj.dimensions.z),
    )
    return primary_obj.matrix_world.to_quaternion().to_matrix()


def _object_uid(obj):
    return Connections.get_object_uid(obj)


def _connected_truss_neighbors(obj, visible_truss_uids):
    neighbors = []

    for _link_index, other_obj, _other_link_index, _other_link in Connections.iter_connected_links(obj):
        other_uid = _object_uid(other_obj)
        if other_uid in visible_truss_uids and _is_truss_object(other_obj):
            neighbors.append(other_obj)

    return neighbors


def _segment_key(objects):
    return tuple(sorted(_object_uid(obj) for obj in objects))


def _build_truss_segments(truss_objects):
    visible_truss_uids = {_object_uid(obj) for obj in truss_objects}
    trusscube_objects = [obj for obj in truss_objects if _is_trusscube_object(obj)]
    segments = [[obj] for obj in trusscube_objects]
    seen_segments = set()

    for obj in trusscube_objects:
        seen_segments.add((_object_uid(obj),))

    for start_obj in trusscube_objects:
        start_uid = _object_uid(start_obj)
        for neighbor in _connected_truss_neighbors(start_obj, visible_truss_uids):
            path = [start_obj, neighbor]
            previous_uid = start_uid
            current_obj = neighbor
            visited = {start_uid}

            while current_obj is not None:
                current_uid = _object_uid(current_obj)
                if current_uid in visited:
                    break

                visited.add(current_uid)

                if _is_trusscube_object(current_obj):
                    segment = [obj for obj in path if not _is_trusscube_object(obj)]
                    key = _segment_key(segment)
                    if segment and key not in seen_segments:
                        seen_segments.add(key)
                        segments.append(segment)
                    break

                next_objects = [
                    obj
                    for obj in _connected_truss_neighbors(current_obj, visible_truss_uids)
                    if _object_uid(obj) != previous_uid
                ]

                if not next_objects:
                    segment = [obj for obj in path if not _is_trusscube_object(obj)]
                    key = _segment_key(segment)
                    if segment and key not in seen_segments:
                        seen_segments.add(key)
                        segments.append(segment)
                    break

                previous_uid = current_uid
                current_obj = next_objects[0]
                path.append(current_obj)

    if not segments and truss_objects:
        segments.append(list(truss_objects))

    return segments


def _connected_truss_groups(truss_objects):
    truss_by_uid = {_object_uid(obj): obj for obj in truss_objects}
    unvisited = set(truss_by_uid)
    groups = []

    while unvisited:
        start_uid = min(unvisited)
        stack = [truss_by_uid[start_uid]]
        group = []
        unvisited.remove(start_uid)

        while stack:
            obj = stack.pop()
            group.append(obj)

            for neighbor in _connected_truss_neighbors(obj, set(truss_by_uid)):
                neighbor_uid = _object_uid(neighbor)
                if neighbor_uid in unvisited:
                    unvisited.remove(neighbor_uid)
                    stack.append(neighbor)

        groups.append(sorted(group, key=lambda obj: obj.name_full))

    return groups


def _projected_bounds(objects, camera_rotation):
    min_corner = Vector((math.inf, math.inf, math.inf))
    max_corner = Vector((-math.inf, -math.inf, -math.inf))
    world_to_camera_rotation = camera_rotation.inverted()

    for obj in objects:
        for corner in obj.bound_box:
            projected = world_to_camera_rotation @ (obj.matrix_world @ Vector(corner))
            min_corner.x = min(min_corner.x, projected.x)
            min_corner.y = min(min_corner.y, projected.y)
            min_corner.z = min(min_corner.z, projected.z)
            max_corner.x = max(max_corner.x, projected.x)
            max_corner.y = max(max_corner.y, projected.y)
            max_corner.z = max(max_corner.z, projected.z)

    return max_corner - min_corner


def _projected_box(objects, camera_rotation):
    min_corner = Vector((math.inf, math.inf, math.inf))
    max_corner = Vector((-math.inf, -math.inf, -math.inf))
    world_to_camera_rotation = camera_rotation.inverted()

    for obj in objects:
        for corner in obj.bound_box:
            projected = world_to_camera_rotation @ (obj.matrix_world @ Vector(corner))
            min_corner.x = min(min_corner.x, projected.x)
            min_corner.y = min(min_corner.y, projected.y)
            min_corner.z = min(min_corner.z, projected.z)
            max_corner.x = max(max_corner.x, projected.x)
            max_corner.y = max(max_corner.y, projected.y)
            max_corner.z = max(max_corner.z, projected.z)

    return min_corner, max_corner


def _set_camera_view(scene, camera, center, objects, view_direction):
    direction = view_direction.normalized()
    camera_rotation = direction.to_track_quat('-Z', 'Y').to_euler()
    projected_dimensions = _projected_bounds(objects, camera_rotation.to_matrix())
    max_dimension = max(projected_dimensions.x, projected_dimensions.y, projected_dimensions.z, 0.1)
    distance = max_dimension * 4.0
    render = scene.render
    frame_aspect = render.resolution_x / max(render.resolution_y, 1)
    required_width_scale = projected_dimensions.x / frame_aspect
    required_height_scale = projected_dimensions.y

    camera.location = center - (direction * distance)
    camera.rotation_euler = camera_rotation
    camera.data.type = 'ORTHO'
    camera.data.ortho_scale = max(required_width_scale, required_height_scale, 0.5) * CAMERA_FIT_MARGIN


def _expand_camera_for_dimensions(scene, camera):
    camera.data.ortho_scale *= DIMENSION_FIT_MARGIN


def _camera_point(camera_rotation, center, point):
    return camera_rotation.inverted() @ (point - center)


def _view_dimension_data(scene, camera, center, truss_segments, view_name, structure_rotation):
    if not truss_segments:
        return None

    camera_rotation = camera.rotation_euler.to_matrix()
    frame_height = camera.data.ortho_scale
    frame_width = frame_height * (scene.render.resolution_x / max(scene.render.resolution_y, 1))
    axes_by_view = {
        "Front": ("X", "Z"),
        "Left": ("Y", "Z"),
        "Top": ("X", "Y"),
        "Iso": ("X", "Z"),
    }
    all_min_corner, all_max_corner = _world_box([obj for segment in truss_segments for obj in segment])
    assembly_projected_center = camera_rotation.inverted() @ ((all_min_corner + all_max_corner) * 0.5)

    def segment_axes(segment_objects):
        min_corner, max_corner = _world_box(segment_objects)
        local_box = _segment_local_box(segment_objects, structure_rotation)

        def local_value_for_axis(axis):
            axis_index = {"X": 0, "Y": 1, "Z": 2}[axis]
            return local_box["dimensions"][axis_index]

        def point_for(axis_values):
            return Vector((
                axis_values.get("X", min_corner.x),
                axis_values.get("Y", min_corner.y),
                axis_values.get("Z", min_corner.z),
            ))

        def axis_dimension(axis):
            if axis == "X":
                p1 = point_for({"X": min_corner.x})
                p2 = point_for({"X": max_corner.x})
            elif axis == "Y":
                p1 = point_for({"Y": min_corner.y, "X": max_corner.x})
                p2 = point_for({"Y": max_corner.y, "X": max_corner.x})
            else:
                p1 = point_for({"Z": min_corner.z, "X": max_corner.x, "Y": max_corner.y})
                p2 = point_for({"Z": max_corner.z, "X": max_corner.x, "Y": max_corner.y})

            return {
                "p1": _camera_point(camera_rotation, center, p1),
                "p2": _camera_point(camera_rotation, center, p2),
                "value": local_value_for_axis(axis),
            }

        projected_min, projected_max = _projected_box(segment_objects, camera_rotation)
        projected_center = camera_rotation.inverted() @ center
        projected_mid = (projected_min + projected_max) * 0.5
        outside_x = projected_max.x if projected_mid.x >= assembly_projected_center.x else projected_min.x
        outside_y = projected_max.y if projected_mid.y >= assembly_projected_center.y else projected_min.y

        def projected_dimension(axis, p1, p2):
            return {
                "p1": p1 - projected_center,
                "p2": p2 - projected_center,
                "value": local_value_for_axis(axis),
            }

        if view_name == "Front":
            dimensions = (
                projected_dimension(
                    "X",
                    Vector((projected_min.x, outside_y, 0.0)),
                    Vector((projected_max.x, outside_y, 0.0)),
                ),
                projected_dimension(
                    "Z",
                    Vector((outside_x, projected_min.y, 0.0)),
                    Vector((outside_x, projected_max.y, 0.0)),
                ),
            )
        elif view_name == "Left":
            dimensions = (
                projected_dimension(
                    "Y",
                    Vector((projected_min.x, outside_y, 0.0)),
                    Vector((projected_max.x, outside_y, 0.0)),
                ),
                projected_dimension(
                    "Z",
                    Vector((outside_x, projected_min.y, 0.0)),
                    Vector((outside_x, projected_max.y, 0.0)),
                ),
            )
        elif view_name == "Top":
            dimensions = (
                projected_dimension(
                    "X",
                    Vector((projected_min.x, outside_y, 0.0)),
                    Vector((projected_max.x, outside_y, 0.0)),
                ),
                projected_dimension(
                    "Y",
                    Vector((outside_x, projected_min.y, 0.0)),
                    Vector((outside_x, projected_max.y, 0.0)),
                ),
            )
        else:
            dimensions = tuple(
                axis_dimension(axis)
                for axis in axes_by_view.get(view_name, ("X", "Z"))
            )

        return (max(dimensions, key=lambda dimension: dimension["value"]),)

    axes = []
    for segment in truss_segments:
        axes.extend(segment_axes(segment))

    return {
        "frame_width": frame_width,
        "frame_height": frame_height,
        "axes": axes,
        "center": _camera_point(camera_rotation, center, (all_min_corner + all_max_corner) * 0.5),
    }


def _capture_attributes(owner, attribute_names):
    if owner is None:
        return {}

    values = {}
    for attribute_name in attribute_names:
        if hasattr(owner, attribute_name):
            value = getattr(owner, attribute_name)
            if hasattr(value, "__len__") and not isinstance(value, str):
                value = tuple(value)
            values[attribute_name] = value
    return values


def _restore_attributes(owner, values):
    if owner is None:
        return

    for attribute_name, value in values.items():
        try:
            setattr(owner, attribute_name, value)
        except (AttributeError, TypeError):
            pass


def _set_attribute_if_available(owner, attribute_name, value):
    if owner is None or not hasattr(owner, attribute_name):
        return

    try:
        setattr(owner, attribute_name, value)
    except (AttributeError, TypeError):
        pass


def _set_render_engine(scene):
    for engine in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "BLENDER_RENDER"):
        try:
            scene.render.engine = engine
            return
        except TypeError:
            continue


def _configure_line_render(scene, view_layer):
    shading = getattr(scene.display, "shading", None)
    _set_attribute_if_available(shading, "type", 'SOLID')
    _set_attribute_if_available(shading, "color_type", 'MATERIAL')
    _set_attribute_if_available(shading, "background_type", 'VIEWPORT')
    _set_attribute_if_available(shading, "background_color", WHITE)
    _set_attribute_if_available(shading, "light", 'FLAT')
    _set_attribute_if_available(shading, "show_xray", True)
    _set_attribute_if_available(shading, "xray_alpha", 1.0)
    _set_attribute_if_available(shading, "show_wireframes", True)
    _set_attribute_if_available(shading, "wireframe_opacity", 1.0)

    world = scene.world
    if world is not None:
        world.color = WHITE

    scene.render.use_freestyle = True
    scene.render.film_transparent = True
    scene.render.image_settings.color_mode = 'RGBA'
    if hasattr(scene, "eevee"):
        scene.eevee.taa_render_samples = 16

    freestyle_settings = view_layer.freestyle_settings
    if not freestyle_settings.linesets:
        bpy.ops.scene.freestyle_lineset_add()

    for line_set in freestyle_settings.linesets:
        line_set.select_silhouette = True
        line_set.select_border = True
        line_set.select_crease = True
        line_set.select_edge_mark = True
        line_set.select_material_boundary = False
        line_set.select_contour = True
        line_set.visibility = 'VISIBLE'
        line_set.linestyle.color = (0.0, 0.0, 0.0)
        line_set.linestyle.thickness = 1.2

    try:
        scene.view_settings.view_transform = 'Standard'
        scene.view_settings.look = 'None'
        scene.view_settings.exposure = 0.0
        scene.view_settings.gamma = 1.0
    except TypeError:
        pass


def _capture_freestyle_settings(scene, view_layer):
    freestyle_settings = view_layer.freestyle_settings
    line_sets = []

    for line_set in freestyle_settings.linesets:
        line_sets.append({
            "line_set": line_set,
            "values": _capture_attributes(
                line_set,
                (
                    "select_silhouette",
                    "select_border",
                    "select_crease",
                    "select_edge_mark",
                    "select_material_boundary",
                    "select_contour",
                    "visibility",
                ),
            ),
            "linestyle": _capture_attributes(line_set.linestyle, ("color", "thickness")),
        })

    return {
        "use_freestyle": scene.render.use_freestyle,
        "line_set_count": len(freestyle_settings.linesets),
        "line_sets": line_sets,
    }


def _restore_freestyle_settings(scene, view_layer, state):
    scene.render.use_freestyle = state["use_freestyle"]
    freestyle_settings = view_layer.freestyle_settings

    while len(freestyle_settings.linesets) > state["line_set_count"]:
        freestyle_settings.linesets.remove(freestyle_settings.linesets[-1])

    for line_set_state in state["line_sets"]:
        line_set = line_set_state["line_set"]
        try:
            _restore_attributes(line_set, line_set_state["values"])
            _restore_attributes(line_set.linestyle, line_set_state["linestyle"])
        except ReferenceError:
            pass


def _create_white_material():
    material = bpy.data.materials.new("Stagehand PDF White Surface")
    material.diffuse_color = OPAQUE_WHITE
    material.use_nodes = True

    nodes = material.node_tree.nodes
    nodes.clear()

    output_node = nodes.new(type="ShaderNodeOutputMaterial")
    emission_node = nodes.new(type="ShaderNodeEmission")
    emission_node.inputs["Color"].default_value = OPAQUE_WHITE
    emission_node.inputs["Strength"].default_value = 1.0
    material.node_tree.links.new(emission_node.outputs["Emission"], output_node.inputs["Surface"])

    return material


def _create_black_material():
    material = bpy.data.materials.new("Stagehand PDF Black Dimension")
    material.diffuse_color = (0.0, 0.0, 0.0, 1.0)
    material.use_nodes = True

    nodes = material.node_tree.nodes
    nodes.clear()

    output_node = nodes.new(type="ShaderNodeOutputMaterial")
    emission_node = nodes.new(type="ShaderNodeEmission")
    emission_node.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
    emission_node.inputs["Strength"].default_value = 1.0
    material.node_tree.links.new(emission_node.outputs["Emission"], output_node.inputs["Surface"])

    return material


def _create_line_render_objects(scene, objects, hidden_objects=None):
    white_material = _create_white_material()
    temporary_objects = []
    original_hide_render = []
    hidden_objects = hidden_objects or objects

    for obj in hidden_objects:
        original_hide_render.append((obj, obj.hide_render))
        obj.hide_render = True

    for obj in objects:
        line_obj = obj.copy()
        line_obj.data = obj.data.copy()
        line_obj.animation_data_clear()
        line_obj.name = f"Stagehand PDF Line {obj.name}"
        line_obj.parent = None
        line_obj.matrix_world = obj.matrix_world.copy()
        line_obj.data.materials.clear()
        line_obj.data.materials.append(white_material)
        line_obj.hide_render = False

        scene.collection.objects.link(line_obj)
        temporary_objects.append(line_obj)

    return temporary_objects, white_material, original_hide_render


def _remove_line_render_objects(temporary_objects, white_material, original_hide_render):
    for obj, hide_render in original_hide_render:
        try:
            obj.hide_render = hide_render
        except ReferenceError:
            pass

    for obj in temporary_objects:
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)

    if white_material is not None and white_material.users == 0:
        bpy.data.materials.remove(white_material)


def _camera_overlay_point(camera_rotation, center, point):
    return center + (camera_rotation @ Vector((point.x, point.y, 0.0)))


def _add_dimension_curve(scene, name, points, material, bevel_depth):
    curve = bpy.data.curves.new(name, 'CURVE')
    curve.dimensions = '3D'
    curve.resolution_u = 1
    curve.bevel_depth = bevel_depth
    curve.bevel_resolution = 0

    for start, end in points:
        spline = curve.splines.new('POLY')
        spline.points.add(1)
        spline.points[0].co = (start.x, start.y, start.z, 1.0)
        spline.points[1].co = (end.x, end.y, end.z, 1.0)

    curve.materials.append(material)
    obj = bpy.data.objects.new(name, curve)
    scene.collection.objects.link(obj)
    return obj


def _add_dimension_text(scene, name, text, location, camera, material, size, direction_x, direction_y):
    curve = bpy.data.curves.new(name, 'FONT')
    curve.body = text
    curve.align_x = 'CENTER'
    curve.align_y = 'CENTER'
    curve.size = size
    curve.materials.append(material)

    obj = bpy.data.objects.new(name, curve)
    obj.location = location
    obj.rotation_euler = camera.rotation_euler
    if direction_x < 0.0:
        direction_x = -direction_x
        direction_y = -direction_y
    obj.rotation_euler.rotate_axis('Z', math.atan2(direction_y, direction_x))
    scene.collection.objects.link(obj)
    return obj


def _create_dimension_render_objects(scene, camera, center, dimension_data):
    if dimension_data is None:
        return [], None

    camera_rotation = camera.rotation_euler.to_matrix()
    material = _create_black_material()
    temporary_objects = []
    base_offset = camera.data.ortho_scale * 0.04
    offset_step = camera.data.ortho_scale * 0.035
    max_stack_offset = camera.data.ortho_scale * 0.16
    tick = camera.data.ortho_scale * 0.012
    bevel_depth = camera.data.ortho_scale * 0.0008
    text_size = camera.data.ortho_scale * 0.035
    assembly_center = dimension_data["center"]
    frame_width = dimension_data["frame_width"]
    frame_height = dimension_data["frame_height"]
    placed_boxes_by_bucket = {}

    for index, axis_dimension in enumerate(dimension_data["axes"]):
        if axis_dimension["value"] <= 0.01:
            continue

        p1 = axis_dimension["p1"].copy()
        p2 = axis_dimension["p2"].copy()
        direction_x, direction_y = _normalize_2d(p2.x - p1.x, p2.y - p1.y)
        normal_x, normal_y = -direction_y, direction_x
        mid = (p1 + p2) * 0.5

        if ((mid.x - assembly_center.x) * normal_x) + ((mid.y - assembly_center.y) * normal_y) < 0.0:
            normal_x = -normal_x
            normal_y = -normal_y

        preferred_normal = Vector((normal_x, normal_y, 0.0))
        direction = Vector((direction_x, direction_y, 0.0))
        text_width = len(_format_dimension(axis_dimension["value"])) * text_size * 0.58
        text_height = text_size
        chosen = None

        for side_index, side_multiplier in enumerate((1.0, -1.0)):
            normal = preferred_normal * side_multiplier
            max_offset = max_stack_offset if side_index == 0 else max_stack_offset * 1.5
            offset = base_offset

            while offset <= max_offset:
                q1 = p1 + (normal * offset)
                q2 = p2 + (normal * offset)
                label_point = ((q1 + q2) * 0.5) + (normal * (text_size * 0.65))
                box = _dimension_layout_box(q1, q2, label_point, text_width, text_height, normal, direction)
                bucket = _dimension_collision_bucket(direction, normal)
                placed_boxes = placed_boxes_by_bucket.get(bucket, [])
                overlaps = any(_boxes_overlap(box, placed_box) for placed_box in placed_boxes)
                fits = _box_inside_frame(box, frame_width, frame_height)

                if not overlaps and fits:
                    chosen = (normal, q1, q2, label_point, box, bucket)
                    break

                offset += offset_step

            if chosen is not None:
                break

        if chosen is None:
            normal = preferred_normal
            q1 = p1 + (normal * base_offset)
            q2 = p2 + (normal * base_offset)
            label_point = ((q1 + q2) * 0.5) + (normal * (text_size * 0.65))
            chosen = (
                normal,
                q1,
                q2,
                label_point,
                _dimension_layout_box(q1, q2, label_point, text_width, text_height, normal, direction),
                _dimension_collision_bucket(direction, normal),
            )

        normal, q1, q2, label_point, box, bucket = chosen
        placed_boxes_by_bucket.setdefault(bucket, []).append(box)
        tick_vector = normal * tick

        world_segments = [
            (_camera_overlay_point(camera_rotation, center, p1), _camera_overlay_point(camera_rotation, center, q1)),
            (_camera_overlay_point(camera_rotation, center, p2), _camera_overlay_point(camera_rotation, center, q2)),
            (_camera_overlay_point(camera_rotation, center, q1), _camera_overlay_point(camera_rotation, center, q2)),
            (
                _camera_overlay_point(camera_rotation, center, q1 - tick_vector),
                _camera_overlay_point(camera_rotation, center, q1 + tick_vector),
            ),
            (
                _camera_overlay_point(camera_rotation, center, q2 - tick_vector),
                _camera_overlay_point(camera_rotation, center, q2 + tick_vector),
            ),
        ]

        temporary_objects.append(_add_dimension_curve(
            scene,
            f"Stagehand PDF Dimension Lines {index}",
            world_segments,
            material,
            bevel_depth,
        ))
        temporary_objects.append(_add_dimension_text(
            scene,
            f"Stagehand PDF Dimension Text {index}",
            _format_dimension(axis_dimension["value"]),
            _camera_overlay_point(camera_rotation, center, label_point),
            camera,
            material,
            text_size,
            direction_x,
            direction_y,
        ))

    return temporary_objects, material


def _remove_dimension_render_objects(temporary_objects, material):
    for obj in temporary_objects:
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and data.users == 0:
            if isinstance(data, bpy.types.Curve):
                bpy.data.curves.remove(data)

    if material is not None and material.users == 0:
        bpy.data.materials.remove(material)


def _render_view(context, view_name, center, objects, truss_segments, structure_rotation, temp_directory):
    scene = context.scene
    camera_data = bpy.data.cameras.new(f"Stagehand PDF {view_name} Camera Data")
    camera = bpy.data.objects.new(f"Stagehand PDF {view_name} Camera", camera_data)
    scene.collection.objects.link(camera)

    view_direction = {
        "Front": Vector((0.0, -1.0, 0.0)),
        "Left": Vector((1.0, 0.0, 0.0)),
        "Top": Vector((0.0, 0.0, -1.0)),
        "Iso": Vector((-1.0, -1.0, -0.75)),
    }[view_name]

    _set_camera_view(scene, camera, center, objects, view_direction)
    _expand_camera_for_dimensions(scene, camera)
    dimension_objects = []
    dimension_material = None

    output_path = Path(temp_directory) / f"{view_name.lower()}.png"
    try:
        dimension_data = _view_dimension_data(scene, camera, center, truss_segments, view_name, structure_rotation)
        dimension_objects, dimension_material = _create_dimension_render_objects(scene, camera, center, dimension_data)
        scene.camera = camera
        scene.render.filepath = str(output_path)
        bpy.ops.render.render(write_still=True)

        image = bpy.data.images.load(str(output_path))
        try:
            return _image_to_pdf_rgb(image)
        finally:
            bpy.data.images.remove(image)
    finally:
        _remove_dimension_render_objects(dimension_objects, dimension_material)
        bpy.data.objects.remove(camera, do_unlink=True)
        bpy.data.cameras.remove(camera_data)


def _image_to_pdf_rgb(image):
    width, height = image.size
    pixels = list(image.pixels)
    data = bytearray(width * height * 3)
    target = 0

    for y in range(height - 1, -1, -1):
        row_start = y * width * 4
        for x in range(width):
            pixel_start = row_start + x * 4
            alpha = pixels[pixel_start + 3]
            red = pixels[pixel_start] * alpha + (1.0 - alpha)
            green = pixels[pixel_start + 1] * alpha + (1.0 - alpha)
            blue = pixels[pixel_start + 2] * alpha + (1.0 - alpha)
            data[target] = max(0, min(255, int(red * 255.0)))
            data[target + 1] = max(0, min(255, int(green * 255.0)))
            data[target + 2] = max(0, min(255, int(blue * 255.0)))
            target += 3

    return {
        "width": width,
        "height": height,
        "data": bytes(data),
    }


def _pdf_escape(value):
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_stream(data):
    if isinstance(data, str):
        data = data.encode("latin-1")
    return b"<< /Length " + str(len(data)).encode("ascii") + b" >>\nstream\n" + data + b"\nendstream"


def _pdf_image_stream(image):
    compressed = zlib.compress(image["data"])
    header = (
        f"<< /Type /XObject /Subtype /Image /Width {image['width']} "
        f"/Height {image['height']} /ColorSpace /DeviceRGB "
        f"/BitsPerComponent 8 /Filter /FlateDecode /Length {len(compressed)} >>\n"
    ).encode("ascii")
    return header + b"stream\n" + compressed + b"\nendstream"


def _format_dimension(value):
    centimeters = value * 100.0
    if centimeters >= 100.0:
        return f"{centimeters / 100.0:.2f} m"
    return f"{centimeters:.0f} cm"


def _normalize_2d(x, y):
    length = math.hypot(x, y)
    if length <= 0.0001:
        return 0.0, 1.0
    return x / length, y / length


def _dimension_layout_box(q1, q2, label_point, text_width, text_height, normal, direction):
    del q1, q2

    points = [label_point]
    tick_padding = max(text_height * 0.15, 0.01)
    along_padding = text_width * 0.5
    normal_padding = text_height * 0.55

    extra_points = []
    for point in points:
        extra_points.extend([
            point + direction * along_padding,
            point - direction * along_padding,
            point + normal * normal_padding,
            point - normal * normal_padding,
        ])

    points.extend(extra_points)
    min_x = min(point.x for point in points) - tick_padding
    max_x = max(point.x for point in points) + tick_padding
    min_y = min(point.y for point in points) - tick_padding
    max_y = max(point.y for point in points) + tick_padding
    return min_x, min_y, max_x, max_y


def _boxes_overlap(first, second):
    return not (
        first[2] < second[0]
        or second[2] < first[0]
        or first[3] < second[1]
        or second[3] < first[1]
    )


def _box_inside_frame(box, frame_width, frame_height):
    half_width = frame_width * 0.5
    half_height = frame_height * 0.5
    return (
        box[0] >= -half_width
        and box[2] <= half_width
        and box[1] >= -half_height
        and box[3] <= half_height
    )


def _dimension_collision_bucket(direction, normal):
    if abs(direction.x) >= abs(direction.y):
        orientation = "horizontal"
    else:
        orientation = "vertical"

    if orientation == "horizontal":
        side = "top" if normal.y >= 0.0 else "bottom"
    else:
        side = "right" if normal.x >= 0.0 else "left"

    return orientation, side


def _page_content_stream(rendered_views, title, image_object_numbers):
    labels = ("Front", "Left", "Top", "Iso")
    slot_width = (PAGE_WIDTH - (PAGE_MARGIN * 2.0) - PAGE_GUTTER) / 2.0
    slot_height = (PAGE_HEIGHT - (PAGE_MARGIN * 2.0) - TITLE_HEIGHT - PAGE_GUTTER) / 2.0
    xobject_entries = " ".join(
        f"/Im{index + 1} {object_number} 0 R"
        for index, object_number in enumerate(image_object_numbers)
    )

    content = [
        "q",
        f"BT /F1 18 Tf 36 566 Td ({_pdf_escape(title)}) Tj ET",
    ]

    for index, label in enumerate(labels):
        column = index % 2
        row = 1 - (index // 2)
        x = PAGE_MARGIN + (slot_width + PAGE_GUTTER) * column
        y = PAGE_MARGIN + (slot_height + PAGE_GUTTER) * row
        image = rendered_views[label]

        scale = min(slot_width / image["width"], (slot_height - 18.0) / image["height"])
        draw_width = image["width"] * scale
        draw_height = image["height"] * scale
        draw_x = x + (slot_width - draw_width) * 0.5
        draw_y = y + (slot_height - 18.0 - draw_height) * 0.5

        content.extend([
            f"BT /F1 10 Tf {x:.2f} {y + slot_height - 10.0:.2f} Td ({_pdf_escape(label)}) Tj ET",
            f"{x:.2f} {y:.2f} {slot_width:.2f} {slot_height:.2f} re S",
            "q",
            f"{draw_width:.2f} 0 0 {draw_height:.2f} {draw_x:.2f} {draw_y:.2f} cm",
            f"/Im{index + 1} Do",
            "Q",
        ])

    content.append("Q")
    return "\n".join(content), xobject_entries


def _write_pdf(filepath, pages, title):
    labels = ("Front", "Left", "Top", "Iso")
    object_entries = [None, None, None]
    page_object_numbers = []
    pending_pages = []
    next_object_number = 4

    for page_index, rendered_views in enumerate(pages, start=1):
        page_object_number = next_object_number
        content_object_number = page_object_number + 1
        image_object_numbers = [
            content_object_number + index + 1
            for index in range(len(labels))
        ]
        next_object_number = content_object_number + len(labels) + 1
        page_title = title if len(pages) == 1 else f"{title} - Structure {page_index}"
        content_stream, xobject_entries = _page_content_stream(
            rendered_views,
            page_title,
            image_object_numbers,
        )

        page_object_numbers.append(page_object_number)
        pending_pages.append((
            page_object_number,
            content_object_number,
            image_object_numbers,
            xobject_entries,
            content_stream,
            rendered_views,
        ))

    object_entries[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    page_refs = " ".join(f"{object_number} 0 R" for object_number in page_object_numbers)
    object_entries[1] = f"<< /Type /Pages /Kids [{page_refs}] /Count {len(pages)} >>".encode("ascii")
    object_entries[2] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    for (
        page_object_number,
        content_object_number,
        image_object_numbers,
        xobject_entries,
        content_stream,
        rendered_views,
    ) in pending_pages:
        page_object = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 595] "
            f"/Resources << /Font << /F1 3 0 R >> "
            f"/XObject << {xobject_entries} >> >> "
            f"/Contents {content_object_number} 0 R >>"
        ).encode("ascii")
        object_entries.append(page_object)
        object_entries.append(_pdf_stream(content_stream))

        for label in labels:
            object_entries.append(_pdf_image_stream(rendered_views[label]))

    objects = [
        entry
        for entry in object_entries
        if entry is not None
    ]

    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")

    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))

    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )

    with open(filepath, "wb") as handle:
        handle.write(output)


class STAGEHAND_OT_generate_pdf_drawings(bpy.types.Operator, ExportHelper):
    bl_idname = "stagehand.generate_pdf_drawings"
    bl_label = "Generate PDF Drawings"
    bl_description = "Generate a PDF page with front, left, top, and isometric scene views"
    bl_options = {'REGISTER'}

    filename_ext = ".pdf"
    filter_glob: bpy.props.StringProperty(
        default="*.pdf",
        options={'HIDDEN'},
    )

    def invoke(self, context, event):
        if not self.filepath:
            blend_path = bpy.data.filepath
            base_directory = Path(blend_path).parent if blend_path else Path.home()
            self.filepath = str(base_directory / _pdf_filename_for_project(_project_name()))

        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        objects = _visible_mesh_objects(context)
        if not objects:
            self.report({'ERROR'}, "No visible mesh objects found for PDF drawings")
            return {'CANCELLED'}
        truss_objects = [obj for obj in objects if _is_truss_object(obj)]
        truss_groups = _connected_truss_groups(truss_objects) if truss_objects else [objects]
        scene = context.scene

        original_camera = scene.camera
        original_filepath = scene.render.filepath
        original_resolution = (scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage)
        original_format = scene.render.image_settings.file_format
        original_color_mode = scene.render.image_settings.color_mode
        original_engine = scene.render.engine
        original_film_transparent = scene.render.film_transparent
        original_freestyle = _capture_freestyle_settings(scene, context.view_layer)
        original_world_color = scene.world.color[:] if scene.world is not None else None
        original_view_settings = _capture_attributes(
            scene.view_settings,
            ("view_transform", "look", "exposure", "gamma"),
        )
        original_shading = _capture_attributes(
            getattr(scene.display, "shading", None),
            (
                "type",
                "color_type",
                "single_color",
                "background_type",
                "background_color",
                "light",
                "show_xray",
                "xray_alpha",
                "show_wireframes",
                "wireframe_opacity",
            ),
        )

        rendered_pages = []
        temporary_line_objects = []
        white_material = None
        original_hide_render = []

        try:
            scene.render.resolution_x = RENDER_WIDTH
            scene.render.resolution_y = RENDER_HEIGHT
            scene.render.resolution_percentage = 100
            scene.render.image_settings.file_format = 'PNG'

            _set_render_engine(scene)
            _configure_line_render(scene, context.view_layer)
            with tempfile.TemporaryDirectory() as temp_directory:
                for group_index, group_objects in enumerate(truss_groups, start=1):
                    page_temp_directory = Path(temp_directory) / f"structure_{group_index}"
                    page_temp_directory.mkdir(exist_ok=True)
                    temporary_line_objects, white_material, original_hide_render = _create_line_render_objects(
                        scene,
                        group_objects,
                        objects,
                    )
                    group_center, _group_dimensions = _object_bounds(group_objects)
                    group_segments = _build_truss_segments(group_objects) if truss_objects else []
                    group_rotation = _structure_rotation(group_objects)
                    rendered_views = {}

                    try:
                        for view_name in ("Front", "Left", "Top", "Iso"):
                            rendered_views[view_name] = _render_view(
                                context,
                                view_name,
                                group_center,
                                group_objects,
                                group_segments,
                                group_rotation,
                                page_temp_directory,
                            )
                    finally:
                        _remove_line_render_objects(temporary_line_objects, white_material, original_hide_render)
                        temporary_line_objects = []
                        white_material = None
                        original_hide_render = []

                    rendered_pages.append(rendered_views)

            _write_pdf(self.filepath, rendered_pages, _project_name())
        except Exception as exc:
            self.report({'ERROR'}, f"Unable to generate PDF drawings: {exc}")
            return {'CANCELLED'}
        finally:
            _remove_line_render_objects(temporary_line_objects, white_material, original_hide_render)
            scene.camera = original_camera
            scene.render.filepath = original_filepath
            scene.render.resolution_x = original_resolution[0]
            scene.render.resolution_y = original_resolution[1]
            scene.render.resolution_percentage = original_resolution[2]
            scene.render.image_settings.file_format = original_format
            scene.render.image_settings.color_mode = original_color_mode
            scene.render.film_transparent = original_film_transparent
            scene.render.engine = original_engine
            _restore_freestyle_settings(scene, context.view_layer, original_freestyle)
            if scene.world is not None and original_world_color is not None:
                scene.world.color = original_world_color
            _restore_attributes(scene.view_settings, original_view_settings)
            _restore_attributes(getattr(scene.display, "shading", None), original_shading)

        self.report({'INFO'}, f"PDF drawings created: {os.path.basename(self.filepath)}")
        return {'FINISHED'}


classes = (
    STAGEHAND_OT_generate_pdf_drawings,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
