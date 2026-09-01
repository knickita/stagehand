import copy
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


def catalogue_display_name(asset_data):
    name = str(asset_data.get("name", "")).strip() or "Stagehand asset"
    asset_id = int(asset_data.get("uniqueId", 0))
    if asset_id < 0:
        parent = CATALOGUE_BY_ID.get(int(asset_data.get("parentId", 0)))
        parent_name = str((parent or {}).get("name", "")).strip()
        return f"{parent_name or name} {asset_id}"
    return name


def catalogue_sort_key(asset_id):
    asset_data = CATALOGUE_BY_ID[asset_id]
    parent_id = int(asset_data.get("parentId", asset_id))
    return parent_id, asset_id < 0, abs(asset_id)


def is_user_selectable_asset(asset_data):
    """Variants are internal alternatives, never direct user choices."""
    return int(asset_data.get("uniqueId", 0)) > 0


def _operator_asset_token(asset_id):
    return f"variant_{abs(asset_id)}" if asset_id < 0 else str(asset_id)


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
        if asset_id == -1 and not str(obj.stagehand.catalogueName).strip():
            # Files created before asset ID 0 became the unassigned sentinel.
            obj.stagehand.asset_id = 0
            continue
        if asset_id == 0:
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
    operator_token = _operator_asset_token(asset_id)
    display_name = catalogue_display_name(asset_data)
    operator_suffix = _normalize_asset_name(display_name)
    class_name = f"STAGEHAND_OT_import_catalogue_{operator_token}_{operator_suffix}"

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
            "bl_idname": f"stagehand.import_catalogue_{operator_token}",
            "bl_label": display_name,
            "bl_description": f"Import Stagehand asset '{display_name}'",
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

        for asset_id in sorted(CATALOGUE_BY_ID, key=catalogue_sort_key):
            asset_data = CATALOGUE_BY_ID[asset_id]
            if not is_user_selectable_asset(asset_data):
                continue
            operator_token = _operator_asset_token(asset_id)
            layout.operator(
                f"stagehand.import_catalogue_{operator_token}",
                text=catalogue_display_name(asset_data),
            )


BASE_CLASSES = (
    STAGEHAND_OT_reload_catalogue,
    STAGEHAND_MT_catalogue_menu,
)


def _require_catalogue_id(entry, entry_label):
    if "uniqueId" not in entry:
        raise ValueError(f"Catalogue {entry_label} is missing uniqueId: {entry}")

    asset_id = entry["uniqueId"]
    if isinstance(asset_id, bool) or not isinstance(asset_id, int):
        raise TypeError(
            f"Catalogue {entry_label} uniqueId must be an integer: {entry}"
        )
    return asset_id


def _resolve_variant(parent, variant):
    variant_id = _require_catalogue_id(variant, "variant")
    parent_id = variant.get("parentId")
    if isinstance(parent_id, bool) or not isinstance(parent_id, int):
        raise TypeError(
            f"Catalogue variant parentId must be an integer: {variant}"
        )

    resolved = copy.deepcopy(parent)
    resolved["uniqueId"] = variant_id
    resolved["parentId"] = parent_id

    for key, value in variant.items():
        if key in {"uniqueId", "parentId", "links"}:
            continue
        resolved[key] = copy.deepcopy(value)

    source_links = resolved.get("links", [])
    if not isinstance(source_links, list):
        raise TypeError(
            f"Catalogue item {parent_id} links must be an array"
        )

    link_overrides = variant.get("links", [])
    if not isinstance(link_overrides, list):
        raise TypeError(
            f"Catalogue variant {variant_id} links must be an array"
        )

    overridden_parent_ids = set()
    for link_override in link_overrides:
        if not isinstance(link_override, dict):
            raise TypeError(
                f"Catalogue variant {variant_id} link override must be an object"
            )

        parent_link_id = link_override.get("parentId")
        if isinstance(parent_link_id, bool) or not isinstance(parent_link_id, int):
            raise TypeError(
                f"Catalogue variant {variant_id} link override requires an integer parentId"
            )
        if parent_link_id < 0 or parent_link_id >= len(source_links):
            raise ValueError(
                f"Catalogue variant {variant_id} link parentId {parent_link_id} is outside "
                f"parent item {parent_id} links"
            )
        if parent_link_id in overridden_parent_ids:
            raise ValueError(
                f"Catalogue variant {variant_id} overrides link parentId {parent_link_id} more than once"
            )
        overridden_parent_ids.add(parent_link_id)

        resolved_link = copy.deepcopy(source_links[parent_link_id])
        for key, value in link_override.items():
            if key == "parentId":
                continue
            resolved_link[key] = copy.deepcopy(value)
        source_links[parent_link_id] = resolved_link

    resolved["links"] = source_links
    return resolved


def _catalogue_from_raw(raw_catalogue):
    if not isinstance(raw_catalogue, dict):
        raise TypeError("Catalogue root must be an object")

    catalogue_by_id = {}
    items = raw_catalogue.get("items", [])
    variants = raw_catalogue.get("variants", [])
    if not isinstance(items, list):
        raise TypeError("Catalogue items must be an array")
    if not isinstance(variants, list):
        raise TypeError("Catalogue variants must be an array")

    for item in items:
        if not isinstance(item, dict):
            raise TypeError(f"Catalogue item must be an object: {item}")
        asset_id = _require_catalogue_id(item, "item")
        if asset_id <= 0:
            raise ValueError(
                f"Catalogue item uniqueId must be greater than zero: {item}"
            )

        if asset_id in catalogue_by_id:
            raise ValueError(f"Duplicate catalogue id found: {asset_id}")

        catalogue_by_id[asset_id] = copy.deepcopy(item)

    for variant in variants:
        if not isinstance(variant, dict):
            raise TypeError(f"Catalogue variant must be an object: {variant}")

        variant_id = _require_catalogue_id(variant, "variant")
        if variant_id >= 0:
            raise ValueError(
                f"Catalogue variant uniqueId must be less than zero: {variant}"
            )
        if variant_id in catalogue_by_id:
            raise ValueError(f"Duplicate catalogue id found: {variant_id}")

        parent_id = variant.get("parentId")
        parent = catalogue_by_id.get(parent_id)
        if parent is None or int(parent.get("uniqueId", 0)) <= 0:
            raise ValueError(
                f"Catalogue variant {variant_id} parent item {parent_id} was not found"
            )

        catalogue_by_id[variant_id] = _resolve_variant(parent, variant)

    return catalogue_by_id


def _load_catalogue():
    catalogue_path = _catalogue_path()
    if not catalogue_path.exists():
        return {}

    with catalogue_path.open("r", encoding="utf-8") as handle:
        raw_catalogue = json.load(handle)

    return _catalogue_from_raw(raw_catalogue)


def canonical_asset_id(asset_id):
    """Return the physical parent item ID used for BOM and external exports."""
    try:
        asset_id = int(asset_id)
    except (TypeError, ValueError):
        return 0

    asset_data = CATALOGUE_BY_ID.get(asset_id)
    if asset_data is None:
        return asset_id
    return int(asset_data.get("parentId", asset_id))


def canonical_asset_data(asset_id):
    canonical_id = canonical_asset_id(asset_id)
    return CATALOGUE_BY_ID.get(canonical_id)


def _unregister_dynamic_classes():
    for cls in reversed(REGISTERED_CLASSES):
        safe_unregister_class(cls)

    REGISTERED_CLASSES.clear()


def reload_catalogue_operators():
    global CATALOGUE_BY_ID

    _unregister_dynamic_classes()
    CATALOGUE_BY_ID = _load_catalogue()

    for asset_id in sorted(CATALOGUE_BY_ID, key=catalogue_sort_key):
        asset_data = CATALOGUE_BY_ID[asset_id]
        if not is_user_selectable_asset(asset_data):
            continue
        operator_class = _build_operator(asset_data)
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
