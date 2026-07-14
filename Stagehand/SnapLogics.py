import bpy
from collections import defaultdict
import time
from math import floor
from bpy_extras import view3d_utils
from mathutils import Quaternion
from mathutils import Vector

from . import Connections, ProjectDatabase
from .LinkTypes import get_compatible_link_types
from .RegistrationUtils import safe_register_class, safe_remove_keymaps, safe_unregister_class


addon_keymaps = []
SNAP_INDEX_CELL_SIZE = 96.0
SNAP_PROFILE = True
SNAP_PROFILE_INTERVAL = 0.5


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


def _connected_link_uids_from_database():
    return set(ProjectDatabase.get_connections(create=False).keys())


def _is_link_connected_cached(link, connected_link_uids):
    if connected_link_uids is None:
        return Connections.is_link_connected(link)

    link_uid = str(getattr(link, "uid", "") or "")
    return bool(link_uid and link_uid in connected_link_uids)

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


def _iter_available_links(obj, profile=None, profile_prefix="", connected_link_uids=None):
    for index, link in enumerate(obj.stagehand.links):
        _profile_count(profile, f"{profile_prefix}links_seen")
        connected_started_at = time.perf_counter()
        is_connected = _is_link_connected_cached(link, connected_link_uids)
        _profile_elapsed(profile, f"{profile_prefix}connected_ms", connected_started_at)
        if is_connected:
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


class _ScreenLinkSpatialIndex:
    def __init__(self, context, moving_objects, profile=None, connected_link_uids=None):
        self.region = context.region
        self.region_data = context.region_data
        self.cells_by_type = defaultdict(lambda: defaultdict(list))
        self.cell_keys_by_type = defaultdict(set)
        self.target_count = 0
        moving_names = {obj.name_full for obj in moving_objects}

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
            for target_link_index, target_link, target_center, target_rotation in _iter_available_links(target_obj, profile, "index_", connected_link_uids):
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

def _find_best_snap_pair(context, moving_objects, target_index=None, profile=None, connected_link_uids=None):
    find_started_at = time.perf_counter()
    best_pair = None
    best_screen_distance = float('inf')
    best_distance = float('inf')
    best_angle = float('inf')

    if target_index is None:
        if connected_link_uids is None:
            connected_link_uids = _connected_link_uids_from_database()
        target_index = _ScreenLinkSpatialIndex(
            context,
            moving_objects,
            profile,
            connected_link_uids,
        )
    if target_index.target_count <= 0:
        _profile_elapsed(profile, "find_total_ms", find_started_at)
        return None

    moving_started_at = time.perf_counter()
    moving_links = []
    for obj in moving_objects:
        for link_index, link, center, rotation in _iter_available_links(obj, profile, "moving_", connected_link_uids):
            _profile_count(profile, "moving_link_candidates")
            moving_project_started_at = time.perf_counter()
            moving_screen = view3d_utils.location_3d_to_region_2d(
                context.region,
                context.region_data,
                center,
            )
            _profile_elapsed(profile, "moving_project_ms", moving_project_started_at)
            if moving_screen is None:
                _profile_count(profile, "moving_project_rejects")
                continue
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
    bl_description = "Move selected Stagehand objects with continuous link snapping and update links on confirm"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            return {'CANCELLED'}

        moving_objects = _selected_stagehand_objects(context)
        if not moving_objects:
            self.report({'WARNING'}, "Select at least one Stagehand object")
            return {'CANCELLED'}

        invoke_started_at = time.perf_counter()
        prune_started_at = time.perf_counter()
        Connections.prune_stale_connections()
        prune_ms = _profile_ms(prune_started_at)
        self.moving_objects = moving_objects
        self.initial_matrices = {obj.name_full: obj.matrix_world.copy() for obj in moving_objects}
        self.plane_origin = context.active_object.matrix_world.to_translation().copy()
        self.plane_normal = context.region_data.view_rotation @ Vector((0, 0, -1))
        screen_started_at = time.perf_counter()
        self.start_plane_point = _screen_to_plane_point(context, event, self.plane_origin, self.plane_normal)
        screen_ms = _profile_ms(screen_started_at)
        connections_started_at = time.perf_counter()
        self.connected_link_uids = _connected_link_uids_from_database()
        connections_ms = _profile_ms(connections_started_at)
        index_started_at = time.perf_counter()
        index_profile = {} if SNAP_PROFILE else None
        self.target_index = _ScreenLinkSpatialIndex(
            context,
            moving_objects,
            index_profile,
            self.connected_link_uids,
        )
        index_ms = _profile_ms(index_started_at)
        self.last_profile_report_at = 0.0
        cell_count = sum(len(cells) for cells in self.target_index.cells_by_type.values())
        _profile_log(
            f"invoke total={_profile_ms(invoke_started_at):.2f}ms prune={prune_ms:.2f}ms "
            f"start_point={screen_ms:.2f}ms connections={connections_ms:.2f}ms index={index_ms:.2f}ms "
            f"connected_links={len(self.connected_link_uids)} targets={self.target_index.target_count} cells={cell_count}"
        )
        _profile_index_detail_log(index_profile)

        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _restore_initial_transforms(self):
        for obj in self.moving_objects:
            initial_matrix = self.initial_matrices.get(obj.name_full)
            if initial_matrix is not None:
                obj.matrix_world = initial_matrix.copy()

    def _translate_objects(self, delta):
        for obj in self.moving_objects:
            initial_matrix = self.initial_matrices.get(obj.name_full)
            if initial_matrix is None:
                continue
            matrix_world = initial_matrix.copy()
            matrix_world.translation += delta
            obj.matrix_world = matrix_world

    def _apply_current_motion(self, context, event):
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
            self.moving_objects,
            self.target_index,
            find_profile,
            self.connected_link_uids,
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
        if event.type == 'MOUSEMOVE':
            self._apply_current_motion(context, event)
            return {'RUNNING_MODAL'}

        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            Connections.refresh_connections_for_objects(self.moving_objects)
            return {'FINISHED'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self._restore_initial_transforms()
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}


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
    unregister_keymap()
    safe_unregister_class(STAGEHAND_OT_link_move_mode)
