import json
from pathlib import Path

import bpy

from .AddStagehandObject import apply_stagehand_catalogue_data
from . import ProjectDatabase
from .RegistrationUtils import safe_register_class, safe_unregister_class


CATALOGUE_BY_ID = {}
REGISTERED_CLASSES = []
SUPPORTED_EXTENSIONS = (".glb", ".gltf")
CACHE_METADATA_PREFIX = "stagehand_cache_"
CACHE_TEMPLATE_KEY = f"{CACHE_METADATA_PREFIX}template"
CACHE_ASSET_ID_KEY = f"{CACHE_METADATA_PREFIX}asset_id"
CACHE_MESH_PATH_KEY = f"{CACHE_METADATA_PREFIX}mesh_path"
CACHE_PART_COUNT_KEY = f"{CACHE_METADATA_PREFIX}part_count"
CACHE_PART_KEY_PREFIX = f"{CACHE_METADATA_PREFIX}part_"
CACHE_SOURCE_NAME_KEY = f"{CACHE_METADATA_PREFIX}source_name"


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
    _refresh_cached_templates_from_catalogue()

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


def _import_asset_source(asset_data):
    mesh_path, attempted_paths = _resolve_mesh_path(asset_data["mesh3d"])
    if mesh_path is None:
        raise FileNotFoundError(
            "Unable to locate mesh for catalogue item "
            f"{asset_data['name']}: {asset_data['mesh3d']}. "
            f"Addon folder: {_addon_directory()}. "
            f"Checked absolute path: {attempted_paths[0] if attempted_paths else 'no path generated'}"
        )

    existing_object_pointers = {obj.as_pointer() for obj in bpy.data.objects}
    bpy.ops.import_scene.gltf(filepath=str(mesh_path))

    imported_objects = [
        obj for obj in bpy.data.objects
        if obj.as_pointer() not in existing_object_pointers
    ]
    if not imported_objects:
        raise RuntimeError(f"No objects were imported from {mesh_path.name}")
    return imported_objects


def _cached_template_parts(template):
    if not isinstance(template, bpy.types.Object):
        return []

    try:
        part_count = max(1, int(template.get(CACHE_PART_COUNT_KEY, 1)))
    except (TypeError, ValueError):
        return []

    parts = [template]
    for part_index in range(1, part_count):
        part = template.get(f"{CACHE_PART_KEY_PREFIX}{part_index}")
        if not isinstance(part, bpy.types.Object):
            return []
        parts.append(part)
    return parts


def _discard_cached_template(asset_id, template):
    ProjectDatabase.remove_cached_asset_template(asset_id)
    if not isinstance(template, bpy.types.Object):
        return

    parts = [template]
    for key, value in template.items():
        if (
            str(key).startswith(CACHE_PART_KEY_PREFIX)
            and isinstance(value, bpy.types.Object)
        ):
            parts.append(value)

    for part in reversed(parts):
        if (
            part.get(CACHE_TEMPLATE_KEY, False)
            and bpy.data.objects.get(part.name) == part
        ):
            bpy.data.objects.remove(part, do_unlink=True)


def _get_cached_template(asset_data):
    asset_id = int(asset_data["uniqueId"])
    template = ProjectDatabase.get_cached_asset_template(asset_id)
    if template is None:
        return None

    parts = _cached_template_parts(template)
    try:
        valid = (
            bool(template.get(CACHE_TEMPLATE_KEY, False))
            and int(template.get(CACHE_ASSET_ID_KEY, -1)) == asset_id
            and str(template.get(CACHE_MESH_PATH_KEY, ""))
            == str(asset_data["mesh3d"])
            and bool(parts)
        )
    except (TypeError, ValueError, ReferenceError):
        valid = False
    if valid:
        return template

    _discard_cached_template(asset_id, template)
    return None


def _refresh_cached_templates_from_catalogue():
    for asset_id, template in list(
        ProjectDatabase.get_asset_cache(create=False).items()
    ):
        asset_data = CATALOGUE_BY_ID.get(asset_id)
        if asset_data is None:
            _discard_cached_template(asset_id, template)
            continue

        cached_template = _get_cached_template(asset_data)
        if cached_template is None:
            continue
        for part in _cached_template_parts(cached_template):
            apply_stagehand_catalogue_data(part, asset_data)
            part.stagehand.is_stagehand_object = False

def _prepare_cached_template(imported_objects, asset_data):
    asset_id = int(asset_data["uniqueId"])
    root = imported_objects[0]
    part_count = len(imported_objects)

    for part_index, obj in enumerate(imported_objects):
        source_name = asset_data["name"] if part_count == 1 else obj.name
        _tag_imported_object(obj, asset_data)
        obj.stagehand.is_stagehand_object = False
        obj[CACHE_TEMPLATE_KEY] = True
        obj[CACHE_ASSET_ID_KEY] = asset_id
        obj[CACHE_MESH_PATH_KEY] = str(asset_data["mesh3d"])
        obj[CACHE_SOURCE_NAME_KEY] = source_name
        obj.hide_select = True
        obj.hide_viewport = True
        obj.hide_render = True
        obj.name = f"[Stagehand Cache {asset_id}] {source_name}"
        if part_index > 0:
            root[f"{CACHE_PART_KEY_PREFIX}{part_index}"] = obj

    root[CACHE_PART_COUNT_KEY] = part_count
    ProjectDatabase.set_cached_asset_template(asset_id, root)

    for obj in imported_objects:
        obj.select_set(False)
        for collection in list(obj.users_collection):
            collection.objects.unlink(obj)

    return root


def _restore_selection(selected_objects, active_object):
    for selected in list(bpy.context.selected_objects):
        selected.select_set(False)
    for obj in selected_objects:
        if bpy.data.objects.get(obj.name) == obj and obj.users_collection:
            obj.select_set(True)

    view_layer = getattr(bpy.context, "view_layer", None)
    if view_layer is not None:
        if (
            active_object is not None
            and bpy.data.objects.get(active_object.name) == active_object
            and active_object.users_collection
        ):
            view_layer.objects.active = active_object
        else:
            view_layer.objects.active = None


def _instance_collection():
    collection = getattr(bpy.context, "collection", None)
    if collection is not None:
        return collection

    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        raise RuntimeError("Unable to find a scene collection for the Stagehand asset")
    return scene.collection


def _clear_cache_metadata(obj):
    for key in list(obj.keys()):
        if str(key).startswith(CACHE_METADATA_PREFIX):
            del obj[key]


def _instantiate_cached_template(template, asset_data, select=False):
    template_parts = _cached_template_parts(template)
    if not template_parts:
        raise RuntimeError(
            f"The Stagehand cache for asset {asset_data['uniqueId']} is incomplete"
        )

    target_collection = _instance_collection()
    instance_by_template = {}
    instances = []

    for template_part in template_parts:
        source_name = str(
            template_part.get(CACHE_SOURCE_NAME_KEY, asset_data["name"])
        )
        instance = template_part.copy()
        if template_part.data is not None:
            instance.data = template_part.data
        _clear_cache_metadata(instance)
        instance.hide_select = False
        instance.hide_viewport = False
        instance.hide_render = False
        instance.stagehand.uid = ""
        apply_stagehand_catalogue_data(instance, asset_data)
        instance.name = source_name
        target_collection.objects.link(instance)
        instance_by_template[template_part] = instance
        instances.append(instance)

    for template_part, instance in instance_by_template.items():
        instance.parent = instance_by_template.get(template_part.parent)

    if select:
        for selected in list(bpy.context.selected_objects):
            selected.select_set(False)
        for instance in instances:
            instance.select_set(True)
        view_layer = getattr(bpy.context, "view_layer", None)
        if view_layer is not None:
            view_layer.objects.active = instances[0]

    return instances


def _import_asset(asset_data, select=False):
    template = _get_cached_template(asset_data)
    if template is None:
        selected_objects = list(bpy.context.selected_objects)
        view_layer = getattr(bpy.context, "view_layer", None)
        active_object = view_layer.objects.active if view_layer is not None else None
        imported_objects = _import_asset_source(asset_data)
        try:
            template = _prepare_cached_template(imported_objects, asset_data)
        except Exception:
            for obj in imported_objects:
                if bpy.data.objects.get(obj.name) == obj:
                    bpy.data.objects.remove(obj, do_unlink=True)
            raise
        finally:
            _restore_selection(selected_objects, active_object)

    return _instantiate_cached_template(template, asset_data, select=select)


def import_catalogue_asset(asset_id):
    """Import one catalogue asset by ID for other Stagehand modules."""
    try:
        asset_id = int(asset_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid catalogue asset ID: {asset_id}") from exc

    asset_data = CATALOGUE_BY_ID.get(asset_id)
    if asset_data is None:
        raise KeyError(f"Catalogue asset ID {asset_id} was not found")

    return _import_asset(asset_data, select=False)


def _build_operator(asset_data):
    asset_id = asset_data["uniqueId"]
    operator_suffix = _normalize_asset_name(asset_data["name"])
    class_name = f"STAGEHAND_OT_import_catalogue_{asset_id}_{operator_suffix}"

    def execute(self, context):
        del context

        try:
            _import_asset(asset_data, select=True)
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
            from . import Recipes

            Recipes.reload_recipe_operators()
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
    bl_label = "Add Item"
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
        safe_unregister_class(cls)

    REGISTERED_CLASSES.clear()


def reload_catalogue_operators():
    global CATALOGUE_BY_ID

    _unregister_dynamic_classes()
    CATALOGUE_BY_ID = _load_catalogue()

    for asset_id in sorted(CATALOGUE_BY_ID):
        operator_class = _build_operator(CATALOGUE_BY_ID[asset_id])
        safe_register_class(operator_class)
        REGISTERED_CLASSES.append(operator_class)


def register():
    for cls in BASE_CLASSES:
        safe_register_class(cls)

    reload_catalogue_operators()


def unregister():
    _unregister_dynamic_classes()

    for cls in reversed(BASE_CLASSES):
        safe_unregister_class(cls)
