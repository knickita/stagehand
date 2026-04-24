import bpy
import uuid

from .LinkTypes import get_compatible_link_types, link_type_label
from .RegistrationUtils import (
    safe_define_property,
    safe_register_class,
    safe_remove_keymaps,
    safe_remove_property,
    safe_unregister_class,
)


addon_keymaps = []


class StagehandTagItem(bpy.types.PropertyGroup):
    value: bpy.props.StringProperty(
        name="Tag",
        default="",
    )


class StagehandLinkItem(bpy.types.PropertyGroup):
    uid: bpy.props.StringProperty(
        name="UID",
        default="",
        options={'HIDDEN'},
    )

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

    connectedObjectUid: bpy.props.StringProperty(
        name="Connected Object UID",
        default="",
        options={'HIDDEN'},
    )

    connectedLinkUid: bpy.props.StringProperty(
        name="Connected Link UID",
        default="",
        options={'HIDDEN'},
    )

    connectedLinkIndex: bpy.props.IntProperty(
        name="Connected Link Index",
        default=-1,
        options={'HIDDEN'},
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

    uid: bpy.props.StringProperty(
        name="UID",
        default="",
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


def _round_link_float(value):
    return round(float(value), 6)


def _link_signature(type_value, cylindrical_type, display_radius, length, pos_dir):
    return (
        int(type_value),
        bool(cylindrical_type),
        _round_link_float(display_radius),
        _round_link_float(length),
        tuple(_round_link_float(value) for value in pos_dir),
    )


def _catalogue_link_signature(link_data):
    return _link_signature(
        link_data.get("type", 0),
        link_data.get("cylindricaltype", False),
        link_data.get("displayradius", 0.0),
        link_data.get("length", 0.0),
        link_data.get("posdir", ()),
    )


def _snapshot_stagehand_links(links):
    snapshots = []

    for index, link in enumerate(links):
        snapshots.append(
            {
                "index": index,
                "uid": str(link.uid),
                "type": int(link.type),
                "cylindricalType": bool(link.cylindricalType),
                "displayRadius": float(link.displayRadius),
                "length": float(link.length),
                "posDir": tuple(float(value) for value in link.posDir),
                "connectedObjectUid": str(link.connectedObjectUid),
                "connectedLinkUid": str(link.connectedLinkUid),
                "connectedLinkIndex": int(link.connectedLinkIndex),
            }
        )

    return snapshots


def _snapshot_link_signature(snapshot):
    return _link_signature(
        snapshot["type"],
        snapshot["cylindricalType"],
        snapshot["displayRadius"],
        snapshot["length"],
        snapshot["posDir"],
    )


def _pop_preserved_link_state(link_states, link_data, link_index):
    target_signature = _catalogue_link_signature(link_data)

    for candidate_index, candidate in enumerate(link_states):
        if candidate["index"] != link_index:
            continue
        if _snapshot_link_signature(candidate) == target_signature:
            return link_states.pop(candidate_index)

    for candidate_index, candidate in enumerate(link_states):
        if _snapshot_link_signature(candidate) == target_signature:
            return link_states.pop(candidate_index)

    for candidate_index, candidate in enumerate(link_states):
        if candidate["index"] == link_index:
            return link_states.pop(candidate_index)

    return None


def ensure_stagehand_uid(obj):
    if not obj.stagehand.uid:
        obj.stagehand.uid = str(uuid.uuid4())
    return obj.stagehand.uid


def ensure_stagehand_link_uid(link):
    if not link.uid:
        link.uid = str(uuid.uuid4())
    return link.uid


def _apply_stagehand_link_data(link_item, link_data, preserved_state=None):
    if preserved_state is not None and preserved_state["uid"]:
        link_item.uid = preserved_state["uid"]

    ensure_stagehand_link_uid(link_item)
    link_item.type = int(link_data.get("type", 0))
    link_item.cylindricalType = bool(link_data.get("cylindricaltype", False))
    link_item.displayRadius = float(link_data.get("displayradius", 0.0))
    link_item.length = float(link_data.get("length", 0.0))

    pos_dir = tuple(float(value) for value in link_data.get("posdir", []))
    if len(pos_dir) == 7:
        link_item.posDir = pos_dir

    if preserved_state is None:
        link_item.connectedObjectUid = ""
        link_item.connectedLinkUid = ""
        link_item.connectedLinkIndex = -1
        return

    link_item.connectedObjectUid = preserved_state["connectedObjectUid"]
    link_item.connectedLinkUid = preserved_state["connectedLinkUid"]
    link_item.connectedLinkIndex = preserved_state["connectedLinkIndex"]


def apply_stagehand_catalogue_data(obj, asset_data=None, preserve_links=False):
    stagehand = obj.stagehand
    stagehand.is_stagehand_object = True
    ensure_stagehand_uid(obj)

    preserved_link_states = []
    if preserve_links and asset_data is not None:
        preserved_link_states = _snapshot_stagehand_links(stagehand.links)

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
    for link_index, link_data in enumerate(asset_data.get("links", [])):
        link_item = stagehand.links.add()
        preserved_state = None
        if preserve_links:
            preserved_state = _pop_preserved_link_state(
                preserved_link_states,
                link_data,
                link_index,
            )
        _apply_stagehand_link_data(link_item, link_data, preserved_state=preserved_state)


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


def _selected_stagehand_objects(context):
    return [
        obj
        for obj in context.selected_objects
        if getattr(obj, "stagehand", None) is not None and obj.stagehand.is_stagehand_object
    ]


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


class STAGEHAND_OT_scale_guard(bpy.types.Operator):
    bl_idname = "stagehand.scale_guard"
    bl_label = "Stagehand Scale Guard"
    bl_description = "Prevent scaling Stagehand objects"

    def invoke(self, context, event):
        del event

        if _selected_stagehand_objects(context):
            self.report({'WARNING'}, "Scaling is disabled for Stagehand objects")
            return {'FINISHED'}

        return bpy.ops.transform.resize('INVOKE_DEFAULT')


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

        if stagehand.uid:
            readonly_column.label(text=f"UID: {stagehand.uid}")

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
                if link_item.uid:
                    item_box.label(text=f"Link UID: {link_item.uid}")
                item_box.prop(link_item, "type")
                item_box.prop(link_item, "cylindricalType")
                item_box.prop(link_item, "displayRadius")
                item_box.prop(link_item, "length")
                item_box.prop(link_item, "posDir")
                if link_item.connectedObjectUid:
                    item_box.label(text=f"Connected UID: {link_item.connectedObjectUid}")
                    if link_item.connectedLinkUid:
                        item_box.label(text=f"Connected Link UID: {link_item.connectedLinkUid}")


classes = (
    StagehandTagItem,
    StagehandLinkItem,
    StagehandObject,
    STAGEHAND_OT_add_object,
    STAGEHAND_OT_scale_guard,
    STAGEHAND_PT_object_properties,
)


def register_keymap():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    km = kc.keymaps.new(name='Object Mode', space_type='EMPTY')
    kmi = km.keymap_items.new(
        STAGEHAND_OT_scale_guard.bl_idname,
        type='S',
        value='PRESS',
    )
    addon_keymaps.append((km, kmi))


def unregister_keymap():
    safe_remove_keymaps(addon_keymaps)


def register():
    for cls in classes:
        safe_register_class(cls)

    safe_define_property(
        bpy.types.Object,
        "stagehand",
        bpy.props.PointerProperty(type=StagehandObject),
    )
    register_keymap()
    if not bpy.app.timers.is_registered(prevent_stagehand_edit_mode):
        bpy.app.timers.register(prevent_stagehand_edit_mode, first_interval=0.2)


def unregister():
    if bpy.app.timers.is_registered(prevent_stagehand_edit_mode):
        bpy.app.timers.unregister(prevent_stagehand_edit_mode)

    unregister_keymap()
    safe_remove_property(bpy.types.Object, "stagehand")

    for cls in reversed(classes):
        safe_unregister_class(cls)
