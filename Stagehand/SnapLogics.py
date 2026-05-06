import bpy
from bpy_extras import view3d_utils
from mathutils import Quaternion
from mathutils import Vector

from . import Connections
from .LinkTypes import are_link_types_compatible
from .RegistrationUtils import safe_register_class, safe_remove_keymaps, safe_unregister_class


addon_keymaps = []


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


def _find_best_snap_pair(context, moving_objects):
    moving_names = {obj.name_full for obj in moving_objects}
    best_pair = None
    best_screen_distance = float('inf')
    best_distance = float('inf')
    best_angle = float('inf')

    moving_links = []
    for obj in moving_objects:
        for link_index, link, center, rotation in _iter_available_links(obj):
            moving_links.append((obj, link_index, link, center, rotation))

    if not moving_links:
        return None

    for target_obj in bpy.data.objects:
        if target_obj.name_full in moving_names or not _is_stagehand_object(target_obj):
            continue

        for target_link_index, target_link, target_center, _target_rotation in _iter_available_links(target_obj):
            for moving_obj, moving_link_index, moving_link, moving_center, _moving_rotation in moving_links:
                if not are_link_types_compatible(moving_link.type, target_link.type):
                    continue

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

                moving_screen = view3d_utils.location_3d_to_region_2d(
                    context.region,
                    context.region_data,
                    moving_center,
                )
                target_screen = view3d_utils.location_3d_to_region_2d(
                    context.region,
                    context.region_data,
                    target_center,
                )
                if moving_screen is None or target_screen is None:
                    screen_distance = float('inf')
                else:
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
                        target_center,
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
        best_pair = _find_best_snap_pair(context, self.moving_objects)
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
            target_center,
        ) = best_pair

        snap_delta = target_center - moving_center
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
