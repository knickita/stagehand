bl_info = {
    "name": "Stagehand Options Panel",
    "author": "nick",
    "version": (0, 0, 1),
    "blender": (2, 80, 0),
    "location": "3D Viewport > Sidebar > Stagehand options panel",
    "description": "Stagehand options panel",
    "category": "Development",
}

# give Python access to Blender's functionality
import bpy
from .RegistrationUtils import safe_register_class, safe_unregister_class

class StageHandOptionsPanel(bpy.types.Panel):  # class naming convention ‘CATEGORY_PT_name’

    # where to add the panel in the UI
    bl_space_type = "VIEW_3D"  # 3D Viewport area (find list of values here https://docs.blender.org/api/current/bpy_types_enum_items/space_type_items.html#rna-enum-space-type-items)
    bl_region_type = "UI"  # Sidebar region (find list of values here https://docs.blender.org/api/current/bpy_types_enum_items/region_type_items.html#rna-enum-region-type-items)

    bl_category = "Stagehand"  # found in the Sidebar
    bl_label = "Stagehand"  # found at the top of the Panel

    def draw(self, context):
        layout = self.layout
        #layout.prop(context.scene, "StageHand_cameraSpeed")
        
        box = layout.box()
        box.label(text="Camera Movements")
        box.operator("object.select_all").action = 'TOGGLE'
        row = box.row()
        row.operator("object.select_all").action = 'INVERT'
        row.operator("object.select_random")
        
        


def register():
    safe_register_class(StageHandOptionsPanel)


def unregister():
    safe_unregister_class(StageHandOptionsPanel)

##maybe you can remove this in the final package?
if __name__ == "__main__" or __name__== "<run_path>":
    register()
