import bpy
from .RegistrationUtils import safe_append_menu, safe_register_class, safe_remove_menu, safe_unregister_class


# --- Menu ---
class Stagehand_MT_menu(bpy.types.Menu):
    bl_label = "Stagehand"
    bl_idname = "Stagehand_MT_menu"

    def draw(self, context):
        self.layout.menu("STAGEHAND_MT_catalogue_menu", text="Import From Catalogue")
        self.layout.operator("stagehand.import_mvr_structure", text="Import MVR Structure")
        self.layout.separator()
        self.layout.operator("stagehand.add_cable_obstacle", text="Add Cable Obstacle")
        self.layout.operator("stagehand.generate_power_lines", text="Generate Power Lines")
        self.layout.separator()
        self.layout.operator("stagehand.generate_pdf_drawings", text="Generate PDF Drawings")
        self.layout.separator()
        self.layout.operator("stagehand.repair_all_connections", text="Repair All Links")
        self.layout.separator()
        self.layout.operator("stagehand.reload_catalogue", text="Reload Catalogue")
        self.layout.operator("stagehand.update_addon", text="Update Addon")

# --- Draw function to add menu to 3D View header ---
def draw_custom_menu(self, context):
    self.layout.menu("Stagehand_MT_menu")

# --- Register / Unregister ---
def register():    
    safe_register_class(Stagehand_MT_menu)
    safe_append_menu(bpy.types.VIEW3D_MT_editor_menus, draw_custom_menu)

def unregister():
    safe_remove_menu(bpy.types.VIEW3D_MT_editor_menus, draw_custom_menu)
    safe_unregister_class(Stagehand_MT_menu)
