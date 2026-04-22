import math

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Quaternion, Vector


addon_keymaps = []
_draw_handler = None

SPHERE_SEGMENTS = 24
SPHERE_COLOR = (1.0, 0.1, 0.1, 1.0)


def _is_stagehand_object(obj):
    return (
        obj is not None
        and getattr(obj, "stagehand", None) is not None
        and obj.stagehand.is_stagehand_object
    )


def _get_link_mode_object(context):
    wm = context.window_manager
    if not getattr(wm, "stagehand_link_mode_enabled", False):
        return None

    object_name = getattr(wm, "stagehand_link_mode_object_name", "")
    if not object_name:
        return None

    obj = bpy.data.objects.get(object_name)
    if obj is None or not _is_stagehand_object(obj):
        return None

    return obj


def _set_link_mode(context, enabled, obj=None):
    wm = context.window_manager
    wm.stagehand_link_mode_enabled = enabled
    wm.stagehand_link_mode_object_name = obj.name_full if enabled and obj is not None else ""


def _circle_points(center, radius, axis_a, axis_b):
    points = []
    for index in range(SPHERE_SEGMENTS):
        angle_a = (2.0 * math.pi * index) / SPHERE_SEGMENTS
        angle_b = (2.0 * math.pi * (index + 1)) / SPHERE_SEGMENTS
        point_a = center + radius * ((math.cos(angle_a) * axis_a) + (math.sin(angle_a) * axis_b))
        point_b = center + radius * ((math.cos(angle_b) * axis_a) + (math.sin(angle_b) * axis_b))
        points.extend((point_a, point_b))
    return points


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


def _cylinder_segments(center, rotation, radius, length):
    axis_x = rotation @ Vector((1, 0, 0))
    axis_y = rotation @ Vector((0, 1, 0))
    axis_z = rotation @ Vector((0, 0, 1))

    base_center = center
    top_center = center + (axis_y * length)

    segments = []
    segments.extend(_circle_points(base_center, radius, axis_x, axis_z))
    segments.extend(_circle_points(top_center, radius, axis_x, axis_z))

    for direction in (axis_x, -axis_x, axis_z, -axis_z):
        segments.extend((
            base_center + (direction * radius),
            top_center + (direction * radius),
        ))

    return segments


def _build_link_segments(obj):
    shape_segments = []
    for link in obj.stagehand.links:
        radius = link.displayRadius if link.displayRadius > 0.0 else 0.1
        center, rotation = _link_transform(obj, link)

        if link.cylindricalType:
            length = link.length if link.length > 0.0 else radius
            shape_segments.extend(_cylinder_segments(center, rotation, radius, length))
            continue

        axis_x = rotation @ Vector((1, 0, 0))
        axis_y = rotation @ Vector((0, 1, 0))
        axis_z = rotation @ Vector((0, 0, 1))
        shape_segments.extend(_circle_points(center, radius, axis_x, axis_y))
        shape_segments.extend(_circle_points(center, radius, axis_y, axis_z))
        shape_segments.extend(_circle_points(center, radius, axis_x, axis_z))
    return shape_segments


def _draw_link_mode():
    context = bpy.context
    obj = _get_link_mode_object(context)
    if obj is None:
        return

    shape_segments = _build_link_segments(obj)
    if not shape_segments:
        return

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")

    if shape_segments:
        shape_batch = batch_for_shader(shader, "LINES", {"pos": shape_segments})
        shader.bind()
        shader.uniform_float("color", SPHERE_COLOR)
        shape_batch.draw(shader)

    gpu.state.blend_set("NONE")


class STAGEHAND_OT_toggle_link_mode(bpy.types.Operator):
    bl_idname = "stagehand.toggle_link_mode"
    bl_label = "Toggle Link Mode"
    bl_description = "Toggle Stagehand Link Mode for the selected object"

    def execute(self, context):
        active_object = context.active_object
        wm = context.window_manager

        if not _is_stagehand_object(active_object):
            if wm.stagehand_link_mode_enabled:
                _set_link_mode(context, False)
            bpy.ops.object.editmode_toggle()
            return {'FINISHED'}

        current_object = _get_link_mode_object(context)
        if wm.stagehand_link_mode_enabled and current_object == active_object:
            _set_link_mode(context, False)
        else:
            if context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            _set_link_mode(context, True, active_object)

        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()

        return {'FINISHED'}


def register_keymap():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    km = kc.keymaps.new(name='Object Mode', space_type='EMPTY')
    kmi = km.keymap_items.new(
        STAGEHAND_OT_toggle_link_mode.bl_idname,
        type='TAB',
        value='PRESS',
    )
    addon_keymaps.append((km, kmi))


def unregister_keymap():
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()


def register():
    global _draw_handler

    bpy.utils.register_class(STAGEHAND_OT_toggle_link_mode)
    bpy.types.WindowManager.stagehand_link_mode_enabled = bpy.props.BoolProperty(
        name="Stagehand Link Mode Enabled",
        default=False,
        options={'HIDDEN'},
    )
    bpy.types.WindowManager.stagehand_link_mode_object_name = bpy.props.StringProperty(
        name="Stagehand Link Mode Object Name",
        default="",
        options={'HIDDEN'},
    )

    register_keymap()

    if _draw_handler is None:
        _draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            _draw_link_mode,
            (),
            'WINDOW',
            'POST_VIEW',
        )


def unregister():
    global _draw_handler

    if _draw_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, 'WINDOW')
        _draw_handler = None

    unregister_keymap()

    del bpy.types.WindowManager.stagehand_link_mode_object_name
    del bpy.types.WindowManager.stagehand_link_mode_enabled
    bpy.utils.unregister_class(STAGEHAND_OT_toggle_link_mode)
