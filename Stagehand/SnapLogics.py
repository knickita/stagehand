import bpy
import bmesh
import gpu
from collections import defaultdict
import time
from math import cos, floor, tau
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Quaternion, Vector

from . import Connections, LinkMode, ProjectDatabase
from .LinkTypes import get_compatible_link_types
from .RegistrationUtils import safe_register_class, safe_remove_keymaps, safe_unregister_class


addon_keymaps = []
SNAP_INDEX_CELL_SIZE = 96.0
SNAP_PROFILE = True
SNAP_PROFILE_INTERVAL = 0.5
SNAP_SOURCE_COLOR = (0.1, 0.75, 1.0)
SNAP_TARGET_COLOR = (1.0, 0.1, 0.1)
SNAP_PULSE_PERIOD = 1.0
_active_move_operators = []


def _profile_ms(started_at):
    return (time.perf_counter() - started_at) * 1000.0


def _profile_log(message):
    if SNAP_PROFILE:
        print(f"[Stagehand][Snap profile] {message}")


def _profile_count(profile, key, amount=1):
    if profile is not None:
        profile[key] = profile.get(key, 0) + amount


def _profile_elapsed(profile, key, started_at):
    if profile is not None:
        profile[key] = profile.get(key, 0.0) + _profile_ms(started_at)


def _profile_index_detail_log(profile):
    if profile is None:
        return

    _profile_log(
        f"index detail objects={profile.get('index_objects_seen', 0)} "
        f"stagehand_objects={profile.get('index_stagehand_objects', 0)} "
        f"links_seen={profile.get('index_links_seen', 0)} connected_skips={profile.get('index_connected_skips', 0)} "
        f"connected_check={profile.get('index_connected_ms', 0.0):.2f}ms "
        f"transform={profile.get('index_transform_ms', 0.0):.2f}ms "
        f"project={profile.get('index_project_ms', 0.0):.2f}ms "
        f"add_cells={profile.get('index_add_cells_ms', 0.0):.2f}ms "
        f"project_calls={profile.get('index_project_calls', 0)} screen_points={profile.get('index_screen_points', 0)} "
        f"cells_added={profile.get('index_cells_added', 0)}"
    )

def _profile_find_detail_log(profile):
    if profile is None:
        return

    _profile_log(
        f"find detail total={profile.get('find_total_ms', 0.0):.2f}ms "
        f"moving_collect={profile.get('moving_collect_ms', 0.0):.2f}ms "
        f"moving_connected={profile.get('moving_connected_ms', 0.0):.2f}ms "
        f"moving_transform={profile.get('moving_transform_ms', 0.0):.2f}ms "
        f"moving_project={profile.get('moving_project_ms', 0.0):.2f}ms "
        f"compatible={profile.get('compatible_types_ms', 0.0):.2f}ms "
        f"max_ring={profile.get('max_ring_ms', 0.0):.2f}ms "
        f"alignment={profile.get('alignment_metrics_ms', 0.0):.2f}ms "
        f"snap_point={profile.get('snap_point_ms', 0.0):.2f}ms "
        f"target_project={profile.get('target_project_ms', 0.0):.2f}ms"
    )
    _profile_log(
        f"find counts moving_candidates={profile.get('moving_link_candidates', 0)} "
        f"moving_seen={profile.get('moving_links_seen', 0)} moving_connected_skips={profile.get('moving_connected_skips', 0)} "
        f"moving_links={profile.get('moving_links', 0)} source_links={profile.get('source_links', 0)} "
        f"compatible_types={profile.get('compatible_type_count', 0)} "
        f"max_ring={profile.get('max_ring', -1)} rings={profile.get('rings_scanned', 0)} "
        f"cells={profile.get('cells_visited', 0)} cell_items={profile.get('cell_items_seen', 0)} "
        f"yields={profile.get('candidate_yields', 0)} tests={profile.get('candidate_tests', 0)} "
        f"duplicates={profile.get('duplicate_candidates', 0)} early_breaks={profile.get('early_breaks', 0)} "
        f"distance_rejects={profile.get('distance_rejects', 0)} angle_rejects={profile.get('angle_rejects', 0)} "
        f"target_project_rejects={profile.get('target_project_rejects', 0)} best_updates={profile.get('best_updates', 0)}"
    )

def _is_stagehand_object(obj):
    return (
        obj is not None
        and getattr(obj, "stagehand", None) is not None
        and obj.stagehand.is_stagehand_object
    )


def _selected_stagehand_objects(context):
    return [obj for obj in context.selected_objects if _is_stagehand_object(obj)]


def _connection_graph_from_database():
    return ProjectDatabase.get_connections(create=False)


def _is_link_available_cached(link, connections):
    if connections is None:
        return Connections.is_link_available(link)
    return Connections.is_link_available_in_connections(link, connections)

def _link_transform(obj, link):
    local_position = Vector(link.posDir[:3])
    local_rotation = Quaternion((
        link.posDir[6],
        link.posDir[3],
        link.posDir[4],
        link.posDir[5],
    ))
    world_rotation = obj.matrix_world.to_quaternion()
    center = obj.matrix_world.to_translation() + (world_rotation @ local_position)
    rotation = world_rotation @ local_rotation
    return center, rotation


def _iter_available_links(obj, profile=None, profile_prefix="", connections=None, link_types=None):
    for index, link in enumerate(obj.stagehand.links):
        _profile_count(profile, f"{profile_prefix}links_seen")
        if link_types is not None and int(link.type) not in link_types:
            continue
        connected_started_at = time.perf_counter()
        is_available = _is_link_available_cached(link, connections)
        _profile_elapsed(profile, f"{profile_prefix}connected_ms", connected_started_at)
        if not is_available:
            _profile_count(profile, f"{profile_prefix}connected_skips")
            continue

        transform_started_at = time.perf_counter()
        center, rotation = _link_transform(obj, link)
        _profile_elapsed(profile, f"{profile_prefix}transform_ms", transform_started_at)
        yield index, link, center, rotation

def _screen_cell_key(screen_position):
    return (
        floor(screen_position.x / SNAP_INDEX_CELL_SIZE),
        floor(screen_position.y / SNAP_INDEX_CELL_SIZE),
    )


def _cylindrical_link_length(link):
    if link.length > 0.0:
        return float(link.length)
    return float(link.displayRadius if link.displayRadius > 0.0 else 0.0)


def _pick_source_link(context, event, moving_objects):
    mouse = Vector((event.mouse_region_x, event.mouse_region_y))
    best_source = None
    best_distance = float('inf')
    for obj in moving_objects:
        for link_index, link in enumerate(obj.stagehand.links):
            center, rotation = _link_transform(obj, link)
            screen_point = view3d_utils.location_3d_to_region_2d(
                context.region, context.region_data, center,
            )
            if link.cylindricalType:
                end = center + (rotation @ Vector((0, _cylindrical_link_length(link), 0)))
                end_screen = view3d_utils.location_3d_to_region_2d(
                    context.region, context.region_data, end,
                )
                if screen_point is None:
                    screen_point = end_screen
                elif end_screen is not None:
                    segment = end_screen - screen_point
                    if segment.length_squared > 0.0:
                        factor = (mouse - screen_point).dot(segment) / segment.length_squared
                        screen_point = screen_point + segment * max(0.0, min(1.0, factor))
            if screen_point is None:
                continue
            distance = (screen_point - mouse).length_squared
            if distance < best_distance:
                best_distance = distance
                best_source = (obj, link_index)
    return best_source


class _ScreenLinkSpatialIndex:
    def __init__(self, context, moving_objects, profile=None, connections=None, source_link=None):
        self.region = context.region
        self.region_data = context.region_data
        self.cells_by_type = defaultdict(lambda: defaultdict(list))
        self.cell_keys_by_type = defaultdict(set)
        self.target_count = 0
        moving_names = {obj.name_full for obj in moving_objects}
        link_types = (
            {int(link_type) for link_type in get_compatible_link_types(source_link.type)}
            if source_link is not None else None
        )

        if self.region is None or self.region_data is None:
            return

        for target_obj in bpy.data.objects:
            _profile_count(profile, "index_objects_seen")
            if target_obj.name_full in moving_names:
                _profile_count(profile, "index_moving_object_skips")
                continue
            if not _is_stagehand_object(target_obj):
                _profile_count(profile, "index_non_stagehand_skips")
                continue

            _profile_count(profile, "index_stagehand_objects")
            for target_link_index, target_link, target_center, target_rotation in _iter_available_links(
                target_obj, profile, "index_", connections, link_types,
            ):
                item = (target_obj, target_link_index, target_link, target_center, target_rotation)
                target_type = int(target_link.type)
                add_started_at = time.perf_counter()
                added_keys = self._add_item_to_cells(target_type, item, profile)
                _profile_elapsed(profile, "index_add_cells_ms", add_started_at)
                if added_keys:
                    self.target_count += 1

    def _project_point(self, point):
        return view3d_utils.location_3d_to_region_2d(self.region, self.region_data, point)

    def _screen_points_for_item(self, item, profile=None):
        _target_obj, _target_link_index, target_link, target_center, target_rotation = item
        project_started_at = time.perf_counter()
        center_screen = self._project_point(target_center)
        _profile_elapsed(profile, "index_project_ms", project_started_at)
        _profile_count(profile, "index_project_calls")
        if center_screen is not None:
            _profile_count(profile, "index_screen_points")
            yield center_screen

        if not target_link.cylindricalType:
            return

        length = _cylindrical_link_length(target_link)
        if length <= 0.0:
            return

        axis = target_rotation @ Vector((0, 1, 0))
        end_center = target_center + (axis * length)
        project_started_at = time.perf_counter()
        end_screen = self._project_point(end_center)
        _profile_elapsed(profile, "index_project_ms", project_started_at)
        _profile_count(profile, "index_project_calls")
        if end_screen is None:
            return

        if center_screen is None:
            _profile_count(profile, "index_screen_points")
            yield end_screen
            return

        screen_length = (end_screen - center_screen).length
        sample_count = max(1, min(32, int(screen_length / SNAP_INDEX_CELL_SIZE) + 1))
        for sample_index in range(1, sample_count + 1):
            factor = sample_index / sample_count
            _profile_count(profile, "index_screen_points")
            yield center_screen.lerp(end_screen, factor)

    def _add_item_to_cells(self, target_type, item, profile=None):
        added_keys = set()
        for screen_point in self._screen_points_for_item(item, profile):
            cell_key = _screen_cell_key(screen_point)
            if cell_key in added_keys:
                continue
            self.cells_by_type[target_type][cell_key].append(item)
            self.cell_keys_by_type[target_type].add(cell_key)
            added_keys.add(cell_key)
            _profile_count(profile, "index_cells_added")
        return added_keys

    def _compatible_target_types(self, source_link):
        return [int(link_type) for link_type in get_compatible_link_types(source_link.type)]

    def _max_search_ring(self, source_cell_key, target_types):
        max_ring = -1
        source_x, source_y = source_cell_key
        for target_type in target_types:
            for cell_x, cell_y in self.cell_keys_by_type.get(target_type, ()):
                max_ring = max(max_ring, abs(cell_x - source_x), abs(cell_y - source_y))
        return max_ring

    def _ring_cell_keys(self, source_cell_key, ring):
        source_x, source_y = source_cell_key
        if ring == 0:
            yield source_cell_key
            return

        min_x = source_x - ring
        max_x = source_x + ring
        min_y = source_y - ring
        max_y = source_y + ring
        for cell_x in range(min_x, max_x + 1):
            yield cell_x, min_y
            yield cell_x, max_y
        for cell_y in range(min_y + 1, max_y):
            yield min_x, cell_y
            yield max_x, cell_y

    def iter_candidates(self, source_link, moving_screen, profile=None):
        type_started_at = time.perf_counter()
        target_types = self._compatible_target_types(source_link)
        _profile_elapsed(profile, "compatible_types_ms", type_started_at)
        _profile_count(profile, "source_links")
        _profile_count(profile, "compatible_type_count", len(target_types))
        if not target_types:
            return

        source_cell_key = _screen_cell_key(moving_screen)
        max_ring_started_at = time.perf_counter()
        max_ring = self._max_search_ring(source_cell_key, target_types)
        _profile_elapsed(profile, "max_ring_ms", max_ring_started_at)
        if profile is not None:
            profile["max_ring"] = max(profile.get("max_ring", -1), max_ring)
        if max_ring < 0:
            return

        seen_targets = set()
        for ring in range(max_ring + 1):
            _profile_count(profile, "rings_scanned")
            for cell_key in self._ring_cell_keys(source_cell_key, ring):
                _profile_count(profile, "cells_visited")
                for target_type in target_types:
                    cell_items = self.cells_by_type.get(target_type, {}).get(cell_key, ())
                    _profile_count(profile, "cell_items_seen", len(cell_items))
                    for item in cell_items:
                        target_obj, target_link_index, _target_link, _target_center, _target_rotation = item
                        target_key = (target_obj.name_full, target_link_index)
                        if target_key in seen_targets:
                            _profile_count(profile, "duplicate_candidates")
                            continue
                        seen_targets.add(target_key)
                        _profile_count(profile, "candidate_yields")
                        yield ring, item

def _find_best_snap_pair(context, source_link, target_index, profile=None):
    find_started_at = time.perf_counter()
    best_pair = None
    best_screen_distance = float('inf')
    best_distance = float('inf')
    best_angle = float('inf')

    if target_index.target_count <= 0:
        _profile_elapsed(profile, "find_total_ms", find_started_at)
        return None

    moving_started_at = time.perf_counter()
    obj, link_index = source_link
    link = obj.stagehand.links[link_index]
    _profile_count(profile, "moving_links_seen")
    _profile_count(profile, "moving_link_candidates")
    transform_started_at = time.perf_counter()
    center, rotation = _link_transform(obj, link)
    _profile_elapsed(profile, "moving_transform_ms", transform_started_at)
    moving_project_started_at = time.perf_counter()
    moving_screen = view3d_utils.location_3d_to_region_2d(
        context.region, context.region_data, center,
    )
    _profile_elapsed(profile, "moving_project_ms", moving_project_started_at)
    moving_links = []
    if moving_screen is None:
        _profile_count(profile, "moving_project_rejects")
    else:
        moving_links.append((obj, link_index, link, center, rotation, moving_screen))
    _profile_elapsed(profile, "moving_collect_ms", moving_started_at)
    if profile is not None:
        profile["moving_links"] = len(moving_links)

    if not moving_links:
        _profile_elapsed(profile, "find_total_ms", find_started_at)
        return None

    for moving_obj, moving_link_index, moving_link, moving_center, moving_rotation, moving_screen in moving_links:
        last_ring = -1
        for ring, candidate in target_index.iter_candidates(moving_link, moving_screen, profile):
            if ring != last_ring:
                if last_ring >= 2 and best_screen_distance <= ((last_ring - 1) * SNAP_INDEX_CELL_SIZE):
                    _profile_count(profile, "early_breaks")
                    break
                last_ring = ring

            target_obj, target_link_index, target_link, target_center, target_rotation = candidate
            _profile_count(profile, "candidate_tests")
            alignment_started_at = time.perf_counter()
            distance, angle = Connections.link_alignment_metrics(
                moving_obj,
                moving_link_index,
                target_obj,
                target_link_index,
            )
            _profile_elapsed(profile, "alignment_metrics_ms", alignment_started_at)
            if distance is None:
                _profile_count(profile, "distance_rejects")
                continue
            if angle > Connections.AUTO_CONNECT_ANGLE_THRESHOLD:
                _profile_count(profile, "angle_rejects")
                continue

            snap_point_started_at = time.perf_counter()
            target_snap_point = Connections.link_snap_target_point(
                moving_link,
                moving_center,
                moving_rotation,
                target_link,
                target_center,
                target_rotation,
            )
            _profile_elapsed(profile, "snap_point_ms", snap_point_started_at)
            target_project_started_at = time.perf_counter()
            target_screen = view3d_utils.location_3d_to_region_2d(
                context.region,
                context.region_data,
                target_snap_point,
            )
            _profile_elapsed(profile, "target_project_ms", target_project_started_at)
            if target_screen is None:
                _profile_count(profile, "target_project_rejects")
                continue

            screen_distance = (target_screen - moving_screen).length
            if (screen_distance, distance, angle) < (best_screen_distance, best_distance, best_angle):
                _profile_count(profile, "best_updates")
                best_screen_distance = screen_distance
                best_distance = distance
                best_angle = angle
                best_pair = (
                    moving_obj,
                    moving_link_index,
                    moving_link,
                    moving_center,
                    target_obj,
                    target_link_index,
                    target_link,
                    target_snap_point,
                )

    _profile_elapsed(profile, "find_total_ms", find_started_at)
    if profile is not None:
        profile["best_found"] = best_pair is not None
    return best_pair

def _screen_to_plane_point(context, event, plane_origin, plane_normal):
    coord = (event.mouse_region_x, event.mouse_region_y)
    ray_origin = view3d_utils.region_2d_to_origin_3d(context.region, context.region_data, coord)
    ray_direction = view3d_utils.region_2d_to_vector_3d(context.region, context.region_data, coord)

    denominator = ray_direction.dot(plane_normal)
    if abs(denominator) < 1e-6:
        return plane_origin.copy()

    distance = (plane_origin - ray_origin).dot(plane_normal) / denominator
    return ray_origin + (ray_direction * distance)


class STAGEHAND_OT_link_move_mode(bpy.types.Operator):
    bl_idname = "stagehand.link_move_mode"
    bl_label = "Stagehand Link Move"
    bl_description = "Hold L to choose a link, then release L to move with link snapping"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        if (
            context.area is None or context.area.type != 'VIEW_3D'
            or context.region is None or context.region.type != 'WINDOW'
            or context.region_data is None
        ):
            return {'CANCELLED'}

        moving_objects = _selected_stagehand_objects(context)
        if not moving_objects:
            self.report({'WARNING'}, "Select at least one Stagehand object")
            return {'CANCELLED'}

        self.source_link = _pick_source_link(context, event, moving_objects)
        if self.source_link is None:
            self.report({'WARNING'}, "No Stagehand link is visible in this view")
            return {'CANCELLED'}

        self.moving_objects = moving_objects
        self.initial_matrices = {obj.name_full: obj.matrix_world.copy() for obj in moving_objects}
        self._selecting_source = True
        self.target_index = None
        try:
            self._start_preview(context)
            if event.type != 'L':
                self._begin_snap_move(context, event)
            context.window_manager.modal_handler_add(self)
        except Exception:
            self._stop_preview()
            raise
        return {'RUNNING_MODAL'}

    def _select_source_link(self, context, event):
        source_link = _pick_source_link(context, event, self.moving_objects)
        if source_link is not None:
            self.source_link = source_link

    def _begin_snap_move(self, context, event):
        self._snap_target = None
        move_started_at = time.perf_counter()
        prune_started_at = time.perf_counter()
        Connections.prune_stale_connections()
        prune_ms = _profile_ms(prune_started_at)
        self.movement_matrices = {
            obj.name_full: obj.matrix_world.copy() for obj in self.moving_objects
        }
        source_object, source_index = self.source_link
        source_link = source_object.stagehand.links[source_index]
        self.plane_origin, _source_rotation = _link_transform(source_object, source_link)
        self.plane_normal = context.region_data.view_rotation @ Vector((0, 0, -1))
        screen_started_at = time.perf_counter()
        self.start_plane_point = _screen_to_plane_point(context, event, self.plane_origin, self.plane_normal)
        screen_ms = _profile_ms(screen_started_at)
        connections_started_at = time.perf_counter()
        self.connections = _connection_graph_from_database()
        connections_ms = _profile_ms(connections_started_at)
        index_started_at = time.perf_counter()
        index_profile = {} if SNAP_PROFILE else None
        self.target_index = _ScreenLinkSpatialIndex(
            context,
            self.moving_objects,
            index_profile,
            self.connections,
            source_link,
        )
        index_ms = _profile_ms(index_started_at)
        self.last_profile_report_at = 0.0
        cell_count = sum(len(cells) for cells in self.target_index.cells_by_type.values())
        _profile_log(
            f"start move total={_profile_ms(move_started_at):.2f}ms prune={prune_ms:.2f}ms "
            f"start_point={screen_ms:.2f}ms connections={connections_ms:.2f}ms index={index_ms:.2f}ms "
            f"connected_links={len(self.connections)} targets={self.target_index.target_count} cells={cell_count}"
        )
        _profile_index_detail_log(index_profile)
        self._selecting_source = False
        self._selection_batch = None
        self._selection_preview_source = None

    def _start_preview(self, context):
        self._area = context.area
        self._region = context.region
        self._window_manager = context.window_manager
        self._draw_handle = None
        self._pulse_timer = None
        self._snap_target = None
        self._target_outline_batch = None
        self._target_geometry_key = None
        self._preview_started_at = time.perf_counter()
        self._shader = gpu.shader.from_builtin('UNIFORM_COLOR')

        radius = 1.0
        center = Vector((0.0, 0.0, 0.0))
        axis_x = Vector((1.0, 0.0, 0.0))
        axis_y = Vector((0.0, 1.0, 0.0))
        axis_z = Vector((0.0, 0.0, 1.0))
        segments = LinkMode._circle_points(center, radius, axis_x, axis_z)
        segments.extend(LinkMode._circle_points(center, radius, axis_x, axis_y))
        segments.extend(LinkMode._circle_points(center, radius, axis_y, axis_z))
        self._sphere_outline_vertices = segments
        self._selection_batch = None
        self._selection_preview_source = None
        sphere = bmesh.new()
        try:
            bmesh.ops.create_uvsphere(sphere, u_segments=24, v_segments=12, radius=radius)
            bmesh.ops.triangulate(sphere, faces=list(sphere.faces))
            triangles = [vertex.co.copy() for face in sphere.faces for vertex in face.verts]
        finally:
            sphere.free()
        self._outline_batch = batch_for_shader(self._shader, 'LINES', {"pos": segments})
        self._fill_batch = (
            batch_for_shader(self._shader, 'TRIS', {"pos": triangles})
            if triangles else None
        )
        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw_source_link, (), 'WINDOW', 'POST_VIEW',
        )
        self._pulse_timer = context.window_manager.event_timer_add(
            1.0 / 30.0, window=context.window,
        )
        _active_move_operators.append(self)
        self._area.tag_redraw()

    def _build_selection_preview(self):
        source_object, source_index = self.source_link
        vertices = []
        for obj in self.moving_objects:
            for link_index, link in enumerate(obj.stagehand.links):
                if obj == source_object and link_index == source_index:
                    continue
                center, rotation = _link_transform(obj, link)
                radius = link.displayRadius if link.displayRadius > 0.0 else 0.1
                transform = Matrix.Translation(center) @ rotation.to_matrix().to_4x4() @ Matrix.Scale(radius, 4)
                vertices.extend(transform @ vertex for vertex in self._sphere_outline_vertices)
        self._selection_batch = (
            batch_for_shader(self._shader, 'LINES', {"pos": vertices}) if vertices else None
        )
        self._selection_preview_source = self.source_link

    def _draw_source_link(self):
        context = bpy.context
        if context.area != self._area or context.region != self._region:
            return
        try:
            obj, link_index = self.source_link
            link = obj.stagehand.links[link_index]
            center, rotation = _link_transform(obj, link)
            radius = link.displayRadius if link.displayRadius > 0.0 else 0.1
        except (ReferenceError, IndexError):
            return
        elapsed = time.perf_counter() - self._preview_started_at
        alpha = 0.35 + 0.65 * (0.5 + 0.5 * cos(tau * elapsed / SNAP_PULSE_PERIOD))
        previous_blend = gpu.state.blend_get()
        previous_depth = gpu.state.depth_test_get()
        previous_depth_mask = gpu.state.depth_mask_get()
        previous_width = gpu.state.line_width_get()
        try:
            gpu.state.blend_set('ALPHA')
            # Keep the anchor visible through scene geometry without changing its depth buffer.
            gpu.state.depth_test_set('NONE')
            gpu.state.depth_mask_set(False)
            gpu.state.line_width_set(2.0)
            if self._selecting_source:
                if self._selection_preview_source != self.source_link:
                    self._build_selection_preview()
                if self._selection_batch is not None:
                    self._shader.bind()
                    self._shader.uniform_float('color', (*LinkMode.SPHERE_COLOR[:3], 0.65))
                    self._selection_batch.draw(self._shader)
            with gpu.matrix.push_pop():
                gpu.matrix.multiply_matrix(
                    Matrix.Translation(center) @ rotation.to_matrix().to_4x4() @ Matrix.Scale(radius, 4)
                )
                self._shader.bind()
                if self._fill_batch is not None:
                    self._shader.uniform_float('color', (*SNAP_SOURCE_COLOR, alpha * 0.25))
                    self._fill_batch.draw(self._shader)
                self._shader.uniform_float('color', (*SNAP_SOURCE_COLOR, alpha))
                self._outline_batch.draw(self._shader)
            if not self._selecting_source and self._snap_target is not None:
                self._draw_snap_target()
        finally:
            gpu.state.line_width_set(previous_width)
            gpu.state.depth_mask_set(previous_depth_mask)
            gpu.state.depth_test_set(previous_depth)
            gpu.state.blend_set(previous_blend)

    def _draw_snap_target(self):
        obj, link_index = self._snap_target
        if any(obj == moving_object for moving_object in self.moving_objects):
            self._snap_target = None
            return
        try:
            link = obj.stagehand.links[link_index]
            center, rotation = _link_transform(obj, link)
            radius = link.displayRadius if link.displayRadius > 0.0 else 0.1
        except (ReferenceError, IndexError):
            self._snap_target = None
            return
        with gpu.matrix.push_pop():
            gpu.matrix.multiply_matrix(Matrix.Translation(center) @ rotation.to_matrix().to_4x4())
            self._shader.bind()
            if link.cylindricalType:
                length = _cylindrical_link_length(link)
                key = (obj.name_full, link_index, radius, length)
                if key != self._target_geometry_key:
                    vertices = LinkMode._cylinder_segments(Vector((0, 0, 0)), Quaternion(), radius, length)
                    self._target_outline_batch = batch_for_shader(self._shader, 'LINES', {"pos": vertices})
                    self._target_geometry_key = key
                previous_width = gpu.state.line_width_get()
                try:
                    for width, opacity in ((8.0, 0.06), (5.0, 0.2), (2.0, 1.0)):
                        gpu.state.line_width_set(width)
                        self._shader.uniform_float('color', (*SNAP_TARGET_COLOR, opacity))
                        self._target_outline_batch.draw(self._shader)
                finally:
                    gpu.state.line_width_set(previous_width)
                return
            for size, opacity in ((1.4, 0.035), (1.2, 0.08), (1.0, 0.18)):
                with gpu.matrix.push_pop():
                    gpu.matrix.multiply_matrix(Matrix.Scale(radius * size, 4))
                    self._shader.uniform_float('color', (*SNAP_TARGET_COLOR, opacity))
                    self._fill_batch.draw(self._shader)
            gpu.matrix.multiply_matrix(Matrix.Scale(radius, 4))
            self._shader.uniform_float('color', (*SNAP_TARGET_COLOR, 1.0))
            self._outline_batch.draw(self._shader)

    def _stop_preview(self):
        draw_handle = getattr(self, '_draw_handle', None)
        if draw_handle is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(draw_handle, 'WINDOW')
            except (RuntimeError, ValueError):
                pass
            self._draw_handle = None
        timer = getattr(self, '_pulse_timer', None)
        if timer is not None:
            try:
                self._window_manager.event_timer_remove(timer)
            except (ReferenceError, RuntimeError):
                pass
            self._pulse_timer = None
        self._outline_batch = None
        self._fill_batch = None
        self._shader = None
        self._selection_batch = None
        self._selection_preview_source = None
        self._sphere_outline_vertices = ()
        self._snap_target = None
        self._target_outline_batch = None
        self._target_geometry_key = None
        _active_move_operators[:] = [operator for operator in _active_move_operators if operator is not self]
        area = getattr(self, '_area', None)
        if area is not None:
            try:
                area.tag_redraw()
            except ReferenceError:
                pass

    def _restore_initial_transforms(self):
        for obj in self.moving_objects:
            initial_matrix = self.initial_matrices.get(obj.name_full)
            if initial_matrix is not None:
                obj.matrix_world = initial_matrix.copy()

    def _translate_objects(self, delta):
        for obj in self.moving_objects:
            initial_matrix = self.movement_matrices.get(obj.name_full)
            if initial_matrix is None:
                continue
            matrix_world = initial_matrix.copy()
            matrix_world.translation += delta
            obj.matrix_world = matrix_world

    def _apply_current_motion(self, context, event):
        self._snap_target = None
        motion_started_at = time.perf_counter()
        screen_started_at = time.perf_counter()
        current_plane_point = _screen_to_plane_point(context, event, self.plane_origin, self.plane_normal)
        screen_ms = _profile_ms(screen_started_at)
        delta = current_plane_point - self.start_plane_point
        translate_started_at = time.perf_counter()
        self._translate_objects(delta)
        translate_ms = _profile_ms(translate_started_at)
        find_started_at = time.perf_counter()
        find_profile = {} if SNAP_PROFILE else None
        best_pair = _find_best_snap_pair(
            context,
            self.source_link,
            self.target_index,
            find_profile,
        )
        find_ms = _profile_ms(find_started_at)
        if best_pair is None:
            now = time.perf_counter()
            if now - self.last_profile_report_at >= SNAP_PROFILE_INTERVAL:
                self.last_profile_report_at = now
                _profile_log(
                    f"move total={_profile_ms(motion_started_at):.2f}ms screen={screen_ms:.2f}ms "
                    f"translate={translate_ms:.2f}ms find={find_ms:.2f}ms snap=0.00ms best=no"
                )
                _profile_find_detail_log(find_profile)
            return
        (
            _moving_obj,
            _moving_link_index,
            _moving_link,
            moving_center,
            _target_obj,
            _target_link_index,
            _target_link,
            target_snap_point,
        ) = best_pair
        self._snap_target = (_target_obj, _target_link_index)

        snap_started_at = time.perf_counter()
        snap_delta = target_snap_point - moving_center
        for obj in self.moving_objects:
            obj.matrix_world.translation += snap_delta
        snap_ms = _profile_ms(snap_started_at)
        now = time.perf_counter()
        if now - self.last_profile_report_at >= SNAP_PROFILE_INTERVAL:
            self.last_profile_report_at = now
            _profile_log(
                f"move total={_profile_ms(motion_started_at):.2f}ms screen={screen_ms:.2f}ms "
                f"translate={translate_ms:.2f}ms find={find_ms:.2f}ms snap={snap_ms:.2f}ms best=yes"
            )
            _profile_find_detail_log(find_profile)

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:
            self.cancel(context)
            raise

    def _modal(self, context, event):
        if self._area.type != 'VIEW_3D':
            self.cancel(context)
            return {'CANCELLED'}

        if event.type == 'TIMER':
            self._area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'L':
            if event.value == 'PRESS':
                self._snap_target = None
                if not self._selecting_source:
                    self._selection_preview_source = None
                self._selecting_source = True
                self._select_source_link(context, event)
            elif event.value == 'RELEASE' and self._selecting_source:
                self._select_source_link(context, event)
                self._begin_snap_move(context, event)
            self._area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            if self._selecting_source:
                self._select_source_link(context, event)
            else:
                self._apply_current_motion(context, event)
            self._area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            if self._selecting_source:
                return {'RUNNING_MODAL'}
            self._stop_preview()
            Connections.refresh_connections_for_objects(self.moving_objects)
            return {'FINISHED'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self.cancel(context)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def cancel(self, context):
        self._stop_preview()
        self._restore_initial_transforms()


def register_keymap():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    for keymap_name, space_type in (('3D View', 'VIEW_3D'), ('Object Mode', 'EMPTY')):
        km = kc.keymaps.new(name=keymap_name, space_type=space_type)
        kmi = km.keymap_items.new(
            STAGEHAND_OT_link_move_mode.bl_idname,
            type='L',
            value='PRESS',
        )
        addon_keymaps.append((km, kmi))


def unregister_keymap():
    safe_remove_keymaps(addon_keymaps)


def register():
    safe_register_class(STAGEHAND_OT_link_move_mode)
    register_keymap()


def unregister():
    for operator in list(_active_move_operators):
        operator.cancel(bpy.context)
    unregister_keymap()
    safe_unregister_class(STAGEHAND_OT_link_move_mode)
