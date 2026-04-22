import math

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from bpy.props import EnumProperty
from bpy_extras import view3d_utils
from mathutils import Matrix
from mathutils import Quaternion, Vector

from . import Connections
from .LinkTypes import are_link_types_compatible
from . import LoadCatalogue


addon_keymaps = []
_draw_handler = None

SPHERE_SEGMENTS = 24
SPHERE_COLOR = (1.0, 0.1, 0.1, 1.0)
CLICK_PIXEL_RADIUS = 18.0


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


def _filled_circle_triangles(center, radius, axis_a, axis_b):
    triangles = []
    for index in range(SPHERE_SEGMENTS):
        angle_a = (2.0 * math.pi * index) / SPHERE_SEGMENTS
        angle_b = (2.0 * math.pi * (index + 1)) / SPHERE_SEGMENTS
        point_a = center + radius * ((math.cos(angle_a) * axis_a) + (math.sin(angle_a) * axis_b))
        point_b = center + radius * ((math.cos(angle_b) * axis_a) + (math.sin(angle_b) * axis_b))
        triangles.extend((center, point_b, point_a))
    return triangles


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


def _link_forward(rotation):
    return rotation @ Vector((0, 1, 0))


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
    line_segments = []
    filled_triangles = []
    for link in obj.stagehand.links:
        if link.connectedObjectUid:
            continue

        radius = link.displayRadius if link.displayRadius > 0.0 else 0.1
        center, rotation = _link_transform(obj, link)

        if link.cylindricalType:
            length = link.length if link.length > 0.0 else radius
            line_segments.extend(_cylinder_segments(center, rotation, radius, length))
            continue

        axis_x = rotation @ Vector((1, 0, 0))
        axis_z = rotation @ Vector((0, 0, 1))
        filled_triangles.extend(_filled_circle_triangles(center, radius, axis_x, axis_z))
    return line_segments, filled_triangles


def _iter_clickable_links(obj):
    for index, link in enumerate(obj.stagehand.links):
        if link.connectedObjectUid:
            continue

        if link.cylindricalType:
            continue

        radius = link.displayRadius if link.displayRadius > 0.0 else 0.1
        center, rotation = _link_transform(obj, link)
        yield index, link, center, rotation, radius


def _pick_clicked_link(context, event):
    obj = _get_link_mode_object(context)
    if obj is None or context.region is None or context.region_data is None:
        return None

    mouse_position = Vector((event.mouse_region_x, event.mouse_region_y))
    best_hit = None
    best_distance = None

    for link_index, link, center, rotation, radius in _iter_clickable_links(obj):
        screen_position = view3d_utils.location_3d_to_region_2d(
            context.region,
            context.region_data,
            center,
        )
        if screen_position is None:
            continue

        edge_world_position = center + (rotation @ Vector((1, 0, 0)) * radius)
        edge_screen_position = view3d_utils.location_3d_to_region_2d(
            context.region,
            context.region_data,
            edge_world_position,
        )
        pixel_radius = CLICK_PIXEL_RADIUS
        if edge_screen_position is not None:
            pixel_radius = max(CLICK_PIXEL_RADIUS, (edge_screen_position - screen_position).length)

        distance = (screen_position - mouse_position).length
        if distance <= pixel_radius and (best_distance is None or distance <= best_distance):
            best_hit = (obj, link_index, link, center)
            best_distance = distance

    return best_hit


def _compatible_catalogue_items(link_type):
    compatible_items = []
    for asset_id, asset_data in sorted(LoadCatalogue.CATALOGUE_BY_ID.items()):
        for link in asset_data.get("links", []):
            if are_link_types_compatible(link.get("type", -1), link_type):
                compatible_items.append((asset_id, asset_data))
                break
    return compatible_items


def _search_popup_items(_self, context):
    wm = context.window_manager
    clicked_link_type = getattr(wm, "stagehand_clicked_link_type", -1)
    items = []
    for asset_id, asset_data in _compatible_catalogue_items(clicked_link_type):
        items.append((str(asset_id), asset_data["name"], asset_data["name"]))
    return items


def _find_imported_compatible_link(imported_objects, target_link_type):
    for obj in imported_objects:
        if not _is_stagehand_object(obj):
            continue

        for link_index, link, center, _rotation, _radius in _iter_clickable_links(obj):
            if are_link_types_compatible(link.type, target_link_type):
                return obj, link_index, link, center

        for link_index, link in enumerate(obj.stagehand.links):
            if are_link_types_compatible(link.type, target_link_type):
                center, _rotation = _link_transform(obj, link)
                return obj, link_index, link, center

    return None


def _translate_objects(objects, delta):
    for obj in objects:
        matrix_world = obj.matrix_world.copy()
        matrix_world.translation += delta
        obj.matrix_world = matrix_world


def _rotate_objects_around_pivot(objects, rotation_delta, pivot):
    pivot_matrix = Matrix.Translation(pivot)
    rotation_matrix = rotation_delta.to_matrix().to_4x4()
    transform_matrix = pivot_matrix @ rotation_matrix @ pivot_matrix.inverted()

    for obj in objects:
        obj.matrix_world = transform_matrix @ obj.matrix_world


def _draw_link_mode():
    context = bpy.context
    obj = _get_link_mode_object(context)
    if obj is None:
        return

    line_segments, filled_triangles = _build_link_segments(obj)
    if not line_segments and not filled_triangles:
        return

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    gpu.state.face_culling_set("BACK")

    if filled_triangles:
        circle_batch = batch_for_shader(shader, "TRIS", {"pos": filled_triangles})
        shader.bind()
        shader.uniform_float("color", SPHERE_COLOR)
        circle_batch.draw(shader)

    if line_segments:
        shape_batch = batch_for_shader(shader, "LINES", {"pos": line_segments})
        shader.bind()
        shader.uniform_float("color", SPHERE_COLOR)
        shape_batch.draw(shader)

    gpu.state.face_culling_set("NONE")
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


class STAGEHAND_OT_add_from_link_popup(bpy.types.Operator):
    bl_idname = "stagehand.add_from_link_popup"
    bl_label = "Add Stagehand Object From Link"
    bl_property = "asset_id"

    asset_id: EnumProperty(
        name="Stagehand Object",
        description="Compatible Stagehand objects",
        items=_search_popup_items,
    )

    def invoke(self, context, event):
        del event

        items = _search_popup_items(self, context)
        if not items:
            self.report({'WARNING'}, "No compatible Stagehand objects found")
            return {'CANCELLED'}

        self.asset_id = items[0][0]
        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        wm = context.window_manager
        target_object = bpy.data.objects.get(getattr(wm, "stagehand_clicked_object_name", ""))
        target_link_index = getattr(wm, "stagehand_clicked_link_index", -1)

        if target_object is None or not _is_stagehand_object(target_object):
            self.report({'ERROR'}, "Clicked Stagehand link is no longer available")
            return {'CANCELLED'}

        if target_link_index < 0 or target_link_index >= len(target_object.stagehand.links):
            self.report({'ERROR'}, "Clicked Stagehand link index is invalid")
            return {'CANCELLED'}

        asset_data = LoadCatalogue.CATALOGUE_BY_ID.get(int(self.asset_id))
        if asset_data is None:
            self.report({'ERROR'}, "Selected Stagehand asset was not found in the catalogue")
            return {'CANCELLED'}

        imported_objects = LoadCatalogue._import_asset(asset_data)
        target_link = target_object.stagehand.links[target_link_index]
        target_center, target_rotation = _link_transform(target_object, target_link)
        imported_link = _find_imported_compatible_link(imported_objects, target_link.type)

        if imported_link is not None:
            imported_object, imported_link_index, _imported_link, imported_center = imported_link
            imported_center, imported_rotation = _link_transform(imported_object, _imported_link)
            target_forward = _link_forward(target_rotation)
            imported_forward = _link_forward(imported_rotation)
            rotation_delta = imported_forward.rotation_difference(-target_forward)
            _rotate_objects_around_pivot(imported_objects, rotation_delta, imported_center)
            imported_center, _imported_rotation = _link_transform(imported_object, _imported_link)
            _translate_objects(imported_objects, target_center - imported_center)
            Connections.connect_links(target_object, target_link_index, imported_object, imported_link_index)

        return {'FINISHED'}


class STAGEHAND_OT_pick_link_for_add(bpy.types.Operator):
    bl_idname = "stagehand.pick_link_for_add"
    bl_label = "Pick Stagehand Link"
    bl_description = "Pick a Stagehand link and add a compatible object there"

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            return {'PASS_THROUGH'}

        hit = _pick_clicked_link(context, event)
        if hit is None:
            return {'PASS_THROUGH'}

        obj, link_index, link, _center = hit
        compatible_items = _compatible_catalogue_items(link.type)
        if not compatible_items:
            self.report({'WARNING'}, "No compatible Stagehand objects found for this link")
            return {'CANCELLED'}

        wm = context.window_manager
        wm.stagehand_clicked_object_name = obj.name_full
        wm.stagehand_clicked_link_index = link_index
        wm.stagehand_clicked_link_type = int(link.type)
        return bpy.ops.stagehand.add_from_link_popup('INVOKE_DEFAULT')


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

    pick_kmi = km.keymap_items.new(
        STAGEHAND_OT_pick_link_for_add.bl_idname,
        type='LEFTMOUSE',
        value='PRESS',
    )
    addon_keymaps.append((km, pick_kmi))


def unregister_keymap():
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()


def register():
    global _draw_handler

    bpy.utils.register_class(STAGEHAND_OT_toggle_link_mode)
    bpy.utils.register_class(STAGEHAND_OT_add_from_link_popup)
    bpy.utils.register_class(STAGEHAND_OT_pick_link_for_add)
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
    bpy.types.WindowManager.stagehand_clicked_object_name = bpy.props.StringProperty(
        name="Stagehand Clicked Object Name",
        default="",
        options={'HIDDEN'},
    )
    bpy.types.WindowManager.stagehand_clicked_link_index = bpy.props.IntProperty(
        name="Stagehand Clicked Link Index",
        default=-1,
        options={'HIDDEN'},
    )
    bpy.types.WindowManager.stagehand_clicked_link_type = bpy.props.IntProperty(
        name="Stagehand Clicked Link Type",
        default=-1,
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

    del bpy.types.WindowManager.stagehand_clicked_link_type
    del bpy.types.WindowManager.stagehand_clicked_link_index
    del bpy.types.WindowManager.stagehand_clicked_object_name
    del bpy.types.WindowManager.stagehand_link_mode_object_name
    del bpy.types.WindowManager.stagehand_link_mode_enabled
    bpy.utils.unregister_class(STAGEHAND_OT_pick_link_for_add)
    bpy.utils.unregister_class(STAGEHAND_OT_add_from_link_popup)
    bpy.utils.unregister_class(STAGEHAND_OT_toggle_link_mode)
