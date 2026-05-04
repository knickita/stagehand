import bpy

from .mesh import build_power_lines_mesh
from .scene import collect_cable_anchor_points, generate_power_solution
from .solver import PowerSolverError
from ..RegistrationUtils import safe_register_class, safe_unregister_class


CABLE_ANCHOR_POINTS_OBJECT_NAME = "Stagehand Cable Anchor Points"
CABLE_ANCHOR_POINTS_MESH_NAME = "Stagehand Cable Anchor Points Mesh"
CABLE_ANCHOR_POINTS_MATERIAL_NAME = "Stagehand Cable Anchor Point Material"
CABLE_OBSTACLE_NAME = "Cable Obstacle"
CABLE_OBSTACLE_COLLECTION_NAME = "cable obstacles"
CABLE_OBSTACLE_MATERIAL_NAME = "Stagehand Cable Obstacle Material"
POWER_OBSTACLE_PROPERTY = "stagehand_power_obstacle"


def _is_cable_obstacle(obj):
    name = obj.name.lower()
    return (
        obj.get(POWER_OBSTACLE_PROPERTY)
        or name.startswith("cable obstacle")
        or name.startswith("power obstacle")
    )


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


def _build_cable_anchor_points_object(context):
    anchor_points = collect_cable_anchor_points()
    if not anchor_points:
        raise PowerSolverError("No cable anchor vertices were found in the scene.")

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

    _remove_existing_cable_anchor_points_object()

    mesh = bpy.data.meshes.new(CABLE_ANCHOR_POINTS_MESH_NAME)
    mesh.from_pydata(vertices, edges, [])
    mesh.update(calc_edges=True)

    obj = bpy.data.objects.new(CABLE_ANCHOR_POINTS_OBJECT_NAME, mesh)
    obj["stagehand_generated_cable_anchor_points"] = True
    obj.display_type = 'WIRE'
    obj.show_in_front = True
    obj.hide_render = True
    obj.data.materials.append(_cable_anchor_points_material())
    context.collection.objects.link(obj)
    return obj, len(anchor_points)


class STAGEHAND_OT_toggle_cable_anchor_points(bpy.types.Operator):
    bl_idname = "stagehand.toggle_cable_anchor_points"
    bl_label = "Show/Hide Anchor Points"
    bl_description = "Show or hide generated cable anchor point markers"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        existing = bpy.data.objects.get(CABLE_ANCHOR_POINTS_OBJECT_NAME)
        if existing is not None:
            should_hide = not existing.hide_viewport and not existing.hide_get()
            existing.hide_viewport = should_hide
            existing.hide_set(should_hide)
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
            result = generate_power_solution(context)
            _obj, link_count, node_count, vertex_count, face_count = build_power_lines_mesh(
                context,
                result.solver,
            )
        except PowerSolverError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, f"Unable to generate power lines: {exc}")
            return {'CANCELLED'}

        message = (
            f"Generated {link_count} cable spans, {node_count} cable nodes, "
            f"{vertex_count} vertices, {face_count} faces"
        )
        if result.warnings:
            message += f" ({'; '.join(result.warnings)})"
        self.report({'INFO'}, message)
        return {'FINISHED'}


classes = (
    STAGEHAND_OT_toggle_cable_anchor_points,
    STAGEHAND_OT_add_cable_obstacle,
    STAGEHAND_OT_generate_power_lines,
)


def register():
    for cls in classes:
        safe_register_class(cls)


def unregister():
    for cls in reversed(classes):
        safe_unregister_class(cls)
