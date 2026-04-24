import uuid
import time
from collections import defaultdict
from math import radians

import bpy
from bpy.app.handlers import persistent
from bpy_extras import view3d_utils
from mathutils import Matrix, Quaternion, Vector

from .AddStagehandObject import ensure_stagehand_link_uid, ensure_stagehand_uid
from .LinkTypes import are_link_types_compatible
from .RegistrationUtils import (
    safe_add_handler,
    safe_register_class,
    safe_remove_handler,
    safe_remove_keymaps,
    safe_unregister_class,
)


CONNECTION_MAINTENANCE_INTERVAL = 0.5
CONNECTION_REFRESH_POLL_INTERVAL = 0.05
CONNECTION_REFRESH_SETTLE_INTERVAL = 0.1
AUTO_CONNECT_DISTANCE_THRESHOLD = 0.0001
AUTO_CONNECT_ANGLE_THRESHOLD = radians(0.1)
LINK_ALIGNMENT_FLIP = Quaternion((0.0, 0.0, 1.0), radians(180.0))
_DIRTY_CONNECTION_OBJECT_UIDS = set()
_DIRTY_CONNECTION_REFRESH_DEADLINE = 0.0
addon_keymaps = []


def _data_objects():
    return getattr(bpy.data, "objects", None)


def is_stagehand_object(obj):
    return (
        obj is not None
        and getattr(obj, "stagehand", None) is not None
        and obj.stagehand.is_stagehand_object
    )


def get_object_uid(obj):
    if not is_stagehand_object(obj):
        return ""
    return ensure_stagehand_uid(obj)


def iter_stagehand_objects():
    objects = _data_objects()
    if objects is None:
        return

    for obj in objects:
        if is_stagehand_object(obj):
            yield obj


def iter_object_links(obj):
    if not is_stagehand_object(obj):
        return

    for index, link in enumerate(obj.stagehand.links):
        ensure_stagehand_link_uid(link)
        yield index, link


def _link_transform(obj, link):
    local_position = Vector(link.posDir[:3])
    local_rotation = Quaternion((
        link.posDir[6],
        link.posDir[3],
        link.posDir[4],
        link.posDir[5],
    ))
    world_rotation = obj.matrix_world.to_quaternion()
    center = obj.matrix_world.to_translation() + (world_rotation @ local_position)
    rotation = world_rotation @ local_rotation
    return center, rotation


def _link_forward(rotation):
    forward = rotation @ Vector((0, 1, 0))
    if forward.length_squared == 0.0:
        return Vector((0, 1, 0))
    return forward.normalized()


def link_alignment_rotation_delta(link_rotation, target_rotation):
    desired_link_rotation = target_rotation @ LINK_ALIGNMENT_FLIP
    return desired_link_rotation @ link_rotation.inverted()


def _link_alignment_angle(link, rotation, other_link, other_rotation):
    if link.cylindricalType or other_link.cylindricalType:
        return _link_forward(rotation).angle(-_link_forward(other_rotation), 0.0)

    desired_rotation = other_rotation @ LINK_ALIGNMENT_FLIP
    return desired_rotation.rotation_difference(rotation).angle


def _rotate_object_around_pivot(obj, rotation_delta, pivot):
    pivot_matrix = Matrix.Translation(pivot)
    rotation_matrix = rotation_delta.to_matrix().to_4x4()
    obj.matrix_world = pivot_matrix @ rotation_matrix @ pivot_matrix.inverted() @ obj.matrix_world


def _align_object_link_to_target(obj, link_index, target_obj, target_link_index):
    link = get_link(obj, link_index)
    target_link = get_link(target_obj, target_link_index)
    if link is None or target_link is None:
        return False

    link_center, link_rotation = _link_transform(obj, link)
    target_center, target_rotation = _link_transform(target_obj, target_link)
    rotation_delta = link_alignment_rotation_delta(link_rotation, target_rotation)
    _rotate_object_around_pivot(obj, rotation_delta, link_center)

    corrected_center, _corrected_rotation = _link_transform(obj, link)
    obj.matrix_world.translation += target_center - corrected_center
    return True


def _link_alignment_metrics(obj, link_index, other_obj, other_link_index):
    link = get_link(obj, link_index)
    other_link = get_link(other_obj, other_link_index)
    if link is None or other_link is None:
        return None, None

    center, rotation = _link_transform(obj, link)
    other_center, other_rotation = _link_transform(other_obj, other_link)
    distance = (other_center - center).length
    angle = _link_alignment_angle(link, rotation, other_link, other_rotation)
    return distance, angle


def _sorted_uid_group(objects):
    return sorted(objects, key=lambda obj: obj.name_full)


def find_object_by_uid(uid):
    if not uid:
        return None

    for obj in iter_stagehand_objects():
        if get_object_uid(obj) == uid:
            return obj

    return None


def get_link(obj, link_index):
    if not is_stagehand_object(obj):
        return None
    if link_index < 0 or link_index >= len(obj.stagehand.links):
        return None
    link = obj.stagehand.links[link_index]
    ensure_stagehand_link_uid(link)
    return link


def find_link_by_uid(obj, link_uid):
    if not is_stagehand_object(obj) or not link_uid:
        return None, -1

    for index, link in iter_object_links(obj):
        if link.uid == link_uid:
            return link, index

    return None, -1


def clear_link_connection(obj, link_index):
    link = get_link(obj, link_index)
    if link is None:
        return

    link.connectedObjectUid = ""
    link.connectedLinkUid = ""
    link.connectedLinkIndex = -1


def disconnect_link(obj, link_index):
    link = get_link(obj, link_index)
    if link is None or not link.connectedObjectUid:
        clear_link_connection(obj, link_index)
        return

    other_obj = find_object_by_uid(link.connectedObjectUid)
    other_link_uid = link.connectedLinkUid
    other_link_index = link.connectedLinkIndex
    clear_link_connection(obj, link_index)

    if other_obj is None:
        return

    other_link = None
    if other_link_uid:
        other_link, resolved_index = find_link_by_uid(other_obj, other_link_uid)
        if other_link is not None:
            other_link_index = resolved_index

    if other_link is None:
        other_link = get_link(other_obj, other_link_index)

    if other_link is not None:
        other_link.connectedObjectUid = ""
        other_link.connectedLinkUid = ""
        other_link.connectedLinkIndex = -1


def connect_links(obj_a, link_index_a, obj_b, link_index_b):
    if not is_stagehand_object(obj_a) or not is_stagehand_object(obj_b):
        return False

    link_a = get_link(obj_a, link_index_a)
    link_b = get_link(obj_b, link_index_b)
    if link_a is None or link_b is None:
        return False

    disconnect_link(obj_a, link_index_a)
    disconnect_link(obj_b, link_index_b)

    uid_a = ensure_stagehand_uid(obj_a)
    uid_b = ensure_stagehand_uid(obj_b)
    link_uid_a = ensure_stagehand_link_uid(link_a)
    link_uid_b = ensure_stagehand_link_uid(link_b)

    link_a.connectedObjectUid = uid_b
    link_a.connectedLinkUid = link_uid_b
    link_a.connectedLinkIndex = link_index_b
    link_b.connectedObjectUid = uid_a
    link_b.connectedLinkUid = link_uid_a
    link_b.connectedLinkIndex = link_index_a
    return True


def get_connected_link(obj, link_index):
    link = get_link(obj, link_index)
    if link is None or not link.connectedObjectUid:
        return None, None, None

    other_obj = find_object_by_uid(link.connectedObjectUid)
    if other_obj is None:
        return None, None, None

    other_link = None
    other_link_index = -1
    if link.connectedLinkUid:
        other_link, other_link_index = find_link_by_uid(other_obj, link.connectedLinkUid)

    if other_link is None and link.connectedLinkIndex >= 0:
        other_link = get_link(other_obj, link.connectedLinkIndex)
        other_link_index = link.connectedLinkIndex

    if other_link is None:
        return other_obj, None, -1

    return other_obj, other_link, other_link_index


def iter_connected_links(obj):
    if not is_stagehand_object(obj):
        return

    for index, _link in iter_object_links(obj):
        other_obj, other_link, other_link_index = get_connected_link(obj, index)
        if other_obj is not None and other_link is not None:
            yield index, other_obj, other_link_index, other_link


def iter_connected_objects(root_obj):
    if not is_stagehand_object(root_obj):
        return

    visited = set()
    pending = [root_obj]

    while pending:
        obj = pending.pop()
        uid = get_object_uid(obj)
        if not uid or uid in visited:
            continue

        visited.add(uid)
        yield obj

        for _link_index, other_obj, _other_link_index, _other_link in iter_connected_links(obj):
            other_uid = get_object_uid(other_obj)
            if other_uid and other_uid not in visited:
                pending.append(other_obj)


def _pick_stagehand_object(context, event):
    if context.region is None or context.region_data is None:
        return None

    coord = (event.mouse_region_x, event.mouse_region_y)
    ray_origin = view3d_utils.region_2d_to_origin_3d(context.region, context.region_data, coord)
    ray_direction = view3d_utils.region_2d_to_vector_3d(context.region, context.region_data, coord)
    depsgraph = context.evaluated_depsgraph_get()
    hit, _location, _normal, _face_index, obj, _matrix = context.scene.ray_cast(
        depsgraph,
        ray_origin,
        ray_direction,
    )
    if not hit or not is_stagehand_object(obj):
        return None
    return obj


def _migrate_legacy_connection_indexes():
    for obj in iter_stagehand_objects():
        ensure_stagehand_uid(obj)
        for index, link in iter_object_links(obj):
            if not link.connectedObjectUid or link.connectedLinkUid:
                continue

            other_obj = find_object_by_uid(link.connectedObjectUid)
            if other_obj is None:
                clear_link_connection(obj, index)
                continue

            other_link = get_link(other_obj, link.connectedLinkIndex)
            if other_link is None:
                clear_link_connection(obj, index)
                continue

            link.connectedLinkUid = ensure_stagehand_link_uid(other_link)


def _repair_duplicate_ids():
    groups = defaultdict(list)
    for obj in iter_stagehand_objects():
        groups[get_object_uid(obj)].append(obj)

    duplicate_groups = {
        uid: _sorted_uid_group(objects)
        for uid, objects in groups.items()
        if uid and len(objects) > 1
    }
    if not duplicate_groups:
        return

    duplicate_snapshots = {}
    object_uid_remap = {}
    link_uid_remap = {}

    for original_uid, objects in duplicate_groups.items():
        for duplicate_index, obj in enumerate(objects[1:], start=1):
            link_snapshots = []
            for link_index, link in iter_object_links(obj):
                link_snapshots.append(
                    {
                        "link_index": link_index,
                        "old_link_uid": link.uid,
                        "connected_object_uid": link.connectedObjectUid,
                        "connected_link_uid": link.connectedLinkUid,
                    }
                )

            duplicate_snapshots[obj.name_full] = {
                "original_uid": original_uid,
                "duplicate_index": duplicate_index,
                "links": link_snapshots,
            }

            new_object_uid = str(uuid.uuid4())
            obj.stagehand.uid = new_object_uid
            object_uid_remap[(original_uid, duplicate_index)] = new_object_uid

            for link_index, link in iter_object_links(obj):
                old_link_uid = link.uid
                new_link_uid = str(uuid.uuid4())
                link.uid = new_link_uid
                link_uid_remap[(original_uid, duplicate_index, old_link_uid)] = new_link_uid
                link.connectedObjectUid = ""
                link.connectedLinkUid = ""
                link.connectedLinkIndex = -1

    for obj_name, snapshot in duplicate_snapshots.items():
        obj = bpy.data.objects.get(obj_name)
        if obj is None or not is_stagehand_object(obj):
            continue

        duplicate_index = snapshot["duplicate_index"]
        for link_snapshot in snapshot["links"]:
            link = get_link(obj, link_snapshot["link_index"])
            if link is None:
                continue

            target_original_uid = link_snapshot["connected_object_uid"]
            target_original_link_uid = link_snapshot["connected_link_uid"]
            if not target_original_uid or not target_original_link_uid:
                continue

            target_duplicate_uid = object_uid_remap.get((target_original_uid, duplicate_index))
            target_duplicate_link_uid = link_uid_remap.get(
                (target_original_uid, duplicate_index, target_original_link_uid)
            )
            if not target_duplicate_uid or not target_duplicate_link_uid:
                clear_link_connection(obj, link_snapshot["link_index"])
                continue

            target_obj = find_object_by_uid(target_duplicate_uid)
            target_link, target_link_index = find_link_by_uid(target_obj, target_duplicate_link_uid)
            if target_obj is None or target_link is None:
                clear_link_connection(obj, link_snapshot["link_index"])
                continue

            link.connectedObjectUid = target_duplicate_uid
            link.connectedLinkUid = target_duplicate_link_uid
            link.connectedLinkIndex = target_link_index


def prune_stale_connections():
    _migrate_legacy_connection_indexes()
    _repair_duplicate_ids()

    live_uids = {get_object_uid(obj) for obj in iter_stagehand_objects()}

    for obj in iter_stagehand_objects():
        uid = get_object_uid(obj)
        for index, link in iter_object_links(obj):
            if not link.connectedObjectUid:
                continue

            if link.connectedObjectUid not in live_uids:
                clear_link_connection(obj, index)
                continue

            other_obj = find_object_by_uid(link.connectedObjectUid)
            if other_obj is None:
                clear_link_connection(obj, index)
                continue

            other_link = None
            other_link_index = -1
            if link.connectedLinkUid:
                other_link, other_link_index = find_link_by_uid(other_obj, link.connectedLinkUid)

            if other_link is None and link.connectedLinkIndex >= 0:
                other_link = get_link(other_obj, link.connectedLinkIndex)
                other_link_index = link.connectedLinkIndex
                if other_link is not None:
                    link.connectedLinkUid = ensure_stagehand_link_uid(other_link)

            if other_link is None:
                clear_link_connection(obj, index)
                continue

            if other_link.connectedObjectUid != uid:
                clear_link_connection(obj, index)
                continue

            if other_link.connectedLinkUid and other_link.connectedLinkUid != ensure_stagehand_link_uid(link):
                clear_link_connection(obj, index)
                continue

            link.connectedLinkIndex = other_link_index


def _iter_compatible_unconnected_links(obj):
    for index, link in iter_object_links(obj):
        if link.connectedObjectUid:
            continue
        yield index, link


def _unique_stagehand_objects(objects):
    unique_objects = []
    seen_uids = set()

    for obj in objects:
        if not is_stagehand_object(obj):
            continue

        uid = get_object_uid(obj)
        if not uid or uid in seen_uids:
            continue

        seen_uids.add(uid)
        unique_objects.append(obj)

    return unique_objects


def _disconnect_invalid_connections(objects):
    for obj in _unique_stagehand_objects(objects):
        for index, link in iter_object_links(obj):
            if not link.connectedObjectUid:
                continue

            other_obj, other_link, other_link_index = get_connected_link(obj, index)
            if other_obj is None or other_link is None:
                clear_link_connection(obj, index)
                continue

            distance, angle = _link_alignment_metrics(obj, index, other_obj, other_link_index)
            if distance is None:
                clear_link_connection(obj, index)
                continue

            if distance <= AUTO_CONNECT_DISTANCE_THRESHOLD and angle <= AUTO_CONNECT_ANGLE_THRESHOLD:
                continue

            disconnect_link(obj, index)


def _iter_connection_candidates(objects):
    refresh_objects = _unique_stagehand_objects(objects)
    refresh_uids = {get_object_uid(obj) for obj in refresh_objects}

    for obj in refresh_objects:
        obj_uid = get_object_uid(obj)
        for link_index, link in _iter_compatible_unconnected_links(obj):
            for other_obj in iter_stagehand_objects():
                if other_obj == obj:
                    continue

                other_uid = get_object_uid(other_obj)
                if not other_uid:
                    continue

                if other_uid in refresh_uids and other_uid <= obj_uid:
                    continue

                for other_link_index, other_link in _iter_compatible_unconnected_links(other_obj):
                    if not are_link_types_compatible(link.type, other_link.type):
                        continue

                    distance, angle = _link_alignment_metrics(obj, link_index, other_obj, other_link_index)
                    if distance is None:
                        continue
                    if distance > AUTO_CONNECT_DISTANCE_THRESHOLD or angle > AUTO_CONNECT_ANGLE_THRESHOLD:
                        continue

                    yield (
                        distance,
                        angle,
                        obj_uid,
                        other_uid,
                        link_index,
                        other_link_index,
                        obj,
                        other_obj,
                    )


def refresh_connections_for_objects(objects):
    refresh_objects = _unique_stagehand_objects(objects)
    if not refresh_objects:
        return

    prune_stale_connections()
    _disconnect_invalid_connections(refresh_objects)

    candidates = sorted(
        _iter_connection_candidates(refresh_objects),
        key=lambda item: (item[0], item[1], item[2], item[3], item[4], item[5]),
    )
    for _distance, _angle, _obj_uid, _other_uid, link_index, other_link_index, obj, other_obj in candidates:
        link = get_link(obj, link_index)
        other_link = get_link(other_obj, other_link_index)
        if link is None or other_link is None:
            continue
        if link.connectedObjectUid or other_link.connectedObjectUid:
            continue
        connect_links(obj, link_index, other_obj, other_link_index)


def _iter_pending_operators():
    context = bpy.context
    window = getattr(context, "window", None)
    if window is not None:
        for operator in getattr(window, "modal_operators", ()):
            yield operator


def _transform_operator_active():
    for operator in _iter_pending_operators():
        operator_id = str(getattr(operator, "bl_idname", "")).lower()
        if operator_id.startswith("transform_ot_"):
            return True
        if operator_id == "stagehand_ot_move_with_snap":
            return True
    return False


def mark_objects_dirty(objects, delay=CONNECTION_REFRESH_SETTLE_INTERVAL):
    global _DIRTY_CONNECTION_REFRESH_DEADLINE

    marked_any = False
    for obj in objects:
        if not is_stagehand_object(obj):
            continue

        uid = get_object_uid(obj)
        if not uid:
            continue

        _DIRTY_CONNECTION_OBJECT_UIDS.add(uid)
        marked_any = True

    if not marked_any:
        return

    _DIRTY_CONNECTION_REFRESH_DEADLINE = time.monotonic() + max(delay, 0.0)
    if not bpy.app.timers.is_registered(dirty_connection_refresh_timer):
        bpy.app.timers.register(
            dirty_connection_refresh_timer,
            first_interval=CONNECTION_REFRESH_POLL_INTERVAL,
        )


def mark_all_objects_dirty(delay=CONNECTION_REFRESH_SETTLE_INTERVAL):
    mark_objects_dirty(iter_stagehand_objects(), delay=delay)


def _process_dirty_connection_refresh():
    refresh_objects = []
    while _DIRTY_CONNECTION_OBJECT_UIDS:
        uid = _DIRTY_CONNECTION_OBJECT_UIDS.pop()
        obj = find_object_by_uid(uid)
        if obj is not None:
            refresh_objects.append(obj)

    refresh_connections_for_objects(refresh_objects)


def dirty_connection_refresh_timer():
    try:
        if not _DIRTY_CONNECTION_OBJECT_UIDS:
            return None

        if _transform_operator_active():
            return CONNECTION_REFRESH_POLL_INTERVAL

        if time.monotonic() < _DIRTY_CONNECTION_REFRESH_DEADLINE:
            return CONNECTION_REFRESH_POLL_INTERVAL

        _process_dirty_connection_refresh()
    except RuntimeError:
        return CONNECTION_REFRESH_POLL_INTERVAL

    if _DIRTY_CONNECTION_OBJECT_UIDS:
        return CONNECTION_REFRESH_POLL_INTERVAL
    return None


def initial_connection_refresh_timer():
    if _data_objects() is None:
        return CONNECTION_REFRESH_POLL_INTERVAL

    mark_all_objects_dirty(delay=0.0)
    return None


@persistent
def stagehand_depsgraph_update_post(_scene, depsgraph):
    dirty_objects = []
    for update in getattr(depsgraph, "updates", ()):
        updated_id = getattr(update, "id", None)
        if not isinstance(updated_id, bpy.types.Object):
            continue
        if not getattr(update, "is_updated_transform", False):
            continue
        if not is_stagehand_object(updated_id):
            continue
        dirty_objects.append(updated_id)

    if dirty_objects:
        mark_objects_dirty(dirty_objects)


@persistent
def stagehand_undo_redo_post(_dummy):
    mark_all_objects_dirty(delay=0.0)


def connection_maintenance_timer():
    try:
        prune_stale_connections()
    except RuntimeError:
        pass

    return CONNECTION_MAINTENANCE_INTERVAL


class STAGEHAND_OT_select_connected_objects(bpy.types.Operator):
    bl_idname = "stagehand.select_connected_objects"
    bl_label = "Select Connected Stagehand Objects"
    bl_description = "Select all Stagehand objects connected to the clicked object"

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            return {'PASS_THROUGH'}

        wm = context.window_manager
        if (
            getattr(wm, "stagehand_link_mode_enabled", False)
            or getattr(wm, "stagehand_selecting_link_mode_enabled", False)
        ):
            return {'FINISHED'}
        if context.mode != 'OBJECT':
            return {'PASS_THROUGH'}

        obj = _pick_stagehand_object(context, event)
        if obj is None:
            return {'PASS_THROUGH'}

        connected_objects = list(iter_connected_objects(obj))
        if not connected_objects:
            return {'PASS_THROUGH'}

        bpy.ops.object.select_all(action='DESELECT')
        for connected_object in connected_objects:
            connected_object.select_set(True)
        context.view_layer.objects.active = obj
        return {'FINISHED'}


def register_keymap():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    km = kc.keymaps.new(name='Object Mode', space_type='EMPTY')
    kmi = km.keymap_items.new(
        STAGEHAND_OT_select_connected_objects.bl_idname,
        type='LEFTMOUSE',
        value='PRESS',
        alt=True,
    )
    addon_keymaps.append((km, kmi))


def unregister_keymap():
    safe_remove_keymaps(addon_keymaps)


def register():
    safe_register_class(STAGEHAND_OT_select_connected_objects)
    register_keymap()
    safe_add_handler(bpy.app.handlers.depsgraph_update_post, stagehand_depsgraph_update_post)
    safe_add_handler(bpy.app.handlers.undo_post, stagehand_undo_redo_post)
    safe_add_handler(bpy.app.handlers.redo_post, stagehand_undo_redo_post)
    safe_add_handler(bpy.app.handlers.load_post, stagehand_undo_redo_post)
    if not bpy.app.timers.is_registered(connection_maintenance_timer):
        bpy.app.timers.register(
            connection_maintenance_timer,
            first_interval=CONNECTION_MAINTENANCE_INTERVAL,
        )
    if not bpy.app.timers.is_registered(initial_connection_refresh_timer):
        bpy.app.timers.register(
            initial_connection_refresh_timer,
            first_interval=CONNECTION_REFRESH_POLL_INTERVAL,
        )


def unregister():
    unregister_keymap()
    safe_remove_handler(bpy.app.handlers.depsgraph_update_post, stagehand_depsgraph_update_post)
    safe_remove_handler(bpy.app.handlers.undo_post, stagehand_undo_redo_post)
    safe_remove_handler(bpy.app.handlers.redo_post, stagehand_undo_redo_post)
    safe_remove_handler(bpy.app.handlers.load_post, stagehand_undo_redo_post)
    if bpy.app.timers.is_registered(initial_connection_refresh_timer):
        bpy.app.timers.unregister(initial_connection_refresh_timer)
    if bpy.app.timers.is_registered(dirty_connection_refresh_timer):
        bpy.app.timers.unregister(dirty_connection_refresh_timer)
    _DIRTY_CONNECTION_OBJECT_UIDS.clear()
    if bpy.app.timers.is_registered(connection_maintenance_timer):
        bpy.app.timers.unregister(connection_maintenance_timer)
    safe_unregister_class(STAGEHAND_OT_select_connected_objects)
