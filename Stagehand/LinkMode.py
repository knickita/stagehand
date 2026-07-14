import math

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from bpy.props import EnumProperty
from bpy_extras import view3d_utils
from mathutils import Matrix
from mathutils import Quaternion, Vector

from . import Connections
from .LinkTypes import are_link_types_compatible, visualize_in_editor
from . import LoadCatalogue
from .RegistrationUtils import (
    safe_define_property,
    safe_register_class,
    safe_remove_keymaps,
    safe_remove_property,
    safe_unregister_class,
)


addon_keymaps = []
_draw_handler = None

SPHERE_SEGMENTS = 24
SPHERE_COLOR = (1.0, 0.1, 0.1, 1.0)
CYAN_COLOR = (0.0, 1.0, 1.0, 1.0)
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


def _tag_view3d_redraw(context):
    screen = getattr(context, "screen", None)
    if screen is None:
        return

    for area in screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def _exit_link_mode(context, force_object_mode=True):
    _set_selecting_link_mode(context, False)
    _set_link_mode(context, False)
    if force_object_mode and context.mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except RuntimeError:
            pass
    _tag_view3d_redraw(context)


def _set_selecting_link_mode(
    context,
    enabled,
    *,
    target_object=None,
    target_link_index=-1,
    pending_anchor_object=None,
    selected_link_index=-1,
    pending_object_names=None,
):
    wm = context.window_manager
    wm.stagehand_selecting_link_mode_enabled = enabled
    wm.stagehand_selecting_target_object_name = target_object.name_full if enabled and target_object is not None else ""
    wm.stagehand_selecting_target_link_index = target_link_index if enabled else -1
    wm.stagehand_selecting_pending_anchor_object_name = (
        pending_anchor_object.name_full if enabled and pending_anchor_object is not None else ""
    )
    wm.stagehand_selecting_selected_link_index = selected_link_index if enabled else -1
    wm.stagehand_selecting_pending_object_names = (
        "|".join(pending_object_names) if enabled and pending_object_names else ""
    )


def _get_selecting_mode_target(context):
    wm = context.window_manager
    if not getattr(wm, "stagehand_selecting_link_mode_enabled", False):
        return None, -1, None, -1

    target_object = bpy.data.objects.get(getattr(wm, "stagehand_selecting_target_object_name", ""))
    target_link_index = getattr(wm, "stagehand_selecting_target_link_index", -1)
    pending_anchor_object = bpy.data.objects.get(
        getattr(wm, "stagehand_selecting_pending_anchor_object_name", "")
    )
    selected_link_index = getattr(wm, "stagehand_selecting_selected_link_index", -1)
    return target_object, target_link_index, pending_anchor_object, selected_link_index


def _get_pending_objects(context):
    wm = context.window_manager
    names_value = getattr(wm, "stagehand_selecting_pending_object_names", "")
    if not names_value:
        return []

    objects = []
    for object_name in names_value.split("|"):
        obj = bpy.data.objects.get(object_name)
        if obj is not None:
            objects.append(obj)
    return objects


def _apply_preview_alignment(context, imported_object, imported_link_index):
    target_object, target_link_index, _pending_anchor_object, _selected_link_index = _get_selecting_mode_target(context)
    if (
        target_object is None
        or imported_object is None
        or target_link_index < 0
        or imported_link_index < 0
        or target_link_index >= len(target_object.stagehand.links)
        or imported_link_index >= len(imported_object.stagehand.links)
    ):
        return False

    pending_objects = _get_pending_objects(context)
    if not pending_objects:
        pending_objects = [imported_object]

    target_link = target_object.stagehand.links[target_link_index]
    imported_link = imported_object.stagehand.links[imported_link_index]
    target_center, target_rotation = _link_transform(target_object, target_link)
    imported_center, imported_rotation = _link_transform(imported_object, imported_link)
    rotation_delta = Connections.link_alignment_rotation_delta(imported_rotation, target_rotation)
    _rotate_objects_around_pivot(pending_objects, rotation_delta, imported_center)
    imported_center, _imported_rotation = _link_transform(imported_object, imported_link)
    _translate_objects(pending_objects, target_center - imported_center)
    context.window_manager.stagehand_selecting_selected_link_index = imported_link_index
    return True


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


def _link_is_facing_camera(context, center, rotation):
    if context.region_data is None:
        return False

    if context.region_data.is_perspective:
        camera_position = context.region_data.view_matrix.inverted().translation
        to_camera = camera_position - center
        if to_camera.length_squared == 0.0:
            return False
        view_vector = to_camera.normalized()
    else:
        view_direction = context.region_data.view_rotation @ Vector((0, 0, -1))
        if view_direction.length_squared == 0.0:
            return False
        view_vector = (-view_direction).normalized()

    forward = _link_forward(rotation)
    if forward.length_squared == 0.0:
        return False

    return forward.normalized().dot(view_vector) > 0.0


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
        if Connections.is_link_connected(link):
            continue
        if not visualize_in_editor(link):
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


def _build_link_segments_for_items(link_items, color):
    line_segments = []
    filled_triangles = []

    for link, center, rotation, radius in link_items:
        if not visualize_in_editor(link):
            continue

        if link.cylindricalType:
            length = link.length if link.length > 0.0 else radius
            line_segments.extend(_cylinder_segments(center, rotation, radius, length))
        else:
            axis_x = rotation @ Vector((1, 0, 0))
            axis_z = rotation @ Vector((0, 0, 1))
            filled_triangles.extend(_filled_circle_triangles(center, radius, axis_x, axis_z))

    return color, line_segments, filled_triangles


def _iter_clickable_links(obj):
    context = bpy.context
    for index, link in enumerate(obj.stagehand.links):
        if Connections.is_link_connected(link):
            continue
        if not visualize_in_editor(link):
            continue

        if link.cylindricalType:
            continue

        radius = link.displayRadius if link.displayRadius > 0.0 else 0.1
        center, rotation = _link_transform(obj, link)
        if not _link_is_facing_camera(context, center, rotation):
            continue
        yield index, link, center, rotation, radius


def _iter_compatible_links(obj, target_link_type):
    context = bpy.context
    for index, link in enumerate(obj.stagehand.links):
        if Connections.is_link_connected(link):
            continue
        if not visualize_in_editor(link):
            continue
        if not are_link_types_compatible(link.type, target_link_type):
            continue

        radius = link.displayRadius if link.displayRadius > 0.0 else 0.1
        center, rotation = _link_transform(obj, link)
        if not _link_is_facing_camera(context, center, rotation):
            continue
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


def _pick_clicked_compatible_link(context, event, obj, target_link_type):
    if obj is None or context.region is None or context.region_data is None:
        return None

    mouse_position = Vector((event.mouse_region_x, event.mouse_region_y))
    best_hit = None
    best_distance = None

    for link_index, link, center, rotation, radius in _iter_compatible_links(obj, target_link_type):
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
    draw_groups = []

    target_object, target_link_index, pending_anchor_object, selected_link_index = _get_selecting_mode_target(context)
    if target_object is not None and pending_anchor_object is not None:
        if 0 <= target_link_index < len(target_object.stagehand.links):
            target_link = target_object.stagehand.links[target_link_index]
            target_center, target_rotation = _link_transform(target_object, target_link)
            target_radius = target_link.displayRadius if target_link.displayRadius > 0.0 else 0.1
            draw_groups.append(
                _build_link_segments_for_items(
                    [(target_link, target_center, target_rotation, target_radius)],
                    SPHERE_COLOR,
                )
            )

            compatible_items = list(_iter_compatible_links(pending_anchor_object, target_link.type))
            draw_groups.append(
                _build_link_segments_for_items(
                    [(link, center, rotation, radius) for _index, link, center, rotation, radius in compatible_items],
                    CYAN_COLOR,
                )
            )
            if 0 <= selected_link_index < len(pending_anchor_object.stagehand.links):
                selected_link = pending_anchor_object.stagehand.links[selected_link_index]
                if not Connections.is_link_connected(selected_link) and are_link_types_compatible(selected_link.type, target_link.type):
                    selected_center, selected_rotation = _link_transform(pending_anchor_object, selected_link)
                    selected_radius = selected_link.displayRadius if selected_link.displayRadius > 0.0 else 0.1
                    draw_groups.append(
                        _build_link_segments_for_items(
                            [(selected_link, selected_center, selected_rotation, selected_radius)],
                            SPHERE_COLOR,
                        )
                    )
    else:
        obj = _get_link_mode_object(context)
        if obj is None:
            _exit_link_mode(context, force_object_mode=False)
            return
        line_segments, filled_triangles = _build_link_segments(obj)
        draw_groups.append((SPHERE_COLOR, line_segments, filled_triangles))

    if not any(line_segments or filled_triangles for _color, line_segments, filled_triangles in draw_groups):
        return

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    gpu.state.face_culling_set("BACK")

    for color, line_segments, filled_triangles in draw_groups:
        if filled_triangles:
            circle_batch = batch_for_shader(shader, "TRIS", {"pos": filled_triangles})
            shader.bind()
            shader.uniform_float("color", color)
            circle_batch.draw(shader)

        if line_segments:
            shape_batch = batch_for_shader(shader, "LINES", {"pos": line_segments})
            shader.bind()
            shader.uniform_float("color", color)
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

        if getattr(wm, "stagehand_selecting_link_mode_enabled", False):
            target_object, target_link_index, pending_anchor_object, selected_link_index = _get_selecting_mode_target(context)
            if (
                target_object is not None
                and pending_anchor_object is not None
                and selected_link_index >= 0
            ):
                Connections.connect_links(target_object, target_link_index, pending_anchor_object, selected_link_index)

            _set_selecting_link_mode(context, False)
            if pending_anchor_object is not None:
                bpy.ops.object.select_all(action='DESELECT')
                pending_anchor_object.select_set(True)
                context.view_layer.objects.active = pending_anchor_object
                _set_link_mode(context, True, pending_anchor_object)
            else:
                _set_link_mode(context, False)

            _tag_view3d_redraw(context)
            return {'FINISHED'}

        if active_object is None:
            _exit_link_mode(context)
            return {'FINISHED'}

        if not _is_stagehand_object(active_object):
            if wm.stagehand_link_mode_enabled:
                _exit_link_mode(context)
                return {'FINISHED'}
            bpy.ops.object.editmode_toggle()
            return {'FINISHED'}

        current_object = _get_link_mode_object(context)
        if wm.stagehand_link_mode_enabled and current_object == active_object:
            _set_link_mode(context, False)
        else:
            if context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            Connections.prune_stale_connections()
            _set_link_mode(context, True, active_object)

        _tag_view3d_redraw(context)

        return {'FINISHED'}


class STAGEHAND_OT_exit_link_mode(bpy.types.Operator):
    bl_idname = "stagehand.exit_link_mode"
    bl_label = "Exit Link Mode"
    bl_description = "Exit Stagehand Link Mode"

    def execute(self, context):
        wm = context.window_manager
        if (
            not getattr(wm, "stagehand_link_mode_enabled", False)
            and not getattr(wm, "stagehand_selecting_link_mode_enabled", False)
        ):
            return {'PASS_THROUGH'}

        _exit_link_mode(context)
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
        imported_link = _find_imported_compatible_link(imported_objects, target_link.type)
        if imported_link is None:
            self.report({'ERROR'}, "Imported object has no compatible link")
            return {'CANCELLED'}

        imported_object, _imported_link_index, _imported_link, _imported_center = imported_link
        _set_link_mode(context, False)
        _set_selecting_link_mode(
            context,
            True,
            target_object=target_object,
            target_link_index=target_link_index,
            pending_anchor_object=imported_object,
            selected_link_index=-1,
            pending_object_names=[obj.name_full for obj in imported_objects],
        )

        _apply_preview_alignment(context, imported_object, _imported_link_index)

        bpy.ops.object.select_all(action='DESELECT')
        for obj in imported_objects:
            obj.select_set(True)
        context.view_layer.objects.active = imported_object

        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()

        return {'FINISHED'}


class STAGEHAND_OT_pick_link_for_add(bpy.types.Operator):
    bl_idname = "stagehand.pick_link_for_add"
    bl_label = "Pick Stagehand Link"
    bl_description = "Pick a Stagehand link and add a compatible object there"

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            return {'PASS_THROUGH'}

        wm = context.window_manager
        link_mode_active = getattr(wm, "stagehand_link_mode_enabled", False)
        selecting_mode_active = getattr(wm, "stagehand_selecting_link_mode_enabled", False)
        if not link_mode_active and not selecting_mode_active:
            return {'PASS_THROUGH'}

        target_object, target_link_index, pending_anchor_object, _selected_link_index = _get_selecting_mode_target(context)
        if target_object is not None and pending_anchor_object is not None:
            if target_link_index < 0 or target_link_index >= len(target_object.stagehand.links):
                return {'FINISHED'}

            target_link = target_object.stagehand.links[target_link_index]
            hit = _pick_clicked_compatible_link(context, event, pending_anchor_object, target_link.type)
            if hit is None:
                return {'FINISHED'}

            imported_object, imported_link_index, imported_link, imported_center = hit
            _apply_preview_alignment(context, imported_object, imported_link_index)

            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
            return {'FINISHED'}

        hit = _pick_clicked_link(context, event)
        if hit is None:
            return {'FINISHED'}

        obj, link_index, link, _center = hit
        compatible_items = _compatible_catalogue_items(link.type)
        if not compatible_items:
            self.report({'WARNING'}, "No compatible Stagehand objects found for this link")
            return {'CANCELLED'}

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

    esc_kmi = km.keymap_items.new(
        STAGEHAND_OT_exit_link_mode.bl_idname,
        type='ESC',
        value='PRESS',
    )
    addon_keymaps.append((km, esc_kmi))

    pick_kmi = km.keymap_items.new(
        STAGEHAND_OT_pick_link_for_add.bl_idname,
        type='LEFTMOUSE',
        value='PRESS',
    )
    addon_keymaps.append((km, pick_kmi))


def unregister_keymap():
    safe_remove_keymaps(addon_keymaps)


def register():
    global _draw_handler

    safe_register_class(STAGEHAND_OT_toggle_link_mode)
    safe_register_class(STAGEHAND_OT_exit_link_mode)
    safe_register_class(STAGEHAND_OT_add_from_link_popup)
    safe_register_class(STAGEHAND_OT_pick_link_for_add)
    safe_define_property(bpy.types.WindowManager, "stagehand_link_mode_enabled", bpy.props.BoolProperty(
        name="Stagehand Link Mode Enabled",
        default=False,
        options={'HIDDEN'},
    ))
    safe_define_property(bpy.types.WindowManager, "stagehand_link_mode_object_name", bpy.props.StringProperty(
        name="Stagehand Link Mode Object Name",
        default="",
        options={'HIDDEN'},
    ))
    safe_define_property(bpy.types.WindowManager, "stagehand_clicked_object_name", bpy.props.StringProperty(
        name="Stagehand Clicked Object Name",
        default="",
        options={'HIDDEN'},
    ))
    safe_define_property(bpy.types.WindowManager, "stagehand_clicked_link_index", bpy.props.IntProperty(
        name="Stagehand Clicked Link Index",
        default=-1,
        options={'HIDDEN'},
    ))
    safe_define_property(bpy.types.WindowManager, "stagehand_clicked_link_type", bpy.props.IntProperty(
        name="Stagehand Clicked Link Type",
        default=-1,
        options={'HIDDEN'},
    ))
    safe_define_property(bpy.types.WindowManager, "stagehand_selecting_link_mode_enabled", bpy.props.BoolProperty(
        name="Stagehand Selecting Link Mode Enabled",
        default=False,
        options={'HIDDEN'},
    ))
    safe_define_property(bpy.types.WindowManager, "stagehand_selecting_target_object_name", bpy.props.StringProperty(
        name="Stagehand Selecting Target Object Name",
        default="",
        options={'HIDDEN'},
    ))
    safe_define_property(bpy.types.WindowManager, "stagehand_selecting_target_link_index", bpy.props.IntProperty(
        name="Stagehand Selecting Target Link Index",
        default=-1,
        options={'HIDDEN'},
    ))
    safe_define_property(bpy.types.WindowManager, "stagehand_selecting_pending_anchor_object_name", bpy.props.StringProperty(
        name="Stagehand Selecting Pending Anchor Object Name",
        default="",
        options={'HIDDEN'},
    ))
    safe_define_property(bpy.types.WindowManager, "stagehand_selecting_selected_link_index", bpy.props.IntProperty(
        name="Stagehand Selecting Selected Link Index",
        default=-1,
        options={'HIDDEN'},
    ))
    safe_define_property(bpy.types.WindowManager, "stagehand_selecting_pending_object_names", bpy.props.StringProperty(
        name="Stagehand Selecting Pending Object Names",
        default="",
        options={'HIDDEN'},
    ))

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
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, 'WINDOW')
        except (RuntimeError, ValueError):
            pass
        _draw_handler = None

    unregister_keymap()

    safe_remove_property(bpy.types.WindowManager, "stagehand_clicked_link_type")
    safe_remove_property(bpy.types.WindowManager, "stagehand_clicked_link_index")
    safe_remove_property(bpy.types.WindowManager, "stagehand_clicked_object_name")
    safe_remove_property(bpy.types.WindowManager, "stagehand_selecting_pending_object_names")
    safe_remove_property(bpy.types.WindowManager, "stagehand_selecting_selected_link_index")
    safe_remove_property(bpy.types.WindowManager, "stagehand_selecting_pending_anchor_object_name")
    safe_remove_property(bpy.types.WindowManager, "stagehand_selecting_target_link_index")
    safe_remove_property(bpy.types.WindowManager, "stagehand_selecting_target_object_name")
    safe_remove_property(bpy.types.WindowManager, "stagehand_selecting_link_mode_enabled")
    safe_remove_property(bpy.types.WindowManager, "stagehand_link_mode_object_name")
    safe_remove_property(bpy.types.WindowManager, "stagehand_link_mode_enabled")
    safe_unregister_class(STAGEHAND_OT_pick_link_for_add)
    safe_unregister_class(STAGEHAND_OT_add_from_link_popup)
    safe_unregister_class(STAGEHAND_OT_exit_link_mode)
    safe_unregister_class(STAGEHAND_OT_toggle_link_mode)
