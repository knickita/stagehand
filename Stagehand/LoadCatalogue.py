import bpy

class SIMPLE_OT_hello(bpy.types.Operator):
    bl_idname = "wm.simple_hello"
    bl_label = "Say Hello"

    def execute(self, context):
        self.report({'INFO'}, "Hello from Blender operator!")
        print("Hello from Blender operator!")
        return {'FINISHED'}
    

def register():
    bpy.utils.register_class(SIMPLE_OT_hello)

def unregister():
    bpy.utils.unregister_class(SIMPLE_OT_hello)