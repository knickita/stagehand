import bpy

from .LinkTypes import get_compatible_link_types, link_type_label


class StagehandTagItem(bpy.types.PropertyGroup):
    value: bpy.props.StringProperty(
        name="Tag",
        default="",
    )


class StagehandLinkItem(bpy.types.PropertyGroup):
    type: bpy.props.IntProperty(
        name="Type",
        default=0,
    )

    cylindricalType: bpy.props.BoolProperty(
        name="Cylindrical Type",
        default=False,
    )

    displayRadius: bpy.props.FloatProperty(
        name="Display Radius",
        default=0.0,
    )

    length: bpy.props.FloatProperty(
        name="Length",
        default=0.0,
    )

    posDir: bpy.props.FloatVectorProperty(
        name="Position and Direction",
        size=7,
        default=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    )


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

    catalogueName: bpy.props.StringProperty(
        name="Catalogue Name",
        default="",
    )

    watt: bpy.props.FloatProperty(
        name="Watt",
        description="Power draw for this Stagehand object",
        default=0.0,
        min=0.0,
    )

    tags: bpy.props.CollectionProperty(type=StagehandTagItem)
    links: bpy.props.CollectionProperty(type=StagehandLinkItem)


def _clear_collection(collection):
    while collection:
        collection.remove(len(collection) - 1)


def apply_stagehand_catalogue_data(obj, asset_data=None):
    stagehand = obj.stagehand
    stagehand.is_stagehand_object = True

    if asset_data is None:
        stagehand.asset_id = -1
        stagehand.catalogueName = ""
        stagehand.watt = 0.0
        _clear_collection(stagehand.tags)
        _clear_collection(stagehand.links)
        return

    stagehand.asset_id = int(asset_data["uniqueId"])
    stagehand.catalogueName = asset_data.get("name", "")
    stagehand.watt = float(asset_data.get("watt", 0.0))

    _clear_collection(stagehand.tags)
    for tag_value in asset_data.get("tags", []):
        tag_item = stagehand.tags.add()
        tag_item.value = str(tag_value)

    _clear_collection(stagehand.links)
    for link_data in asset_data.get("links", []):
        link_item = stagehand.links.add()
        link_item.type = int(link_data.get("type", 0))
        link_item.cylindricalType = bool(link_data.get("cylindricaltype", False))
        link_item.displayRadius = float(link_data.get("displayradius", 0.0))
        link_item.length = float(link_data.get("length", 0.0))

        pos_dir = tuple(float(value) for value in link_data.get("posdir", []))
        if len(pos_dir) == 7:
            link_item.posDir = pos_dir


def prevent_stagehand_edit_mode():
    obj = bpy.context.object
    if obj is None:
        return 0.2

    if getattr(obj, "stagehand", None) and obj.stagehand.is_stagehand_object and obj.mode == 'EDIT':
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except RuntimeError:
            pass

    return 0.2


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
        apply_stagehand_catalogue_data(obj)
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
        readonly_column = layout.column()
        readonly_column.enabled = False

        if stagehand.asset_id >= 0:
            readonly_column.label(text=f"Asset ID: {stagehand.asset_id}")

        if stagehand.catalogueName:
            readonly_column.prop(stagehand, "catalogueName")

        readonly_column.prop(stagehand, "watt")

        if stagehand.tags:
            tags_box = readonly_column.box()
            tags_box.label(text="Tags")
            for tag_item in stagehand.tags:
                tags_box.label(text=tag_item.value)

        if stagehand.links:
            links_box = readonly_column.box()
            links_box.label(text=f"Links: {len(stagehand.links)}")
            for index, link_item in enumerate(stagehand.links, start=1):
                item_box = links_box.box()
                item_box.label(text=f"Link {index}")
                item_box.label(text=f"Type: {link_type_label(link_item.type)}")
                compatible_types = get_compatible_link_types(link_item.type)
                compatible_label = ", ".join(link_type_label(link_type) for link_type in compatible_types)
                item_box.label(text=f"Compatible With: {compatible_label or 'None'}")
                item_box.prop(link_item, "type")
                item_box.prop(link_item, "cylindricalType")
                item_box.prop(link_item, "displayRadius")
                item_box.prop(link_item, "length")
                item_box.prop(link_item, "posDir")


classes = (
    StagehandTagItem,
    StagehandLinkItem,
    StagehandObject,
    STAGEHAND_OT_add_object,
    STAGEHAND_PT_object_properties,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Object.stagehand = bpy.props.PointerProperty(type=StagehandObject)
    if not bpy.app.timers.is_registered(prevent_stagehand_edit_mode):
        bpy.app.timers.register(prevent_stagehand_edit_mode, first_interval=0.2)


def unregister():
    if bpy.app.timers.is_registered(prevent_stagehand_edit_mode):
        bpy.app.timers.unregister(prevent_stagehand_edit_mode)

    del bpy.types.Object.stagehand

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
