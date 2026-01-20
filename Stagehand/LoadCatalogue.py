import bpy

class LoadCatalogue(bpy.types.Operator):
    bl_idname = "wm.simple_hello"
    bl_label = "cane"

    def execute(self, context):
        print("eccolo!")        
        return {'FINISHED'}
    
    
def menu_func(self, context):
    self.layout.operator(LoadCatalogue.bl_idname, text="Hello Nick75")


def register():    
    bpy.utils.register_class(LoadCatalogue)
    

def unregister():    
    bpy.utils.unregister_class(LoadCatalogue)    