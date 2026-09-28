import uuid
import time
from collections import defaultdict, namedtuple
from contextlib import contextmanager
from math import degrees, floor, pi, radians

import bpy
from bpy.app.handlers import persistent
from bpy_extras import view3d_utils
from mathutils import Matrix, Quaternion, Vector

from .AddStagehandObject import ensure_stagehand_link_uid, ensure_stagehand_uid
from .LinkTypes import (
    StagehandLinkType,
    are_link_types_compatible,
    default_link_allow_rotations,
    get_child_link_rotation_constraint,
    snap_child_link_rotation_degrees,
)
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
CYLINDRICAL_LINK_SEARCH_BUCKET_SIZE = 0.25
LINK_ALIGNMENT_FLIP = Quaternion((0.0, 0.0, 1.0), radians(180.0))
LINK_ROTATION_MODE_NONE = "none"
LINK_ROTATION_MODE_90 = "90"
LINK_ROTATION_MODE_FREE = "free"
LINK_ROTATION_MODES = {
    LINK_ROTATION_MODE_NONE,
    LINK_ROTATION_MODE_90,
    LINK_ROTATION_MODE_FREE,
}
AUDIO_ARRAY_TAG = "audioarray"
AUDIO_ARRAY_PARENT_UID_KEY = "stagehand_audioarray_parent_uid"
AUDIO_ARRAY_PARENT_LINK_KEY = "stagehand_audioarray_parent_link"
AUDIO_ARRAY_CHILD_LINK_KEY = "stagehand_audioarray_child_link"
AUDIO_ARRAY_REST_LOCATION_KEY = "stagehand_audioarray_rest_location"
AUDIO_ARRAY_REST_ROTATION_KEY = "stagehand_audioarray_rest_rotation"
AUDIO_ARRAY_REST_SCALE_KEY = "stagehand_audioarray_rest_scale"
AUDIO_ARRAY_PREVIOUS_LOCKS_KEY = "stagehand_audioarray_previous_locks"
AUDIO_ARRAY_LIMIT_CONSTRAINT_NAME = "Stagehand Audio Array Rotation"
_AUDIO_ARRAY_UPDATE_ACTIVE = False
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
_DIRTY_GENERATED_POWERLINE_OBJECT_UIDS = set()
_ALL_CONNECTIONS_DIRTY = False
_DUPLICATE_REPAIR_NEEDED = True
_LAST_REPAIRED_DUPLICATE_OBJECT_UIDS = set()
_MEMBERSHIP_REFRESH_NEEDS_AUTOCONNECT = False
_MEMBERSHIP_TRACKING_INITIALIZED = False
_DIRTY_CONNECTION_REFRESH_DEADLINE = 0.0
_LAST_STAGEHAND_OBJECT_NAMES = set()
addon_keymaps = []
_ACTIVE_DATABASE_TRANSACTION = None


class DatabaseTransaction:
    """Batch persistent connection-index changes into a single commit."""

    def __init__(self):
        self.connections = ProjectDatabase.get_connections(create=False)
        self.link_parents = ProjectDatabase.get_link_parents(create=False)
        self.object_names = ProjectDatabase.get_object_names(create=False)
        self.dirty_mappings = set()
        self.write_requests = {
            "connections": 0,
            "link_parents": 0,
            "object_names": 0,
        }
        self.persisted_writes = 0
        self.commit_elapsed = 0.0
        self.committed = False
        self.rolled_back = False

    def update_mapping(self, mapping_name, values):
        current_values = getattr(self, mapping_name)
        if values is not current_values:
            current_values.clear()
            current_values.update(values)
        self.dirty_mappings.add(mapping_name)
        self.write_requests[mapping_name] += 1

    def commit(self):
        started_at = time.perf_counter()
        if "object_names" in self.dirty_mappings:
            ProjectDatabase.set_object_names(self.object_names)
            self.persisted_writes += 1
        if "link_parents" in self.dirty_mappings:
            ProjectDatabase.set_link_parents(self.link_parents)
            self.persisted_writes += 1
        if "connections" in self.dirty_mappings:
            ProjectDatabase.set_connections(self.connections)
            self.persisted_writes += 1
        self.commit_elapsed = time.perf_counter() - started_at
        self.committed = True


@contextmanager
def database_transaction():
    """Use in-memory connection indexes and persist them once on success."""
    global _ACTIVE_DATABASE_TRANSACTION

    if _ACTIVE_DATABASE_TRANSACTION is not None:
        yield _ACTIVE_DATABASE_TRANSACTION
        return

    transaction = DatabaseTransaction()
    _ACTIVE_DATABASE_TRANSACTION = transaction
    try:
        yield transaction
    except BaseException:
        transaction.rolled_back = True
        raise
    else:
        _ACTIVE_DATABASE_TRANSACTION = None
        transaction.commit()
    finally:
        _ACTIVE_DATABASE_TRANSACTION = None


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
    if _ACTIVE_DATABASE_TRANSACTION is not None:
        return _ACTIVE_DATABASE_TRANSACTION.connections
    return ProjectDatabase.get_connections(create=create)


def _set_database_connections(connections):
    if _ACTIVE_DATABASE_TRANSACTION is not None:
        _ACTIVE_DATABASE_TRANSACTION.update_mapping("connections", connections)
        return
    ProjectDatabase.set_connections(connections)


def _get_database_link_parents(create=False):
    if _ACTIVE_DATABASE_TRANSACTION is not None:
        return _ACTIVE_DATABASE_TRANSACTION.link_parents
    return ProjectDatabase.get_link_parents(create=create)


def _set_database_link_parents(link_parents):
    if _ACTIVE_DATABASE_TRANSACTION is not None:
        _ACTIVE_DATABASE_TRANSACTION.update_mapping("link_parents", link_parents)
        return
    ProjectDatabase.set_link_parents(link_parents)


def _get_database_object_names(create=False):
    if _ACTIVE_DATABASE_TRANSACTION is not None:
        return _ACTIVE_DATABASE_TRANSACTION.object_names
    return ProjectDatabase.get_object_names(create=create)


def _set_database_object_names(object_names):
    if _ACTIVE_DATABASE_TRANSACTION is not None:
        _ACTIVE_DATABASE_TRANSACTION.update_mapping("object_names", object_names)
        return
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
    if link_uid not in connections:
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


def _is_pipe_attachment_pair(link, other_link):
    link_type = StagehandLinkType(int(link.type))
    other_link_type = StagehandLinkType(int(other_link.type))
    pipe_attachment_types = {
        StagehandLinkType.HOOK,
        StagehandLinkType.LITEC_CARRELLO_SECTION_INNER,
    }
    return (
        (link_type in pipe_attachment_types and other_link_type == StagehandLinkType.PIPE)
        or (link_type == StagehandLinkType.PIPE and other_link_type in pipe_attachment_types)
    )


def _cylindrical_link_length(link):
    if link.length > 0.0:
        return float(link.length)
    return float(link.displayRadius if link.displayRadius > 0.0 else 0.0)


def _closest_point_on_cylindrical_link(point, cylindrical_link, cylindrical_center, cylindrical_rotation):
    axis = _link_forward(cylindrical_rotation)
    length = _cylindrical_link_length(cylindrical_link)
    offset = point - cylindrical_center
    projected_distance = offset.dot(axis)
    clamped_distance = max(0.0, min(length, projected_distance))
    closest_point = cylindrical_center + (axis * clamped_distance)
    outside_distance = 0.0
    if projected_distance < 0.0:
        outside_distance = -projected_distance
    elif projected_distance > length:
        outside_distance = projected_distance - length
    return closest_point, projected_distance, outside_distance


def link_snap_target_point(link, center, rotation, other_link, other_center, other_rotation):
    if _is_pipe_attachment_pair(link, other_link):
        if other_link.cylindricalType:
            closest_point, _projected_distance, _outside_distance = _closest_point_on_cylindrical_link(
                center,
                other_link,
                other_center,
                other_rotation,
            )
            return closest_point
        if link.cylindricalType:
            return other_center
    return other_center


def _link_position_distance(link, center, rotation, other_link, other_center, other_rotation):
    if _is_pipe_attachment_pair(link, other_link):
        if other_link.cylindricalType:
            closest_point, _projected_distance, outside_distance = _closest_point_on_cylindrical_link(
                center,
                other_link,
                other_center,
                other_rotation,
            )
            if outside_distance > AUTO_CONNECT_DISTANCE_THRESHOLD:
                return None
            return (closest_point - center).length
        if link.cylindricalType:
            closest_point, _projected_distance, outside_distance = _closest_point_on_cylindrical_link(
                other_center,
                link,
                center,
                rotation,
            )
            if outside_distance > AUTO_CONNECT_DISTANCE_THRESHOLD:
                return None
            return (closest_point - other_center).length

    return (other_center - center).length


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


def _child_link_constraint_components(link, rotation, other_link, other_rotation):
    constraint = get_child_link_rotation_constraint(other_link.type, link.type)
    if constraint is not None:
        child_rotation = rotation
        parent_rotation = other_rotation
        parent_link_type = other_link.type
        child_link_type = link.type
    else:
        constraint = get_child_link_rotation_constraint(link.type, other_link.type)
        if constraint is None:
            return None
        child_rotation = other_rotation
        parent_rotation = rotation
        parent_link_type = link.type
        child_link_type = other_link.type

    desired_child_rotation = parent_rotation @ LINK_ALIGNMENT_FLIP
    relative_rotation = desired_child_rotation.inverted() @ child_rotation
    relative_euler = relative_rotation.to_euler('XYZ')
    axis_index = {"X": 0, "Y": 1, "Z": 2}[constraint.axis]
    angle_degrees = degrees(relative_euler[axis_index])
    off_axis_error = max(
        abs(relative_euler[index])
        for index in range(3)
        if index != axis_index
    )
    snapped_degrees = snap_child_link_rotation_degrees(
        parent_link_type,
        child_link_type,
        angle_degrees,
    )
    step_error = radians(abs(angle_degrees - snapped_degrees))
    return constraint, angle_degrees, off_axis_error, step_error


def _child_link_constraint_alignment_error(
    link,
    rotation,
    other_link,
    other_rotation,
):
    components = _child_link_constraint_components(
        link,
        rotation,
        other_link,
        other_rotation,
    )
    if components is None:
        return None
    _constraint, _angle_degrees, off_axis_error, step_error = components
    return max(off_axis_error, step_error)


def link_alignment_rotation_delta(link_rotation, target_rotation):
    desired_link_rotation = target_rotation @ LINK_ALIGNMENT_FLIP
    return desired_link_rotation @ link_rotation.inverted()


def _link_alignment_angle(link, rotation, other_link, other_rotation):
    if _is_pipe_attachment_pair(link, other_link) and (link.cylindricalType or other_link.cylindricalType):
        return 0.0
    if link.cylindricalType or other_link.cylindricalType:
        return _link_forward(rotation).angle(-_link_forward(other_rotation), 0.0)

    constrained_error = _child_link_constraint_alignment_error(
        link,
        rotation,
        other_link,
        other_rotation,
    )
    if constrained_error is not None:
        return constrained_error

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

def align_object_link_to_target(obj, link_index, target_obj, target_link_index):
    """Align one object link to another without creating the connection."""
    return _align_object_link_to_target(obj, link_index, target_obj, target_link_index)



def _link_alignment_metrics(obj, link_index, other_obj, other_link_index):
    link = get_link(obj, link_index)
    other_link = get_link(other_obj, other_link_index)
    if link is None or other_link is None:
        return None, None

    center, rotation = _link_transform(obj, link)
    other_center, other_rotation = _link_transform(other_obj, other_link)
    distance = _link_position_distance(link, center, rotation, other_link, other_center, other_rotation)
    if distance is None:
        return None, None
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
    other_link = None
    other_link_index = -1
    if other_obj is not None:
        other_link, other_link_index = find_link_by_uid(other_obj, other_link_uid)
        if other_link is not None:
            _clear_constrained_connection_parenting(
                obj,
                link_index,
                other_obj,
                other_link_index,
            )

    clear_link_connection(obj, link_index)

    if other_obj is None:
        return

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
    _sync_constrained_connection_parenting(
        obj_a,
        link_index_a,
        obj_b,
        link_index_b,
    )
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


def _stagehand_object_has_tag(obj, tag):
    if not is_stagehand_object(obj):
        return False
    target = str(tag).strip().lower()
    return any(
        str(tag_item.value).strip().lower() == target
        for tag_item in obj.stagehand.tags
    )


def _constrained_connection_pair(obj_a, link_index_a, obj_b, link_index_b):
    link_a = get_link(obj_a, link_index_a)
    link_b = get_link(obj_b, link_index_b)
    if link_a is None or link_b is None:
        return None

    constraint = get_child_link_rotation_constraint(link_a.type, link_b.type)
    if constraint is not None:
        return obj_a, link_index_a, link_a, obj_b, link_index_b, link_b, constraint

    constraint = get_child_link_rotation_constraint(link_b.type, link_a.type)
    if constraint is not None:
        return obj_b, link_index_b, link_b, obj_a, link_index_a, link_a, constraint
    return None


def _set_constrained_child_locks(child_obj):
    child_obj.lock_location = (True, True, True)
    child_obj.lock_rotation = (False, True, True)
    child_obj.lock_scale = (True, True, True)


def _ensure_constrained_child_rotation_limit(
    child_obj,
    constraint,
    rest_rotation,
):
    rotation_limit = child_obj.constraints.get(AUDIO_ARRAY_LIMIT_CONSTRAINT_NAME)
    if rotation_limit is not None and rotation_limit.type != 'LIMIT_ROTATION':
        child_obj.constraints.remove(rotation_limit)
        rotation_limit = None
    if rotation_limit is None:
        rotation_limit = child_obj.constraints.new(type='LIMIT_ROTATION')
        rotation_limit.name = AUDIO_ARRAY_LIMIT_CONSTRAINT_NAME

    rotation_limit.owner_space = 'LOCAL'
    rotation_limit.influence = 1.0
    if hasattr(rotation_limit, "use_transform_limit"):
        rotation_limit.use_transform_limit = True

    axis_index = {"X": 0, "Y": 1, "Z": 2}[constraint.axis]
    for index, axis_name in enumerate(("x", "y", "z")):
        setattr(rotation_limit, f"use_limit_{axis_name}", True)
        if index == axis_index:
            minimum = rest_rotation[index] + radians(constraint.min_degrees)
            maximum = rest_rotation[index] + radians(constraint.max_degrees)
        else:
            minimum = rest_rotation[index]
            maximum = rest_rotation[index]
        setattr(rotation_limit, f"min_{axis_name}", minimum)
        setattr(rotation_limit, f"max_{axis_name}", maximum)


def _clear_constrained_child_parent(child_obj, expected_parent=None):
    stored_parent_uid = str(child_obj.get(AUDIO_ARRAY_PARENT_UID_KEY, ""))
    if not stored_parent_uid:
        return False
    if (
        expected_parent is not None
        and stored_parent_uid != get_object_uid(expected_parent)
    ):
        return False

    world_matrix = child_obj.matrix_world.copy()
    if expected_parent is None or child_obj.parent == expected_parent:
        child_obj.parent = None
        child_obj.matrix_world = world_matrix

    previous_locks = child_obj.get(AUDIO_ARRAY_PREVIOUS_LOCKS_KEY, ())
    if len(previous_locks) == 9:
        child_obj.lock_location = tuple(bool(value) for value in previous_locks[:3])
        child_obj.lock_rotation = tuple(bool(value) for value in previous_locks[3:6])
        child_obj.lock_scale = tuple(bool(value) for value in previous_locks[6:9])
    else:
        child_obj.lock_location = (False, False, False)
        child_obj.lock_rotation = (False, False, False)
        child_obj.lock_scale = (False, False, False)

    rotation_limit = child_obj.constraints.get(AUDIO_ARRAY_LIMIT_CONSTRAINT_NAME)
    if rotation_limit is not None:
        child_obj.constraints.remove(rotation_limit)

    for key in (
        AUDIO_ARRAY_PARENT_UID_KEY,
        AUDIO_ARRAY_PARENT_LINK_KEY,
        AUDIO_ARRAY_CHILD_LINK_KEY,
        AUDIO_ARRAY_REST_LOCATION_KEY,
        AUDIO_ARRAY_REST_ROTATION_KEY,
        AUDIO_ARRAY_REST_SCALE_KEY,
        AUDIO_ARRAY_PREVIOUS_LOCKS_KEY,
    ):
        if key in child_obj:
            del child_obj[key]
    return True


def _sync_constrained_connection_parenting(
    obj_a,
    link_index_a,
    obj_b,
    link_index_b,
):
    pair = _constrained_connection_pair(
        obj_a,
        link_index_a,
        obj_b,
        link_index_b,
    )
    if pair is None:
        return False

    (
        parent_obj,
        parent_link_index,
        parent_link,
        child_obj,
        child_link_index,
        child_link,
        constraint,
    ) = pair
    if not _stagehand_object_has_tag(child_obj, AUDIO_ARRAY_TAG):
        return False

    parent_uid = get_object_uid(parent_obj)
    if (
        str(child_obj.get(AUDIO_ARRAY_PARENT_UID_KEY, "")) == parent_uid
        and child_obj.parent == parent_obj
    ):
        _enforce_constrained_child_transform(child_obj)
        return True

    if child_obj.get(AUDIO_ARRAY_PARENT_UID_KEY, ""):
        _clear_constrained_child_parent(child_obj)

    child_center, child_rotation = _link_transform(child_obj, child_link)
    parent_center, parent_rotation = _link_transform(parent_obj, parent_link)
    del child_center, parent_center
    components = _child_link_constraint_components(
        child_link,
        child_rotation,
        parent_link,
        parent_rotation,
    )
    angle_degrees = components[1] if components is not None else 0.0

    previous_locks = (
        *tuple(child_obj.lock_location),
        *tuple(child_obj.lock_rotation),
        *tuple(child_obj.lock_scale),
    )
    world_matrix = child_obj.matrix_world.copy()
    child_obj.parent = parent_obj
    child_obj.matrix_world = world_matrix
    child_obj.rotation_mode = 'XYZ'

    axis_index = {"X": 0, "Y": 1, "Z": 2}[constraint.axis]
    rest_rotation = list(child_obj.rotation_euler)
    rest_rotation[axis_index] -= radians(angle_degrees)
    child_obj[AUDIO_ARRAY_PARENT_UID_KEY] = parent_uid
    child_obj[AUDIO_ARRAY_PARENT_LINK_KEY] = int(parent_link_index)
    child_obj[AUDIO_ARRAY_CHILD_LINK_KEY] = int(child_link_index)
    child_obj[AUDIO_ARRAY_REST_LOCATION_KEY] = list(child_obj.location)
    child_obj[AUDIO_ARRAY_REST_ROTATION_KEY] = rest_rotation
    child_obj[AUDIO_ARRAY_REST_SCALE_KEY] = list(child_obj.scale)
    child_obj[AUDIO_ARRAY_PREVIOUS_LOCKS_KEY] = [
        int(value) for value in previous_locks
    ]
    _set_constrained_child_locks(child_obj)
    _ensure_constrained_child_rotation_limit(
        child_obj,
        constraint,
        rest_rotation,
    )
    _enforce_constrained_child_transform(child_obj)
    return True


def _clear_constrained_connection_parenting(
    obj_a,
    link_index_a,
    obj_b,
    link_index_b,
):
    pair = _constrained_connection_pair(
        obj_a,
        link_index_a,
        obj_b,
        link_index_b,
    )
    if pair is None:
        return False
    parent_obj, _parent_index, _parent_link, child_obj, *_rest = pair
    return _clear_constrained_child_parent(child_obj, expected_parent=parent_obj)


def _enforce_constrained_child_transform(child_obj):
    parent_uid = str(child_obj.get(AUDIO_ARRAY_PARENT_UID_KEY, ""))
    parent_obj = child_obj.parent
    if not parent_uid or parent_obj is None:
        return False
    if get_object_uid(parent_obj) != parent_uid:
        return False

    try:
        parent_link_index = int(child_obj[AUDIO_ARRAY_PARENT_LINK_KEY])
        child_link_index = int(child_obj[AUDIO_ARRAY_CHILD_LINK_KEY])
        rest_location = tuple(child_obj[AUDIO_ARRAY_REST_LOCATION_KEY])
        rest_rotation = tuple(child_obj[AUDIO_ARRAY_REST_ROTATION_KEY])
        rest_scale = tuple(child_obj[AUDIO_ARRAY_REST_SCALE_KEY])
    except (KeyError, TypeError, ValueError):
        return False
    if not all(len(values) == 3 for values in (rest_location, rest_rotation, rest_scale)):
        return False

    parent_link = get_link(parent_obj, parent_link_index)
    child_link = get_link(child_obj, child_link_index)
    if parent_link is None or child_link is None:
        return False
    constraint = get_child_link_rotation_constraint(
        parent_link.type,
        child_link.type,
    )
    if constraint is None:
        return False

    child_obj.rotation_mode = 'XYZ'
    axis_index = {"X": 0, "Y": 1, "Z": 2}[constraint.axis]
    rotation = list(child_obj.rotation_euler)
    requested_degrees = degrees(rotation[axis_index] - rest_rotation[axis_index])
    snapped_degrees = snap_child_link_rotation_degrees(
        parent_link.type,
        child_link.type,
        requested_degrees,
    )
    for index in range(3):
        rotation[index] = rest_rotation[index]
    rotation[axis_index] += radians(snapped_degrees)

    child_obj.location = rest_location
    child_obj.rotation_euler = rotation
    child_obj.scale = rest_scale
    _set_constrained_child_locks(child_obj)
    _ensure_constrained_child_rotation_limit(
        child_obj,
        constraint,
        rest_rotation,
    )
    return True


def sync_constrained_link_hierarchy():
    constrained_child_uids = set()
    for child_obj in list(iter_stagehand_objects()):
        if not _stagehand_object_has_tag(child_obj, AUDIO_ARRAY_TAG):
            continue
        for child_link_index, child_link in iter_object_links(child_obj):
            parent_obj, parent_link, parent_link_index = get_connected_link(
                child_obj,
                child_link_index,
            )
            if parent_obj is None or parent_link is None:
                continue
            if get_child_link_rotation_constraint(parent_link.type, child_link.type) is None:
                continue
            if _sync_constrained_connection_parenting(
                parent_obj,
                parent_link_index,
                child_obj,
                child_link_index,
            ):
                constrained_child_uids.add(get_object_uid(child_obj))

    for obj in list(iter_stagehand_objects()):
        if not obj.get(AUDIO_ARRAY_PARENT_UID_KEY, ""):
            continue
        if get_object_uid(obj) not in constrained_child_uids:
            _clear_constrained_child_parent(obj)


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

    distance = _link_position_distance(link, center, rotation, other_link, other_center, other_rotation)
    if distance is None:
        return False
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
    sync_constrained_link_hierarchy()


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

    distance = _link_position_distance(
        item_a.link,
        item_a.center,
        item_a.rotation,
        item_b.link,
        item_b.center,
        item_b.rotation,
    )
    if distance is None:
        return None
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


def _cylindrical_search_bucket_key(point):
    return (
        floor(point.x / CYLINDRICAL_LINK_SEARCH_BUCKET_SIZE),
        floor(point.y / CYLINDRICAL_LINK_SEARCH_BUCKET_SIZE),
        floor(point.z / CYLINDRICAL_LINK_SEARCH_BUCKET_SIZE),
    )


def _iter_cylindrical_search_bucket_range(min_point, max_point):
    min_key = _cylindrical_search_bucket_key(min_point)
    max_key = _cylindrical_search_bucket_key(max_point)
    for bucket_x in range(min_key[0], max_key[0] + 1):
        for bucket_y in range(min_key[1], max_key[1] + 1):
            for bucket_z in range(min_key[2], max_key[2] + 1):
                yield (bucket_x, bucket_y, bucket_z)


def _is_pipe_item(item):
    return item.link.cylindricalType and int(item.link.type) == int(StagehandLinkType.PIPE)


def _is_pipe_attachment_item(item):
    return int(item.link.type) in {
        int(StagehandLinkType.HOOK),
        int(StagehandLinkType.LITEC_CARRELLO_SECTION_INNER),
    }


def _pipe_item_bucket_keys(item):
    axis = _link_forward(item.rotation)
    length = _cylindrical_link_length(item.link)
    end_point = item.center + (axis * length)
    min_point = Vector((
        min(item.center.x, end_point.x) - AUTO_CONNECT_DISTANCE_THRESHOLD,
        min(item.center.y, end_point.y) - AUTO_CONNECT_DISTANCE_THRESHOLD,
        min(item.center.z, end_point.z) - AUTO_CONNECT_DISTANCE_THRESHOLD,
    ))
    max_point = Vector((
        max(item.center.x, end_point.x) + AUTO_CONNECT_DISTANCE_THRESHOLD,
        max(item.center.y, end_point.y) + AUTO_CONNECT_DISTANCE_THRESHOLD,
        max(item.center.z, end_point.z) + AUTO_CONNECT_DISTANCE_THRESHOLD,
    ))
    yield from _iter_cylindrical_search_bucket_range(min_point, max_point)


def _append_pipe_attachment_candidates(candidates, attachment_items, pipe_items, seen_pair_keys):
    pipe_buckets = defaultdict(list)
    for pipe_item in pipe_items:
        for bucket_key in _pipe_item_bucket_keys(pipe_item):
            pipe_buckets[bucket_key].append(pipe_item)

    for attachment_item in attachment_items:
        bucket_key = _cylindrical_search_bucket_key(attachment_item.center)
        for pipe_item in pipe_buckets.get(bucket_key, ()):
            pair_key = tuple(sorted((attachment_item.link_uid, pipe_item.link_uid)))
            if pair_key in seen_pair_keys:
                continue
            seen_pair_keys.add(pair_key)
            candidate = _connection_candidate(attachment_item, pipe_item)
            if candidate is not None:
                candidates.append(candidate)

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

    seen_pair_keys = {
        tuple(sorted((candidate[6].link_uid, candidate[7].link_uid)))
        for candidate in candidates
    }
    _append_pipe_attachment_candidates(
        candidates,
        [item for item in free_items if _is_pipe_attachment_item(item)],
        [item for item in free_items if _is_pipe_item(item)],
        seen_pair_keys,
    )

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

    seen_pair_keys = {
        tuple(sorted((candidate[6].link_uid, candidate[7].link_uid)))
        for candidate in candidates
    }
    _append_pipe_attachment_candidates(
        candidates,
        [item for item in free_items if _is_pipe_attachment_item(item)],
        [item for item in scene_items if _is_pipe_item(item)],
        seen_pair_keys,
    )
    _append_pipe_attachment_candidates(
        candidates,
        [item for item in scene_items if _is_pipe_attachment_item(item)],
        [item for item in free_items if _is_pipe_item(item)],
        seen_pair_keys,
    )

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


def _mark_generated_powerlines_dirty(objects):
    for obj in objects:
        if not is_stagehand_object(obj):
            continue

        uid = get_object_uid(obj)
        if uid:
            _DIRTY_GENERATED_POWERLINE_OBJECT_UIDS.add(uid)


def _clear_generated_powerlines_for_dirty_objects():
    if not _DIRTY_GENERATED_POWERLINE_OBJECT_UIDS:
        return 0

    link_uids = set()
    while _DIRTY_GENERATED_POWERLINE_OBJECT_UIDS:
        uid = _DIRTY_GENERATED_POWERLINE_OBJECT_UIDS.pop()
        obj = find_object_by_uid(uid)
        if obj is None:
            continue

        for _link_index, link in iter_object_links(obj):
            link_uids.add(ensure_stagehand_link_uid(link))

    return ProjectDatabase.remove_generated_powerlines_for_link_uids(link_uids)

def _process_dirty_connection_refresh():
    global _ALL_CONNECTIONS_DIRTY, _MEMBERSHIP_REFRESH_NEEDS_AUTOCONNECT

    sync_constrained_link_hierarchy()
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

    _clear_generated_powerlines_for_dirty_objects()

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
    global _AUDIO_ARRAY_UPDATE_ACTIVE

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
        if not _AUDIO_ARRAY_UPDATE_ACTIVE:
            _AUDIO_ARRAY_UPDATE_ACTIVE = True
            try:
                for dirty_obj in dirty_objects:
                    _enforce_constrained_child_transform(dirty_obj)
            finally:
                _AUDIO_ARRAY_UPDATE_ACTIVE = False
        _mark_generated_powerlines_dirty(dirty_objects)
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
            or getattr(wm, "stagehand_second_anchor_mode_enabled", False)
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
        ctrl=True,
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
    _DIRTY_GENERATED_POWERLINE_OBJECT_UIDS.clear()
    _ALL_CONNECTIONS_DIRTY = False
    _MEMBERSHIP_REFRESH_NEEDS_AUTOCONNECT = False
    _MEMBERSHIP_TRACKING_INITIALIZED = False
    safe_unregister_class(STAGEHAND_OT_select_connected_objects)
    safe_unregister_class(STAGEHAND_OT_repair_all_connections)
    safe_unregister_class(STAGEHAND_OT_report_connection_profile)
