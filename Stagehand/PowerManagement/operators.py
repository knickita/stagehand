import bpy

from .. import ProjectDatabase
from .mesh import (
    THREEPHASE_POWER_LINES_MATERIAL_NAME,
    THREEPHASE_POWER_LINES_MESH_NAME,
    THREEPHASE_POWER_LINES_OBJECT_NAME,
    build_power_lines_mesh,
)
from .scene import collect_cable_anchor_points, generate_power_solution
from .solver import PowerSolverError
from ..RegistrationUtils import (
    safe_add_handler,
    safe_register_class,
    safe_remove_handler,
    safe_unregister_class,
)


CABLE_ANCHOR_POINTS_OBJECT_NAME = "Stagehand Cable Anchor Points"
CABLE_ANCHOR_POINTS_MESH_NAME = "Stagehand Cable Anchor Points Mesh"
CABLE_ANCHOR_POINTS_MATERIAL_NAME = "Stagehand Cable Anchor Point Material"
CABLE_OBSTACLE_NAME = "Cable Obstacle"
CABLE_OBSTACLE_COLLECTION_NAME = "cable obstacles"
CABLE_OBSTACLE_MATERIAL_NAME = "Stagehand Cable Obstacle Material"
POWER_OBSTACLE_PROPERTY = "stagehand_power_obstacle"
STAGEHAND_COLLECTION_NAME = "stagehand"

_ANCHOR_POINTS_REFRESH_PENDING = False
_ANCHOR_POINTS_REFRESHING = False


def _is_cable_obstacle(obj):
    name = obj.name.lower()
    return (
        obj.get(POWER_OBSTACLE_PROPERTY)
        or name.startswith("cable obstacle")
        or name.startswith("power obstacle")
    )


def _is_stagehand_object(obj):
    stagehand = getattr(obj, "stagehand", None)
    return stagehand is not None and stagehand.is_stagehand_object


def _anchor_points_visible():
    obj = bpy.data.objects.get(CABLE_ANCHOR_POINTS_OBJECT_NAME)
    return obj is not None and not obj.hide_viewport and not obj.hide_get()


def _stagehand_collection(context):
    collection = bpy.data.collections.get(STAGEHAND_COLLECTION_NAME)
    if collection is None:
        collection = bpy.data.collections.new(STAGEHAND_COLLECTION_NAME)

    scene = context.scene if context is not None else bpy.context.scene
    if scene is not None and all(child != collection for child in scene.collection.children):
        scene.collection.children.link(collection)

    return collection


def _move_to_stagehand_collection(obj, context):
    collection = _stagehand_collection(context)
    if all(existing != obj for existing in collection.objects):
        collection.objects.link(obj)

    for user_collection in list(obj.users_collection):
        if user_collection != collection:
            user_collection.objects.unlink(obj)


def _cable_obstacle_collection(context):
    collection = bpy.data.collections.get(CABLE_OBSTACLE_COLLECTION_NAME)
    if collection is None:
        collection = bpy.data.collections.new(CABLE_OBSTACLE_COLLECTION_NAME)

    scene = context.scene if context is not None else None
    if scene is not None and all(child != collection for child in scene.collection.children):
        scene.collection.children.link(collection)

    return collection


def _move_to_cable_obstacle_collection(obj, context):
    target_collection = _cable_obstacle_collection(context)
    if all(existing != obj for existing in target_collection.objects):
        target_collection.objects.link(obj)

    for collection in list(obj.users_collection):
        if collection != target_collection:
            collection.objects.unlink(obj)


def _normalize_cable_obstacle_collections(context):
    for obj in bpy.data.objects:
        if _is_cable_obstacle(obj):
            _move_to_cable_obstacle_collection(obj, context)


def _cable_obstacle_material():
    material = bpy.data.materials.get(CABLE_OBSTACLE_MATERIAL_NAME)
    if material is None:
        material = bpy.data.materials.new(CABLE_OBSTACLE_MATERIAL_NAME)
    material.diffuse_color = (1.0, 0.2, 0.05, 0.22)
    material.use_nodes = True
    material.blend_method = 'BLEND'

    node_tree = material.node_tree
    if node_tree is None:
        return material

    principled = node_tree.nodes.get("Principled BSDF")
    if principled is None:
        return material

    base_color_input = principled.inputs.get("Base Color")
    alpha_input = principled.inputs.get("Alpha")
    if base_color_input is not None:
        base_color_input.default_value = material.diffuse_color
    if alpha_input is not None:
        alpha_input.default_value = material.diffuse_color[3]

    return material


def _cable_anchor_points_material():
    material = bpy.data.materials.get(CABLE_ANCHOR_POINTS_MATERIAL_NAME)
    if material is None:
        material = bpy.data.materials.new(CABLE_ANCHOR_POINTS_MATERIAL_NAME)
    material.diffuse_color = (1.0, 0.82, 0.12, 1.0)
    return material


def _remove_existing_cable_anchor_points_object():
    existing = bpy.data.objects.get(CABLE_ANCHOR_POINTS_OBJECT_NAME)
    if existing is None:
        return

    existing_mesh = existing.data
    bpy.data.objects.remove(existing, do_unlink=True)
    if existing_mesh is not None and existing_mesh.users == 0:
        bpy.data.meshes.remove(existing_mesh)


def _cable_anchor_points_geometry(anchor_points):
    radius = 0.035
    vertices = []
    edges = []
    for point in anchor_points:
        base_index = len(vertices)
        x, y, z = point
        vertices.extend((
            (x - radius, y, z),
            (x + radius, y, z),
            (x, y - radius, z),
            (x, y + radius, z),
            (x, y, z - radius),
            (x, y, z + radius),
        ))
        edges.extend((
            (base_index, base_index + 1),
            (base_index + 2, base_index + 3),
            (base_index + 4, base_index + 5),
        ))
    return vertices, edges


def _set_cable_anchor_points_mesh(obj, anchor_points):
    vertices, edges = _cable_anchor_points_geometry(anchor_points)
    mesh = obj.data
    mesh.clear_geometry()
    mesh.from_pydata(vertices, edges, [])
    mesh.update(calc_edges=True)
    obj.hide_select = True


def _refresh_cable_anchor_points_object(obj):
    anchor_points = collect_cable_anchor_points()
    if not anchor_points:
        raise PowerSolverError("No cable anchor vertices were found in the scene.")
    _set_cable_anchor_points_mesh(obj, anchor_points)
    return len(anchor_points)


def _build_cable_anchor_points_object(context):
    anchor_points = collect_cable_anchor_points()
    if not anchor_points:
        raise PowerSolverError("No cable anchor vertices were found in the scene.")

    _remove_existing_cable_anchor_points_object()

    mesh = bpy.data.meshes.new(CABLE_ANCHOR_POINTS_MESH_NAME)
    vertices, edges = _cable_anchor_points_geometry(anchor_points)
    mesh.from_pydata(vertices, edges, [])
    mesh.update(calc_edges=True)

    obj = bpy.data.objects.new(CABLE_ANCHOR_POINTS_OBJECT_NAME, mesh)
    obj["stagehand_generated_cable_anchor_points"] = True
    obj.display_type = 'WIRE'
    obj.show_in_front = True
    obj.hide_select = True
    obj.hide_render = True
    obj.data.materials.append(_cable_anchor_points_material())
    _move_to_stagehand_collection(obj, context)
    return obj, len(anchor_points)


def _refresh_visible_cable_anchor_points():
    global _ANCHOR_POINTS_REFRESH_PENDING, _ANCHOR_POINTS_REFRESHING

    _ANCHOR_POINTS_REFRESH_PENDING = False
    obj = bpy.data.objects.get(CABLE_ANCHOR_POINTS_OBJECT_NAME)
    if obj is None or obj.hide_viewport or obj.hide_get():
        return None

    _ANCHOR_POINTS_REFRESHING = True
    try:
        _refresh_cable_anchor_points_object(obj)
    except Exception as exc:
        print(f"Unable to refresh cable anchor points: {exc}")
    finally:
        _ANCHOR_POINTS_REFRESHING = False

    return None


def _schedule_cable_anchor_points_refresh():
    global _ANCHOR_POINTS_REFRESH_PENDING

    if _ANCHOR_POINTS_REFRESH_PENDING:
        return
    _ANCHOR_POINTS_REFRESH_PENDING = True
    bpy.app.timers.register(_refresh_visible_cable_anchor_points, first_interval=0.05)


def cable_anchor_points_depsgraph_update_post(_scene, depsgraph):
    if _ANCHOR_POINTS_REFRESHING or not _anchor_points_visible():
        return

    for update in depsgraph.updates:
        updated_id = update.id
        if not isinstance(updated_id, bpy.types.Object):
            continue
        if updated_id.name == CABLE_ANCHOR_POINTS_OBJECT_NAME:
            continue
        if _is_stagehand_object(updated_id) or _is_cable_obstacle(updated_id):
            _schedule_cable_anchor_points_refresh()
            return


class STAGEHAND_OT_toggle_cable_anchor_points(bpy.types.Operator):
    bl_idname = "stagehand.toggle_cable_anchor_points"
    bl_label = "Show/Hide Anchor Points"
    bl_description = "Show or hide generated cable anchor point markers"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        existing = bpy.data.objects.get(CABLE_ANCHOR_POINTS_OBJECT_NAME)
        if existing is not None:
            should_hide = not existing.hide_viewport and not existing.hide_get()
            if not should_hide:
                try:
                    _refresh_cable_anchor_points_object(existing)
                    _move_to_stagehand_collection(existing, context)
                except PowerSolverError as exc:
                    self.report({'ERROR'}, str(exc))
                    return {'CANCELLED'}
                except Exception as exc:
                    self.report({'ERROR'}, f"Unable to refresh cable anchor points: {exc}")
                    return {'CANCELLED'}
            existing.hide_viewport = should_hide
            existing.hide_set(should_hide)
            existing.hide_select = True
            self.report({'INFO'}, "Cable anchor points hidden" if should_hide else "Cable anchor points shown")
            return {'FINISHED'}

        try:
            _obj, anchor_count = _build_cable_anchor_points_object(context)
        except PowerSolverError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, f"Unable to show cable anchor points: {exc}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Showing {anchor_count} cable anchor points")
        return {'FINISHED'}


class STAGEHAND_OT_add_cable_obstacle(bpy.types.Operator):
    bl_idname = "stagehand.add_cable_obstacle"
    bl_label = "Add Cable Obstacle"
    bl_description = "Create a movable/scalable/rotatable cable obstacle volume"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        location = context.scene.cursor.location if context.scene is not None else (0.0, 0.0, 0.0)
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=location)
        obj = context.object
        if obj is None:
            self.report({'ERROR'}, "Unable to create cable obstacle")
            return {'CANCELLED'}

        obj.name = CABLE_OBSTACLE_NAME
        obj.data.name = f"{CABLE_OBSTACLE_NAME} Mesh"
        obj[POWER_OBSTACLE_PROPERTY] = True
        obj.display_type = 'WIRE'
        obj.show_in_front = True
        obj.hide_render = True
        obj.data.materials.append(_cable_obstacle_material())
        _move_to_cable_obstacle_collection(obj, context)
        _normalize_cable_obstacle_collections(context)

        self.report({'INFO'}, "Cable obstacle created")
        return {'FINISHED'}


class STAGEHAND_OT_generate_power_lines(bpy.types.Operator):
    bl_idname = "stagehand.generate_power_lines"
    bl_label = "Generate Power Lines"
    bl_description = "Calculate cable routes and create one mesh containing all generated power lines"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            _normalize_cable_obstacle_collections(context)
            ProjectDatabase.clear_generated_powerlines()
            result = generate_power_solution(context)
            _obj, link_count, node_count, vertex_count, face_count = build_power_lines_mesh(
                context,
                result.solver,
                result.cable_anchor_offsets,
                result.power_line_routes,
                result.power_line_roots,
            )
            (
                _threephase_obj,
                threephase_link_count,
                threephase_node_count,
                threephase_vertex_count,
                threephase_face_count,
            ) = build_power_lines_mesh(
                context,
                result.solver,
                result.cable_anchor_offsets,
                result.threephase_routes,
                result.threephase_roots,
                object_name=THREEPHASE_POWER_LINES_OBJECT_NAME,
                mesh_name=THREEPHASE_POWER_LINES_MESH_NAME,
                material_name=THREEPHASE_POWER_LINES_MATERIAL_NAME,
                cable_radius_scale=2.0,
            )
            ProjectDatabase.set_generated_powerlines(result.generated_powerline_connections)
        except PowerSolverError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, f"Unable to generate power lines: {exc}")
            return {'CANCELLED'}

        used_outputs = len(result.power_line_output_assignments)
        used_threephase_outputs = len(result.threephase_output_assignments)
        message = (
            f"Generated {result.required_power_lines} monophase power lines from "
            f"{used_outputs} 16A outputs, {link_count} cable spans, "
            f"{node_count} cable nodes, {vertex_count} vertices, {face_count} faces; "
            f"generated {used_threephase_outputs} of {result.required_threephase_lines} "
            f"threephase cables, {threephase_link_count} cable spans, "
            f"{threephase_node_count} cable nodes, {threephase_vertex_count} vertices, "
            f"{threephase_face_count} faces"
        )
        if result.warnings:
            message += f" ({'; '.join(result.warnings)})"
        self.report({'WARNING'} if result.warnings else {'INFO'}, message)
        return {'FINISHED'}


classes = (
    STAGEHAND_OT_toggle_cable_anchor_points,
    STAGEHAND_OT_add_cable_obstacle,
    STAGEHAND_OT_generate_power_lines,
)


def register():
    for cls in classes:
        safe_register_class(cls)
    safe_add_handler(bpy.app.handlers.depsgraph_update_post, cable_anchor_points_depsgraph_update_post)


def unregister():
    safe_remove_handler(bpy.app.handlers.depsgraph_update_post, cable_anchor_points_depsgraph_update_post)
    for cls in reversed(classes):
        safe_unregister_class(cls)
