import math
import os
import tempfile
import time
import zlib
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import blf
import bpy
from bpy_extras.io_utils import ExportHelper
from mathutils import Matrix, Quaternion, Vector

from . import Connections
from .LinkTypes import StagehandLinkType
from .RegistrationUtils import safe_register_class, safe_unregister_class

try:
    import numpy as np
except ImportError:
    np = None


PAGE_WIDTH = 842.0
PAGE_HEIGHT = 595.0
PAGE_MARGIN = 36.0
PAGE_GUTTER = 18.0
TITLE_HEIGHT = 32.0
RENDER_WIDTH = 1200
RENDER_HEIGHT = 850
CAMERA_FIT_MARGIN = 1.65
DIMENSION_FIT_MARGIN = 1.28
DIMENSION_DUPLICATE_TOLERANCE = 0.01
PDF_PROGRESS_METADATA_STEPS = 1
PDF_PROGRESS_WRITE_STEPS = 1
PDF_MAX_CONVERSION_WORKERS = os.cpu_count() or 1
PDF_RENDER_ENGINE_CANDIDATES = ("BLENDER_WORKBENCH", "BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "BLENDER_RENDER")
PDF_FALLBACK_EEVEE_TAA_RENDER_SAMPLES = 4
PDF_USE_FREESTYLE = False
LAYHER_HORIZONTAL_TAG = "structure-horizontal"
LAYHER_VERTICAL_TAG = "structure-vertical"
WHITE = (1.0, 1.0, 1.0)
BLACK = (0.0, 0.0, 0.0)
OPAQUE_WHITE = (1.0, 1.0, 1.0, 1.0)


class _PdfPhaseProfiler:
    def __init__(self):
        self.phases = OrderedDict()
        self.counts = OrderedDict()
        self.view_timings = []
        self.structure_timings = []

    def record(self, name, duration):
        self.phases[name] = self.phases.get(name, 0.0) + duration

    def record_since(self, name, started_at):
        self.record(name, time.perf_counter() - started_at)

    def count(self, name, amount=1):
        self.counts[name] = self.counts.get(name, 0) + amount

    def record_view(self, label, duration):
        self.view_timings.append((label, duration))

    def record_structure(self, label, duration):
        self.structure_timings.append((label, duration))

    def summary(self, limit=12):
        if not self.phases:
            return "no phase data"

        ordered_phases = sorted(
            self.phases.items(),
            key=lambda item: item[1],
            reverse=True,
        )
        return ", ".join(
            f"{name} {duration:.1f}s"
            for name, duration in ordered_phases[:limit]
        )

    def count_summary(self):
        if not self.counts:
            return "no count data"

        return ", ".join(
            f"{name} {count}"
            for name, count in self.counts.items()
        )

    def slow_view_summary(self, limit=6):
        if not self.view_timings:
            return "no view data"

        slow_views = sorted(
            self.view_timings,
            key=lambda item: item[1],
            reverse=True,
        )
        return ", ".join(
            f"{label} {duration:.1f}s"
            for label, duration in slow_views[:limit]
        )

    def slow_structure_summary(self, limit=6):
        if not self.structure_timings:
            return "no structure data"

        slow_structures = sorted(
            self.structure_timings,
            key=lambda item: item[1],
            reverse=True,
        )
        return ", ".join(
            f"{label} {duration:.1f}s"
            for label, duration in slow_structures[:limit]
        )


class _CursorProgressOverlay:
    def __init__(self, context):
        self.window_manager = context.window_manager
        self.message = None
        self.mouse_x = 24
        self.mouse_y = 72
        self.draw_handler = None

    def begin(self):
        if self.draw_handler is not None:
            return

        self.draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw,
            (),
            'WINDOW',
            'POST_PIXEL',
        )
        self._redraw_windows()

    def set_message(self, message):
        self.message = message
        self._redraw_windows()

    def update_mouse(self, event):
        if not hasattr(event, "mouse_region_x") or not hasattr(event, "mouse_region_y"):
            return

        if event.mouse_region_x < 0 or event.mouse_region_y < 0:
            return

        self.mouse_x = event.mouse_region_x
        self.mouse_y = event.mouse_region_y
        self._redraw_windows()

    def finish(self):
        if self.draw_handler is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self.draw_handler, 'WINDOW')
            self.draw_handler = None

        self.message = None
        self._redraw_windows()

    def _draw(self):
        if not self.message:
            return

        font_id = 0
        x = self.mouse_x + 28
        y = max(18, self.mouse_y - 18)

        try:
            blf.size(font_id, 13)
        except TypeError:
            blf.size(font_id, 13, 72)

        blf.position(font_id, x + 1, y - 1, 0)
        blf.color(font_id, 0.0, 0.0, 0.0, 0.85)
        blf.draw(font_id, self.message)
        blf.position(font_id, x, y, 0)
        blf.color(font_id, 1.0, 1.0, 1.0, 1.0)
        blf.draw(font_id, self.message)

    def _redraw_windows(self):
        if self.window_manager is None:
            return

        for window in getattr(self.window_manager, "windows", []):
            screen = getattr(window, "screen", None)
            if screen is None:
                continue

            for area in screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()


class _ProgressReporter:
    def __init__(self, context, total_steps):
        self.window_manager = context.window_manager
        self.workspace = getattr(context, "workspace", None)
        self.overlay = _CursorProgressOverlay(context)
        self.total_steps = max(1, total_steps)
        self.current_step = 0
        self.active = False

    def begin(self, message=None):
        if self.window_manager is None:
            return

        self.window_manager.progress_begin(0, self.total_steps)
        self.active = True
        self.overlay.begin()
        self.set_message(message)

    def advance(self, steps=1, message=None):
        if not self.active:
            return

        self.current_step = min(self.total_steps, self.current_step + steps)
        self.window_manager.progress_update(self.current_step)
        self.set_message(message)

    def set_message(self, message):
        if not self.active:
            return

        progress = (self.current_step / self.total_steps) * 100.0
        status = f"{message} ({progress:.0f}%)" if message else None
        if self.workspace is not None and hasattr(self.workspace, "status_text_set"):
            self.workspace.status_text_set(status)

        self.overlay.set_message(status)
        self._redraw_windows()

    def update_mouse(self, event):
        self.overlay.update_mouse(event)

    def finish(self):
        if not self.active:
            return

        if self.workspace is not None and hasattr(self.workspace, "status_text_set"):
            self.workspace.status_text_set(None)
        self.window_manager.progress_end()
        self.overlay.finish()
        self.active = False

    def _redraw_windows(self):
        if self.window_manager is None:
            return

        for window in getattr(self.window_manager, "windows", []):
            screen = getattr(window, "screen", None)
            if screen is None:
                continue

            for area in screen.areas:
                area.tag_redraw()


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

    return [tag.value.strip().lower() for tag in stagehand.tags if tag.value.strip()]


def _has_tag(obj, tag_name):
    tag_name = tag_name.lower()
    return tag_name in _object_tags(obj)


def _stagehand_catalogue_name(obj):
    stagehand = getattr(obj, "stagehand", None)
    if stagehand is not None and getattr(stagehand, "is_stagehand_object", False):
        return str(getattr(stagehand, "catalogueName", "")).strip()

    return ""


def _is_structure_object(obj):
    return _has_tag(obj, "structure") or _is_truss_object(obj) or _is_layher_object(obj)


def _is_truss_object(obj):
    stagehand = getattr(obj, "stagehand", None)
    if stagehand is None or not getattr(stagehand, "is_stagehand_object", False):
        return False

    return _has_tag(obj, "truss")


def _is_layher_object(obj):
    return _has_tag(obj, "layher") or "layher" in _stagehand_catalogue_name(obj).lower() or "layher" in obj.name_full.lower()


def _structure_kind(obj):
    if _is_layher_object(obj):
        return "layher"
    if _is_truss_object(obj):
        return "truss"
    return None


def _is_structure_joint_object(obj):
    return _has_tag(obj, "structure-joint")


def _is_truss_joint_object(obj):
    return _is_truss_object(obj) and _is_structure_joint_object(obj)


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
        rotation = Matrix.Identity(3)
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
        "min_corner": min_corner,
        "max_corner": max_corner,
        "rotation": rotation,
        "origin": origin,
    }


def _object_longest_local_axis_data(obj):
    local_min = Vector((math.inf, math.inf, math.inf))
    local_max = Vector((-math.inf, -math.inf, -math.inf))

    for corner in obj.bound_box:
        corner = Vector(corner)
        local_min.x = min(local_min.x, corner.x)
        local_min.y = min(local_min.y, corner.y)
        local_min.z = min(local_min.z, corner.z)
        local_max.x = max(local_max.x, corner.x)
        local_max.y = max(local_max.y, corner.y)
        local_max.z = max(local_max.z, corner.z)

    dimensions = local_max - local_min
    axis_index = max(range(3), key=lambda index: dimensions[index])
    if dimensions[axis_index] <= 0.000001:
        return None

    return Vector((
        1.0 if axis_index == 0 else 0.0,
        1.0 if axis_index == 1 else 0.0,
        1.0 if axis_index == 2 else 0.0,
    )), dimensions[axis_index]


def _object_longest_local_axis(obj):
    axis_data = _object_longest_local_axis_data(obj)
    if axis_data is None:
        return None

    return axis_data[0]


def _object_longest_local_axis_length(obj):
    axis_data = _object_longest_local_axis_data(obj)
    if axis_data is None:
        return 0.0

    return axis_data[1]


def _object_longest_world_axis(obj):
    local_axis = _object_longest_local_axis(obj)
    if local_axis is None:
        return None

    world_axis = obj.matrix_world.to_quaternion() @ local_axis
    if world_axis.length_squared <= 0.000001:
        return None

    return world_axis.normalized()


def _matrix_from_axes(x_axis, y_axis, z_axis):
    return Matrix((
        (x_axis.x, y_axis.x, z_axis.x),
        (x_axis.y, y_axis.y, z_axis.y),
        (x_axis.z, y_axis.z, z_axis.z),
    ))


def _nearest_cardinal_orientation_quaternion(orientation):
    cardinal_axes = (
        Vector((1.0, 0.0, 0.0)),
        Vector((-1.0, 0.0, 0.0)),
        Vector((0.0, 1.0, 0.0)),
        Vector((0.0, -1.0, 0.0)),
        Vector((0.0, 0.0, 1.0)),
        Vector((0.0, 0.0, -1.0)),
    )
    world_axes = (
        orientation @ Vector((1.0, 0.0, 0.0)),
        orientation @ Vector((0.0, 1.0, 0.0)),
        orientation @ Vector((0.0, 0.0, 1.0)),
    )
    best_axes = None
    best_score = -math.inf

    for x_axis in cardinal_axes:
        for y_axis in cardinal_axes:
            if abs(x_axis.dot(y_axis)) > 0.0001:
                continue

            z_axis = x_axis.cross(y_axis)
            if z_axis.length_squared <= 0.0001:
                continue

            target_axes = (x_axis, y_axis, z_axis.normalized())
            score = sum(world_axis.dot(target_axis) for world_axis, target_axis in zip(world_axes, target_axes))
            if score > best_score:
                best_score = score
                best_axes = target_axes

    if best_axes is None:
        return Quaternion()

    return _matrix_from_axes(*best_axes).to_quaternion()


def _structure_rotation(objects):
    if not objects:
        return Matrix.Identity(3)

    reference_orientation = None
    fallback_orientation = None
    best_horizontal_score = 0.0
    for obj in objects:
        orientation = obj.matrix_world.to_quaternion()
        world_axis = _object_longest_world_axis(obj)
        if world_axis is None:
            continue

        if fallback_orientation is None:
            fallback_orientation = orientation

        axis_length = _object_longest_local_axis_length(obj)
        horizontal_score = axis_length * math.hypot(world_axis.x, world_axis.y)
        if horizontal_score > best_horizontal_score:
            best_horizontal_score = horizontal_score
            reference_orientation = orientation

    if reference_orientation is None or best_horizontal_score <= 0.0001:
        reference_orientation = fallback_orientation
    if reference_orientation is None:
        return Matrix.Identity(3)

    snapped_orientation = _nearest_cardinal_orientation_quaternion(reference_orientation)
    correction = snapped_orientation @ reference_orientation.inverted()
    return correction.inverted().to_matrix()


def _object_uid(obj):
    return Connections.get_object_uid(obj)


def _connected_structure_neighbors(obj, visible_structure_uids, structure_kind=None):
    neighbors = []

    for _link_index, other_obj, _other_link_index, _other_link in Connections.iter_connected_links(obj):
        other_uid = _object_uid(other_obj)
        if other_uid not in visible_structure_uids or not _is_structure_object(other_obj):
            continue
        if structure_kind is not None and _structure_kind(other_obj) != structure_kind:
            continue

        neighbors.append(other_obj)

    return neighbors


def _connected_truss_neighbors(obj, visible_truss_uids):
    neighbors = []

    for other_obj in _connected_structure_neighbors(obj, visible_truss_uids, "truss"):
        if _is_truss_object(other_obj):
            neighbors.append(other_obj)

    return neighbors


def _segment_key(objects):
    return tuple(sorted(_object_uid(obj) for obj in objects))


class _StructureSegment(list):
    def __init__(self, objects, quote_axis=None):
        super().__init__(objects)
        self.quote_axis = quote_axis


def _build_truss_segments(truss_objects):
    visible_truss_uids = {_object_uid(obj) for obj in truss_objects}
    truss_joint_objects = [obj for obj in truss_objects if _is_truss_joint_object(obj)]
    segments = [[obj] for obj in truss_joint_objects]
    seen_segments = set()

    for obj in truss_joint_objects:
        seen_segments.add((_object_uid(obj),))

    for start_obj in truss_joint_objects:
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

                if _is_truss_joint_object(current_obj):
                    segment = [obj for obj in path if not _is_truss_joint_object(obj)]
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
                    segment = [obj for obj in path if not _is_truss_joint_object(obj)]
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


def _object_cardinal_axis(obj, structure_rotation, threshold=0.82):
    local_min = Vector((math.inf, math.inf, math.inf))
    local_max = Vector((-math.inf, -math.inf, -math.inf))

    for corner in obj.bound_box:
        corner = Vector(corner)
        local_min.x = min(local_min.x, corner.x)
        local_min.y = min(local_min.y, corner.y)
        local_min.z = min(local_min.z, corner.z)
        local_max.x = max(local_max.x, corner.x)
        local_max.y = max(local_max.y, corner.y)
        local_max.z = max(local_max.z, corner.z)

    dimensions = local_max - local_min
    local_axis_index = max(range(3), key=lambda index: dimensions[index])
    local_axis = Vector((
        1.0 if local_axis_index == 0 else 0.0,
        1.0 if local_axis_index == 1 else 0.0,
        1.0 if local_axis_index == 2 else 0.0,
    ))
    world_axis = obj.matrix_world.to_quaternion().to_matrix() @ local_axis
    if world_axis.length_squared <= 0.000001:
        return None

    structure_axis = structure_rotation.inverted() @ world_axis.normalized()
    axis_index = max(range(3), key=lambda index: abs(structure_axis[index]))
    if abs(structure_axis[axis_index]) < threshold:
        return None

    return ("X", "Y", "Z")[axis_index]


def _layher_quote_axis(obj, structure_rotation):
    is_horizontal = _has_tag(obj, LAYHER_HORIZONTAL_TAG)
    is_vertical = _has_tag(obj, LAYHER_VERTICAL_TAG)
    if not is_horizontal and not is_vertical:
        return None

    axis = _object_cardinal_axis(obj, structure_rotation)
    if is_horizontal:
        return axis if axis in {"X", "Y"} else None
    if is_vertical:
        return "Z" if axis == "Z" else None

    return None


def _build_layher_segments(layher_objects):
    if not layher_objects:
        return []

    visible_layher_uids = {_object_uid(obj) for obj in layher_objects}
    object_by_uid = {_object_uid(obj): obj for obj in layher_objects}
    structure_rotation = _structure_rotation(layher_objects)
    axis_by_uid = {
        _object_uid(obj): _layher_quote_axis(obj, structure_rotation)
        for obj in layher_objects
    }
    neighbors_by_uid = {
        _object_uid(obj): [
            _object_uid(neighbor)
            for neighbor in _connected_structure_neighbors(obj, visible_layher_uids, "layher")
        ]
        for obj in layher_objects
    }
    segments = []
    seen_segments = set()

    for axis in ("X", "Y", "Z"):
        axis_uids = {uid for uid, object_axis in axis_by_uid.items() if object_axis == axis}
        if not axis_uids:
            continue

        connector_uids = set()
        for uid, object_axis in axis_by_uid.items():
            if object_axis is None or object_axis == axis:
                continue

            connected_axis_count = sum(1 for neighbor_uid in neighbors_by_uid[uid] if neighbor_uid in axis_uids)
            if axis in {"X", "Y"}:
                if object_axis == "Z" and connected_axis_count >= 1:
                    connector_uids.add(uid)
            elif connected_axis_count >= 2:
                connector_uids.add(uid)

        graph = {uid: set() for uid in axis_uids | connector_uids}

        for uid in axis_uids:
            for neighbor_uid in neighbors_by_uid[uid]:
                if neighbor_uid in axis_uids or neighbor_uid in connector_uids:
                    graph[uid].add(neighbor_uid)
                    graph.setdefault(neighbor_uid, set()).add(uid)

        unvisited = set(axis_uids)
        while unvisited:
            start_uid = min(unvisited)
            pending = [start_uid]
            component_uids = set()
            unvisited.remove(start_uid)

            while pending:
                current_uid = pending.pop()
                component_uids.add(current_uid)

                for neighbor_uid in graph.get(current_uid, ()):
                    if neighbor_uid in component_uids:
                        continue

                    if neighbor_uid in axis_uids and neighbor_uid in unvisited:
                        unvisited.remove(neighbor_uid)
                    pending.append(neighbor_uid)

            key = tuple(sorted(component_uids))
            if key in seen_segments:
                continue

            seen_segments.add(key)
            segments.append(_StructureSegment([object_by_uid[uid] for uid in sorted(component_uids)], axis))

    return segments


def _build_structure_segments(structure_objects):
    structure_kind = _structure_kind(structure_objects[0]) if structure_objects else None
    if structure_kind == "layher":
        return _build_layher_segments(structure_objects)

    return _build_truss_segments(structure_objects)


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


def _connected_structure_groups(structure_objects):
    structures_by_uid = {_object_uid(obj): obj for obj in structure_objects}
    unvisited = set(structures_by_uid)
    groups = []

    while unvisited:
        start_uid = min(unvisited)
        start_obj = structures_by_uid[start_uid]
        structure_kind = _structure_kind(start_obj)
        stack = [start_obj]
        group = []
        unvisited.remove(start_uid)

        while stack:
            obj = stack.pop()
            group.append(obj)

            for neighbor in _connected_structure_neighbors(obj, set(structures_by_uid), structure_kind):
                neighbor_uid = _object_uid(neighbor)
                if neighbor_uid in unvisited:
                    unvisited.remove(neighbor_uid)
                    stack.append(neighbor)

        groups.append(sorted(group, key=lambda obj: obj.name_full))

    return groups


def _structure_collection_name(objects, fallback):
    if not objects:
        return fallback

    common_collection_names = None
    for obj in objects:
        collection_names = {
            collection.name
            for collection in getattr(obj, "users_collection", ())
            if getattr(collection, "name", "")
        }
        if common_collection_names is None:
            common_collection_names = collection_names
        else:
            common_collection_names &= collection_names

    if common_collection_names:
        return sorted(common_collection_names, key=str.casefold)[0]

    for obj in objects:
        for collection in getattr(obj, "users_collection", ()):
            collection_name = getattr(collection, "name", "")
            if collection_name:
                return collection_name

    return fallback


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


def _camera_rotation_from_direction(direction, up):
    direction = direction.normalized()
    base_quaternion = direction.to_track_quat('-Z', 'Y')
    axis = direction
    desired_up = up - (axis * up.dot(axis))
    camera_up = base_quaternion @ Vector((0.0, 1.0, 0.0))
    current_up = camera_up - (axis * camera_up.dot(axis))

    if desired_up.length_squared <= 0.000001 or current_up.length_squared <= 0.000001:
        return base_quaternion.to_euler()

    desired_up.normalize()
    current_up.normalize()
    roll = math.atan2(axis.dot(current_up.cross(desired_up)), current_up.dot(desired_up))
    return (Quaternion(axis, roll) @ base_quaternion).to_euler()


def _view_direction_and_up(view_name, structure_rotation):
    direction, up = {
        "Front": (Vector((0.0, 1.0, 0.0)), Vector((0.0, 0.0, 1.0))),
        "Left": (Vector((-1.0, 0.0, 0.0)), Vector((0.0, 0.0, 1.0))),
        "Top": (Vector((0.0, 0.0, -1.0)), Vector((0.0, 1.0, 0.0))),
        "Iso": (Vector((-1.0, 1.0, -0.75)), Vector((0.0, 0.0, 1.0))),
    }[view_name]

    return structure_rotation @ direction, structure_rotation @ up


def _fit_camera_to_projected_objects(scene, camera, objects, view_center, padding=1.16):
    if not objects:
        return False, view_center

    camera_rotation = camera.rotation_euler.to_matrix()
    min_corner, max_corner = _projected_box(objects, camera_rotation)
    dimensions = max_corner - min_corner
    if not all(math.isfinite(value) for value in (dimensions.x, dimensions.y)):
        return False, view_center

    render = scene.render
    frame_aspect = render.resolution_x / max(render.resolution_y, 1)
    required_scale = max(camera.data.ortho_scale, max(dimensions.y, dimensions.x / frame_aspect, 0.5) * padding)
    projected_center = (min_corner + max_corner) * 0.5
    world_to_camera_rotation = camera_rotation.inverted()
    view_center_projected = world_to_camera_rotation @ view_center
    new_view_center_projected = Vector((projected_center.x, projected_center.y, view_center_projected.z))
    new_view_center = camera_rotation @ new_view_center_projected
    new_camera_location = new_view_center + (camera.location - view_center)
    scale_changed = abs(required_scale - camera.data.ortho_scale) > 0.0001
    location_changed = (new_camera_location - camera.location).length > 0.0001

    camera.location = new_camera_location
    camera.data.ortho_scale = required_scale
    return scale_changed or location_changed, new_view_center


def _set_camera_view(scene, camera, center, objects, view_direction, view_up):
    direction = view_direction.normalized()
    camera_rotation = _camera_rotation_from_direction(direction, view_up)
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


def _same_projected_point(point_a, point_b, tolerance):
    return abs(point_a.x - point_b.x) <= tolerance and abs(point_a.y - point_b.y) <= tolerance


def _same_projected_dimension(axis_dimension, other_dimension, tolerance):
    if abs(axis_dimension["value"] - other_dimension["value"]) > tolerance:
        return False

    p1 = axis_dimension["p1"]
    p2 = axis_dimension["p2"]
    other_p1 = other_dimension["p1"]
    other_p2 = other_dimension["p2"]
    return (
        _same_projected_point(p1, other_p1, tolerance)
        and _same_projected_point(p2, other_p2, tolerance)
    ) or (
        _same_projected_point(p1, other_p2, tolerance)
        and _same_projected_point(p2, other_p1, tolerance)
    )


def _dimension_axis_vector(axis):
    return {
        "X": Vector((1.0, 0.0, 0.0)),
        "Y": Vector((0.0, 1.0, 0.0)),
        "Z": Vector((0.0, 0.0, 1.0)),
    }[axis]


def _dimension_axis_is_visible(axis, view_name, structure_rotation):
    view_direction, _view_up = _view_direction_and_up(view_name, structure_rotation)
    world_axis = structure_rotation @ _dimension_axis_vector(axis)
    if world_axis.length_squared <= 0.000001:
        return False

    return abs(world_axis.normalized().dot(view_direction.normalized())) < 0.97


def _local_point_for_structure(point, origin, structure_rotation):
    return structure_rotation.inverted() @ (point - origin)


def _layher_center_span(segment_objects, axis, structure_rotation, origin):
    if axis not in {"X", "Y"}:
        return None

    axis_index = {"X": 0, "Y": 1}[axis]
    vertical_positions = []
    for obj in segment_objects:
        if not _has_tag(obj, LAYHER_VERTICAL_TAG):
            continue

        local_center = _local_point_for_structure(obj.matrix_world.translation, origin, structure_rotation)
        vertical_positions.append(local_center[axis_index])

    if len(vertical_positions) < 2:
        return None

    span_min = min(vertical_positions)
    span_max = max(vertical_positions)
    if span_max - span_min <= DIMENSION_DUPLICATE_TOLERANCE:
        return None

    return span_min, span_max


def _dimension_axis_for_segment(segment_objects, structure_rotation, dimensions):
    quote_axis = getattr(segment_objects, "quote_axis", None)
    if quote_axis in {"X", "Y", "Z"}:
        return quote_axis

    layher_axes = [
        _layher_quote_axis(obj, structure_rotation)
        for obj in segment_objects
        if _is_layher_object(obj)
    ]
    horizontal_axes = [axis for axis in layher_axes if axis in {"X", "Y"}]
    if horizontal_axes:
        return max(
            ("X", "Y"),
            key=lambda axis: horizontal_axes.count(axis),
        )
    if any(axis == "Z" for axis in layher_axes):
        return "Z"

    return max(
        ("X", "Y", "Z"),
        key=lambda candidate_axis: dimensions[{"X": 0, "Y": 1, "Z": 2}[candidate_axis]],
    )


def _build_dimension_candidates(structure_segments, structure_rotation):
    candidates = []
    seen = set()

    for index, segment_objects in enumerate(structure_segments):
        if not segment_objects:
            continue

        local_box = _segment_local_box(segment_objects, structure_rotation)
        dimensions = local_box["dimensions"]
        axis = _dimension_axis_for_segment(segment_objects, structure_rotation, dimensions)
        axis_index = {"X": 0, "Y": 1, "Z": 2}[axis]
        span_override = _layher_center_span(segment_objects, axis, structure_rotation, local_box["origin"])
        value = (span_override[1] - span_override[0]) if span_override is not None else dimensions[axis_index]
        if value <= DIMENSION_DUPLICATE_TOLERANCE:
            continue

        key = (_segment_key(segment_objects), axis)
        if key in seen:
            continue

        seen.add(key)
        candidates.append({
            "axis": axis,
            "value": value,
            "segment": tuple(segment_objects),
            "index": index,
            "span": span_override,
        })

    return candidates


def _project_dimension_candidate(dimension_candidate, camera_rotation, center, structure_rotation, assembly_projected_center):
    axis = dimension_candidate["axis"]
    local_box = _segment_local_box(dimension_candidate["segment"], structure_rotation)
    axis_index = {"X": 0, "Y": 1, "Z": 2}[axis]
    local_min = local_box["min_corner"]
    local_max = local_box["max_corner"]
    local_mid = (local_min + local_max) * 0.5
    span = dimension_candidate.get("span")
    axis_min = span[0] if span is not None else local_min[axis_index]
    axis_max = span[1] if span is not None else local_max[axis_index]
    other_axis_indices = [index for index in range(3) if index != axis_index]
    projected_center = camera_rotation.inverted() @ center
    candidates = []

    def local_world_point(local_point):
        return local_box["origin"] + (local_box["rotation"] @ local_point)

    for first_side in (local_min[other_axis_indices[0]], local_max[other_axis_indices[0]]):
        for second_side in (local_min[other_axis_indices[1]], local_max[other_axis_indices[1]]):
            p1_local = local_mid.copy()
            p2_local = local_mid.copy()
            p1_local[axis_index] = axis_min
            p2_local[axis_index] = axis_max
            p1_local[other_axis_indices[0]] = first_side
            p2_local[other_axis_indices[0]] = first_side
            p1_local[other_axis_indices[1]] = second_side
            p2_local[other_axis_indices[1]] = second_side

            p1_projected = camera_rotation.inverted() @ local_world_point(p1_local)
            p2_projected = camera_rotation.inverted() @ local_world_point(p2_local)
            p1_camera = Vector((p1_projected.x, p1_projected.y, 0.0))
            p2_camera = Vector((p2_projected.x, p2_projected.y, 0.0))
            if (p2_camera - p1_camera).length <= DIMENSION_DUPLICATE_TOLERANCE:
                continue

            direction_x, direction_y = _normalize_2d(p2_camera.x - p1_camera.x, p2_camera.y - p1_camera.y)
            normal = Vector((-direction_y, direction_x, 0.0))
            mid_camera = (p1_camera + p2_camera) * 0.5
            outside_score = abs((mid_camera - assembly_projected_center).dot(normal))
            candidates.append((outside_score, p1_camera, p2_camera))

    if not candidates:
        return None

    p1_camera, p2_camera = max(candidates, key=lambda candidate: candidate[0])[1:]
    return {
        "p1": p1_camera - projected_center,
        "p2": p2_camera - projected_center,
        "value": dimension_candidate["value"],
        "candidate": dimension_candidate,
    }


def _segment_orientation(point_a, point_b, point_c):
    return ((point_b.x - point_a.x) * (point_c.y - point_a.y)) - ((point_b.y - point_a.y) * (point_c.x - point_a.x))


def _projected_point_line_distance(point, segment_start, segment_end):
    line_length = math.hypot(segment_end.x - segment_start.x, segment_end.y - segment_start.y)
    if line_length <= 0.0001:
        return math.hypot(point.x - segment_start.x, point.y - segment_start.y)

    return abs(_segment_orientation(segment_start, segment_end, point)) / line_length


def _projected_dimension_midpoint(projected_dimension):
    return (projected_dimension["p1"] + projected_dimension["p2"]) * 0.5


def _projected_dimension_direction(projected_dimension):
    direction_x, direction_y = _normalize_2d(
        projected_dimension["p2"].x - projected_dimension["p1"].x,
        projected_dimension["p2"].y - projected_dimension["p1"].y,
    )
    if direction_x < -0.0001 or (abs(direction_x) <= 0.0001 and direction_y < 0.0):
        direction_x = -direction_x
        direction_y = -direction_y

    return direction_x, direction_y


def _projected_dimensions_are_parallel(first_dimension, second_dimension, tolerance):
    first_direction_x, first_direction_y = _projected_dimension_direction(first_dimension)
    second_direction_x, second_direction_y = _projected_dimension_direction(second_dimension)
    cross = (first_direction_x * second_direction_y) - (first_direction_y * second_direction_x)
    return abs(cross) <= tolerance


def _projected_dimensions_share_axis_midpoint(first_dimension, second_dimension, tolerance):
    if not _projected_dimensions_are_parallel(first_dimension, second_dimension, 0.05):
        return False

    direction_x, direction_y = _projected_dimension_direction(first_dimension)
    first_midpoint = _projected_dimension_midpoint(first_dimension)
    second_midpoint = _projected_dimension_midpoint(second_dimension)
    first_axis_midpoint = (first_midpoint.x * direction_x) + (first_midpoint.y * direction_y)
    second_axis_midpoint = (second_midpoint.x * direction_x) + (second_midpoint.y * direction_y)
    return abs(first_axis_midpoint - second_axis_midpoint) <= tolerance


def _projected_dimension_preference_key(projected_dimension):
    direction_x, direction_y = _projected_dimension_direction(projected_dimension)
    midpoint = _projected_dimension_midpoint(projected_dimension)
    if abs(direction_x) >= abs(direction_y):
        return midpoint.y, midpoint.x

    return midpoint.x, midpoint.y


def _projected_dimensions_are_redundant(first_dimension, second_dimension, tolerance, view_name=None):
    if abs(first_dimension["value"] - second_dimension["value"]) > tolerance:
        return False

    if _same_projected_dimension(first_dimension, second_dimension, tolerance):
        return True
    if view_name != "Iso" and _projected_dimensions_share_axis_midpoint(first_dimension, second_dimension, tolerance):
        return True

    p1 = first_dimension["p1"]
    p2 = first_dimension["p2"]
    q1 = second_dimension["p1"]
    q2 = second_dimension["p2"]
    if not (
        _projected_point_line_distance(q1, p1, p2) <= tolerance
        and _projected_point_line_distance(q2, p1, p2) <= tolerance
        and _projected_point_line_distance(p1, q1, q2) <= tolerance
        and _projected_point_line_distance(p2, q1, q2) <= tolerance
    ):
        return False

    first_midpoint = _projected_dimension_midpoint(first_dimension)
    second_midpoint = _projected_dimension_midpoint(second_dimension)
    return _same_projected_point(first_midpoint, second_midpoint, tolerance)


def _remove_aligned_duplicate_dimensions(projected_dimensions, view_name=None):
    kept = []

    for projected_dimension in sorted(
        projected_dimensions,
        key=lambda dimension: (
            -dimension["value"],
            _projected_dimension_preference_key(dimension),
            dimension["candidate"]["index"],
        ),
    ):
        if any(
            _projected_dimensions_are_redundant(
                projected_dimension,
                kept_dimension,
                DIMENSION_DUPLICATE_TOLERANCE,
                view_name=view_name,
            )
            for kept_dimension in kept
        ):
            continue

        kept.append(projected_dimension)

    return sorted(kept, key=lambda dimension: dimension["candidate"]["index"])


def _view_dimension_data(
    scene,
    camera,
    center,
    dimension_candidates,
    view_name,
    structure_rotation,
    allowed_dimension_candidate_indices=None,
):
    if not dimension_candidates:
        return None

    camera_rotation = camera.rotation_euler.to_matrix()
    frame_height = camera.data.ortho_scale
    frame_width = frame_height * (scene.render.resolution_x / max(scene.render.resolution_y, 1))
    overlay_depth = (camera.location - center).length * 0.5
    all_min_corner, all_max_corner = _world_box([obj for candidate in dimension_candidates for obj in candidate["segment"]])
    assembly_projected_center = camera_rotation.inverted() @ ((all_min_corner + all_max_corner) * 0.5)
    axes = []
    for dimension_candidate in dimension_candidates:
        if (
            allowed_dimension_candidate_indices is not None
            and dimension_candidate["index"] not in allowed_dimension_candidate_indices
        ):
            continue
        if not _dimension_axis_is_visible(dimension_candidate["axis"], view_name, structure_rotation):
            continue

        projected_dimension = _project_dimension_candidate(
            dimension_candidate,
            camera_rotation,
            center,
            structure_rotation,
            assembly_projected_center,
        )
        if projected_dimension is not None:
            axes.append(projected_dimension)

    axes = _remove_aligned_duplicate_dimensions(axes, view_name=view_name)

    return {
        "frame_width": frame_width,
        "frame_height": frame_height,
        "overlay_depth": overlay_depth,
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
    for engine in PDF_RENDER_ENGINE_CANDIDATES:
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
    _set_attribute_if_available(shading, "show_object_outline", True)

    world = scene.world
    if world is not None:
        world.color = WHITE

    scene.render.use_freestyle = PDF_USE_FREESTYLE
    scene.render.film_transparent = True
    scene.render.image_settings.color_mode = 'RGBA'
    if hasattr(scene, "eevee"):
        scene.eevee.taa_render_samples = PDF_FALLBACK_EEVEE_TAA_RENDER_SAMPLES

    freestyle_settings = view_layer.freestyle_settings
    if not freestyle_settings.linesets:
        bpy.ops.scene.freestyle_lineset_add()

    for line_set in freestyle_settings.linesets:
        line_set.select_silhouette = True
        line_set.select_border = True
        line_set.select_crease = False
        line_set.select_edge_mark = False
        line_set.select_material_boundary = False
        line_set.select_contour = False
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


def _camera_overlay_point(camera_rotation, center, point, overlay_depth=0.0):
    return center + (camera_rotation @ Vector((point.x, point.y, overlay_depth)))


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
    text_size = camera.data.ortho_scale * 0.023
    assembly_center = dimension_data["center"]
    overlay_depth = dimension_data["overlay_depth"]
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

                if not overlaps:
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
            (_camera_overlay_point(camera_rotation, center, p1, overlay_depth), _camera_overlay_point(camera_rotation, center, q1, overlay_depth)),
            (_camera_overlay_point(camera_rotation, center, p2, overlay_depth), _camera_overlay_point(camera_rotation, center, q2, overlay_depth)),
            (_camera_overlay_point(camera_rotation, center, q1, overlay_depth), _camera_overlay_point(camera_rotation, center, q2, overlay_depth)),
            (
                _camera_overlay_point(camera_rotation, center, q1 - tick_vector, overlay_depth),
                _camera_overlay_point(camera_rotation, center, q1 + tick_vector, overlay_depth),
            ),
            (
                _camera_overlay_point(camera_rotation, center, q2 - tick_vector, overlay_depth),
                _camera_overlay_point(camera_rotation, center, q2 + tick_vector, overlay_depth),
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
            _camera_overlay_point(camera_rotation, center, label_point, overlay_depth),
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


def _render_view(
    context,
    view_name,
    center,
    objects,
    dimension_candidates,
    structure_rotation,
    temp_directory,
    profiler=None,
    conversion_executor=None,
    profile_label=None,
    allowed_dimension_candidate_indices=None,
    visible_dimension_candidate_indices=None,
):
    view_started_at = time.perf_counter()
    scene = context.scene
    camera_data = bpy.data.cameras.new(f"Stagehand PDF {view_name} Camera Data")
    camera = bpy.data.objects.new(f"Stagehand PDF {view_name} Camera", camera_data)
    scene.collection.objects.link(camera)

    setup_started_at = time.perf_counter()
    view_direction, view_up = _view_direction_and_up(view_name, structure_rotation)
    view_center = center.copy()
    _set_camera_view(scene, camera, view_center, objects, view_direction, view_up)
    _expand_camera_for_dimensions(scene, camera)
    if profiler is not None:
        profiler.record_since("view setup", setup_started_at)

    dimension_objects = []
    dimension_material = None

    output_path = Path(temp_directory) / f"{view_name.lower()}.png"
    try:
        fitting_started_at = time.perf_counter()
        fit_changed = False
        fit_pass_count = 0
        dimension_object_count = 0
        dimension_data = None
        for _fit_pass in range(5):
            fit_pass_count += 1
            _remove_dimension_render_objects(dimension_objects, dimension_material)
            dimension_objects = []
            dimension_material = None
            dimension_data = _view_dimension_data(
                scene,
                camera,
                view_center,
                dimension_candidates,
                view_name,
                structure_rotation,
                allowed_dimension_candidate_indices=allowed_dimension_candidate_indices,
            )
            dimension_objects, dimension_material = _create_dimension_render_objects(scene, camera, view_center, dimension_data)
            dimension_object_count += len(dimension_objects)
            context.view_layer.update()
            fit_changed, view_center = _fit_camera_to_projected_objects(
                scene,
                camera,
                list(objects) + dimension_objects,
                view_center,
            )
            if not fit_changed:
                break

        if fit_changed or not dimension_objects:
            _remove_dimension_render_objects(dimension_objects, dimension_material)
            dimension_objects = []
            dimension_material = None
            dimension_data = _view_dimension_data(
                scene,
                camera,
                view_center,
                dimension_candidates,
                view_name,
                structure_rotation,
                allowed_dimension_candidate_indices=allowed_dimension_candidate_indices,
            )
            dimension_objects, dimension_material = _create_dimension_render_objects(scene, camera, view_center, dimension_data)
            dimension_object_count += len(dimension_objects)
            context.view_layer.update()
        if visible_dimension_candidate_indices is not None and dimension_data is not None:
            visible_dimension_candidate_indices.update(
                axis_dimension["candidate"]["index"]
                for axis_dimension in dimension_data["axes"]
            )
        if profiler is not None:
            profiler.record_since("dimension fitting", fitting_started_at)
            profiler.count("fit passes", fit_pass_count)
            profiler.count("dimension objects", dimension_object_count)

        scene.camera = camera
        scene.render.filepath = str(output_path)
        render_started_at = time.perf_counter()
        bpy.ops.render.render(write_still=True)
        if profiler is not None:
            render_duration = time.perf_counter() - render_started_at
            profiler.record("render", render_duration)
            profiler.record_view(profile_label or view_name, render_duration)

        image_load_started_at = time.perf_counter()
        image = bpy.data.images.load(str(output_path))
        if profiler is not None:
            profiler.record_since("image load", image_load_started_at)
        try:
            pixel_read_started_at = time.perf_counter()
            image_size = tuple(image.size)
            pixels = list(image.pixels)
            if profiler is not None:
                profiler.record_since("pixel read", pixel_read_started_at)

            if conversion_executor is None:
                image_convert_started_at = time.perf_counter()
                converted_image = _image_pixels_to_pdf_rgb(image_size, pixels)
                if profiler is not None:
                    profiler.record_since("image conversion", image_convert_started_at)
                return converted_image

            return conversion_executor.submit(_timed_image_pixels_to_pdf_rgb, image_size, pixels)
        finally:
            bpy.data.images.remove(image)
    finally:
        dimension_cleanup_started_at = time.perf_counter()
        _remove_dimension_render_objects(dimension_objects, dimension_material)
        if profiler is not None:
            profiler.record_since("dimension cleanup", dimension_cleanup_started_at)

        camera_cleanup_started_at = time.perf_counter()
        bpy.data.objects.remove(camera, do_unlink=True)
        bpy.data.cameras.remove(camera_data)
        if profiler is not None:
            profiler.record_since("camera cleanup", camera_cleanup_started_at)
            profiler.record_since("view total", view_started_at)


def _image_to_pdf_rgb(image):
    return _image_pixels_to_pdf_rgb(tuple(image.size), list(image.pixels))


def _timed_image_pixels_to_pdf_rgb(image_size, pixels):
    started_at = time.perf_counter()
    return _image_pixels_to_pdf_rgb(image_size, pixels), time.perf_counter() - started_at


def _image_pixels_to_pdf_rgb(image_size, pixels):
    if np is not None:
        return _image_pixels_to_pdf_rgb_numpy(image_size, pixels)

    return _image_pixels_to_pdf_rgb_python(image_size, pixels)


def _image_pixels_to_pdf_rgb_numpy(image_size, pixels):
    width, height = image_size
    pixel_array = np.asarray(pixels, dtype=np.float32).reshape((height, width, 4))
    rgb_array = pixel_array[:, :, :3]
    alpha_array = pixel_array[:, :, 3:4]
    rgb_array = np.flipud((rgb_array * alpha_array) + (1.0 - alpha_array))
    rgb_array = np.clip(rgb_array * 255.0, 0.0, 255.0).astype(np.uint8, copy=False)

    return {
        "width": width,
        "height": height,
        "data": rgb_array.tobytes(),
    }


def _image_pixels_to_pdf_rgb_python(image_size, pixels):
    width, height = image_size
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


def _resolve_pdf_image(image_or_future, profiler=None):
    if not isinstance(image_or_future, Future):
        return image_or_future

    wait_started_at = time.perf_counter()
    image, conversion_duration = image_or_future.result()
    if profiler is not None:
        profiler.record_since("image conversion wait", wait_started_at)
        profiler.record("image conversion worker", conversion_duration)
    return image


def _resolve_rendered_pages(rendered_pages, profiler=None):
    resolve_started_at = time.perf_counter()
    for rendered_views in rendered_pages:
        for view_name, image_or_future in rendered_views.items():
            rendered_views[view_name] = _resolve_pdf_image(image_or_future, profiler=profiler)

    if profiler is not None:
        profiler.record_since("image conversion resolve", resolve_started_at)


def _pdf_escape(value):
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_text_line(font_size, x, y, text):
    return f"BT /F1 {font_size} Tf {x:.2f} {y:.2f} Td ({_pdf_escape(text)}) Tj ET"


def _pdf_right_text_line(font_size, right_x, y, text):
    estimated_width = len(text) * font_size * 0.55
    return _pdf_text_line(font_size, right_x - estimated_width, y, text)


def _pdf_page_number_line(page_index, page_count):
    return _pdf_right_text_line(
        9,
        PAGE_WIDTH - PAGE_MARGIN,
        PAGE_MARGIN * 0.45,
        f"{page_index}/{page_count}",
    )


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


def _bom_entry_data(obj):
    stagehand = getattr(obj, "stagehand", None)
    if stagehand is not None and getattr(stagehand, "is_stagehand_object", False):
        item_name = str(stagehand.catalogueName).strip() or obj.name_full
        asset_id = int(getattr(stagehand, "asset_id", -1))
        return {
            "key": ("stagehand", asset_id, item_name.casefold()),
            "name": item_name,
        }

    return {
        "key": ("mesh", obj.name_full.casefold()),
        "name": obj.name_full,
    }


def _collect_bom_entries(objects):
    grouped_entries = OrderedDict()

    for obj in objects:
        entry_data = _bom_entry_data(obj)
        entry = grouped_entries.get(entry_data["key"])
        if entry is None:
            entry = {
                "name": entry_data["name"],
                "quantity": 0,
            }
            grouped_entries[entry_data["key"]] = entry

        entry["quantity"] += 1

    return sorted(
        grouped_entries.values(),
        key=lambda entry: (entry["name"].casefold(), entry["quantity"]),
    )


def _object_truss_z_dimension(obj):
    box = _segment_local_box([obj], obj.matrix_world.to_quaternion().to_matrix())
    dimensions = box["dimensions"]
    return abs(dimensions.z)


def _is_truss_object_for_link_type(obj, link_type):
    if not _is_truss_object(obj) or _is_truss_joint_object(obj):
        return False

    for _index, link in Connections.iter_object_links(obj):
        if int(link.type) == int(link_type):
            return True

    return False


def _collect_truss_length_entries(objects, link_type):
    truss_objects = [obj for obj in objects if _is_truss_object_for_link_type(obj, link_type)]
    object_by_uid = {_object_uid(obj): obj for obj in truss_objects}
    unvisited_uids = set(object_by_uid)
    grouped_lengths = {}

    while unvisited_uids:
        start_uid = min(unvisited_uids)
        pending = [object_by_uid[start_uid]]
        component = []
        unvisited_uids.remove(start_uid)

        while pending:
            current_obj = pending.pop()
            component.append(current_obj)

            for _link_index, other_obj, _other_link_index, _other_link in Connections.iter_connected_links(current_obj):
                other_uid = _object_uid(other_obj)
                if other_uid not in unvisited_uids:
                    continue
                if not _is_truss_object_for_link_type(other_obj, link_type):
                    continue

                unvisited_uids.remove(other_uid)
                pending.append(other_obj)

        length_value = round(sum(_object_truss_z_dimension(obj) for obj in component), 4)
        entry = grouped_lengths.get(length_value)
        if entry is None:
            entry = {
                "length": length_value,
                "label": _format_dimension(length_value),
                "quantity": 0,
            }
            grouped_lengths[length_value] = entry

        entry["quantity"] += 1

    return [grouped_lengths[key] for key in sorted(grouped_lengths)]


def _active_link_indexes(obj):
    active_indexes = []

    for index, _link in Connections.iter_object_links(obj):
        other_obj, other_link, _other_link_index = Connections.get_connected_link(obj, index)
        if other_obj is not None and other_link is not None:
            active_indexes.append(index)

    return active_indexes


def _cube_category(active_indexes):
    active_set = set(active_indexes)
    active_count = len(active_set)
    opposite_pairs = ({0, 1}, {2, 3}, {4, 5})
    opposite_pair_count = sum(1 for pair in opposite_pairs if pair.issubset(active_set))

    if active_count == 0:
        return "senza vie"
    if active_count == 1:
        return "1 via"
    if active_count == 2:
        return "2 vie dritte" if opposite_pair_count == 1 else "2 vie ad angolo"
    if active_count == 3:
        return "3 vie a T" if opposite_pair_count >= 1 else "3 vie ad angolo"
    if active_count == 4:
        return "4 vie a croce" if opposite_pair_count >= 2 else "4 vie a T"
    if active_count == 5:
        return "5 vie"
    return "6 vie"


def _collect_cube_type_entries(objects, asset_id):
    category_order = [
        "senza vie",
        "1 via",
        "2 vie dritte",
        "2 vie ad angolo",
        "3 vie a T",
        "3 vie ad angolo",
        "4 vie a croce",
        "4 vie a T",
        "5 vie",
        "6 vie",
    ]
    category_counts = OrderedDict((category, 0) for category in category_order)

    for obj in objects:
        stagehand = getattr(obj, "stagehand", None)
        if stagehand is None or not getattr(stagehand, "is_stagehand_object", False):
            continue
        if int(getattr(stagehand, "asset_id", -1)) != asset_id:
            continue

        category_counts[_cube_category(_active_link_indexes(obj))] += 1

    return [
        {"label": category, "quantity": quantity}
        for category, quantity in category_counts.items()
        if quantity > 0
    ]


def _collect_fastener_totals(objects):
    connection_keys = set()
    total_ovetti_tratte = 0
    total_chiodi_coppiglie_tratte = 0
    total_chiodi_coppiglie_cubi = 0
    counted_link_types = {int(StagehandLinkType.LITEC30), int(StagehandLinkType.LITEC40)}

    for obj in objects:
        stagehand = getattr(obj, "stagehand", None)
        if stagehand is None or not getattr(stagehand, "is_stagehand_object", False):
            continue

        for index, link in Connections.iter_object_links(obj):
            if int(link.type) not in counted_link_types:
                continue

            other_obj, other_link, _other_link_index = Connections.get_connected_link(obj, index)
            if other_obj is None or other_link is None or int(other_link.type) not in counted_link_types:
                continue

            key = tuple(sorted((f"{_object_uid(obj)}:{index}", f"{_object_uid(other_obj)}:{_other_link_index}")))
            if key in connection_keys:
                continue

            connection_keys.add(key)
            if _is_truss_joint_object(obj) or _is_truss_joint_object(other_obj):
                total_chiodi_coppiglie_cubi += 4
            else:
                total_ovetti_tratte += 4
                total_chiodi_coppiglie_tratte += 8

    return {
        "ovetti_tratte": total_ovetti_tratte,
        "chiodi_coppiglie_tratte": total_chiodi_coppiglie_tratte,
        "chiodi_coppiglie_cubi_basi": total_chiodi_coppiglie_cubi,
    }


def _collect_structure_details(objects):
    return {
        "litec30_lengths": _collect_truss_length_entries(objects, StagehandLinkType.LITEC30),
        "litec40_lengths": _collect_truss_length_entries(objects, StagehandLinkType.LITEC40),
        "cube30_types": _collect_cube_type_entries(objects, 9),
        "cube40_types": _collect_cube_type_entries(objects, 34),
        "fasteners": _collect_fastener_totals(objects),
    }


def _truncate_pdf_text(text, max_characters):
    if len(text) <= max_characters:
        return text
    return f"{text[:max(0, max_characters - 3)].rstrip()}..."


def _bom_page_content_stream(rows, page_title, show_title=True):
    top_y = PAGE_HEIGHT - PAGE_MARGIN
    row_height = 18.0
    header_rows = 1 if show_title else 0
    table_top = top_y - 42.0 if show_title else top_y
    table_width = PAGE_WIDTH - (PAGE_MARGIN * 2.0)
    item_column_x = PAGE_MARGIN + 12.0
    quantity_column_x = PAGE_WIDTH - PAGE_MARGIN - 54.0
    quantity_divider_x = PAGE_WIDTH - PAGE_MARGIN - 72.0
    table_bottom = table_top - (row_height * (len(rows) + header_rows))

    content = ["q"]
    if show_title:
        content.extend([
            _pdf_text_line(18, PAGE_MARGIN, top_y - 7.0, page_title),
            _pdf_text_line(14, PAGE_MARGIN, top_y - 29.0, "Elenco materiale"),
        ])

    content.append(f"{PAGE_MARGIN:.2f} {table_bottom:.2f} {table_width:.2f} {table_top - table_bottom:.2f} re S")

    separator_count = len(rows) + header_rows - 1
    for line_index in range(1, separator_count + 1):
        y = table_top - (row_height * line_index)
        content.append(
            f"{PAGE_MARGIN:.2f} {y:.2f} m {PAGE_WIDTH - PAGE_MARGIN:.2f} {y:.2f} l S"
        )

    content.append(f"{quantity_divider_x:.2f} {table_bottom:.2f} m {quantity_divider_x:.2f} {table_top:.2f} l S")
    if show_title:
        content.extend([
            _pdf_text_line(11, item_column_x, table_top - 12.0, "Item"),
            _pdf_text_line(11, quantity_column_x, table_top - 12.0, "Qty"),
        ])

    for row_index, row in enumerate(rows, start=1):
        baseline_y = table_top - (row_height * (row_index + header_rows - 1)) - 12.0
        content.extend([
            _pdf_text_line(10, item_column_x, baseline_y, _truncate_pdf_text(row["name"], 88)),
            _pdf_text_line(10, quantity_column_x, baseline_y, str(row["quantity"])),
        ])

    content.append("Q")
    return "\n".join(content)


def _bom_rows_per_page(show_title=True):
    top_y = PAGE_HEIGHT - PAGE_MARGIN
    row_height = 18.0
    table_top = top_y - 42.0 if show_title else top_y
    header_rows = 1 if show_title else 0
    available_height = table_top - PAGE_MARGIN
    return max(1, int(available_height // row_height) - header_rows)


def _bom_page_streams(title, bom_entries):
    if not bom_entries:
        bom_entries = [{"name": "No visible items", "quantity": 0}]

    page_streams = []
    start = 0
    page_index = 1

    while start < len(bom_entries):
        show_title = page_index == 1
        rows_per_page = _bom_rows_per_page(show_title=show_title)
        page_rows = bom_entries[start:start + rows_per_page]
        page_streams.append(_bom_page_content_stream(page_rows, title, show_title=show_title))
        start += rows_per_page
        page_index += 1

    return page_streams


def _detail_page_streams(details):
    top_y = PAGE_HEIGHT - PAGE_MARGIN
    left_x = PAGE_MARGIN
    bottom_y = PAGE_MARGIN
    line_height = 17.0
    page_streams = []
    content = None
    y = None

    def start_page(page_index):
        nonlocal content, y
        y = top_y - 7.0
        content = ["q"]
        if page_index == 1:
            content.append(_pdf_text_line(18, left_x, y, "Dettagli Struttture"))
            y -= line_height + 10.0

    def finish_page():
        content.append("Q")
        page_streams.append("\n".join(content))

    def add_line(text, font_size=10, extra_gap=0.0, before_gap=0.0):
        nonlocal y
        if y - before_gap < bottom_y:
            finish_page()
            start_page(len(page_streams) + 1)

        y -= before_gap
        if y < bottom_y:
            finish_page()
            start_page(len(page_streams) + 1)

        content.append(_pdf_text_line(font_size, left_x, y, text))
        y -= line_height + extra_gap

    def format_meters_label(length_value):
        centimeters = length_value * 100.0
        if centimeters >= 100.0:
            meters = centimeters / 100.0
            if abs(meters - round(meters)) <= 0.0001:
                return f"{int(round(meters))} metri"
            return f"{meters:.2f} metri"
        if abs(centimeters - round(centimeters)) <= 0.0001:
            return f"{int(round(centimeters))} cm"
        return f"{centimeters:.0f} cm"

    start_page(1)
    add_line("Lunghezze americane da 30", font_size=13, extra_gap=2.0)

    if details["litec30_lengths"]:
        for entry in details["litec30_lengths"]:
            add_line(f"- {entry['quantity']} da {format_meters_label(entry['length'])}")
    else:
        add_line("- Nessuna")

    add_line("Tipi di cubi da 30", font_size=13, extra_gap=2.0, before_gap=8.0)
    if details["cube30_types"]:
        for entry in details["cube30_types"]:
            add_line(f"- {entry['quantity']} da {entry['label']}")
    else:
        add_line("- Nessuna")

    add_line("Lunghezze americane da 40", font_size=13, extra_gap=2.0, before_gap=8.0)
    if details["litec40_lengths"]:
        for entry in details["litec40_lengths"]:
            add_line(f"- {entry['quantity']} da {format_meters_label(entry['length'])}")
    else:
        add_line("- Nessuna")

    add_line("Tipi di cubi da 40", font_size=13, extra_gap=2.0, before_gap=8.0)
    if details["cube40_types"]:
        for entry in details["cube40_types"]:
            add_line(f"- {entry['quantity']} da {entry['label']}")
    else:
        add_line("- Nessuna")

    add_line(f"{details['fasteners']['ovetti_tratte']} totale ovetti per le tratte", font_size=12, before_gap=8.0)
    add_line(
        f"{details['fasteners']['chiodi_coppiglie_tratte']} totale chiodi e coppiglie per le tratte",
        font_size=12,
    )
    add_line(
        f"{details['fasteners']['chiodi_coppiglie_cubi_basi']} totale chiodi e coppiglie per i cubi e le basi",
        font_size=12,
    )

    finish_page()
    return page_streams


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
        display_label = "Side" if label == "Left" else label

        scale = min(slot_width / image["width"], (slot_height - 18.0) / image["height"])
        draw_width = image["width"] * scale
        draw_height = image["height"] * scale
        draw_x = x + (slot_width - draw_width) * 0.5
        draw_y = y + (slot_height - 18.0 - draw_height) * 0.5

        content.extend([
            "q",
            f"{draw_width:.2f} 0 0 {draw_height:.2f} {draw_x:.2f} {draw_y:.2f} cm",
            f"/Im{index + 1} Do",
            "Q",
            f"{x:.2f} {y:.2f} {slot_width:.2f} {slot_height:.2f} re S",
            f"BT /F1 10 Tf {x:.2f} {y + slot_height - 10.0:.2f} Td ({_pdf_escape(display_label)}) Tj ET",
        ])

    content.append("Q")
    return "\n".join(content), xobject_entries


def _write_pdf(filepath, pages, title, page_titles=None, bom_entries=None, details_data=None, profiler=None):
    write_started_at = time.perf_counter()
    labels = ("Front", "Left", "Top", "Iso")
    object_entries = [None, None, None]
    page_object_numbers = []
    pending_pages = []
    next_object_number = 4

    layout_started_at = time.perf_counter()
    bom_streams = _bom_page_streams(title, bom_entries or [])

    for bom_stream in bom_streams:
        page_object_number = next_object_number
        content_object_number = page_object_number + 1
        next_object_number = content_object_number + 1

        page_object_numbers.append(page_object_number)
        pending_pages.append((
            "bom",
            page_object_number,
            content_object_number,
            [],
            "",
            bom_stream,
            None,
        ))

    if details_data is not None:
        for detail_stream in _detail_page_streams(details_data):
            page_object_number = next_object_number
            content_object_number = page_object_number + 1
            next_object_number = content_object_number + 1

            page_object_numbers.append(page_object_number)
            pending_pages.append((
                "details",
                page_object_number,
                content_object_number,
                [],
                "",
                detail_stream,
                None,
            ))

    page_titles = page_titles or []
    for page_index, rendered_views in enumerate(pages, start=1):
        page_object_number = next_object_number
        content_object_number = page_object_number + 1
        image_object_numbers = [
            content_object_number + index + 1
            for index in range(len(labels))
        ]
        next_object_number = content_object_number + len(labels) + 1
        if page_index <= len(page_titles) and page_titles[page_index - 1]:
            page_title = page_titles[page_index - 1]
        else:
            page_title = title if len(pages) == 1 else f"{title} - Structure {page_index}"
        content_stream, xobject_entries = _page_content_stream(
            rendered_views,
            page_title,
            image_object_numbers,
        )

        page_object_numbers.append(page_object_number)
        pending_pages.append((
            "views",
            page_object_number,
            content_object_number,
            image_object_numbers,
            xobject_entries,
            content_stream,
            rendered_views,
        ))
    if profiler is not None:
        profiler.record_since("pdf layout", layout_started_at)

    object_entries[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    page_refs = " ".join(f"{object_number} 0 R" for object_number in page_object_numbers)
    object_entries[1] = f"<< /Type /Pages /Kids [{page_refs}] /Count {len(page_object_numbers)} >>".encode("ascii")
    object_entries[2] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    stream_started_at = time.perf_counter()
    page_count = len(page_object_numbers)
    for page_index, (
        page_type,
        page_object_number,
        content_object_number,
        image_object_numbers,
        xobject_entries,
        content_stream,
        rendered_views,
    ) in enumerate(pending_pages, start=1):
        if page_type == "views":
            page_object = (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 595] "
                f"/Resources << /Font << /F1 3 0 R >> "
                f"/XObject << {xobject_entries} >> >> "
                f"/Contents {content_object_number} 0 R >>"
            ).encode("ascii")
        else:
            page_object = (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 595] "
                f"/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {content_object_number} 0 R >>"
            ).encode("ascii")
        object_entries.append(page_object)
        content_stream = f"{content_stream}\n{_pdf_page_number_line(page_index, page_count)}"
        object_entries.append(_pdf_stream(content_stream))

        if page_type == "views":
            for label in labels:
                object_entries.append(_pdf_image_stream(rendered_views[label]))
    if profiler is not None:
        profiler.record_since("pdf image compression", stream_started_at)

    assembly_started_at = time.perf_counter()
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
    if profiler is not None:
        profiler.record_since("pdf assembly", assembly_started_at)

    disk_write_started_at = time.perf_counter()
    with open(filepath, "wb") as handle:
        handle.write(output)
    if profiler is not None:
        profiler.record_since("pdf disk write", disk_write_started_at)
        profiler.record_since("pdf write total", write_started_at)


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
        if getattr(self, "_pdf_steps", None) is not None:
            self.report({'WARNING'}, "PDF generation is already running")
            return {'CANCELLED'}

        objects = _visible_mesh_objects(context)
        if not objects:
            self.report({'ERROR'}, "No visible mesh objects found for PDF drawings")
            return {'CANCELLED'}

        structure_objects = [obj for obj in objects if _is_structure_object(obj)]
        structure_groups = _connected_structure_groups(structure_objects) if structure_objects else [objects]
        conversion_workers = min(len(structure_groups) * 4, PDF_MAX_CONVERSION_WORKERS)
        self._pdf_started_at = time.perf_counter()
        self._pdf_profiler = _PdfPhaseProfiler()
        self._pdf_conversion_executor = ThreadPoolExecutor(max_workers=max(1, conversion_workers))
        self._pdf_timer = context.window_manager.event_timer_add(0.01, window=context.window)
        self._pdf_steps = self._generate_pdf_steps(context, objects, structure_groups)
        self._pdf_last_step_finished_at = time.perf_counter()
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        progress = getattr(self, "_pdf_progress", None)
        if progress is not None:
            progress.update_mouse(event)

        if event.type == 'ESC':
            self._cancel_pdf_generation(context, "PDF generation cancelled")
            return {'CANCELLED'}

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        event_timer = getattr(event, "timer", None)
        if event_timer is not None and event_timer != getattr(self, "_pdf_timer", None):
            return {'PASS_THROUGH'}

        profiler = getattr(self, "_pdf_profiler", None)
        step_started_at = time.perf_counter()
        last_step_finished_at = getattr(self, "_pdf_last_step_finished_at", None)
        if profiler is not None:
            profiler.count("modal timer ticks")
            if last_step_finished_at is not None:
                profiler.record("modal idle gap", max(0.0, step_started_at - last_step_finished_at))

        try:
            next(self._pdf_steps)
        except StopIteration:
            elapsed = time.perf_counter() - getattr(self, "_pdf_started_at", time.perf_counter())
            profiler = getattr(self, "_pdf_profiler", _PdfPhaseProfiler())
            self._finish_pdf_generation(context)
            profile_summary = profiler.summary()
            count_summary = profiler.count_summary()
            slow_view_summary = profiler.slow_view_summary()
            slow_structure_summary = profiler.slow_structure_summary()
            self.report({'INFO'}, f"PDF drawings created in {elapsed:.1f} seconds: {os.path.basename(self.filepath)}")
            self.report({'INFO'}, f"PDF timing: {profile_summary}")
            self.report({'INFO'}, f"PDF counts: {count_summary}")
            print(f"Stagehand PDF timing: total {elapsed:.1f}s; {profile_summary}")
            print(f"Stagehand PDF counts: {count_summary}")
            print(f"Stagehand PDF slow renders: {slow_view_summary}")
            print(f"Stagehand PDF slow structures: {slow_structure_summary}")
            return {'FINISHED'}
        except Exception as exc:
            self._cancel_pdf_generation(context, f"Unable to generate PDF drawings: {exc}")
            return {'CANCELLED'}

        self._pdf_last_step_finished_at = time.perf_counter()
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        self._cancel_pdf_generation(context, "PDF generation cancelled")

    def _finish_pdf_generation(self, context):
        self._remove_pdf_timer(context)
        self._shutdown_pdf_conversion_executor(wait=False)
        self._pdf_steps = None
        self._pdf_progress = None
        self._pdf_profiler = None
        self._pdf_last_step_finished_at = None

    def _cancel_pdf_generation(self, context, message):
        pdf_steps = getattr(self, "_pdf_steps", None)
        if pdf_steps is not None:
            pdf_steps.close()
            self._pdf_steps = None

        self._remove_pdf_timer(context)
        self._shutdown_pdf_conversion_executor(wait=False, cancel_futures=True)
        self._pdf_progress = None
        self._pdf_profiler = None
        self._pdf_last_step_finished_at = None
        self.report({'ERROR'}, message)

    def _remove_pdf_timer(self, context):
        pdf_timer = getattr(self, "_pdf_timer", None)
        if pdf_timer is not None:
            context.window_manager.event_timer_remove(pdf_timer)
            self._pdf_timer = None

    def _shutdown_pdf_conversion_executor(self, wait=True, cancel_futures=False):
        conversion_executor = getattr(self, "_pdf_conversion_executor", None)
        if conversion_executor is not None:
            profiler = getattr(self, "_pdf_profiler", None)
            shutdown_started_at = time.perf_counter()
            conversion_executor.shutdown(wait=wait, cancel_futures=cancel_futures)
            if profiler is not None:
                profiler.record_since("conversion executor shutdown", shutdown_started_at)
            self._pdf_conversion_executor = None

    def _generate_pdf_steps(self, context, objects, structure_groups):
        scene = context.scene
        progress = _ProgressReporter(
            context,
            PDF_PROGRESS_METADATA_STEPS + (len(structure_groups) * 4) + PDF_PROGRESS_WRITE_STEPS,
        )
        profiler = getattr(self, "_pdf_profiler", None)
        conversion_executor = getattr(self, "_pdf_conversion_executor", None)
        self._pdf_progress = progress

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
                "show_object_outline",
            ),
        )

        rendered_pages = []
        page_titles = []
        temporary_line_objects = []
        white_material = None
        original_hide_render = []

        try:
            progress.begin("Preparing PDF drawing data")
            if profiler is not None:
                profiler.count("structures", len(structure_groups))
                profiler.count("views", len(structure_groups) * 4)
            yield
            data_started_at = time.perf_counter()
            bom_entries = _collect_bom_entries(objects)
            details_data = _collect_structure_details(objects)
            if profiler is not None:
                profiler.record_since("drawing data", data_started_at)
            progress.advance(message="PDF drawing data ready")
            yield

            render_config_started_at = time.perf_counter()
            scene.render.resolution_x = RENDER_WIDTH
            scene.render.resolution_y = RENDER_HEIGHT
            scene.render.resolution_percentage = 100
            scene.render.image_settings.file_format = 'PNG'

            _set_render_engine(scene)
            _configure_line_render(scene, context.view_layer)
            if profiler is not None:
                profiler.record_since("render config", render_config_started_at)
            temp_directory_started_at = time.perf_counter()
            with tempfile.TemporaryDirectory() as temp_directory:
                for group_index, group_objects in enumerate(structure_groups, start=1):
                    structure_total_started_at = time.perf_counter()
                    page_title = _structure_collection_name(group_objects, f"{_project_name()} - Structure {group_index}")
                    page_temp_directory = Path(temp_directory) / f"structure_{group_index}"
                    page_temp_directory.mkdir(exist_ok=True)
                    line_objects_started_at = time.perf_counter()
                    temporary_line_objects, white_material, original_hide_render = _create_line_render_objects(
                        scene,
                        group_objects,
                        objects,
                    )
                    if profiler is not None:
                        profiler.record_since("line object setup", line_objects_started_at)
                        profiler.count("line objects", len(temporary_line_objects))
                    structure_started_at = time.perf_counter()
                    group_center, _group_dimensions = _object_bounds(group_objects)
                    group_segments = _build_structure_segments(group_objects) if group_objects else []
                    group_rotation = _structure_rotation(group_objects)
                    group_dimension_candidates = _build_dimension_candidates(group_segments, group_rotation)
                    if profiler is not None:
                        profiler.record_since("structure prep", structure_started_at)
                    rendered_views = {}
                    non_iso_dimension_candidate_indices = set()

                    try:
                        for view_name in ("Front", "Left", "Top", "Iso"):
                            progress.set_message(
                                f"Rendering structure {group_index}/{len(structure_groups)}: {view_name}"
                            )
                            rendered_views[view_name] = _render_view(
                                context,
                                view_name,
                                group_center,
                                group_objects,
                                group_dimension_candidates,
                                group_rotation,
                                page_temp_directory,
                                profiler=profiler,
                                conversion_executor=conversion_executor,
                                profile_label=f"structure {group_index} {view_name}",
                                allowed_dimension_candidate_indices=(
                                    non_iso_dimension_candidate_indices if view_name == "Iso" else None
                                ),
                                visible_dimension_candidate_indices=(
                                    None if view_name == "Iso" else non_iso_dimension_candidate_indices
                                ),
                            )
                            progress.advance(
                                message=f"Rendered structure {group_index}/{len(structure_groups)}: {view_name}"
                            )
                    finally:
                        line_cleanup_started_at = time.perf_counter()
                        _remove_line_render_objects(temporary_line_objects, white_material, original_hide_render)
                        if profiler is not None:
                            profiler.record_since("line object cleanup", line_cleanup_started_at)
                        temporary_line_objects = []
                        white_material = None
                        original_hide_render = []

                    if profiler is not None:
                        profiler.record_structure(f"structure {group_index}", time.perf_counter() - structure_total_started_at)
                    page_titles.append(page_title)
                    rendered_pages.append(rendered_views)
                    yield
            if profiler is not None:
                profiler.record_since("temporary directory total", temp_directory_started_at)

            progress.set_message("Writing PDF file")
            yield
            progress.set_message("Finishing image conversion")
            yield
            _resolve_rendered_pages(rendered_pages, profiler=profiler)
            progress.set_message("Writing PDF file")
            yield
            _write_pdf(
                self.filepath,
                rendered_pages,
                _project_name(),
                page_titles=page_titles,
                bom_entries=bom_entries,
                details_data=details_data,
                profiler=profiler,
            )
            progress.advance(message="PDF file written")
            yield
        finally:
            progress_finish_started_at = time.perf_counter()
            progress.finish()
            if profiler is not None:
                profiler.record_since("progress finish", progress_finish_started_at)
            self._pdf_progress = None
            final_line_cleanup_started_at = time.perf_counter()
            _remove_line_render_objects(temporary_line_objects, white_material, original_hide_render)
            if profiler is not None:
                profiler.record_since("final line cleanup", final_line_cleanup_started_at)
            restore_started_at = time.perf_counter()
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
            if profiler is not None:
                profiler.record_since("render state restore", restore_started_at)


classes = (
    STAGEHAND_OT_generate_pdf_drawings,
)


def register():
    for cls in classes:
        safe_register_class(cls)


def unregister():
    for cls in reversed(classes):
        safe_unregister_class(cls)
