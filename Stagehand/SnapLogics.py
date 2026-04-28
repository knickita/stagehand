import bpy
from bpy_extras import view3d_utils
from mathutils import Quaternion, Vector

from . import Connections
from .LinkTypes import are_link_types_compatible
from .RegistrationUtils import safe_register_class, safe_remove_keymaps, safe_unregister_class


addon_keymaps = []
SNAP_DEBUG = True


def _debug_report(operator, message):
    if SNAP_DEBUG:
        operator.report({'INFO'}, f"Stagehand Snap: {message}")


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
    center = obj.matrix_world.to_translation() + (obj.matrix_world.to_quaternion() @ local_position)
    rotation = obj.matrix_world.to_quaternion() @ local_rotation
    return center, rotation


def _iter_available_links(obj):
    for index, link in enumerate(obj.stagehand.links):
        if Connections.is_link_connected(link):
            continue
        center, rotation = _link_transform(obj, link)
        yield index, link, center, rotation


def _find_best_snap_pair(moving_objects):
    moving_names = {obj.name_full for obj in moving_objects}
    best_pair = None
    best_distance = float('inf')

    moving_links = []
    for obj in moving_objects:
        for link_index, link, center, rotation in _iter_available_links(obj):
            moving_links.append((obj, link_index, link, center, rotation))

    if not moving_links:
        return None

    for target_obj in bpy.data.objects:
        if target_obj.name_full in moving_names or not _is_stagehand_object(target_obj):
            continue

        for target_link_index, target_link, target_center, target_rotation in _iter_available_links(target_obj):
            del target_rotation
            for moving_obj, moving_link_index, moving_link, moving_center, moving_rotation in moving_links:
                del moving_rotation
                if not are_link_types_compatible(moving_link.type, target_link.type):
                    continue

                distance = (target_center - moving_center).length
                if distance < best_distance:
                    best_distance = distance
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
    region = context.region
    rv3d = context.region_data
    coord = (event.mouse_region_x, event.mouse_region_y)
    ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    ray_direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)

    denominator = ray_direction.dot(plane_normal)
    if abs(denominator) < 1e-6:
        return plane_origin.copy()

    distance = (plane_origin - ray_origin).dot(plane_normal) / denominator
    return ray_origin + (ray_direction * distance)


def _begin_snap_motion(operator, context, event, moving_objects):
    operator.moving_objects = moving_objects
    operator.initial_matrices = {obj.name_full: obj.matrix_world.copy() for obj in moving_objects}
    operator.plane_origin = context.active_object.matrix_world.to_translation().copy()
    operator.plane_normal = context.region_data.view_rotation @ Vector((0, 0, -1))
    operator.start_plane_point = _screen_to_plane_point(context, event, operator.plane_origin, operator.plane_normal)
    operator.snap_was_used = False
    operator.debug_mousemove_reports = 0

    _debug_report(
        operator,
        "modal start "
        f"objects={[obj.name_full for obj in moving_objects]} "
        f"mouse=({event.mouse_region_x},{event.mouse_region_y}) "
        f"plane_origin=({operator.plane_origin.x:.3f},{operator.plane_origin.y:.3f},{operator.plane_origin.z:.3f})",
    )

    context.window_manager.modal_handler_add(operator)
    return {'RUNNING_MODAL'}


class _StagehandSnapMoveMixin:
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
        if getattr(self, "debug_mousemove_reports", 0) < 5 or event.ctrl:
            _debug_report(
                self,
                "mousemove "
                f"mouse=({event.mouse_region_x},{event.mouse_region_y}) "
                f"delta=({delta.x:.3f},{delta.y:.3f},{delta.z:.3f}) "
                f"len={delta.length:.3f} ctrl={event.ctrl}",
            )
            self.debug_mousemove_reports = getattr(self, "debug_mousemove_reports", 0) + 1
        self._translate_objects(delta)

        if event.ctrl:
            best_pair = _find_best_snap_pair(self.moving_objects)
            if best_pair is not None:
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
                self.snap_was_used = True

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            self._apply_current_motion(context, event)
            return {'RUNNING_MODAL'}

        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            _debug_report(
                self,
                f"confirm snap_was_used={getattr(self, 'snap_was_used', False)}",
            )
            if getattr(self, "snap_was_used", False):
                Connections.refresh_connections_for_objects(self.moving_objects)
            return {'FINISHED'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            _debug_report(self, "cancel")
            self._restore_initial_transforms()
            if getattr(self, "delete_on_cancel", False):
                for obj in list(self.moving_objects):
                    bpy.data.objects.remove(obj, do_unlink=True)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}


class STAGEHAND_OT_move_with_snap(_StagehandSnapMoveMixin, bpy.types.Operator):
    bl_idname = "stagehand.move_with_snap"
    bl_label = "Stagehand Snap Move"
    bl_description = "Move Stagehand objects and snap to compatible links while holding Ctrl"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        _debug_report(
            self,
            "move invoke "
            f"area={getattr(context.area, 'type', None)} "
            f"active={getattr(context.active_object, 'name_full', None)} "
            f"selected={[obj.name_full for obj in context.selected_objects]}",
        )
        if context.area is None or context.area.type != 'VIEW_3D':
            _debug_report(self, "move cancelled: not in VIEW_3D")
            return {'CANCELLED'}

        moving_objects = _selected_stagehand_objects(context)
        if not moving_objects:
            _debug_report(self, "no Stagehand selection, delegating to native transform.translate")
            return bpy.ops.transform.translate('INVOKE_DEFAULT')

        self.delete_on_cancel = False
        return _begin_snap_motion(self, context, event, moving_objects)


class STAGEHAND_OT_duplicate_with_snap(_StagehandSnapMoveMixin, bpy.types.Operator):
    bl_idname = "stagehand.duplicate_with_snap"
    bl_label = "Stagehand Snap Duplicate"
    bl_description = "Duplicate Stagehand objects and snap to compatible links while holding Ctrl"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        _debug_report(
            self,
            "duplicate invoke "
            f"area={getattr(context.area, 'type', None)} "
            f"active={getattr(context.active_object, 'name_full', None)} "
            f"selected={[obj.name_full for obj in context.selected_objects]}",
        )
        if context.area is None or context.area.type != 'VIEW_3D':
            _debug_report(self, "duplicate cancelled: not in VIEW_3D")
            return {'CANCELLED'}

        source_objects = _selected_stagehand_objects(context)
        if not source_objects:
            _debug_report(self, "no Stagehand selection, delegating to native duplicate_move")
            return bpy.ops.object.duplicate_move('INVOKE_DEFAULT')

        bpy.ops.object.duplicate()
        duplicated_objects = _selected_stagehand_objects(context)
        if not duplicated_objects:
            _debug_report(self, "duplicate cancelled: no duplicated Stagehand objects selected")
            return {'CANCELLED'}

        _debug_report(
            self,
            f"duplicated objects={[obj.name_full for obj in duplicated_objects]}",
        )
        self.delete_on_cancel = True
        return _begin_snap_motion(self, context, event, duplicated_objects)


def register_keymap():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    for keymap_name, space_type in (('3D View', 'VIEW_3D'), ('Object Mode', 'EMPTY')):
        km = kc.keymaps.new(name=keymap_name, space_type=space_type)
        kmi = km.keymap_items.new(
            STAGEHAND_OT_move_with_snap.bl_idname,
            type='G',
            value='PRESS',
        )
        addon_keymaps.append((km, kmi))

        duplicate_kmi = km.keymap_items.new(
            STAGEHAND_OT_duplicate_with_snap.bl_idname,
            type='D',
            value='PRESS',
            shift=True,
        )
        addon_keymaps.append((km, duplicate_kmi))


def unregister_keymap():
    safe_remove_keymaps(addon_keymaps)


def register():
    safe_register_class(STAGEHAND_OT_move_with_snap)
    safe_register_class(STAGEHAND_OT_duplicate_with_snap)
    register_keymap()


def unregister():
    unregister_keymap()
    safe_unregister_class(STAGEHAND_OT_duplicate_with_snap)
    safe_unregister_class(STAGEHAND_OT_move_with_snap)
