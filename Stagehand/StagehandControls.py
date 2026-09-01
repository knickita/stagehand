"""Stagehand controls shown in Blender's Tool properties context."""

import bpy
from mathutils import Vector

from .RegistrationUtils import safe_register_class, safe_unregister_class


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


class STAGEHAND_PT_properties_controls(bpy.types.Panel):
    """Stagehand controls shown in the Properties editor Tool context."""

    bl_idname = "STAGEHAND_PT_properties_controls"
    # The Properties editor Tool context mirrors VIEW_3D sidebar panels from
    # the Tool category; declaring this as a PROPERTIES panel does not display.
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Tool"
    bl_label = "Stagehand Controls"

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'PROPERTIES'

    def draw(self, _context):
        layout = self.layout
        layout.scale_y = 1.2
        flow = layout.grid_flow(
            row_major=True,
            columns=0,
            even_columns=True,
            even_rows=False,
            align=True,
        )

        create_box = flow.box()
        create_box.label(text="Creazione", icon='ADD')
        create_column = create_box.column(align=True)
        create_column.operator(
            "stagehand.recipe_sixtema_stage",
            text="Crea Palco Sixtema",
        )
        create_column.operator(
            "stagehand.recipe_selvoline_stage",
            text="Crea Palco Selvoline",
        )
        create_column.operator(
            "stagehand.recipe_americana_structure",
            text="Crea Struttura Americana",
        )

        scene_box = flow.box()
        scene_box.label(text="Scena", icon='VIEW3D')
        scene_column = scene_box.column(align=True)
        scene_column.operator("stagehand.delete_all", text="Cancella Tutto", icon='TRASH')
        scene_column.operator("stagehand.center_view", text="Centra Vista", icon='VIEW3D')

        export_box = flow.box()
        export_box.label(text="Esportazione", icon='EXPORT')
        export_column = export_box.column(align=True)
        export_column.operator(
            "stagehand.export_rentman_csv",
            text="Esporta Lista Rentman",
        )
        export_column.operator(
            "stagehand.generate_pdf_drawings",
            text="Esporta Disegni",
        )


classes = (
    STAGEHAND_OT_delete_all,
    STAGEHAND_OT_center_view,
    STAGEHAND_PT_properties_controls,
)


def _remove_legacy_node_editor_types():
    for class_name in ("STAGEHAND_PT_controls_help", "STAGEHAND_OT_open_controls"):
        legacy_class = getattr(bpy.types, class_name, None)
        if legacy_class is not None:
            safe_unregister_class(legacy_class)

    legacy_node = bpy.types.Node.bl_rna_get_subclass_py("StagehandControlsNode", None)
    if legacy_node is not None:
        safe_unregister_class(legacy_node)

    legacy_tree = bpy.types.NodeTree.bl_rna_get_subclass_py("StagehandControls", None)
    if legacy_tree is not None:
        safe_unregister_class(legacy_tree)


def register():
    _remove_legacy_node_editor_types()
    for cls in classes:
        safe_register_class(cls)


def unregister():
    for cls in reversed(classes):
        safe_unregister_class(cls)


if __name__ == "__main__" or __name__ == "<run_path>":
    register()
