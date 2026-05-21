import uuid
import time
from collections import defaultdict, namedtuple
from math import floor, pi, radians

import bpy
from bpy.app.handlers import persistent
from bpy_extras import view3d_utils
from mathutils import Matrix, Quaternion, Vector

from .AddStagehandObject import ensure_stagehand_link_uid, ensure_stagehand_uid
from .LinkTypes import are_link_types_compatible, default_link_allow_rotations
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
LINK_ROTATION_MODE_NONE = "none"
LINK_ROTATION_MODE_90 = "90"
LINK_ROTATION_MODE_FREE = "free"
LINK_ROTATION_MODES = {
    LINK_ROTATION_MODE_NONE,
    LINK_ROTATION_MODE_90,
    LINK_ROTATION_MODE_FREE,
}
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
LinkIndexItem = namedtuple(
    "LinkIndexItem",
    (
        "obj",
        "link_index",
        "link",
    ),
)
_DIRTY_CONNECTION_OBJECT_UIDS = set()
_ALL_CONNECTIONS_DIRTY = False
_DUPLICATE_REPAIR_NEEDED = True
_LAST_REPAIRED_DUPLICATE_OBJECT_UIDS = set()
_MEMBERSHIP_REFRESH_NEEDS_AUTOCONNECT = False
_MEMBERSHIP_TRACKING_INITIALIZED = False
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


def _link_allow_rotations(link):
    if hasattr(link, "is_property_set") and not link.is_property_set("allowRotations"):
        return default_link_allow_rotations(link.type)

    mode = str(
        getattr(link, "allowRotations", LINK_ROTATION_MODE_NONE)
        or LINK_ROTATION_MODE_NONE
    ).strip().lower()
    if mode not in LINK_ROTATION_MODES:
        return LINK_ROTATION_MODE_NONE
    return mode


def _combined_link_rotation_mode(link, other_link):
    modes = {_link_allow_rotations(link), _link_allow_rotations(other_link)}
    if LINK_ROTATION_MODE_FREE in modes:
        return LINK_ROTATION_MODE_FREE
    if LINK_ROTATION_MODE_90 in modes:
        return LINK_ROTATION_MODE_90
    return LINK_ROTATION_MODE_NONE


def _project_to_plane(vector, normal):
    projected = vector - (normal * vector.dot(normal))
    if projected.length_squared <= 0.000001:
        return None
    return projected.normalized()


def _quarter_turn_roll_error(rotation, desired_rotation, forward):
    actual_x = _project_to_plane(rotation @ Vector((1, 0, 0)), forward)
    desired_x = _project_to_plane(desired_rotation @ Vector((1, 0, 0)), forward)
    if actual_x is None or desired_x is None:
        return pi

    roll_angle = desired_x.angle(actual_x, 0.0)
    return min(
        abs(roll_angle),
        abs(roll_angle - (pi * 0.5)),
        abs(roll_angle - pi),
    )


def link_alignment_rotation_delta(link_rotation, target_rotation):
    desired_link_rotation = target_rotation @ LINK_ALIGNMENT_FLIP
    return desired_link_rotation @ link_rotation.inverted()


def _link_alignment_angle(link, rotation, other_link, other_rotation):
    if link.cylindricalType or other_link.cylindricalType:
        return _link_forward(rotation).angle(-_link_forward(other_rotation), 0.0)

    mode = _combined_link_rotation_mode(link, other_link)
    if mode != LINK_ROTATION_MODE_NONE:
        forward_angle = _link_forward(rotation).angle(-_link_forward(other_rotation), 0.0)
        if mode == LINK_ROTATION_MODE_FREE:
            return forward_angle

        forward = _link_forward(rotation)
        desired_rotation = other_rotation @ LINK_ALIGNMENT_FLIP
        return max(forward_angle, _quarter_turn_roll_error(rotation, desired_rotation, forward))

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
    global _LAST_REPAIRED_DUPLICATE_OBJECT_UIDS

    _LAST_REPAIRED_DUPLICATE_OBJECT_UIDS = set()
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
            _LAST_REPAIRED_DUPLICATE_OBJECT_UIDS.add(new_object_uid)
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
    context=None,
):
    if context is not None:
        connections = context.connections
        link_parents = context.link_parents
        live_uids = context.live_uids
        object_by_uid = context.object_by_uid
        link_by_uid = context.link_by_uid
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

    link_entry = None
    if link_by_uid is not None:
        link_entry = link_by_uid.get(link_uid)
    other_link_entry = None
    if link_by_uid is not None:
        other_link_entry = link_by_uid.get(other_link_uid)

    if other_link_entry is not None:
        _other_obj, other_link_index, other_link = other_link_entry
    elif link_by_uid is not None:
        return False
    else:
        other_link, other_link_index = find_link_by_uid(other_obj, other_link_uid)
    if other_link is None:
        return False
    if str(connections.get(other_link_uid, "")) != link_uid:
        return False

    if link_entry is not None:
        _obj, _link_index, _link = link_entry
    if context is not None:
        center, rotation = context.get_link_transform(obj, link)
    else:
        center, rotation = _link_transform(obj, link)

    if context is not None:
        other_center, other_rotation = context.get_link_transform(other_obj, other_link)
    else:
        other_center, other_rotation = _link_transform(other_obj, other_link)

    distance = (other_center - center).length
    angle = _link_alignment_angle(link, rotation, other_link, other_rotation)
    angle = min(angle, abs((2.0 * pi) - angle))

    if distance > AUTO_CONNECT_DISTANCE_THRESHOLD or angle > AUTO_CONNECT_ANGLE_THRESHOLD:
        return False

    return True


def _remove_connections_not_working(objects, context=None):
    if context is None:
        context = ConnectionContext()

    removed_count = 0

    for obj in context.unique_stagehand_objects(objects):
        for index, link in iter_object_links(obj):
            if not _is_link_connected_in_connections(link, context.connections):
                _clear_legacy_link_connection(link)
                continue
            if _connection_is_working(
                obj,
                index,
                context=context,
            ):
                _clear_legacy_link_connection(link)
                continue
            link_uid = ensure_stagehand_link_uid(link)
            disconnect_link(obj, index)
            context.note_connection_removed(link_uid)
            removed_count += 1

    return removed_count


def _prune_orphan_database_connections(context=None):
    if context is None:
        context = ConnectionContext()

    live_link_uids = context.live_link_uids
    live_object_uids = context.live_uids

    link_parents = context.link_parents
    live_link_parents = {
        link_uid: object_uid
        for link_uid, object_uid in link_parents.items()
        if link_uid in live_link_uids and object_uid in live_object_uids
    }
    if live_link_parents != link_parents:
        _set_database_link_parents(live_link_parents)
        context.link_parents = live_link_parents

    object_names = context.object_names
    live_object_names = {
        object_uid: object_name
        for object_uid, object_name in object_names.items()
        if object_uid in live_object_uids and context.object_by_uid.get(object_uid) is not None
    }
    if live_object_names != object_names:
        _set_database_object_names(live_object_names)
        context.object_names = live_object_names

    connections = context.connections
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
            context.connections = live_connections
            context.connected_link_uids = set(live_connections.keys())


def prune_stale_connections():
    if _DUPLICATE_REPAIR_NEEDED:
        _repair_duplicate_ids_if_needed()
        _rebuild_database_indexes()
    else:
        _rebuild_database_indexes()
    context = ConnectionContext()
    _prune_orphan_database_connections(context=context)
    _remove_connections_not_working(iter_stagehand_objects(), context=context)


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


def _free_link_items(objects, connections=None, context=None):
    if context is not None:
        return context.free_link_items(objects)
    if connections is None:
        connections = _get_database_connections(create=False)

    context = ConnectionContext()
    context.connections = connections
    context.connected_link_uids = set(connections.keys())
    return context.free_link_items(objects)


def _link_center_bucket_key(center):
    cell_size = AUTO_CONNECT_DISTANCE_THRESHOLD
    return (
        floor(center.x / cell_size),
        floor(center.y / cell_size),
        floor(center.z / cell_size),
    )


class ConnectionContext:
    def __init__(self):
        self.refresh_database()
        self.refresh_live_indexes()

    def refresh_database(self):
        self.connections = _get_database_connections(create=False)
        self.link_parents = _get_database_link_parents(create=False)
        self.object_names = _get_database_object_names(create=False)
        self.connected_link_uids = set(self.connections.keys())

    def refresh_live_indexes(self):
        self.live_objects = list(iter_stagehand_objects())
        self.object_by_uid = {}
        self.link_by_uid = {}
        self._link_transform_cache = {}

        for obj in self.live_objects:
            object_uid = get_object_uid(obj)
            if object_uid:
                self.object_by_uid[object_uid] = obj
            for link_index, link in iter_object_links(obj):
                link_uid = ensure_stagehand_link_uid(link)
                self.link_by_uid[link_uid] = LinkIndexItem(obj, link_index, link)

        self.live_uids = set(self.object_by_uid.keys())
        self.live_link_uids = set(self.link_by_uid.keys())

    def refresh_after_database_write(self):
        self.refresh_database()
        self.refresh_live_indexes()

    def note_connection_removed(self, link_uid):
        other_link_uid = self.connections.pop(link_uid, "")
        self.connected_link_uids.discard(link_uid)
        if other_link_uid and self.connections.get(other_link_uid) == link_uid:
            del self.connections[other_link_uid]
            self.connected_link_uids.discard(other_link_uid)

    def unique_stagehand_objects(self, objects):
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

    def get_link_transform(self, obj, link):
        link_uid = ensure_stagehand_link_uid(link)
        cached = self._link_transform_cache.get(link_uid)
        if cached is None:
            cached = _link_transform(obj, link)
            self._link_transform_cache[link_uid] = cached
        return cached

    def make_search_item(self, obj, link_index, link):
        center, rotation = self.get_link_transform(obj, link)
        return LinkSearchItem(
            obj,
            link_index,
            link,
            get_object_uid(obj),
            ensure_stagehand_link_uid(link),
            center,
            rotation,
            _link_center_bucket_key(center),
        )

    def free_link_items(self, objects):
        link_items = []
        for obj in self.unique_stagehand_objects(objects):
            for link_index, link in _iter_compatible_unconnected_links(
                obj,
                connections=self.connections,
            ):
                link_items.append(self.make_search_item(obj, link_index, link))
        return link_items


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


def _connect_candidate_pairs(candidates, context=None):
    connected_any = False
    connected_count = 0
    if context is not None:
        connected_link_uids = set(context.connected_link_uids)
    else:
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
            if context is not None:
                context.connected_link_uids.add(item_a.link_uid)
                context.connected_link_uids.add(item_b.link_uid)
                context.connections[item_a.link_uid] = item_b.link_uid
                context.connections[item_b.link_uid] = item_a.link_uid

    return connected_any, connected_count


def _connect_free_links_inside_group(objects, context=None):
    if context is None:
        context = ConnectionContext()

    candidates = []
    free_items = context.free_link_items(objects)
    buckets = defaultdict(list)

    for item in free_items:
        for nearby_bucket_key in _nearby_link_center_bucket_keys(item.bucket_key):
            for other_item in buckets.get(nearby_bucket_key, ()):
                candidate = _connection_candidate(item, other_item)
                if candidate is not None:
                    candidates.append(candidate)

        buckets[item.bucket_key].append(item)

    connected_any, connected_count = _connect_candidate_pairs(candidates, context=context)
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

    return [
        item
        for item in free_items
        if item.link_uid not in context.connected_link_uids
    ]


def _connect_free_links_to_scene(free_items, group_objects, context=None):
    if context is None:
        context = ConnectionContext()

    group_uids = {get_object_uid(obj) for obj in group_objects}
    scene_items = []

    for obj in context.live_objects:
        obj_uid = get_object_uid(obj)
        if not obj_uid or obj_uid in group_uids:
            continue
        scene_items.extend(context.free_link_items((obj,)))

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

    _connected_any, connected_count = _connect_candidate_pairs(candidates, context=context)
    if candidates or free_items or scene_items:
        print("Stagehand connect free links to scene")
        print(f"  group free links: {len(free_items)}")
        print(f"  scene free links: {len(scene_items)}")
        print(f"  candidate pairs: {len(candidates)}")
        print(f"  connected pairs: {connected_count}")

    return [
        item
        for item in free_items
        if item.link_uid not in context.connected_link_uids
    ]


def UpdateConnections(
    objects,
    auto_connect=True,
    validate_existing=True,
    prefer_repaired_duplicates=False,
    connect_inside_group=True,
):
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

    duplicate_repair_needed = _DUPLICATE_REPAIR_NEEDED
    if duplicate_repair_needed:
        repaired_duplicate_count = _profile_step(
            profile_entries,
            "repair duplicate ids",
            _repair_duplicate_ids_if_needed,
        )
        _profile_step(profile_entries, "rebuild indexes", _rebuild_database_indexes)
    else:
        _profile_step(profile_entries, "rebuild indexes", _rebuild_database_indexes)
        repaired_duplicate_count = _profile_step(
            profile_entries,
            "repair duplicate ids",
            _repair_duplicate_ids_if_needed,
        )
    context = _profile_step(profile_entries, "build connection context", ConnectionContext)
    _profile_step(
        profile_entries,
        "prune orphan database connections",
        lambda: _prune_orphan_database_connections(context=context),
    )
    if prefer_repaired_duplicates and _LAST_REPAIRED_DUPLICATE_OBJECT_UIDS:
        update_objects = _profile_step(
            profile_entries,
            "unique update objects",
            lambda: [
                context.object_by_uid[uid]
                for uid in _LAST_REPAIRED_DUPLICATE_OBJECT_UIDS
                if uid in context.object_by_uid
            ],
        )
    else:
        update_objects = _profile_step(
            profile_entries,
            "unique update objects",
            lambda: context.unique_stagehand_objects(raw_objects),
        )

    if validate_existing:
        removed_count = _profile_step(
            profile_entries,
            "remove invalid connections",
            lambda: _remove_connections_not_working(update_objects, context=context),
        )
    else:
        removed_count = 0
        profile_entries.append(("remove invalid connections skipped", 0.0))
    if auto_connect:
        if connect_inside_group:
            free_links = _profile_step(
                profile_entries,
                "connect free links inside group",
                lambda: _connect_free_links_inside_group(update_objects, context=context),
            )
        else:
            free_links = _profile_step(
                profile_entries,
                "collect free links for scene connect",
                lambda: context.free_link_items(update_objects),
            )
        remaining_free_links = _profile_step(
            profile_entries,
            "connect free links to scene",
            lambda: _connect_free_links_to_scene(free_links, update_objects, context=context),
        )
    else:
        remaining_free_links = []
        profile_entries.append(("auto connect skipped", 0.0))

    total_time = time.perf_counter() - profile_start_time
    profile_summary = "; ".join(
        f"{label}: {_format_profile_time(elapsed)}"
        for label, elapsed in profile_entries
    )
    _report_connection_profile(
        f"Stagehand UpdateConnections: total {_format_profile_time(total_time)}; "
        f"objects: {len(update_objects)}; removed: {removed_count}; "
        f"repaired duplicates: {repaired_duplicate_count}; "
        f"auto connect: {auto_connect}; "
        f"validate existing: {validate_existing}; "
        f"free links: {len(remaining_free_links)}; {profile_summary}"
    )
    _print_connection_profile(
        profile_entries,
        total_time,
        objects=len(update_objects),
        removed=removed_count,
        repaired_duplicates=repaired_duplicate_count,
        auto_connect=auto_connect,
        validate_existing=validate_existing,
        prefer_repaired_duplicates=prefer_repaired_duplicates,
        connect_inside_group=connect_inside_group,
        free_links=len(remaining_free_links),
    )
    return remaining_free_links


def refresh_connections_for_objects(
    objects,
    auto_connect=True,
    validate_existing=True,
    prefer_repaired_duplicates=False,
    connect_inside_group=True,
):
    return UpdateConnections(
        objects,
        auto_connect=auto_connect,
        validate_existing=validate_existing,
        prefer_repaired_duplicates=prefer_repaired_duplicates,
        connect_inside_group=connect_inside_group,
    )


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


def _mark_stagehand_object_membership_changes(delay=CONNECTION_REFRESH_SETTLE_INTERVAL, auto_connect=True):
    global _LAST_STAGEHAND_OBJECT_NAMES, _MEMBERSHIP_REFRESH_NEEDS_AUTOCONNECT, _MEMBERSHIP_TRACKING_INITIALIZED

    current_names = _stagehand_object_name_set()
    if current_names == _LAST_STAGEHAND_OBJECT_NAMES:
        _MEMBERSHIP_TRACKING_INITIALIZED = True
        return False

    first_membership_snapshot = not _MEMBERSHIP_TRACKING_INITIALIZED
    _LAST_STAGEHAND_OBJECT_NAMES = current_names
    _MEMBERSHIP_TRACKING_INITIALIZED = True
    mark_duplicate_repair_needed()
    if auto_connect and not first_membership_snapshot:
        _MEMBERSHIP_REFRESH_NEEDS_AUTOCONNECT = True
    mark_all_objects_dirty(delay=delay)
    return True


def _process_dirty_connection_refresh():
    global _ALL_CONNECTIONS_DIRTY, _MEMBERSHIP_REFRESH_NEEDS_AUTOCONNECT

    processed_all_objects = _ALL_CONNECTIONS_DIRTY
    membership_autoconnect = _MEMBERSHIP_REFRESH_NEEDS_AUTOCONNECT
    _MEMBERSHIP_REFRESH_NEEDS_AUTOCONNECT = False
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

    duplicate_membership_refresh = processed_all_objects and membership_autoconnect
    UpdateConnections(
        refresh_objects,
        auto_connect=(not processed_all_objects) or duplicate_membership_refresh,
        validate_existing=not processed_all_objects,
        prefer_repaired_duplicates=duplicate_membership_refresh,
        connect_inside_group=True,
    )
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
    _mark_stagehand_object_membership_changes(delay=0.0, auto_connect=False)
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


class STAGEHAND_OT_repair_all_connections(bpy.types.Operator):
    bl_idname = "stagehand.repair_all_connections"
    bl_label = "Repair All Stagehand Links"
    bl_description = "Run a full Stagehand link repair, validation, and auto-connect pass"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, _context):
        start_time = time.perf_counter()
        mark_duplicate_repair_needed()
        remaining_free_links = UpdateConnections(
            list(iter_stagehand_objects()),
            auto_connect=True,
            validate_existing=True,
            prefer_repaired_duplicates=False,
            connect_inside_group=True,
        )
        elapsed = time.perf_counter() - start_time
        self.report(
            {'INFO'},
            (
                "Stagehand link repair completed in "
                f"{_format_profile_time(elapsed)}; "
                f"free links: {len(remaining_free_links)}"
            ),
        )
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
    safe_register_class(STAGEHAND_OT_repair_all_connections)
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
    global _ALL_CONNECTIONS_DIRTY, _MEMBERSHIP_REFRESH_NEEDS_AUTOCONNECT, _MEMBERSHIP_TRACKING_INITIALIZED

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
    _MEMBERSHIP_REFRESH_NEEDS_AUTOCONNECT = False
    _MEMBERSHIP_TRACKING_INITIALIZED = False
    safe_unregister_class(STAGEHAND_OT_select_connected_objects)
    safe_unregister_class(STAGEHAND_OT_repair_all_connections)
    safe_unregister_class(STAGEHAND_OT_report_connection_profile)
