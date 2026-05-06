import bpy
from bpy_extras import view3d_utils
from mathutils import Vector

from . import Connections
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
    bl_description = "Move selected Stagehand objects and update links on confirm"
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
