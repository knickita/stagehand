import uuid
import time
from collections import defaultdict, namedtuple
from math import floor, pi, radians

import bpy
from bpy.app.handlers import persistent
from bpy_extras import view3d_utils
from mathutils import Matrix, Quaternion, Vector

from .AddStagehandObject import ensure_stagehand_link_uid, ensure_stagehand_uid
from .LinkTypes import are_link_types_compatible
from . import ProjectDatabase
from .RegistrationUtils import (
    safe_add_handler,
    safe_register_class,
    safe_remove_handler,
    safe_remove_keymaps,
    safe_unregister_class,
)


CONNECTION_REFRESH_POLL_INTERVAL = 0.05
CONNECTION_REFRESH_SETTLE_INTERVAL = 0.1
AUTO_CONNECT_DISTANCE_THRESHOLD = 0.0001
AUTO_CONNECT_ANGLE_THRESHOLD = radians(0.1)
LINK_ALIGNMENT_FLIP = Quaternion((0.0, 0.0, 1.0), radians(180.0))
LinkSearchItem = namedtuple(
    "LinkSearchItem",
    (
        "obj",
        "link_index",
        "link",
        "object_uid",
        "link_uid",
        "center",
        "rotation",
        "bucket_key",
    ),
)
_DIRTY_CONNECTION_OBJECT_UIDS = set()
_ALL_CONNECTIONS_DIRTY = False
_DUPLICATE_REPAIR_NEEDED = True
_DIRTY_CONNECTION_REFRESH_DEADLINE = 0.0
_LAST_STAGEHAND_OBJECT_NAMES = set()
addon_keymaps = []


def _report_connection_profile(message):
    try:
        bpy.ops.stagehand.report_connection_profile('EXEC_DEFAULT', message=message)
    except Exception:
        pass


def _print_connection_profile(profile_entries, total_time, **metadata):
    print("Stagehand UpdateConnections profile")
    print(f"  total: {_format_profile_time(total_time)}")
    for key, value in metadata.items():
        print(f"  {key}: {value}")
    for label, elapsed in profile_entries:
        print(f"  {label}: {_format_profile_time(elapsed)}")


def _log_connection_timer_run(name, start_time, **metadata):
    print(f"Stagehand timer: {name}")
    print(f"  elapsed: {_format_profile_time(time.perf_counter() - start_time)}")
    for key, value in metadata.items():
        print(f"  {key}: {value}")


def _format_profile_time(seconds):
    return f"{seconds * 1000.0:.2f} ms"


def _profile_step(profile_entries, label, callback):
    start_time = time.perf_counter()
    result = callback()
    profile_entries.append((label, time.perf_counter() - start_time))
    return result


def _data_objects():
    return getattr(bpy.data, "objects", None)


def is_stagehand_object(obj):
    return (
        obj is not None
        and getattr(obj, "stagehand", None) is not None
        and obj.stagehand.is_stagehand_object
    )


def _clear_legacy_link_connection(link):
    link.connectedObjectUid = ""
    link.connectedLinkUid = ""
    link.connectedLinkIndex = -1


def _get_database_connections(create=False):
    return ProjectDatabase.get_connections(create=create)


def _set_database_connections(connections):
    ProjectDatabase.set_connections(connections)


def _get_database_link_parents(create=False):
    return ProjectDatabase.get_link_parents(create=create)


def _set_database_link_parents(link_parents):
    ProjectDatabase.set_link_parents(link_parents)


def _get_database_object_names(create=False):
    return ProjectDatabase.get_object_names(create=create)


def _set_database_object_names(object_names):
    ProjectDatabase.set_object_names(object_names)


def _set_database_connection_pair(link_uid_a, link_uid_b):
    if not link_uid_a or not link_uid_b:
        return

    connections = _get_database_connections(create=True)
    if connections.get(link_uid_a) == link_uid_b and connections.get(link_uid_b) == link_uid_a:
        return

    connections[link_uid_a] = link_uid_b
    connections[link_uid_b] = link_uid_a
    _set_database_connections(connections)


def _remove_database_connection(link_uid):
    if not link_uid:
        return

    connections = _get_database_connections(create=False)
    if not connections:
        return

    other_link_uid = connections.pop(link_uid, "")
    if other_link_uid and connections.get(other_link_uid) == link_uid:
        del connections[other_link_uid]
    _set_database_connections(connections)


def _get_connected_link_uid(link):
    if link is None:
        return ""
    link_uid = ensure_stagehand_link_uid(link)
    connections = _get_database_connections(create=False)
    return str(connections.get(link_uid, ""))


def _get_connected_link_uid_from_connections(link, connections):
    if link is None:
        return ""
    return str(connections.get(ensure_stagehand_link_uid(link), ""))


def _is_link_connected(link):
    return bool(_get_connected_link_uid(link))


def _is_link_connected_in_connections(link, connections):
    return bool(_get_connected_link_uid_from_connections(link, connections))


def _set_link_parent(link_uid, object_uid):
    if not link_uid or not object_uid:
        return

    link_parents = _get_database_link_parents(create=True)
    if link_parents.get(link_uid) == object_uid:
        return

    link_parents[link_uid] = object_uid
    _set_database_link_parents(link_parents)


def _remove_link_parent(link_uid):
    if not link_uid:
        return

    link_parents = _get_database_link_parents(create=False)
    if link_uid not in link_parents:
        return

    del link_parents[link_uid]
    _set_database_link_parents(link_parents)


def _set_object_name(object_uid, object_name):
    if not object_uid or not object_name:
        return

    object_names = _get_database_object_names(create=True)
    if object_names.get(object_uid) == object_name:
        return

    object_names[object_uid] = object_name
    _set_database_object_names(object_names)


def _remove_object_name(object_uid):
    if not object_uid:
        return

    object_names = _get_database_object_names(create=False)
    if object_uid not in object_names:
        return

    del object_names[object_uid]
    _set_database_object_names(object_names)


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
    angle = min(angle, abs((2.0 * pi) - angle))
    return distance, angle


def link_alignment_metrics(obj, link_index, other_obj, other_link_index):
    return _link_alignment_metrics(obj, link_index, other_obj, other_link_index)


def links_are_aligned(obj, link_index, other_obj, other_link_index):
    distance, angle = _link_alignment_metrics(obj, link_index, other_obj, other_link_index)
    if distance is None:
        return False
    return distance <= AUTO_CONNECT_DISTANCE_THRESHOLD and angle <= AUTO_CONNECT_ANGLE_THRESHOLD


def _sorted_uid_group(objects):
    return sorted(objects, key=lambda obj: obj.name_full)


def find_object_by_uid(uid):
    if not uid:
        return None

    object_names = _get_database_object_names(create=False)
    object_name = object_names.get(uid, "")
    if object_name:
        objects = _data_objects()
        if objects is not None:
            obj = objects.get(object_name)
            if obj is not None and is_stagehand_object(obj) and get_object_uid(obj) == uid:
                return obj

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


def get_connected_link_uid(link):
    return _get_connected_link_uid(link)


def is_link_connected(link):
    return _is_link_connected(link)


def get_link_parent_object_uid(link_uid):
    return str(_get_database_link_parents(create=False).get(link_uid, ""))


def clear_link_connection(obj, link_index):
    link = get_link(obj, link_index)
    if link is None:
        return

    _remove_database_connection(ensure_stagehand_link_uid(link))
    _clear_legacy_link_connection(link)


def disconnect_link(obj, link_index):
    link = get_link(obj, link_index)
    if link is None:
        return

    other_link_uid = _get_connected_link_uid(link)
    if not other_link_uid:
        clear_link_connection(obj, link_index)
        return

    other_obj = find_object_by_uid(_get_database_link_parents(create=False).get(other_link_uid, ""))
    clear_link_connection(obj, link_index)

    if other_obj is None:
        return

    other_link, _other_link_index = find_link_by_uid(other_obj, other_link_uid)
    if other_link is not None:
        _clear_legacy_link_connection(other_link)


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

    _set_object_name(uid_a, obj_a.name_full)
    _set_object_name(uid_b, obj_b.name_full)
    _set_link_parent(link_uid_a, uid_a)
    _set_link_parent(link_uid_b, uid_b)
    _set_database_connection_pair(link_uid_a, link_uid_b)
    _clear_legacy_link_connection(link_a)
    _clear_legacy_link_connection(link_b)
    return True


def get_connected_link(obj, link_index):
    link = get_link(obj, link_index)
    if link is None:
        return None, None, None

    other_link_uid = _get_connected_link_uid(link)
    if not other_link_uid:
        _clear_legacy_link_connection(link)
        return None, None, None

    other_obj_uid = _get_database_link_parents(create=False).get(other_link_uid, "")
    other_obj = find_object_by_uid(other_obj_uid)
    if other_obj is None:
        return None, None, None

    other_link, other_link_index = find_link_by_uid(other_obj, other_link_uid)

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


def _rebuild_database_indexes():
    object_names = {}
    link_parents = {}

    for obj in iter_stagehand_objects():
        object_uid = get_object_uid(obj)
        object_names[object_uid] = obj.name_full

        for _index, link in iter_object_links(obj):
            link_parents[ensure_stagehand_link_uid(link)] = object_uid

    _set_database_object_names(object_names)
    _set_database_link_parents(link_parents)


def mark_duplicate_repair_needed():
    global _DUPLICATE_REPAIR_NEEDED
    _DUPLICATE_REPAIR_NEEDED = True


def _repair_duplicate_ids(rebuild_indexes=True):
    if rebuild_indexes:
        _rebuild_database_indexes()

    repaired_count = 0
    connections = _get_database_connections(create=False)
    link_parents = _get_database_link_parents(create=False)
    groups = defaultdict(list)
    for obj in iter_stagehand_objects():
        groups[get_object_uid(obj)].append(obj)

    duplicate_groups = {
        uid: _sorted_uid_group(objects)
        for uid, objects in groups.items()
        if uid and len(objects) > 1
    }
    if not duplicate_groups:
        return 0

    duplicate_snapshots = {}
    object_uid_remap = {}
    link_uid_remap = {}
    duplicated_link_items = {}

    for original_uid, objects in duplicate_groups.items():
        for duplicate_index, obj in enumerate(objects[1:], start=1):
            link_snapshots = []
            for link_index, link in iter_object_links(obj):
                old_link_uid = ensure_stagehand_link_uid(link)
                connected_link_uid = str(connections.get(old_link_uid, ""))
                link_snapshots.append(
                    {
                        "link_index": link_index,
                        "old_link_uid": old_link_uid,
                        "connected_object_uid": str(link_parents.get(connected_link_uid, "")),
                        "connected_link_uid": connected_link_uid,
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
            repaired_count += 1

            for link_index, link in iter_object_links(obj):
                old_link_uid = ensure_stagehand_link_uid(link)
                new_link_uid = str(uuid.uuid4())
                link.uid = new_link_uid
                link_uid_remap[(original_uid, duplicate_index, old_link_uid)] = new_link_uid
                duplicated_link_items[new_link_uid] = link
                _clear_legacy_link_connection(link)

    _rebuild_database_indexes()
    link_parents = _get_database_link_parents(create=False)

    for _obj_name, snapshot in duplicate_snapshots.items():
        duplicate_index = snapshot["duplicate_index"]
        for link_snapshot in snapshot["links"]:
            new_link_uid = link_uid_remap.get(
                (
                    snapshot["original_uid"],
                    duplicate_index,
                    link_snapshot["old_link_uid"],
                )
            )
            link = duplicated_link_items.get(new_link_uid)
            if not new_link_uid or link is None:
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
                continue

            if target_duplicate_link_uid not in link_parents:
                continue

            connections[new_link_uid] = target_duplicate_link_uid
            connections[target_duplicate_link_uid] = new_link_uid
            _clear_legacy_link_connection(link)
            target_link = duplicated_link_items.get(target_duplicate_link_uid)
            if target_link is not None:
                _clear_legacy_link_connection(target_link)

    _set_database_connections(connections)

    return repaired_count


def _repair_duplicate_ids_if_needed():
    global _DUPLICATE_REPAIR_NEEDED

    if not _DUPLICATE_REPAIR_NEEDED:
        return 0

    repaired_count = _repair_duplicate_ids(rebuild_indexes=False)
    _DUPLICATE_REPAIR_NEEDED = False
    return repaired_count


def _connection_is_working(
    obj,
    link_index,
    live_uids=None,
    connections=None,
    link_parents=None,
    object_by_uid=None,
    link_by_uid=None,
):
    if connections is None:
        connections = _get_database_connections(create=False)
    if link_parents is None:
        link_parents = _get_database_link_parents(create=False)

    link = get_link(obj, link_index)
    if link is None:
        return False

    link_uid = ensure_stagehand_link_uid(link)
    other_link_uid = str(connections.get(link_uid, ""))
    if not other_link_uid:
        _clear_legacy_link_connection(link)
        return True

    other_obj_uid = str(link_parents.get(other_link_uid, ""))
    if not other_obj_uid:
        return False
    if live_uids is not None and other_obj_uid not in live_uids:
        return False

    other_obj = object_by_uid.get(other_obj_uid) if object_by_uid is not None else find_object_by_uid(other_obj_uid)
    if other_obj is None:
        return False

    if link_by_uid is not None:
        other_link_entry = link_by_uid.get(other_link_uid)
        if other_link_entry is None:
            return False
        _other_obj, other_link_index, other_link = other_link_entry
    else:
        other_link, other_link_index = find_link_by_uid(other_obj, other_link_uid)
    if other_link is None:
        return False
    if str(connections.get(other_link_uid, "")) != link_uid:
        return False

    distance, angle = _link_alignment_metrics(obj, link_index, other_obj, other_link_index)
    if distance is None:
        return False

    if distance > AUTO_CONNECT_DISTANCE_THRESHOLD or angle > AUTO_CONNECT_ANGLE_THRESHOLD:
        return False

    return True


def _remove_connections_not_working(objects):
    live_objects = list(iter_stagehand_objects())
    object_by_uid = {}
    link_by_uid = {}

    for live_obj in live_objects:
        live_uid = get_object_uid(live_obj)
        if live_uid:
            object_by_uid[live_uid] = live_obj
        for live_link_index, live_link in iter_object_links(live_obj):
            link_by_uid[ensure_stagehand_link_uid(live_link)] = (
                live_obj,
                live_link_index,
                live_link,
            )

    live_uids = set(object_by_uid.keys())
    connections = _get_database_connections(create=False)
    link_parents = _get_database_link_parents(create=False)
    removed_count = 0

    for obj in _unique_stagehand_objects(objects):
        for index, link in iter_object_links(obj):
            if not _is_link_connected_in_connections(link, connections):
                _clear_legacy_link_connection(link)
                continue
            if _connection_is_working(
                obj,
                index,
                live_uids=live_uids,
                connections=connections,
                link_parents=link_parents,
                object_by_uid=object_by_uid,
                link_by_uid=link_by_uid,
            ):
                _clear_legacy_link_connection(link)
                continue
            disconnect_link(obj, index)
            removed_count += 1

    return removed_count


def _prune_orphan_database_connections():
    live_link_uids = {
        ensure_stagehand_link_uid(link)
        for obj in iter_stagehand_objects()
        for _index, link in iter_object_links(obj)
    }
    live_object_uids = {get_object_uid(obj) for obj in iter_stagehand_objects()}

    link_parents = _get_database_link_parents(create=False)
    live_link_parents = {
        link_uid: object_uid
        for link_uid, object_uid in link_parents.items()
        if link_uid in live_link_uids and object_uid in live_object_uids
    }
    if live_link_parents != link_parents:
        _set_database_link_parents(live_link_parents)

    object_names = _get_database_object_names(create=False)
    live_object_names = {
        object_uid: object_name
        for object_uid, object_name in object_names.items()
        if object_uid in live_object_uids and find_object_by_uid(object_uid) is not None
    }
    if live_object_names != object_names:
        _set_database_object_names(live_object_names)

    connections = _get_database_connections(create=False)
    if connections:
        live_connections = {
            link_uid: other_link_uid
            for link_uid, other_link_uid in connections.items()
            if (
                link_uid in live_link_uids
                and other_link_uid in live_link_uids
                and link_uid != other_link_uid
                and connections.get(other_link_uid) == link_uid
            )
        }
        if live_connections != connections:
            _set_database_connections(live_connections)


def prune_stale_connections():
    _rebuild_database_indexes()
    _repair_duplicate_ids()
    _prune_orphan_database_connections()
    _remove_connections_not_working(iter_stagehand_objects())


def _iter_compatible_unconnected_links(obj, connections=None):
    if connections is None:
        connections = _get_database_connections(create=False)

    for index, link in iter_object_links(obj):
        if _is_link_connected_in_connections(link, connections):
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


def _link_candidate_key(item_a, item_b, distance, angle):
    return (
        distance,
        angle,
        item_a.object_uid,
        item_b.object_uid,
        item_a.link_index,
        item_b.link_index,
    )


def _connection_candidate(item_a, item_b):
    if item_a.obj == item_b.obj:
        return None
    if not are_link_types_compatible(item_a.link.type, item_b.link.type):
        return None

    distance = (item_b.center - item_a.center).length
    angle = _link_alignment_angle(
        item_a.link,
        item_a.rotation,
        item_b.link,
        item_b.rotation,
    )
    angle = min(angle, abs((2.0 * pi) - angle))
    if distance > AUTO_CONNECT_DISTANCE_THRESHOLD or angle > AUTO_CONNECT_ANGLE_THRESHOLD:
        return None

    return (
        *_link_candidate_key(item_a, item_b, distance, angle),
        item_a,
        item_b,
    )


def _free_link_items(objects, connections=None):
    if connections is None:
        connections = _get_database_connections(create=False)

    link_items = []
    for obj in _unique_stagehand_objects(objects):
        object_uid = get_object_uid(obj)
        for link_index, link in _iter_compatible_unconnected_links(obj, connections=connections):
            center, rotation = _link_transform(obj, link)
            link_items.append(
                LinkSearchItem(
                    obj,
                    link_index,
                    link,
                    object_uid,
                    ensure_stagehand_link_uid(link),
                    center,
                    rotation,
                    _link_center_bucket_key(center),
                )
            )
    return link_items


def _link_center_bucket_key(center):
    cell_size = AUTO_CONNECT_DISTANCE_THRESHOLD
    return (
        floor(center.x / cell_size),
        floor(center.y / cell_size),
        floor(center.z / cell_size),
    )


def _nearby_link_center_bucket_keys(bucket_key):
    bucket_x, bucket_y, bucket_z = bucket_key
    for offset_x in (-1, 0, 1):
        for offset_y in (-1, 0, 1):
            for offset_z in (-1, 0, 1):
                yield (
                    bucket_x + offset_x,
                    bucket_y + offset_y,
                    bucket_z + offset_z,
                )


def _connect_candidate_pairs(candidates):
    connected_any = False
    connected_count = 0
    connected_link_uids = set(_get_database_connections(create=False).keys())

    for candidate in sorted(candidates, key=lambda item: item[:6]):
        item_a, item_b = candidate[6], candidate[7]

        if item_a.link_uid in connected_link_uids or item_b.link_uid in connected_link_uids:
            continue
        if connect_links(item_a.obj, item_a.link_index, item_b.obj, item_b.link_index):
            connected_any = True
            connected_count += 1
            connected_link_uids.add(item_a.link_uid)
            connected_link_uids.add(item_b.link_uid)

    return connected_any, connected_count


def _connect_free_links_inside_group(objects):
    candidates = []
    connections = _get_database_connections(create=False)
    free_items = _free_link_items(objects, connections=connections)
    buckets = defaultdict(list)

    for item in free_items:
        for nearby_bucket_key in _nearby_link_center_bucket_keys(item.bucket_key):
            for other_item in buckets.get(nearby_bucket_key, ()):
                candidate = _connection_candidate(item, other_item)
                if candidate is not None:
                    candidates.append(candidate)

        buckets[item.bucket_key].append(item)

    connected_any, connected_count = _connect_candidate_pairs(candidates)
    if candidates:
        print("Stagehand connect free links inside group")
        print(f"  free links: {len(free_items)}")
        print(f"  candidate pairs: {len(candidates)}")
        print(f"  connected pairs: {connected_count}")
    elif free_items:
        print("Stagehand connect free links inside group")
        print(f"  free links: {len(free_items)}")
        print("  candidate pairs: 0")
        print("  connected pairs: 0")

    connections = _get_database_connections(create=False)
    return [
        item
        for item in free_items
        if not connections.get(item.link_uid)
    ]


def _connect_free_links_to_scene(free_items, group_objects):
    connections = _get_database_connections(create=False)
    group_uids = {get_object_uid(obj) for obj in group_objects}
    scene_items = []

    for obj in iter_stagehand_objects():
        obj_uid = get_object_uid(obj)
        if not obj_uid or obj_uid in group_uids:
            continue
        scene_items.extend(_free_link_items((obj,), connections=connections))

    scene_buckets = defaultdict(list)
    for item in scene_items:
        scene_buckets[item.bucket_key].append(item)

    candidates = []
    for item_a in free_items:
        for nearby_bucket_key in _nearby_link_center_bucket_keys(item_a.bucket_key):
            for item_b in scene_buckets.get(nearby_bucket_key, ()):
                candidate = _connection_candidate(item_a, item_b)
                if candidate is not None:
                    candidates.append(candidate)

    _connected_any, connected_count = _connect_candidate_pairs(candidates)
    if candidates or free_items or scene_items:
        print("Stagehand connect free links to scene")
        print(f"  group free links: {len(free_items)}")
        print(f"  scene free links: {len(scene_items)}")
        print(f"  candidate pairs: {len(candidates)}")
        print(f"  connected pairs: {connected_count}")

    connections = _get_database_connections(create=False)
    return [
        item
        for item in free_items
        if not connections.get(item.link_uid)
    ]


def UpdateConnections(objects):
    _report_connection_profile("\n")
    profile_start_time = time.perf_counter()
    profile_entries = []
    raw_objects = [obj for obj in (objects or ()) if is_stagehand_object(obj)]
    if not raw_objects:
        _profile_step(profile_entries, "rebuild indexes", _rebuild_database_indexes)
        _profile_step(profile_entries, "prune orphan database connections", _prune_orphan_database_connections)
        total_time = time.perf_counter() - profile_start_time
        profile_summary = "; ".join(
            f"{label}: {_format_profile_time(elapsed)}"
            for label, elapsed in profile_entries
        )
        _report_connection_profile(
            f"Stagehand UpdateConnections: total {_format_profile_time(total_time)}; "
            f"objects: 0; free links: 0; {profile_summary}"
        )
        _print_connection_profile(
            profile_entries,
            total_time,
            objects=0,
            free_links=0,
        )
        return []

    _profile_step(profile_entries, "rebuild indexes", _rebuild_database_indexes)
    repaired_duplicate_count = _profile_step(
        profile_entries,
        "repair duplicate ids",
        _repair_duplicate_ids_if_needed,
    )
    _profile_step(profile_entries, "prune orphan database connections", _prune_orphan_database_connections)
    update_objects = _profile_step(
        profile_entries,
        "unique update objects",
        lambda: _unique_stagehand_objects(raw_objects),
    )

    removed_count = _profile_step(
        profile_entries,
        "remove invalid connections",
        lambda: _remove_connections_not_working(update_objects),
    )
    free_links = _profile_step(
        profile_entries,
        "connect free links inside group",
        lambda: _connect_free_links_inside_group(update_objects),
    )
    remaining_free_links = _profile_step(
        profile_entries,
        "connect free links to scene",
        lambda: _connect_free_links_to_scene(free_links, update_objects),
    )

    total_time = time.perf_counter() - profile_start_time
    profile_summary = "; ".join(
        f"{label}: {_format_profile_time(elapsed)}"
        for label, elapsed in profile_entries
    )
    _report_connection_profile(
        f"Stagehand UpdateConnections: total {_format_profile_time(total_time)}; "
        f"objects: {len(update_objects)}; removed: {removed_count}; "
        f"free links: {len(remaining_free_links)}; {profile_summary}"
    )
    _print_connection_profile(
        profile_entries,
        total_time,
        objects=len(update_objects),
        removed=removed_count,
        repaired_duplicates=repaired_duplicate_count,
        free_links=len(remaining_free_links),
    )
    return remaining_free_links


def refresh_connections_for_objects(objects):
    return UpdateConnections(objects)


def _iter_pending_operators():
    seen_ids = set()
    context = bpy.context
    window = getattr(context, "window", None)
    if window is not None:
        for operator in getattr(window, "modal_operators", ()):
            operator_key = id(operator)
            if operator_key in seen_ids:
                continue
            seen_ids.add(operator_key)
            yield operator


def _pending_operator_ids():
    return [str(getattr(operator, "bl_idname", "")).lower() for operator in _iter_pending_operators()]


def _transform_operator_active():
    for operator_id in _pending_operator_ids():
        if operator_id.startswith("transform_ot_"):
            return True
        if operator_id.startswith("object_ot_duplicate"):
            return True
    return False


def _schedule_connection_refresh(delay=CONNECTION_REFRESH_SETTLE_INTERVAL):
    global _DIRTY_CONNECTION_REFRESH_DEADLINE

    _DIRTY_CONNECTION_REFRESH_DEADLINE = time.monotonic() + max(delay, 0.0)
    if not bpy.app.timers.is_registered(dirty_connection_refresh_timer):
        bpy.app.timers.register(
            dirty_connection_refresh_timer,
            first_interval=CONNECTION_REFRESH_POLL_INTERVAL,
        )


def mark_objects_dirty(objects, delay=CONNECTION_REFRESH_SETTLE_INTERVAL):
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

    _schedule_connection_refresh(delay=delay)


def mark_all_objects_dirty(delay=CONNECTION_REFRESH_SETTLE_INTERVAL):
    global _ALL_CONNECTIONS_DIRTY

    _ALL_CONNECTIONS_DIRTY = True
    _schedule_connection_refresh(delay=delay)


def _stagehand_object_name_set():
    return {obj.name_full for obj in iter_stagehand_objects()}


def _mark_stagehand_object_membership_changes(delay=CONNECTION_REFRESH_SETTLE_INTERVAL):
    global _LAST_STAGEHAND_OBJECT_NAMES

    current_names = _stagehand_object_name_set()
    if current_names == _LAST_STAGEHAND_OBJECT_NAMES:
        return False

    _LAST_STAGEHAND_OBJECT_NAMES = current_names
    mark_duplicate_repair_needed()
    mark_all_objects_dirty(delay=delay)
    return True


def _process_dirty_connection_refresh():
    global _ALL_CONNECTIONS_DIRTY

    processed_all_objects = _ALL_CONNECTIONS_DIRTY
    if _ALL_CONNECTIONS_DIRTY:
        refresh_objects = list(iter_stagehand_objects())
        _DIRTY_CONNECTION_OBJECT_UIDS.clear()
        _ALL_CONNECTIONS_DIRTY = False
    else:
        refresh_objects = []
        while _DIRTY_CONNECTION_OBJECT_UIDS:
            uid = _DIRTY_CONNECTION_OBJECT_UIDS.pop()
            obj = find_object_by_uid(uid)
            if obj is not None:
                refresh_objects.append(obj)

    UpdateConnections(refresh_objects)
    return processed_all_objects


def dirty_connection_refresh_timer():
    timer_start = time.perf_counter()
    try:
        if not _DIRTY_CONNECTION_OBJECT_UIDS and not _ALL_CONNECTIONS_DIRTY:
            return None

        if _transform_operator_active():
            return CONNECTION_REFRESH_POLL_INTERVAL

        if time.monotonic() < _DIRTY_CONNECTION_REFRESH_DEADLINE:
            return CONNECTION_REFRESH_POLL_INTERVAL

        processed_all_objects = _process_dirty_connection_refresh()
    except Exception:
        _log_connection_timer_run(
            "dirty_connection_refresh_timer",
            timer_start,
            action="exception",
            next_interval=CONNECTION_REFRESH_POLL_INTERVAL,
        )
        return CONNECTION_REFRESH_POLL_INTERVAL

    if processed_all_objects:
        _DIRTY_CONNECTION_OBJECT_UIDS.clear()

    if _DIRTY_CONNECTION_OBJECT_UIDS or _ALL_CONNECTIONS_DIRTY:
        _log_connection_timer_run(
            "dirty_connection_refresh_timer",
            timer_start,
            dirty_objects=len(_DIRTY_CONNECTION_OBJECT_UIDS),
            all_dirty=_ALL_CONNECTIONS_DIRTY,
            action="processed and reschedule",
            next_interval=CONNECTION_REFRESH_POLL_INTERVAL,
        )
        return CONNECTION_REFRESH_POLL_INTERVAL
    _log_connection_timer_run(
        "dirty_connection_refresh_timer",
        timer_start,
        dirty_objects=0,
        all_dirty=False,
        action="processed and stop",
    )
    return None


def initial_connection_refresh_timer():
    timer_start = time.perf_counter()
    if _data_objects() is None:
        _log_connection_timer_run(
            "initial_connection_refresh_timer",
            timer_start,
            action="wait for bpy.data.objects",
            next_interval=CONNECTION_REFRESH_POLL_INTERVAL,
        )
        return CONNECTION_REFRESH_POLL_INTERVAL

    ProjectDatabase.get_database_object(create=True)
    _mark_stagehand_object_membership_changes(delay=0.0)
    mark_all_objects_dirty(delay=0.0)
    _log_connection_timer_run(
        "initial_connection_refresh_timer",
        timer_start,
        action="initialized and stop",
    )
    return None


@persistent
def stagehand_depsgraph_update_post(_scene, depsgraph):
    handler_start = time.perf_counter()
    dirty_objects = []
    for update in getattr(depsgraph, "updates", ()):
        updated_id = getattr(update, "id", None)
        if not isinstance(updated_id, bpy.types.Object):
            continue
        if not is_stagehand_object(updated_id):
            continue
        if getattr(update, "is_updated_transform", False):
            dirty_objects.append(updated_id)

    if dirty_objects:
        mark_objects_dirty(dirty_objects)
    membership_changed = _mark_stagehand_object_membership_changes()
    if membership_changed:
        _log_connection_timer_run(
            "stagehand_depsgraph_update_post",
            handler_start,
            dirty_objects=len(dirty_objects),
            membership_changed=membership_changed,
        )


@persistent
def stagehand_undo_redo_post(_dummy):
    mark_duplicate_repair_needed()
    mark_all_objects_dirty(delay=0.0)


class STAGEHAND_OT_report_connection_profile(bpy.types.Operator):
    bl_idname = "stagehand.report_connection_profile"
    bl_label = "Stagehand Connection Profile Report"

    message: bpy.props.StringProperty(default="")

    def execute(self, _context):
        self.report({'INFO'}, self.message)
        return {'FINISHED'}


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
    safe_register_class(STAGEHAND_OT_report_connection_profile)
    safe_register_class(STAGEHAND_OT_select_connected_objects)
    register_keymap()
    safe_add_handler(bpy.app.handlers.depsgraph_update_post, stagehand_depsgraph_update_post)
    safe_add_handler(bpy.app.handlers.undo_post, stagehand_undo_redo_post)
    safe_add_handler(bpy.app.handlers.redo_post, stagehand_undo_redo_post)
    safe_add_handler(bpy.app.handlers.load_post, stagehand_undo_redo_post)
    if not bpy.app.timers.is_registered(initial_connection_refresh_timer):
        bpy.app.timers.register(
            initial_connection_refresh_timer,
            first_interval=CONNECTION_REFRESH_POLL_INTERVAL,
        )


def unregister():
    global _ALL_CONNECTIONS_DIRTY

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
    _ALL_CONNECTIONS_DIRTY = False
    safe_unregister_class(STAGEHAND_OT_select_connected_objects)
    safe_unregister_class(STAGEHAND_OT_report_connection_profile)
