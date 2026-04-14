import bpy


class StagehandObject(bpy.types.PropertyGroup):
    is_stagehand_object: bpy.props.BoolProperty(
        name="Is Stagehand Object",
        default=False,
        options={'HIDDEN'},
    )

    asset_id: bpy.props.IntProperty(
        name="Asset ID",
        default=-1,
        options={'HIDDEN'},
    )

    powerConsumption: bpy.props.FloatProperty(
        name="Power Consumption",
        description="Power draw for this Stagehand object",
        default=0.0,
        min=0.0,
    )


class STAGEHAND_OT_add_object(bpy.types.Operator):
    bl_idname = "mesh.stagehand_add_object"
    bl_label = "Add Stagehand Object"
    bl_description = "Create a cube with Stagehand properties"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        bpy.ops.mesh.primitive_cube_add()
        obj = context.active_object

        if obj is None:
            return {'CANCELLED'}

        obj.name = "Stagehand Object"
        obj.stagehand.is_stagehand_object = True
        obj.stagehand.asset_id = -1
        obj.stagehand.powerConsumption = 0.0
        return {'FINISHED'}


class STAGEHAND_PT_object_properties(bpy.types.Panel):
    bl_label = "Stagehand"
    bl_idname = "STAGEHAND_PT_object_properties"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj is not None and obj.stagehand.is_stagehand_object

    def draw(self, context):
        layout = self.layout
        stagehand = context.object.stagehand

        if stagehand.asset_id >= 0:
            layout.label(text=f"Asset ID: {stagehand.asset_id}")

        layout.prop(stagehand, "powerConsumption")


classes = (
    StagehandObject,
    STAGEHAND_OT_add_object,
    STAGEHAND_PT_object_properties,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Object.stagehand = bpy.props.PointerProperty(type=StagehandObject)


def unregister():
    del bpy.types.Object.stagehand

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
