"""Stagehand controls editor.

Blender does not expose Python registration for native ``SpaceType`` objects.
This module therefore implements StagehandControls as a custom Node Editor
type, which is the supported Python extension point for a selectable editor
subtype.
"""

import bpy
from mathutils import Vector

from .RegistrationUtils import safe_register_class, safe_unregister_class


EDITOR_IDNAME = "StagehandControls"
EDITOR_DATA_NAME = "Stagehand Controls"
CONTROLS_NODE_IDNAME = "StagehandControlsNode"
CONTROLS_NODE_WIDTH = 680.0


class StagehandControls(bpy.types.NodeTree):
    """Node tree used as the StagehandControls editor type."""

    bl_idname = EDITOR_IDNAME
    bl_label = EDITOR_IDNAME
    bl_icon = 'TOOL_SETTINGS'


class StagehandControlsNode(bpy.types.Node):
    """Dashboard containing the main Stagehand commands."""

    bl_idname = CONTROLS_NODE_IDNAME
    bl_label = "Stagehand Controls"
    bl_icon = 'TOOL_SETTINGS'

    @classmethod
    def poll(cls, node_tree):
        return node_tree.bl_idname == EDITOR_IDNAME

    def init(self, _context):
        self.width = CONTROLS_NODE_WIDTH

    def draw_label(self):
        return "Stagehand Controls"

    def draw_buttons(self, _context, layout):
        row = layout.row(align=True)
        row.scale_y = 4.0

        create_column = row.column(align=True)
        create_column.operator(
            "stagehand.recipe_sixtema_stage",
            text="Crea Palco Sixtema",
            icon='ADD',
        )
        create_column.operator(
            "stagehand.recipe_selvoline_stage",
            text="Crea Palco Selvoline",
            icon='ADD',
        )
        create_column.operator(
            "stagehand.recipe_americana_structure",
            text="Crea Struttura Americana",
            icon='ADD',
        )

        row.separator(factor=2.0)

        scene_column = row.column(align=True)
        scene_column.scale_y = 1.5
        scene_column.operator("stagehand.delete_all", text="Cancella Tutto", icon='TRASH')
        scene_column.operator("stagehand.center_view", text="Centra Vista", icon='VIEW3D')

        row.separator(factor=2.0)

        export_column = row.column(align=True)
        export_column.scale_y = 1.5
        export_column.operator(
            "stagehand.export_rentman_csv",
            text="Esporta Lista Rentman",
            icon='EXPORT',
        )
        export_column.operator(
            "stagehand.generate_pdf_drawings",
            text="Esporta Disegni",
            icon='FILE_BLANK',
        )


def _get_or_create_editor_tree():
    tree = bpy.data.node_groups.get(EDITOR_DATA_NAME)
    if tree is not None and tree.bl_idname != EDITOR_IDNAME:
        tree = None

    if tree is None:
        tree = bpy.data.node_groups.new(EDITOR_DATA_NAME, EDITOR_IDNAME)

    controls_node = next(
        (node for node in tree.nodes if node.bl_idname == CONTROLS_NODE_IDNAME),
        None,
    )
    if controls_node is None:
        controls_node = tree.nodes.new(CONTROLS_NODE_IDNAME)
        controls_node.location = (0.0, 0.0)
    controls_node.width = CONTROLS_NODE_WIDTH

    for node in tree.nodes:
        node.select = node == controls_node
    tree.nodes.active = controls_node
    return tree


def _schedule_controls_view_fit(window, area):
    """Frame the dashboard after Blender has calculated its UI dimensions."""

    def fit_controls_view():
        try:
            if window.screen is None or area.type != 'NODE_EDITOR':
                return None

            space = area.spaces.active
            if space.tree_type != EDITOR_IDNAME:
                return None

            region = next(
                (candidate for candidate in area.regions if candidate.type == 'WINDOW'),
                None,
            )
            if region is None:
                return None

            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.node.view_all()
        except (ReferenceError, RuntimeError, TypeError):
            pass
        return None

    bpy.app.timers.register(fit_controls_view, first_interval=0.05)


class STAGEHAND_OT_open_controls(bpy.types.Operator):
    """Open StagehandControls in the current Blender area."""

    bl_idname = "stagehand.open_controls"
    bl_label = "StagehandControls"
    bl_description = "Open the StagehandControls editor in the current area"

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.window is not None

    def execute(self, context):
        area = context.area
        area.type = 'NODE_EDITOR'
        space = area.spaces.active
        space.tree_type = EDITOR_IDNAME
        space.pin = True
        space.node_tree = _get_or_create_editor_tree()
        space.show_region_ui = False
        area.tag_redraw()
        _schedule_controls_view_fit(context.window, area)
        return {'FINISHED'}


class STAGEHAND_OT_delete_all(bpy.types.Operator):
    """Delete every object in the current scene."""

    bl_idname = "stagehand.delete_all"
    bl_label = "Cancella Tutto"
    bl_description = "Cancella tutti gli oggetti presenti nella scena corrente"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene is not None and bool(context.scene.objects)

    def invoke(self, context, _event):
        return context.window_manager.invoke_confirm(self, _event)

    def execute(self, context):
        objects = list(context.scene.objects)
        for obj in objects:
            bpy.data.objects.remove(obj, do_unlink=True)

        self.report({'INFO'}, f"Cancellati {len(objects)} oggetti")
        return {'FINISHED'}


def _find_view3d_space(context):
    screen = context.screen
    if screen is None:
        return None, None

    visible_areas = [area for area in screen.areas if area.type == 'VIEW_3D']
    if visible_areas:
        area = max(visible_areas, key=lambda item: item.width * item.height)
        return area, area.spaces.active

    ordered_areas = list(screen.areas)
    if context.area in ordered_areas:
        ordered_areas.remove(context.area)
        ordered_areas.insert(0, context.area)

    for area in ordered_areas:
        for space in area.spaces:
            if space.type == 'VIEW_3D':
                return area, space

    return None, None


class STAGEHAND_OT_center_view(bpy.types.Operator):
    """Place the 3D viewport at (10, -10, 10), aimed at the origin."""

    bl_idname = "stagehand.center_view"
    bl_label = "Centra Vista"
    bl_description = "Posiziona la vista 3D in (10, -10, 10) orientata verso l'origine"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        _area, space = _find_view3d_space(context)
        return space is not None

    def execute(self, context):
        viewport_area, viewport_space = _find_view3d_space(context)
        if viewport_space is None:
            self.report({'ERROR'}, "Nessun 3D Viewport disponibile")
            return {'CANCELLED'}

        region_3d = viewport_space.region_3d

        view_position = Vector((10.0, -10.0, 10.0))
        view_target = Vector((0.0, 0.0, 0.0))
        view_direction = (view_target - view_position).normalized()

        region_3d.view_perspective = 'PERSP'
        region_3d.view_location = view_target
        region_3d.view_distance = (view_position - view_target).length
        region_3d.view_rotation = view_direction.to_track_quat('-Z', 'Y')
        region_3d.update()
        viewport_area.tag_redraw()

        self.report({'INFO'}, "Vista 3D centrata verso l'origine")
        return {'FINISHED'}


class STAGEHAND_PT_controls_help(bpy.types.Panel):
    """Small help panel kept in the editor sidebar."""

    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Stagehand"
    bl_label = "StagehandControls"

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return space is not None and space.tree_type == EDITOR_IDNAME

    def draw(self, _context):
        layout = self.layout
        layout.label(text="Main commands are available")
        layout.label(text="in the Stagehand Controls node.")


classes = (
    StagehandControls,
    StagehandControlsNode,
    STAGEHAND_OT_open_controls,
    STAGEHAND_OT_delete_all,
    STAGEHAND_OT_center_view,
    STAGEHAND_PT_controls_help,
)


def _registered_node_type(base_type, idname):
    return base_type.bl_rna_get_subclass_py(idname, None)


def register():
    # Custom node types are not always exposed as attributes of ``bpy.types``.
    # Remove definitions left by an in-place addon reload before registering
    # the freshly imported classes.
    existing_node = _registered_node_type(bpy.types.Node, CONTROLS_NODE_IDNAME)
    if existing_node is not None and existing_node is not StagehandControlsNode:
        safe_unregister_class(existing_node)

    existing_tree = _registered_node_type(bpy.types.NodeTree, EDITOR_IDNAME)
    if existing_tree is not None and existing_tree is not StagehandControls:
        safe_unregister_class(existing_tree)

    for cls in classes:
        safe_register_class(cls)


def unregister():
    for cls in reversed(classes[2:]):
        safe_unregister_class(cls)

    registered_node = _registered_node_type(bpy.types.Node, CONTROLS_NODE_IDNAME)
    safe_unregister_class(registered_node or StagehandControlsNode)

    registered_tree = _registered_node_type(bpy.types.NodeTree, EDITOR_IDNAME)
    safe_unregister_class(registered_tree or StagehandControls)


if __name__ == "__main__" or __name__ == "<run_path>":
    register()
