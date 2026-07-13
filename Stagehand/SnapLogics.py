import bpy
from collections import defaultdict
from math import floor
from bpy_extras import view3d_utils
from mathutils import Quaternion
from mathutils import Vector

from . import Connections
from .LinkTypes import get_compatible_link_types
from .RegistrationUtils import safe_register_class, safe_remove_keymaps, safe_unregister_class


addon_keymaps = []
SNAP_INDEX_CELL_SIZE = 96.0


def _is_stagehand_object(obj):
    return (
        obj is not None
        and getattr(obj, "stagehand", None) is not None
        and obj.stagehand.is_stagehand_object
    )


def _selected_stagehand_objects(context):
    return [obj for obj in context.selected_objects if _is_stagehand_object(obj)]


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


def _iter_available_links(obj):
    for index, link in enumerate(obj.stagehand.links):
        if Connections.is_link_connected(link):
            continue
        center, rotation = _link_transform(obj, link)
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
    def __init__(self, context, moving_objects):
        self.region = context.region
        self.region_data = context.region_data
        self.cells_by_type = defaultdict(lambda: defaultdict(list))
        self.cell_keys_by_type = defaultdict(set)
        self.target_count = 0
        moving_names = {obj.name_full for obj in moving_objects}

        if self.region is None or self.region_data is None:
            return

        for target_obj in bpy.data.objects:
            if target_obj.name_full in moving_names or not _is_stagehand_object(target_obj):
                continue

            for target_link_index, target_link, target_center, target_rotation in _iter_available_links(target_obj):
                item = (target_obj, target_link_index, target_link, target_center, target_rotation)
                target_type = int(target_link.type)
                added_keys = self._add_item_to_cells(target_type, item)
                if added_keys:
                    self.target_count += 1

    def _project_point(self, point):
        return view3d_utils.location_3d_to_region_2d(self.region, self.region_data, point)

    def _screen_points_for_item(self, item):
        _target_obj, _target_link_index, target_link, target_center, target_rotation = item
        center_screen = self._project_point(target_center)
        if center_screen is not None:
            yield center_screen

        if not target_link.cylindricalType:
            return

        length = _cylindrical_link_length(target_link)
        if length <= 0.0:
            return

        axis = target_rotation @ Vector((0, 1, 0))
        end_center = target_center + (axis * length)
        end_screen = self._project_point(end_center)
        if end_screen is None:
            return

        if center_screen is None:
            yield end_screen
            return

        screen_length = (end_screen - center_screen).length
        sample_count = max(1, min(32, int(screen_length / SNAP_INDEX_CELL_SIZE) + 1))
        for sample_index in range(1, sample_count + 1):
            factor = sample_index / sample_count
            yield center_screen.lerp(end_screen, factor)

    def _add_item_to_cells(self, target_type, item):
        added_keys = set()
        for screen_point in self._screen_points_for_item(item):
            cell_key = _screen_cell_key(screen_point)
            if cell_key in added_keys:
                continue
            self.cells_by_type[target_type][cell_key].append(item)
            self.cell_keys_by_type[target_type].add(cell_key)
            added_keys.add(cell_key)
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

    def iter_candidates(self, source_link, moving_screen):
        target_types = self._compatible_target_types(source_link)
        if not target_types:
            return

        source_cell_key = _screen_cell_key(moving_screen)
        max_ring = self._max_search_ring(source_cell_key, target_types)
        if max_ring < 0:
            return

        seen_targets = set()
        for ring in range(max_ring + 1):
            for cell_key in self._ring_cell_keys(source_cell_key, ring):
                for target_type in target_types:
                    for item in self.cells_by_type.get(target_type, {}).get(cell_key, ()):
                        target_obj, target_link_index, _target_link, _target_center, _target_rotation = item
                        target_key = (target_obj.name_full, target_link_index)
                        if target_key in seen_targets:
                            continue
                        seen_targets.add(target_key)
                        yield ring, item

def _find_best_snap_pair(context, moving_objects, target_index=None):
    best_pair = None
    best_screen_distance = float('inf')
    best_distance = float('inf')
    best_angle = float('inf')

    if target_index is None:
        target_index = _ScreenLinkSpatialIndex(context, moving_objects)
    if target_index.target_count <= 0:
        return None

    moving_links = []
    for obj in moving_objects:
        for link_index, link, center, rotation in _iter_available_links(obj):
            moving_screen = view3d_utils.location_3d_to_region_2d(
                context.region,
                context.region_data,
                center,
            )
            if moving_screen is None:
                continue
            moving_links.append((obj, link_index, link, center, rotation, moving_screen))

    if not moving_links:
        return None

    for moving_obj, moving_link_index, moving_link, moving_center, moving_rotation, moving_screen in moving_links:
        last_ring = -1
        for ring, candidate in target_index.iter_candidates(moving_link, moving_screen):
            if ring != last_ring:
                if last_ring >= 2 and best_screen_distance <= ((last_ring - 1) * SNAP_INDEX_CELL_SIZE):
                    break
                last_ring = ring

            target_obj, target_link_index, target_link, target_center, target_rotation = candidate
            distance, angle = Connections.link_alignment_metrics(
                moving_obj,
                moving_link_index,
                target_obj,
                target_link_index,
            )
            if distance is None:
                continue
            if angle > Connections.AUTO_CONNECT_ANGLE_THRESHOLD:
                continue

            target_snap_point = Connections.link_snap_target_point(
                moving_link,
                moving_center,
                moving_rotation,
                target_link,
                target_center,
                target_rotation,
            )
            target_screen = view3d_utils.location_3d_to_region_2d(
                context.region,
                context.region_data,
                target_snap_point,
            )
            if target_screen is None:
                continue

            screen_distance = (target_screen - moving_screen).length
            if (screen_distance, distance, angle) < (best_screen_distance, best_distance, best_angle):
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

        Connections.prune_stale_connections()
        self.moving_objects = moving_objects
        self.initial_matrices = {obj.name_full: obj.matrix_world.copy() for obj in moving_objects}
        self.plane_origin = context.active_object.matrix_world.to_translation().copy()
        self.plane_normal = context.region_data.view_rotation @ Vector((0, 0, -1))
        self.start_plane_point = _screen_to_plane_point(context, event, self.plane_origin, self.plane_normal)
        self.target_index = _ScreenLinkSpatialIndex(context, moving_objects)

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
        current_plane_point = _screen_to_plane_point(context, event, self.plane_origin, self.plane_normal)
        delta = current_plane_point - self.start_plane_point
        self._translate_objects(delta)
        best_pair = _find_best_snap_pair(context, self.moving_objects, self.target_index)
        if best_pair is None:
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

        snap_delta = target_snap_point - moving_center
        for obj in self.moving_objects:
            obj.matrix_world.translation += snap_delta

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
