import bpy

# --- Menu ---
class Stagehand_MT_menu(bpy.types.Menu):
    bl_label = "Stagehand"
    bl_idname = "Stagehand_MT_menu"

    def draw(self, context):
        self.layout.operator("wm.simple_hello")

# --- Draw function to add menu to 3D View header ---
def draw_custom_menu(self, context):
    self.layout.menu("Stagehand_MT_menu")

# --- Register / Unregister ---
def register():    
    bpy.utils.register_class(Stagehand_MT_menu)
    bpy.types.VIEW3D_MT_editor_menus.append(draw_custom_menu)

def unregister():
    bpy.types.VIEW3D_MT_editor_menus.remove(draw_custom_menu)
    bpy.utils.unregister_class(Stagehand_MT_menu)