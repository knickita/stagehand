import json
from pathlib import Path

import bpy

from .AddStagehandObject import apply_stagehand_catalogue_data


CATALOGUE_BY_ID = {}
REGISTERED_CLASSES = []
SUPPORTED_EXTENSIONS = (".glb", ".gltf")


def _addon_directory():
    return Path(__file__).resolve().parent


def _catalogue_path():
    return _addon_directory() / "Catalogue.json"


def _normalize_asset_name(name):
    sanitized = "".join(ch if ch.isalnum() else "_" for ch in name.lower())
    sanitized = sanitized.strip("_")
    return sanitized or "asset"


def _candidate_mesh_paths(mesh_path):
    raw_path = Path(mesh_path)

    if raw_path.suffix.lower() in SUPPORTED_EXTENSIONS:
        yield raw_path
        return

    for extension in SUPPORTED_EXTENSIONS:
        yield raw_path.with_suffix(extension)


def _resolve_mesh_path(mesh_path):
    attempted_paths = []
    base_directory = _addon_directory()

    for candidate in _candidate_mesh_paths(mesh_path):
        resolved_path = (base_directory / candidate).resolve()
        attempted_paths.append(str(resolved_path))
        if resolved_path.exists():
            return resolved_path, attempted_paths

    return None, attempted_paths


def _tag_imported_object(obj, asset_data):
    apply_stagehand_catalogue_data(obj, asset_data)


def _iter_stagehand_scene_objects():
    for obj in bpy.data.objects:
        stagehand = getattr(obj, "stagehand", None)
        if stagehand is None or not stagehand.is_stagehand_object:
            continue
        yield obj


def refresh_scene_objects_from_catalogue():
    refreshed_count = 0
    skipped_count = 0

    for obj in _iter_stagehand_scene_objects():
        asset_id = int(obj.stagehand.asset_id)
        if asset_id < 0:
            continue

        asset_data = CATALOGUE_BY_ID.get(asset_id)
        if asset_data is None:
            skipped_count += 1
            continue

        apply_stagehand_catalogue_data(obj, asset_data, preserve_links=True)
        refreshed_count += 1

    if refreshed_count or skipped_count:
        from . import Connections

        Connections.prune_stale_connections()

    return refreshed_count, skipped_count


def _import_asset(asset_data):
    mesh_path, attempted_paths = _resolve_mesh_path(asset_data["mesh3d"])
    if mesh_path is None:
        raise FileNotFoundError(
            "Unable to locate mesh for catalogue item "
            f"{asset_data['name']}: {asset_data['mesh3d']}. "
            f"Addon folder: {_addon_directory()}. "
            f"Checked absolute path: {attempted_paths[0] if attempted_paths else 'no path generated'}"
        )

    existing_objects = {obj.name_full for obj in bpy.data.objects}
    bpy.ops.import_scene.gltf(filepath=str(mesh_path))

    imported_objects = [
        obj for obj in bpy.context.selected_objects
        if obj.name_full not in existing_objects
    ]

    if not imported_objects:
        imported_objects = [obj for obj in bpy.context.selected_objects]

    if not imported_objects:
        raise RuntimeError(f"No objects were imported from {mesh_path.name}")

    for obj in imported_objects:
        _tag_imported_object(obj, asset_data)

    if len(imported_objects) == 1:
        imported_objects[0].name = asset_data["name"]

    return imported_objects


def _build_operator(asset_data):
    asset_id = asset_data["uniqueId"]
    operator_suffix = _normalize_asset_name(asset_data["name"])
    class_name = f"STAGEHAND_OT_import_catalogue_{asset_id}_{operator_suffix}"

    def execute(self, context):
        del context

        try:
            _import_asset(asset_data)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        return {'FINISHED'}

    operator_class = type(
        class_name,
        (bpy.types.Operator,),
        {
            "bl_idname": f"stagehand.import_catalogue_{asset_id}",
            "bl_label": asset_data["name"],
            "bl_description": f"Import Stagehand asset '{asset_data['name']}'",
            "bl_options": {'REGISTER', 'UNDO'},
            "execute": execute,
        },
    )

    return operator_class


class STAGEHAND_OT_reload_catalogue(bpy.types.Operator):
    bl_idname = "stagehand.reload_catalogue"
    bl_label = "Reload Stagehand Catalogue"
    bl_description = "Reload Stagehand catalogue assets"

    def execute(self, context):
        try:
            reload_catalogue_operators()
            refreshed_count, skipped_count = refresh_scene_objects_from_catalogue()
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        if context.screen is not None:
            for area in context.screen.areas:
                if area.type in {'VIEW_3D', 'PROPERTIES'}:
                    area.tag_redraw()

        message = (
            f"Loaded {len(CATALOGUE_BY_ID)} Stagehand assets and refreshed "
            f"{refreshed_count} scene objects"
        )
        if skipped_count:
            message += f" ({skipped_count} without catalogue match)"
        self.report({'INFO'}, message)
        return {'FINISHED'}


class STAGEHAND_MT_catalogue_menu(bpy.types.Menu):
    bl_label = "Import From Catalogue"
    bl_idname = "STAGEHAND_MT_catalogue_menu"

    def draw(self, context):
        del context
        layout = self.layout

        if not CATALOGUE_BY_ID:
            layout.label(text="No catalogue items found")
            return

        for asset_id in sorted(CATALOGUE_BY_ID):
            asset_data = CATALOGUE_BY_ID[asset_id]
            layout.operator(f"stagehand.import_catalogue_{asset_id}", text=asset_data["name"])


BASE_CLASSES = (
    STAGEHAND_OT_reload_catalogue,
    STAGEHAND_MT_catalogue_menu,
)


def _load_catalogue():
    catalogue_path = _catalogue_path()
    if not catalogue_path.exists():
        return {}

    with catalogue_path.open("r", encoding="utf-8") as handle:
        raw_catalogue = json.load(handle)

    catalogue_by_id = {}

    for item in raw_catalogue.get("items", []):
        if "uniqueId" not in item:
            raise ValueError(f"Catalogue item is missing uniqueId: {item}")

        asset_id = item["uniqueId"]
        if not isinstance(asset_id, int):
            raise TypeError(f"Catalogue item uniqueId must be an integer: {item}")

        if asset_id in catalogue_by_id:
            raise ValueError(f"Duplicate catalogue id found: {asset_id}")

        catalogue_by_id[asset_id] = item

    return catalogue_by_id


def _unregister_dynamic_classes():
    for cls in reversed(REGISTERED_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass

    REGISTERED_CLASSES.clear()


def reload_catalogue_operators():
    global CATALOGUE_BY_ID

    _unregister_dynamic_classes()
    CATALOGUE_BY_ID = _load_catalogue()

    for asset_id in sorted(CATALOGUE_BY_ID):
        operator_class = _build_operator(CATALOGUE_BY_ID[asset_id])
        bpy.utils.register_class(operator_class)
        REGISTERED_CLASSES.append(operator_class)


def register():
    for cls in BASE_CLASSES:
        bpy.utils.register_class(cls)

    reload_catalogue_operators()


def unregister():
    _unregister_dynamic_classes()

    for cls in reversed(BASE_CLASSES):
        bpy.utils.unregister_class(cls)
