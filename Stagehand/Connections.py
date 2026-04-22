import bpy

from .AddStagehandObject import ensure_stagehand_uid


CONNECTION_MAINTENANCE_INTERVAL = 0.5


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
    for obj in bpy.data.objects:
        if is_stagehand_object(obj):
            yield obj


def find_object_by_uid(uid):
    if not uid:
        return None

    for obj in iter_stagehand_objects():
        if obj.stagehand.uid == uid:
            return obj

    return None


def get_link(obj, link_index):
    if not is_stagehand_object(obj):
        return None
    if link_index < 0 or link_index >= len(obj.stagehand.links):
        return None
    return obj.stagehand.links[link_index]


def clear_link_connection(obj, link_index):
    link = get_link(obj, link_index)
    if link is None:
        return

    link.connectedObjectUid = ""
    link.connectedLinkIndex = -1


def disconnect_link(obj, link_index):
    link = get_link(obj, link_index)
    if link is None or not link.connectedObjectUid:
        clear_link_connection(obj, link_index)
        return

    other_obj = find_object_by_uid(link.connectedObjectUid)
    other_link_index = link.connectedLinkIndex
    clear_link_connection(obj, link_index)

    other_link = get_link(other_obj, other_link_index)
    if other_link is not None:
        other_link.connectedObjectUid = ""
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

    link_a.connectedObjectUid = uid_b
    link_a.connectedLinkIndex = link_index_b
    link_b.connectedObjectUid = uid_a
    link_b.connectedLinkIndex = link_index_a
    return True


def get_connected_link(obj, link_index):
    link = get_link(obj, link_index)
    if link is None or not link.connectedObjectUid:
        return None, None, None

    other_obj = find_object_by_uid(link.connectedObjectUid)
    if other_obj is None:
        return None, None, None

    other_link = get_link(other_obj, link.connectedLinkIndex)
    if other_link is None:
        return other_obj, None, link.connectedLinkIndex

    return other_obj, other_link, link.connectedLinkIndex


def iter_connected_links(obj):
    if not is_stagehand_object(obj):
        return

    for index, _link in enumerate(obj.stagehand.links):
        other_obj, other_link, other_link_index = get_connected_link(obj, index)
        if other_obj is not None and other_link is not None:
            yield index, other_obj, other_link_index, other_link


def prune_stale_connections():
    live_uids = {obj.stagehand.uid for obj in iter_stagehand_objects() if obj.stagehand.uid}

    for obj in iter_stagehand_objects():
        for link in obj.stagehand.links:
            if not link.connectedObjectUid:
                continue

            if link.connectedObjectUid not in live_uids:
                link.connectedObjectUid = ""
                link.connectedLinkIndex = -1
                continue

            other_obj = find_object_by_uid(link.connectedObjectUid)
            if other_obj is None:
                link.connectedObjectUid = ""
                link.connectedLinkIndex = -1
                continue

            other_link = get_link(other_obj, link.connectedLinkIndex)
            if other_link is None or other_link.connectedObjectUid != obj.stagehand.uid:
                link.connectedObjectUid = ""
                link.connectedLinkIndex = -1


def connection_maintenance_timer():
    try:
        prune_stale_connections()
    except RuntimeError:
        pass

    return CONNECTION_MAINTENANCE_INTERVAL


def register():
    if not bpy.app.timers.is_registered(connection_maintenance_timer):
        bpy.app.timers.register(
            connection_maintenance_timer,
            first_interval=CONNECTION_MAINTENANCE_INTERVAL,
        )


def unregister():
    if bpy.app.timers.is_registered(connection_maintenance_timer):
        bpy.app.timers.unregister(connection_maintenance_timer)
