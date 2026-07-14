"""Data-driven recipes for complex Stagehand systems.

Recipes.json describes the UI, catalogue assets, and builder settings. This
module owns the executable builder functions. New reusable algorithms can be
added with register_builder(name, function) and selected by name in JSON.
Keeping executable code out of JSON makes recipe files safe and easy to edit.
"""

import json
from math import ceil, radians
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

from . import Connections, LoadCatalogue
from .LinkTypes import are_link_types_compatible
from .RegistrationUtils import safe_register_class, safe_unregister_class


RECIPES_BY_ID = {}
BUILDER_FUNCTIONS = {}
REGISTERED_CLASSES = []
SUPPORTED_SCHEMA_VERSION = 2


def _addon_directory():
    return Path(__file__).resolve().parent


def _recipes_path():
    return _addon_directory() / "Recipes.json"


def _normalize_identifier(value):
    normalized = "".join(ch if ch.isalnum() else "_" for ch in str(value).lower())
    return normalized.strip("_") or "complex_item"


def register_builder(name, function):
    """Register a reusable builder function referenced by Recipes.json."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Recipe builder names must be non-empty strings")
    if not callable(function):
        raise TypeError(f"Recipe builder '{name}' must be callable")
    BUILDER_FUNCTIONS[name] = function


def _single_stagehand_object(imported_objects, asset_id):
    objects = [
        obj for obj in imported_objects
        if getattr(obj, "stagehand", None) is not None and obj.stagehand.is_stagehand_object
    ]
    if len(objects) != 1:
        raise RuntimeError(
            f"Grid asset {asset_id} must import exactly one Stagehand object; "
            f"found {len(objects)}"
        )
    return objects[0]


GRID_SIDES = ("left", "right", "bottom", "top")


def _positive_setting(settings, name):
    value = float(settings.get(name, 0.0))
    if value <= 0.0:
        raise ValueError(f"Recipe setting '{name}' must be greater than zero")
    return value


def _vector_from_data(data, name):
    try:
        values = tuple(float(value) for value in data.get(name, ()))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Recipe setting '{name}' must contain numbers") from exc
    if len(values) != 3:
        raise ValueError(f"Recipe setting '{name}' must contain three values")
    return Vector(values)


def _grid_unit_count(value, unit, module_name, dimension_name):
    units = int(round(value / unit))
    if units <= 0 or abs(value - (units * unit)) > 0.000001:
        raise ValueError(
            f"Grid module '{module_name}' {dimension_name} must be a positive "
            f"multiple of {unit:g}m"
        )
    return units


def _requested_grid_unit_count(value, unit, dimension_name, mode):
    normalized_mode = str(mode).strip().upper()
    if normalized_mode == "CEIL":
        return max(1, ceil((value / unit) - 1e-9))
    if normalized_mode != "EXACT":
        raise ValueError(
            "Recipe setting 'dimensionMode' must be either 'CEIL' or 'EXACT'"
        )

    units = int(round(value / unit))
    if units <= 0 or abs(value - (units * unit)) > 0.000001:
        raise ValueError(
            f"The requested grid {dimension_name} of {value:g}m cannot be built "
            f"exactly; it must be a multiple of {unit:g}m"
        )
    return units


def _normalized_grid_modules(definition, cell_width, cell_height):
    raw_modules = definition.get("modules")
    if not isinstance(raw_modules, list) or not raw_modules:
        raise ValueError("The grid builder requires a non-empty modules list")

    modules = []
    for raw_module in raw_modules:
        if not isinstance(raw_module, dict):
            raise TypeError("Every grid module must be an object")

        try:
            asset_id = int(raw_module["assetId"])
            width = float(raw_module["width"])
            height = float(raw_module["height"])
            rotation_degrees = float(raw_module.get("rotationDegrees", 0.0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "Every grid module requires assetId, width, and height"
            ) from exc

        asset_data = LoadCatalogue.CATALOGUE_BY_ID.get(asset_id)
        if asset_data is None:
            raise ValueError(f"Catalogue asset ID {asset_id} was not found")

        module_name = str(raw_module.get("name", asset_data.get("name", asset_id)))
        columns = _grid_unit_count(width, cell_width, module_name, "width")
        rows = _grid_unit_count(height, cell_height, module_name, "height")
        origin_offset = _vector_from_data(raw_module, "originOffset")

        raw_links = raw_module.get("links")
        if not isinstance(raw_links, dict):
            raise ValueError(f"Grid module '{module_name}' requires a links object")

        links = {}
        link_count = len(asset_data.get("links", ()))
        for side in GRID_SIDES:
            raw_indices = raw_links.get(side)
            if not isinstance(raw_indices, list) or not raw_indices:
                raise ValueError(
                    f"Grid module '{module_name}' requires link indices for '{side}'"
                )
            try:
                indices = tuple(int(index) for index in raw_indices)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Grid module '{module_name}' has invalid '{side}' link indices"
                ) from exc
            if any(index < 0 or index >= link_count for index in indices):
                raise ValueError(
                    f"Grid module '{module_name}' has an out-of-range '{side}' link"
                )
            links[side] = indices
        raw_supports = raw_module.get("supports", [])
        if not isinstance(raw_supports, list):
            raise ValueError(
                f"Grid module '{module_name}' supports must be a list"
            )
        try:
            supports = tuple(int(index) for index in raw_supports)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Grid module '{module_name}' has invalid support link indices"
            ) from exc
        if any(index < 0 or index >= link_count for index in supports):
            raise ValueError(
                f"Grid module '{module_name}' has an out-of-range support link"
            )

        modules.append(
            {
                "assetId": asset_id,
                "name": module_name,
                "width": width,
                "height": height,
                "columns": columns,
                "rows": rows,
                "rotationDegrees": rotation_degrees,
                "originOffset": origin_offset,
                "links": links,
                "supports": supports,
            }
        )

    modules.sort(
        key=lambda module: (
            -(module["columns"] * module["rows"]),
            abs(module["rotationDegrees"]) > 0.000001,
            -module["rows"],
            -module["columns"],
            module["assetId"],
        )
    )
    return modules


def _module_fits(occupied, column, row, module, columns, rows):
    end_column = column + module["columns"]
    end_row = row + module["rows"]
    if end_column > columns or end_row > rows:
        return False

    return all(
        not occupied[target_row][target_column]
        for target_row in range(row, end_row)
        for target_column in range(column, end_column)
    )


def _pack_grid(columns, rows, modules, max_items):
    occupied = [[False] * columns for _row in range(rows)]
    placements = []

    for row in range(rows):
        for column in range(columns):
            if occupied[row][column]:
                continue

            module = next(
                (
                    candidate
                    for candidate in modules
                    if _module_fits(
                        occupied,
                        column,
                        row,
                        candidate,
                        columns,
                        rows,
                    )
                ),
                None,
            )
            if module is None:
                raise ValueError(
                    f"The configured modules cannot fill the grid at "
                    f"{column}, {row}"
                )

            placement = {
                "module": module,
                "column": column,
                "row": row,
                "object": None,
            }
            placements.append(placement)
            if len(placements) > max_items:
                raise ValueError(
                    f"This build needs more than the recipe limit of {max_items} items"
                )

            for target_row in range(row, row + module["rows"]):
                for target_column in range(
                    column,
                    column + module["columns"],
                ):
                    occupied[target_row][target_column] = True

    return placements


def _apply_vertical_remainder_position(
    placements,
    rows,
    settings,
    parameters,
):
    parameter_name = settings.get("verticalRemainderParameter")
    if not parameter_name:
        return

    position = str(parameters.get(parameter_name, "TOP")).strip().upper()
    if position not in {"BOTTOM", "TOP"}:
        raise ValueError(
            f"Grid parameter '{parameter_name}' must be BOTTOM or TOP"
        )
    if position == "BOTTOM":
        for placement in placements:
            placement["row"] = (
                rows
                - placement["row"]
                - placement["module"]["rows"]
            )


def _position_grid_module(
    obj,
    grid_matrix,
    placement,
    cell_width,
    cell_height,
    column_vector,
    row_vector,
):
    module = placement["module"]
    offset = (
        column_vector * (placement["column"] * cell_width)
        + row_vector * (placement["row"] * cell_height)
        + module["originOffset"]
    )
    rotation_matrix = Matrix.Rotation(
        radians(module["rotationDegrees"]),
        4,
        'Z',
    )
    matrix_world = grid_matrix @ rotation_matrix
    matrix_world.translation = (
        grid_matrix.translation
        + grid_matrix.to_quaternion() @ offset
    )
    obj.matrix_world = matrix_world


def _side_connection_candidates(placement_a, side_a, placement_b, side_b):
    obj_a = placement_a["object"]
    obj_b = placement_b["object"]
    candidates = []

    for index_a in placement_a["module"]["links"][side_a]:
        link_a = Connections.get_link(obj_a, index_a)
        if link_a is None or Connections.is_link_connected(link_a):
            continue

        for index_b in placement_b["module"]["links"][side_b]:
            link_b = Connections.get_link(obj_b, index_b)
            if link_b is None or Connections.is_link_connected(link_b):
                continue
            if not are_link_types_compatible(link_a.type, link_b.type):
                continue
            if not Connections.links_are_aligned(obj_a, index_a, obj_b, index_b):
                continue

            distance, angle = Connections.link_alignment_metrics(
                obj_a,
                index_a,
                obj_b,
                index_b,
            )
            candidates.append((distance, angle, index_a, index_b))

    return sorted(candidates)


def _connect_grid_sides(placement_a, side_a, placement_b, side_b):
    obj_a = placement_a["object"]
    obj_b = placement_b["object"]
    connected_count = 0

    for _distance, _angle, index_a, index_b in _side_connection_candidates(
        placement_a,
        side_a,
        placement_b,
        side_b,
    ):
        link_a = Connections.get_link(obj_a, index_a)
        link_b = Connections.get_link(obj_b, index_b)
        if (
            link_a is None
            or link_b is None
            or Connections.is_link_connected(link_a)
            or Connections.is_link_connected(link_b)
        ):
            continue
        if Connections.connect_links(obj_a, index_a, obj_b, index_b):
            connected_count += 1

    if connected_count == 0:
        module_a = placement_a["module"]
        module_b = placement_b["module"]
        raise RuntimeError(
            f"No aligned links between '{module_a['name']}' ({side_a}) and "
            f"'{module_b['name']}' ({side_b})"
        )

    return connected_count


def _ranges_overlap(start_a, size_a, start_b, size_b):
    return max(start_a, start_b) < min(start_a + size_a, start_b + size_b)


def _connect_grid_modules(placements):
    connection_count = 0

    for index, placement_a in enumerate(placements):
        module_a = placement_a["module"]
        for placement_b in placements[index + 1:]:
            module_b = placement_b["module"]

            if (
                placement_a["column"] + module_a["columns"]
                == placement_b["column"]
                and _ranges_overlap(
                    placement_a["row"],
                    module_a["rows"],
                    placement_b["row"],
                    module_b["rows"],
                )
            ):
                connection_count += _connect_grid_sides(
                    placement_a,
                    "right",
                    placement_b,
                    "left",
                )
            elif (
                placement_b["column"] + module_b["columns"]
                == placement_a["column"]
                and _ranges_overlap(
                    placement_a["row"],
                    module_a["rows"],
                    placement_b["row"],
                    module_b["rows"],
                )
            ):
                connection_count += _connect_grid_sides(
                    placement_b,
                    "right",
                    placement_a,
                    "left",
                )

            if (
                placement_a["row"] + module_a["rows"] == placement_b["row"]
                and _ranges_overlap(
                    placement_a["column"],
                    module_a["columns"],
                    placement_b["column"],
                    module_b["columns"],
                )
            ):
                connection_count += _connect_grid_sides(
                    placement_a,
                    "top",
                    placement_b,
                    "bottom",
                )
            elif (
                placement_b["row"] + module_b["rows"] == placement_a["row"]
                and _ranges_overlap(
                    placement_a["column"],
                    module_a["columns"],
                    placement_b["column"],
                    module_b["columns"],
                )
            ):
                connection_count += _connect_grid_sides(
                    placement_b,
                    "top",
                    placement_a,
                    "bottom",
                )

    return connection_count


def _grid_elevation_offset(settings, parameters):
    elevation_settings = settings.get("elevation")
    if elevation_settings is None:
        return Vector((0.0, 0.0, 0.0))
    if not isinstance(elevation_settings, dict):
        raise ValueError("Recipe setting 'elevation' must be an object")

    try:
        height_parameter = str(elevation_settings["heightParameter"])
        parameter_scale = float(elevation_settings.get("parameterScale", 1.0))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "Elevation settings require heightParameter"
        ) from exc
    if height_parameter not in parameters:
        raise ValueError(
            f"Elevation height parameter '{height_parameter}' was not provided"
        )
    if parameter_scale <= 0.0:
        raise ValueError("Elevation parameterScale must be positive")

    direction = _vector_from_data(elevation_settings, "vector")
    height = float(parameters[height_parameter]) * parameter_scale
    return direction * height


def _add_grid_supports(
    placements,
    settings,
    parameters,
    imported_objects,
):
    support_settings = settings.get("supports")
    if support_settings is None:
        return 0, 0
    if not isinstance(support_settings, dict):
        raise ValueError("Recipe setting 'supports' must be an object")

    try:
        asset_id = int(support_settings["assetId"])
        height_parameter = str(support_settings["heightParameter"])
        parameter_scale = float(support_settings.get("parameterScale", 1.0))
        base_length = float(support_settings.get("baseLength", 1.0))
        support_link_index = int(support_settings.get("linkIndex", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "Support settings require assetId and heightParameter"
        ) from exc

    asset_data = LoadCatalogue.CATALOGUE_BY_ID.get(asset_id)
    if asset_data is None:
        raise ValueError(f"Catalogue support asset ID {asset_id} was not found")
    if (
        support_link_index < 0
        or support_link_index >= len(asset_data.get("links", ()))
    ):
        raise ValueError("Support linkIndex is outside the catalogue asset links")
    if height_parameter not in parameters:
        raise ValueError(
            f"Support height parameter '{height_parameter}' was not provided"
        )
    if parameter_scale <= 0.0 or base_length <= 0.0:
        raise ValueError("Support parameterScale and baseLength must be positive")

    target_length = float(parameters[height_parameter]) * parameter_scale
    if target_length <= 0.0:
        raise ValueError("Support height must be greater than zero")
    scale_factor = target_length / base_length
    scale_axis = str(support_settings.get("scaleAxis", "Z")).strip().lower()
    if scale_axis not in {"x", "y", "z"}:
        raise ValueError("Support scaleAxis must be X, Y, or Z")

    support_count = 0
    connection_count = 0
    for placement in placements:
        deck_obj = placement["object"]
        for deck_link_index in placement["module"]["supports"]:
            imported = LoadCatalogue.import_catalogue_asset(asset_id)
            imported_objects.extend(imported)
            support_obj = _single_stagehand_object(imported, asset_id)

            support_link = Connections.get_link(
                support_obj,
                support_link_index,
            )
            deck_link = Connections.get_link(deck_obj, deck_link_index)
            if support_link is None or deck_link is None:
                raise RuntimeError("Unable to find a configured support link")
            if not are_link_types_compatible(support_link.type, deck_link.type):
                raise RuntimeError("Configured support links are incompatible")
            if not Connections.align_object_link_to_target(
                support_obj,
                support_link_index,
                deck_obj,
                deck_link_index,
            ):
                raise RuntimeError("Unable to align a grid support")
            scale = support_obj.scale.copy()
            scale[{"x": 0, "y": 1, "z": 2}[scale_axis]] = scale_factor
            support_obj.scale = scale

            if not Connections.connect_links(
                support_obj,
                support_link_index,
                deck_obj,
                deck_link_index,
            ):
                raise RuntimeError("Unable to connect a grid support")

            support_count += 1
            connection_count += 1

    return support_count, connection_count


def _remove_imported_objects(objects):
    for obj in reversed(objects):
        if obj.name in bpy.data.objects:
            bpy.data.objects.remove(obj, do_unlink=True)
    Connections.prune_stale_connections()


def build_grid(context, definition, parameters):
    """Pack compatible modules into a rectangle and connect adjacent sides."""
    settings = definition.get("settings", {})
    cell_width = _positive_setting(settings, "cellWidth")
    cell_height = _positive_setting(settings, "cellHeight")
    column_vector = _vector_from_data(settings, "columnVector")
    row_vector = _vector_from_data(settings, "rowVector")
    requested_width = float(parameters["width"])
    requested_height = float(parameters["height"])
    if requested_width <= 0.0 or requested_height <= 0.0:
        raise ValueError("Grid width and height must be greater than zero")

    dimension_mode = settings.get("dimensionMode", "CEIL")
    columns = _requested_grid_unit_count(
        requested_width,
        cell_width,
        "width",
        dimension_mode,
    )
    rows = _requested_grid_unit_count(
        requested_height,
        cell_height,
        "depth",
        dimension_mode,
    )
    cell_count = columns * rows
    max_cells = int(settings.get("maxCells", 10000))
    if cell_count > max_cells:
        raise ValueError(
            f"This build needs {cell_count} grid cells; "
            f"the recipe limit is {max_cells}"
        )

    max_items = int(settings.get("maxItems", 1000))
    modules = _normalized_grid_modules(
        definition,
        cell_width,
        cell_height,
    )
    placements = _pack_grid(
        columns,
        rows,
        modules,
        max_items,
    )
    _apply_vertical_remainder_position(
        placements,
        rows,
        settings,
        parameters,
    )
    configured_support_count = sum(
        len(placement["module"]["supports"])
        for placement in placements
    )
    if len(placements) + configured_support_count > max_items:
        raise ValueError(
            f"This build exceeds the recipe limit of {max_items} total items"
        )
    elevation_offset = _grid_elevation_offset(settings, parameters)

    imported_objects = []
    grid_matrix = None

    try:
        for placement in placements:
            asset_id = placement["module"]["assetId"]
            imported = LoadCatalogue.import_catalogue_asset(asset_id)
            imported_objects.extend(imported)
            obj = _single_stagehand_object(imported, asset_id)
            placement["object"] = obj

            if grid_matrix is None:
                grid_matrix = obj.matrix_world.copy()
                grid_matrix.translation = (
                    context.scene.cursor.location
                    + grid_matrix.to_quaternion() @ elevation_offset
                )
            _position_grid_module(
                obj,
                grid_matrix,
                placement,
                cell_width,
                cell_height,
                column_vector,
                row_vector,
            )

        connection_count = _connect_grid_modules(placements)
        support_count, support_connections = _add_grid_supports(
            placements,
            settings,
            parameters,
            imported_objects,
        )
        connection_count += support_connections

    except Exception:
        _remove_imported_objects(imported_objects)
        raise

    for selected in context.selected_objects:
        selected.select_set(False)
    for obj in imported_objects:
        obj.select_set(True)
    context.view_layer.objects.active = placements[0]["object"]

    actual_width = columns * cell_width
    actual_height = rows * cell_height
    support_message = (
        f" and {support_count} supports"
        if support_count
        else ""
    )

    return (
        imported_objects,
        f"Added {len(placements)} modules{support_message} "
        f"({actual_width:g}m x {actual_height:g}m, "
        f"{connection_count} connections)",
    )


register_builder("grid", build_grid)


def _property_from_definition(parameter_name, parameter):
    if not isinstance(parameter, dict):
        raise TypeError(f"Recipe parameter '{parameter_name}' must be an object")

    property_type = str(parameter.get("type", "FLOAT")).upper()
    common = {
        "name": str(parameter.get("label", parameter_name.replace("_", " ").title())),
        "description": str(parameter.get("description", "")),
    }

    if property_type == "FLOAT":
        options = dict(common)
        options["default"] = float(parameter.get("default", 0.0))
        if "min" in parameter:
            options["min"] = float(parameter["min"])
        if "max" in parameter:
            options["max"] = float(parameter["max"])
        if "unit" in parameter:
            options["unit"] = str(parameter["unit"])
        return bpy.props.FloatProperty(**options)

    if property_type == "INT":
        options = dict(common)
        options["default"] = int(parameter.get("default", 0))
        if "min" in parameter:
            options["min"] = int(parameter["min"])
        if "max" in parameter:
            options["max"] = int(parameter["max"])
        return bpy.props.IntProperty(**options)

    if property_type == "ENUM":
        raw_items = parameter.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError(
                f"Recipe enum parameter '{parameter_name}' requires items"
            )

        items = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict) or "value" not in raw_item:
                raise ValueError(
                    f"Recipe enum parameter '{parameter_name}' has an invalid item"
                )
            value = str(raw_item["value"])
            if not value:
                raise ValueError(
                    f"Recipe enum parameter '{parameter_name}' has an empty value"
                )
            items.append(
                (
                    value,
                    str(raw_item.get("label", value.replace("_", " ").title())),
                    str(raw_item.get("description", "")),
                )
            )

        default = str(parameter.get("default", items[0][0]))
        if default not in {item[0] for item in items}:
            raise ValueError(
                f"Recipe enum parameter '{parameter_name}' has an invalid default"
            )
        return bpy.props.EnumProperty(
            items=items,
            default=default,
            **common,
        )

    if property_type == "BOOL":
        return bpy.props.BoolProperty(default=bool(parameter.get("default", False)), **common)

    if property_type == "STRING":
        return bpy.props.StringProperty(default=str(parameter.get("default", "")), **common)

    raise ValueError(
        f"Unsupported Recipe parameter type '{property_type}' for '{parameter_name}'"
    )


def _build_operator(definition):
    recipe_id = definition["id"]
    normalized_id = _normalize_identifier(recipe_id)
    annotations = {}
    parameters = definition.get("parameters", {})
    for parameter_name, parameter in parameters.items():
        annotations[parameter_name] = _property_from_definition(parameter_name, parameter)

    def draw(self, context):
        del context
        for parameter_name in parameters:
            self.layout.prop(self, parameter_name)

    def invoke(self, context, event):
        del event
        if parameters:
            return context.window_manager.invoke_props_dialog(self)
        return self.execute(context)

    def execute(self, context):
        builder = BUILDER_FUNCTIONS.get(definition["builder"])
        if builder is None:
            self.report({'ERROR'}, f"Unknown recipe builder: {definition['builder']}")
            return {'CANCELLED'}

        values = {name: getattr(self, name) for name in parameters}
        try:
            _objects, message = builder(context, definition, values)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        self.report({'INFO'}, message)
        return {'FINISHED'}

    return type(
        f"STAGEHAND_OT_recipe_{normalized_id}",
        (bpy.types.Operator,),
        {
            "bl_idname": f"stagehand.recipe_{normalized_id}",
            "bl_label": definition["name"],
            "bl_description": definition.get(
                "description",
                f"Build Stagehand recipe '{definition['name']}'",
            ),
            "bl_options": {'REGISTER', 'UNDO'},
            "__annotations__": annotations,
            "draw": draw,
            "invoke": invoke,
            "execute": execute,
        },
    )


class STAGEHAND_MT_recipe_menu(bpy.types.Menu):
    bl_label = "Add Recipe"
    bl_idname = "STAGEHAND_MT_recipe_menu"

    def draw(self, context):
        del context
        layout = self.layout
        if not RECIPES_BY_ID:
            layout.label(text="No recipes found")
            return

        for recipe_id, definition in RECIPES_BY_ID.items():
            operator_id = _normalize_identifier(recipe_id)
            layout.operator(
                f"stagehand.recipe_{operator_id}",
                text=definition["name"],
            )


BASE_CLASSES = (STAGEHAND_MT_recipe_menu,)


def _load_recipes():
    path = _recipes_path()
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    version = data.get("schemaVersion")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported Recipes schema version {version}; "
            f"expected {SUPPORTED_SCHEMA_VERSION}"
        )

    definitions = {}
    normalized_ids = set()
    for definition in data.get("items", []):
        for required_key in ("id", "name", "builder"):
            if required_key not in definition:
                raise ValueError(
                    f"Recipe is missing '{required_key}': {definition}"
                )

        recipe_id = definition["id"]
        if not isinstance(recipe_id, str) or not recipe_id.strip():
            raise TypeError("Recipe IDs must be non-empty strings")
        normalized_id = _normalize_identifier(recipe_id)
        if recipe_id in definitions or normalized_id in normalized_ids:
            raise ValueError(f"Duplicate recipe ID: {recipe_id}")
        if definition["builder"] not in BUILDER_FUNCTIONS:
            raise ValueError(f"Unknown recipe builder: {definition['builder']}")
        if (
            "assetId" in definition
            and int(definition["assetId"]) not in LoadCatalogue.CATALOGUE_BY_ID
        ):
            raise ValueError(
                f"Recipe '{recipe_id}' references missing catalogue asset "
                f"{definition['assetId']}"
            )
        if not isinstance(definition.get("parameters", {}), dict):
            raise TypeError(f"Recipe '{recipe_id}' parameters must be an object")

        definitions[recipe_id] = definition
        normalized_ids.add(normalized_id)

    return definitions


def _unregister_dynamic_classes():
    for cls in reversed(REGISTERED_CLASSES):
        safe_unregister_class(cls)
    REGISTERED_CLASSES.clear()


def reload_recipe_operators():
    global RECIPES_BY_ID

    _unregister_dynamic_classes()
    RECIPES_BY_ID = _load_recipes()
    for definition in RECIPES_BY_ID.values():
        operator_class = _build_operator(definition)
        safe_register_class(operator_class)
        REGISTERED_CLASSES.append(operator_class)


def register():
    for cls in BASE_CLASSES:
        safe_register_class(cls)
    reload_recipe_operators()


def unregister():
    _unregister_dynamic_classes()
    for cls in reversed(BASE_CLASSES):
        safe_unregister_class(cls)
