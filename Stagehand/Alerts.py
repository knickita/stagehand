import math
import queue
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime

import bpy
from mathutils import Quaternion, Vector

from . import Connections
from . import ProjectDatabase
from .LinkTypes import are_link_types_compatible, is_power_input
from .RegistrationUtils import (
    safe_define_property,
    safe_register_class,
    safe_remove_property,
    safe_unregister_class,
)


DOMAIN_LINKS = "links"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"
RULE_NEARBY_UNCONNECTED_LINKS = "nearby_unconnected_compatible_links"
RULE_UNCONNECTED_POWER_INPUT = "unconnected_power_input"
PUBLISH_POLL_INTERVAL = 0.1
AUTO_SCAN_INTERVAL = 3.0


@dataclass(frozen=True)
class AlertRecord:
    id: str
    domain: str
    rule_id: str
    severity: str
    label: str
    sort_key: tuple
    object_uids: tuple
    link_uids: tuple
    link_numbers: tuple
    center: tuple


@dataclass(frozen=True)
class _LinkCandidate:
    obj: object
    object_uid: str
    object_name: str
    link_index: int
    link_uid: str
    link: object
    center: Vector
    bucket_key: tuple


_ALERTS_BY_ID = {}
_RESULT_QUEUE = queue.Queue()
_SCAN_EVENT = threading.Event()
_STOP_EVENT = threading.Event()
_CANCEL_SCAN_EVENT = threading.Event()
_STATE_LOCK = threading.Lock()
_WORKER_THREAD = None
_REGISTERED = False
_AUTO_SCAN_ENABLED = False
_SCAN_RUNNING = False
_LAST_STATUS_SIGNATURE = None
_LAST_SCAN_LABEL = "Never"
_LAST_SCAN_MESSAGE = "No scan run yet"
_LAST_SCAN_ERROR = ""


def _severity_rank(severity):
    if severity == SEVERITY_ERROR:
        return 0
    return 1


def _severity_icon(severity):
    if severity == SEVERITY_ERROR:
        return 'ERROR'
    return 'INFO'


def _current_time_label():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get_status_locked():
    return {
        "running": _SCAN_RUNNING,
        "auto_scan": _AUTO_SCAN_ENABLED,
        "worker_alive": _WORKER_THREAD is not None and _WORKER_THREAD.is_alive(),
    }


def _status_signature():
    with _STATE_LOCK:
        status = _get_status_locked()
        last_scan_label = _LAST_SCAN_LABEL
        last_scan_message = _LAST_SCAN_MESSAGE
        last_scan_error = _LAST_SCAN_ERROR
    return (
        status["running"],
        status["auto_scan"],
        status["worker_alive"],
        len(_ALERTS_BY_ID),
        last_scan_label,
        last_scan_message,
        last_scan_error,
    )


def _tag_alert_ui_redraw():
    try:
        wm = getattr(bpy.context, "window_manager", None)
        windows = getattr(wm, "windows", ()) if wm is not None else ()
        tagged_any = False
        for window in windows:
            screen = getattr(window, "screen", None)
            if screen is None:
                continue
            for area in screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
                    tagged_any = True

        if tagged_any:
            return

        screen = getattr(bpy.context, "screen", None)
        if screen is None:
            return
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    except Exception:
        pass


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


def _link_center_bucket_key(center):
    cell_size = Connections.AUTO_CONNECT_DISTANCE_THRESHOLD
    return (
        math.floor(center.x / cell_size),
        math.floor(center.y / cell_size),
        math.floor(center.z / cell_size),
    )


def _nearby_bucket_keys(bucket_key):
    for x_offset in (-1, 0, 1):
        for y_offset in (-1, 0, 1):
            for z_offset in (-1, 0, 1):
                yield (
                    bucket_key[0] + x_offset,
                    bucket_key[1] + y_offset,
                    bucket_key[2] + z_offset,
                )


def _iter_stagehand_link_candidates(connections):
    for obj in Connections.iter_stagehand_objects():
        if _CANCEL_SCAN_EVENT.is_set() or _STOP_EVENT.is_set():
            return

        object_uid = Connections.get_object_uid(obj)
        if not object_uid:
            continue

        for link_index, link in Connections.iter_object_links(obj):
            if _CANCEL_SCAN_EVENT.is_set() or _STOP_EVENT.is_set():
                return

            link_uid = str(getattr(link, "uid", ""))
            if not link_uid:
                continue
            if connections.get(link_uid):
                continue

            center, _rotation = _link_transform(obj, link)
            yield _LinkCandidate(
                obj=obj,
                object_uid=object_uid,
                object_name=obj.name_full,
                link_index=link_index,
                link_uid=link_uid,
                link=link,
                center=center,
                bucket_key=_link_center_bucket_key(center),
            )


def _ordered_link_pair(item_a, item_b):
    key_a = (
        item_a.object_name.casefold(),
        item_a.object_name,
        item_a.link_index,
        item_a.object_uid,
        item_a.link_uid,
    )
    key_b = (
        item_b.object_name.casefold(),
        item_b.object_name,
        item_b.link_index,
        item_b.object_uid,
        item_b.link_uid,
    )
    if key_a <= key_b:
        return item_a, item_b
    return item_b, item_a


def _link_pair_alert_id(link_uid_a, link_uid_b):
    lower_uid, higher_uid = sorted((link_uid_a, link_uid_b))
    return f"{RULE_NEARBY_UNCONNECTED_LINKS}:{lower_uid}:{higher_uid}"


def _make_link_pair_alert(item_a, item_b):
    first, second = _ordered_link_pair(item_a, item_b)
    center = (item_a.center + item_b.center) * 0.5
    label = (
        f"{first.object_name} link {first.link_index + 1} <-> "
        f"{second.object_name} link {second.link_index + 1}"
    )
    sort_key = (
        _severity_rank(SEVERITY_WARNING),
        first.object_name.casefold(),
        second.object_name.casefold(),
        first.link_index + 1,
        second.link_index + 1,
        first.object_uid,
        second.object_uid,
    )
    return AlertRecord(
        id=_link_pair_alert_id(first.link_uid, second.link_uid),
        domain=DOMAIN_LINKS,
        rule_id=RULE_NEARBY_UNCONNECTED_LINKS,
        severity=SEVERITY_WARNING,
        label=label,
        sort_key=sort_key,
        object_uids=(first.object_uid, second.object_uid),
        link_uids=(first.link_uid, second.link_uid),
        link_numbers=(first.link_index + 1, second.link_index + 1),
        center=(float(center.x), float(center.y), float(center.z)),
    )


def _make_unconnected_power_input_alert(item):
    label = f"Power input not connected: {item.object_name} link {item.link_index + 1}"
    sort_key = (
        _severity_rank(SEVERITY_ERROR),
        item.object_name.casefold(),
        item.link_index + 1,
        item.object_uid,
        item.link_uid,
    )
    return AlertRecord(
        id=f"{RULE_UNCONNECTED_POWER_INPUT}:{item.link_uid}",
        domain=DOMAIN_LINKS,
        rule_id=RULE_UNCONNECTED_POWER_INPUT,
        severity=SEVERITY_ERROR,
        label=label,
        sort_key=sort_key,
        object_uids=(item.object_uid,),
        link_uids=(item.link_uid,),
        link_numbers=(item.link_index + 1,),
        center=(float(item.center.x), float(item.center.y), float(item.center.z)),
    )


def _is_power_input_link(link):
    try:
        return is_power_input(link.type)
    except (TypeError, ValueError):
        return False


def _link_types_are_compatible(link_a, link_b):
    try:
        return are_link_types_compatible(link_a.type, link_b.type)
    except (TypeError, ValueError):
        return False


def _analyze_links():
    connections = ProjectDatabase.get_connections(create=False)
    candidates = list(_iter_stagehand_link_candidates(connections))
    buckets = {}
    alerts = []

    for item in candidates:
        if _CANCEL_SCAN_EVENT.is_set() or _STOP_EVENT.is_set():
            return None

        if _is_power_input_link(item.link):
            alerts.append(_make_unconnected_power_input_alert(item))

        for bucket_key in _nearby_bucket_keys(item.bucket_key):
            if _CANCEL_SCAN_EVENT.is_set() or _STOP_EVENT.is_set():
                return None

            for other_item in buckets.get(bucket_key, ()):
                if _CANCEL_SCAN_EVENT.is_set() or _STOP_EVENT.is_set():
                    return None

                if item.object_uid == other_item.object_uid:
                    continue
                if connections.get(item.link_uid) or connections.get(other_item.link_uid):
                    continue
                if not _link_types_are_compatible(item.link, other_item.link):
                    continue
                if not Connections.links_are_aligned(
                    item.obj,
                    item.link_index,
                    other_item.obj,
                    other_item.link_index,
                ):
                    continue
                alerts.append(_make_link_pair_alert(item, other_item))

        buckets.setdefault(item.bucket_key, []).append(item)

    return alerts


def _run_scan():
    global _SCAN_RUNNING

    with _STATE_LOCK:
        if _SCAN_RUNNING:
            return
        _SCAN_RUNNING = True

    try:
        alerts = _analyze_links()
        if alerts is None:
            result = {
                "ok": False,
                "cancelled": True,
                "alerts": [],
                "error": "",
                "finished_at": _current_time_label(),
            }
        else:
            result = {
                "ok": True,
                "cancelled": False,
                "alerts": alerts,
                "error": "",
                "finished_at": _current_time_label(),
            }
    except Exception:
        result = {
            "ok": False,
            "cancelled": False,
            "alerts": [],
            "error": traceback.format_exc(),
            "finished_at": _current_time_label(),
        }
    finally:
        with _STATE_LOCK:
            _SCAN_RUNNING = False

    _RESULT_QUEUE.put(result)


def _worker_loop():
    next_auto_scan_at = time.monotonic() + AUTO_SCAN_INTERVAL

    while not _STOP_EVENT.is_set():
        wait_time = 0.1
        with _STATE_LOCK:
            auto_scan_enabled = _AUTO_SCAN_ENABLED

        if auto_scan_enabled:
            wait_time = max(0.0, min(0.5, next_auto_scan_at - time.monotonic()))

        _SCAN_EVENT.wait(wait_time)
        if _STOP_EVENT.is_set():
            break

        manual_requested = _SCAN_EVENT.is_set()
        if manual_requested:
            _SCAN_EVENT.clear()

        now = time.monotonic()
        with _STATE_LOCK:
            auto_scan_enabled = _AUTO_SCAN_ENABLED
        auto_due = auto_scan_enabled and now >= next_auto_scan_at

        if manual_requested or auto_due:
            _CANCEL_SCAN_EVENT.clear()
            _run_scan()
            next_auto_scan_at = time.monotonic() + AUTO_SCAN_INTERVAL

        with _STATE_LOCK:
            auto_scan_enabled = _AUTO_SCAN_ENABLED
        if not auto_scan_enabled:
            break


def _replace_alerts(alerts):
    _ALERTS_BY_ID.clear()
    for alert in alerts:
        _ALERTS_BY_ID[alert.id] = alert


def _drain_result_queue():
    global _LAST_SCAN_LABEL, _LAST_SCAN_MESSAGE, _LAST_SCAN_ERROR

    published_any = False

    while True:
        try:
            result = _RESULT_QUEUE.get_nowait()
        except queue.Empty:
            break

        _LAST_SCAN_LABEL = result.get("finished_at", _current_time_label())
        if result.get("cancelled", False):
            _LAST_SCAN_MESSAGE = "Scan cancelled"
            _LAST_SCAN_ERROR = ""
        elif result.get("ok", False):
            alerts = result.get("alerts", ())
            _replace_alerts(alerts)
            _LAST_SCAN_MESSAGE = f"Last scan: {len(alerts)} alerts"
            _LAST_SCAN_ERROR = ""
        else:
            _LAST_SCAN_MESSAGE = "Last scan failed"
            error_lines = result.get("error", "Unknown error").splitlines()
            _LAST_SCAN_ERROR = error_lines[-1] if error_lines else "Unknown error"
            print("Stagehand alert scan failed:")
            print(result.get("error", "Unknown error"))
        published_any = True

    return published_any


def _publish_results_timer():
    global _LAST_STATUS_SIGNATURE

    try:
        published_any = _drain_result_queue()
        current_signature = _status_signature()
        if published_any or current_signature != _LAST_STATUS_SIGNATURE:
            _LAST_STATUS_SIGNATURE = current_signature
            _tag_alert_ui_redraw()
    except Exception as exc:
        print(f"Stagehand alert publish timer failed: {exc}")

    return PUBLISH_POLL_INTERVAL


def scan_now():
    global _LAST_SCAN_MESSAGE, _LAST_SCAN_ERROR

    if not _REGISTERED:
        return False

    _LAST_SCAN_MESSAGE = "Scan requested"
    _LAST_SCAN_ERROR = ""
    _start_worker()
    _SCAN_EVENT.set()
    _tag_alert_ui_redraw()
    return True


def _current_alerts():
    return sorted(_ALERTS_BY_ID.values(), key=lambda alert: alert.sort_key)


def _alert_counts(alerts):
    errors = sum(1 for alert in alerts if alert.severity == SEVERITY_ERROR)
    warnings = sum(1 for alert in alerts if alert.severity == SEVERITY_WARNING)
    return warnings, errors


def _current_status():
    with _STATE_LOCK:
        return _get_status_locked()


def _find_alert(alert_id):
    return _ALERTS_BY_ID.get(alert_id)


def _resolve_alert_objects(alert):
    objects = []
    for object_uid in alert.object_uids:
        obj = Connections.find_object_by_uid(object_uid)
        if obj is not None:
            objects.append(obj)
    return objects


def _view_distance_for_objects(objects, center):
    max_distance = 0.5
    center_vector = Vector(center)
    for obj in objects:
        bound_box = getattr(obj, "bound_box", ())
        matrix_world = getattr(obj, "matrix_world", None)
        if matrix_world is None:
            continue
        for corner in bound_box:
            distance = (matrix_world @ Vector(corner) - center_vector).length
            max_distance = max(max_distance, distance)
    return max(0.5, max_distance * 3.0)


def _find_view3d_region(context):
    areas = []
    if getattr(context, "area", None) is not None and context.area.type == 'VIEW_3D':
        areas.append(context.area)

    screen = getattr(context, "screen", None)
    if screen is not None:
        areas.extend(area for area in screen.areas if area.type == 'VIEW_3D' and area not in areas)

    for area in areas:
        for space in area.spaces:
            if space.type == 'VIEW_3D' and getattr(space, "region_3d", None) is not None:
                return space.region_3d
    return None


def _focus_view_on_alert(context, alert, objects):
    region_3d = _find_view3d_region(context)
    if region_3d is None:
        return False

    region_3d.view_location = Vector(alert.center)
    region_3d.view_distance = _view_distance_for_objects(objects, alert.center)
    return True


def _auto_scan_update(self, _context):
    global _AUTO_SCAN_ENABLED

    with _STATE_LOCK:
        _AUTO_SCAN_ENABLED = bool(getattr(self, "stagehand_alert_auto_scan", False))
    if _AUTO_SCAN_ENABLED:
        _start_worker()
        _SCAN_EVENT.set()
    else:
        _stop_worker()
    _tag_alert_ui_redraw()


class STAGEHAND_OT_toggle_alerts(bpy.types.Operator):
    bl_idname = "stagehand.toggle_alerts"
    bl_label = "Toggle Stagehand Alerts"
    bl_description = "Show or hide the Stagehand alert list"

    def execute(self, context):
        wm = context.window_manager
        wm.stagehand_alerts_expanded = not getattr(wm, "stagehand_alerts_expanded", False)
        _tag_alert_ui_redraw()
        return {'FINISHED'}


class STAGEHAND_OT_focus_alert(bpy.types.Operator):
    bl_idname = "stagehand.focus_alert"
    bl_label = "Focus Stagehand Alert"
    bl_description = "Select the objects involved in this alert and focus the active 3D view"

    alert_id: bpy.props.StringProperty(default="")

    def execute(self, context):
        alert = _find_alert(self.alert_id)
        if alert is None:
            self.report({'WARNING'}, "Stagehand alert is no longer available")
            return {'CANCELLED'}

        objects = _resolve_alert_objects(alert)
        if not objects:
            self.report({'WARNING'}, "Stagehand alert objects are no longer available")
            return {'CANCELLED'}

        if context.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except RuntimeError:
                pass

        try:
            bpy.ops.object.select_all(action='DESELECT')
        except RuntimeError:
            pass

        for obj in objects:
            try:
                obj.select_set(True)
            except RuntimeError:
                continue

        context.view_layer.objects.active = objects[0]
        focused = _focus_view_on_alert(context, alert, objects)
        if not focused:
            self.report({'WARNING'}, "No active 3D View was available to focus")
        return {'FINISHED'}


class STAGEHAND_OT_scan_alerts_now(bpy.types.Operator):
    bl_idname = "stagehand.scan_alerts_now"
    bl_label = "Scan Now"
    bl_description = "Scan Stagehand links for alert conditions now"

    def execute(self, _context):
        if not scan_now():
            self.report({'WARNING'}, "Stagehand alert system is not running")
            return {'CANCELLED'}
        self.report({'INFO'}, "Stagehand alert scan requested")
        return {'FINISHED'}


def draw_alerts(layout, context):
    alerts = _current_alerts()
    warnings, errors = _alert_counts(alerts)
    status = _current_status()
    wm = context.window_manager
    expanded = getattr(wm, "stagehand_alerts_expanded", False)

    box = layout.box()
    row = box.row(align=True)
    toggle_icon = 'TRIA_DOWN' if expanded else 'TRIA_RIGHT'
    row.operator(STAGEHAND_OT_toggle_alerts.bl_idname, text="", icon=toggle_icon, emboss=False)
    row.label(text=f"Errors: {errors}")
    row.label(text=f"Warnings: {warnings}")

    controls = box.row(align=True)
    controls.prop(wm, "stagehand_alert_auto_scan", text="Auto Scan")
    controls.operator(STAGEHAND_OT_scan_alerts_now.bl_idname, text="Scan Now", icon='VIEWZOOM')

    status_row = box.row(align=True)
    status_row.label(text=f"Running: {1 if status['running'] else 0}")
    box.label(text=f"Last scan: {_LAST_SCAN_LABEL}")
    if _LAST_SCAN_ERROR:
        box.label(text=_LAST_SCAN_ERROR, icon='ERROR')

    if not expanded:
        return

    if not alerts:
        box.label(text="No alerts")
        return

    column = box.column(align=True)
    for alert in alerts:
        operator = column.operator(
            STAGEHAND_OT_focus_alert.bl_idname,
            text=alert.label,
            icon=_severity_icon(alert.severity),
        )
        operator.alert_id = alert.id


def _start_worker():
    global _WORKER_THREAD

    if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
        return

    _STOP_EVENT.clear()
    _CANCEL_SCAN_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop,
        name="StagehandAlertWorker",
        daemon=True,
    )
    _WORKER_THREAD.start()


def _stop_worker():
    global _WORKER_THREAD

    _STOP_EVENT.set()
    _CANCEL_SCAN_EVENT.set()
    _SCAN_EVENT.set()
    worker = _WORKER_THREAD
    if worker is not None and worker.is_alive():
        worker.join(timeout=1.0)
        if worker.is_alive():
            print("Stagehand alert worker did not stop within timeout")
            return
    _WORKER_THREAD = None


def _clear_queue(result_queue):
    while True:
        try:
            result_queue.get_nowait()
        except queue.Empty:
            return


def _clear_runtime_state():
    global _SCAN_RUNNING, _LAST_STATUS_SIGNATURE
    global _LAST_SCAN_LABEL, _LAST_SCAN_MESSAGE, _LAST_SCAN_ERROR
    global _AUTO_SCAN_ENABLED

    with _STATE_LOCK:
        _ALERTS_BY_ID.clear()
        _SCAN_RUNNING = False
        _AUTO_SCAN_ENABLED = False
        _SCAN_EVENT.clear()
    _clear_queue(_RESULT_QUEUE)
    _LAST_STATUS_SIGNATURE = None
    _LAST_SCAN_LABEL = "Never"
    _LAST_SCAN_MESSAGE = "No scan run yet"
    _LAST_SCAN_ERROR = ""


classes = (
    STAGEHAND_OT_toggle_alerts,
    STAGEHAND_OT_focus_alert,
    STAGEHAND_OT_scan_alerts_now,
)


def register():
    global _REGISTERED, _AUTO_SCAN_ENABLED

    for cls in classes:
        safe_register_class(cls)

    safe_define_property(
        bpy.types.WindowManager,
        "stagehand_alerts_expanded",
        bpy.props.BoolProperty(
            name="Stagehand Alerts Expanded",
            default=False,
            options={'HIDDEN'},
        ),
    )
    safe_define_property(
        bpy.types.WindowManager,
        "stagehand_alert_auto_scan",
        bpy.props.BoolProperty(
            name="Auto Scan",
            description="Automatically scan Stagehand links for alerts in the background",
            default=False,
            update=_auto_scan_update,
        ),
    )

    _AUTO_SCAN_ENABLED = bool(getattr(bpy.context.window_manager, "stagehand_alert_auto_scan", False))
    _REGISTERED = True
    if _AUTO_SCAN_ENABLED:
        _start_worker()
    if not bpy.app.timers.is_registered(_publish_results_timer):
        bpy.app.timers.register(_publish_results_timer, first_interval=PUBLISH_POLL_INTERVAL)


def unregister():
    global _REGISTERED

    _REGISTERED = False
    if bpy.app.timers.is_registered(_publish_results_timer):
        bpy.app.timers.unregister(_publish_results_timer)
    _stop_worker()
    _clear_runtime_state()
    safe_remove_property(bpy.types.WindowManager, "stagehand_alert_auto_scan")
    safe_remove_property(bpy.types.WindowManager, "stagehand_alerts_expanded")

    for cls in reversed(classes):
        safe_unregister_class(cls)
