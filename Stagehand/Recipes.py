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
RECIPE_RESOURCES = {}
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


class GridPackingError(ValueError):
    """The configured modules cannot tile the requested grid."""


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
        unit_value = value / unit
        lower_units = int(unit_value + 1e-9)
        upper_units = ceil(unit_value - 1e-9)
        alternatives = []
        if lower_units > 0:
            alternatives.append(f"{lower_units * unit:g}m (più piccola)")
        if upper_units > 0:
            alternatives.append(f"{upper_units * unit:g}m (più grande)")

        dimension_label = {
            "width": "La larghezza",
            "height": "L'altezza",
            "depth": "La profondità",
        }.get(dimension_name, dimension_name)
        if len(alternatives) == 2:
            suggestion = (
                "Le misure realizzabili più vicine sono "
                f"{alternatives[0]} oppure {alternatives[1]}."
            )
        elif alternatives:
            suggestion = f"La misura realizzabile più vicina è {alternatives[0]}."
        else:
            suggestion = "Non ci sono misure alternative positive."

        raise ValueError(
            f"{dimension_label} richiesta di {value:g}m non è realizzabile "
            f"esattamente. {suggestion}"
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
            if not isinstance(raw_indices, list):
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
                raise GridPackingError(
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


def _grid_can_pack(columns, rows, modules, max_items, max_cells):
    if columns <= 0 or rows <= 0 or columns * rows > max_cells:
        return False
    try:
        _pack_grid(columns, rows, modules, max_items)
    except ValueError:
        return False
    return True


def _nearest_packable_grid_dimensions(
    columns,
    rows,
    modules,
    max_items,
    max_cells,
    cell_width,
    cell_height,
    row_dimension_name,
):
    lower_candidates = []
    upper_candidates = []

    for candidate_columns in range(columns - 1, 0, -1):
        if _grid_can_pack(
            candidate_columns,
            rows,
            modules,
            max_items,
            max_cells,
        ):
            lower_candidates.append(
                (
                    (columns - candidate_columns) * cell_width,
                    0,
                    candidate_columns,
                    rows,
                    "larghezza",
                )
            )
            break

    maximum_columns = max_cells // rows
    for candidate_columns in range(columns + 1, maximum_columns + 1):
        if _grid_can_pack(
            candidate_columns,
            rows,
            modules,
            max_items,
            max_cells,
        ):
            upper_candidates.append(
                (
                    (candidate_columns - columns) * cell_width,
                    0,
                    candidate_columns,
                    rows,
                    "larghezza",
                )
            )
            break

    for candidate_rows in range(rows - 1, 0, -1):
        if _grid_can_pack(
            columns,
            candidate_rows,
            modules,
            max_items,
            max_cells,
        ):
            lower_candidates.append(
                (
                    (rows - candidate_rows) * cell_height,
                    1,
                    columns,
                    candidate_rows,
                    row_dimension_name,
                )
            )
            break

    maximum_rows = max_cells // columns
    for candidate_rows in range(rows + 1, maximum_rows + 1):
        if _grid_can_pack(
            columns,
            candidate_rows,
            modules,
            max_items,
            max_cells,
        ):
            upper_candidates.append(
                (
                    (candidate_rows - rows) * cell_height,
                    1,
                    columns,
                    candidate_rows,
                    row_dimension_name,
                )
            )
            break

    labels = {
        "width": "larghezza",
        "height": "altezza",
        "depth": "profondità",
    }

    def describe(candidate, direction):
        _distance, _priority, candidate_columns, candidate_rows, dimension = candidate
        dimension_label = labels.get(dimension, dimension)
        width = candidate_columns * cell_width
        height = candidate_rows * cell_height
        return (
            f"{width:g}m x {height:g}m "
            f"({dimension_label} più {direction})"
        )

    alternatives = []
    if lower_candidates:
        alternatives.append(describe(min(lower_candidates), "piccola"))
    if upper_candidates:
        alternatives.append(describe(min(upper_candidates), "grande"))

    if len(alternatives) == 2:
        return (
            "Le dimensioni realizzabili più vicine sono "
            f"{alternatives[0]} oppure {alternatives[1]}."
        )
    if alternatives:
        return f"La dimensione realizzabile più vicina è {alternatives[0]}."
    return "Non ci sono dimensioni alternative entro i limiti della ricetta."

def _apply_vertical_remainder_position(
    placements,
    rows,
    settings,
    parameters,
):
    parameter_name = settings.get("verticalRemainderParameter")
    configured_position = settings.get("verticalRemainderPosition")
    if not parameter_name and configured_position is None:
        return

    if parameter_name:
        position = parameters.get(
            parameter_name,
            configured_position if configured_position is not None else "TOP",
        )
    else:
        position = configured_position
    position = str(position).strip().upper()
    if position not in {"BOTTOM", "TOP"}:
        setting_name = parameter_name or "verticalRemainderPosition"
        raise ValueError(
            f"Grid parameter '{setting_name}' must be BOTTOM or TOP"
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
    if (
        not placement_a["module"]["links"][side_a]
        or not placement_b["module"]["links"][side_b]
    ):
        return 0
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
        height_offset = float(elevation_settings.get("heightOffset", 0.0))
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
    height = (
        float(parameters[height_parameter]) * parameter_scale
        + height_offset
    )
    if height <= 0.0:
        raise ValueError("Grid elevation height must be greater than zero")
    return direction * height


def _scale_object_world_axis_around_pivot(obj, axis, factor, pivot):
    factors = [1.0, 1.0, 1.0, 1.0]
    factors[{"x": 0, "y": 1, "z": 2}[axis]] = factor
    pivot = Vector(pivot)
    obj.matrix_world = (
        Matrix.Translation(pivot)
        @ Matrix.Diagonal(factors)
        @ Matrix.Translation(-pivot)
        @ obj.matrix_world
    )


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
        length_offset = float(support_settings.get("lengthOffset", 0.0))
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

    target_length = (
        float(parameters[height_parameter]) * parameter_scale
        + length_offset
    )
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
            deck_link_position = Vector(deck_link.posDir[:3])
            pivot = (
                deck_obj.matrix_world.to_translation()
                + deck_obj.matrix_world.to_quaternion() @ deck_link_position
            )
            _scale_object_world_axis_around_pivot(
                support_obj,
                scale_axis,
                scale_factor,
                pivot,
            )

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


def _enabled_grid_boundary_accessories(settings, parameters):
    raw_accessories = settings.get("boundaryAccessories", [])
    if not isinstance(raw_accessories, list):
        raise ValueError("Recipe setting 'boundaryAccessories' must be a list")

    accessories = []
    for raw_accessory in raw_accessories:
        if not isinstance(raw_accessory, dict):
            raise ValueError("Every boundary accessory must be an object")

        parameter_name = str(raw_accessory.get("parameter", "")).strip()
        if parameter_name and not bool(parameters.get(parameter_name, False)):
            continue

        try:
            asset_id = int(raw_accessory["assetId"])
            link_index = int(raw_accessory.get("linkIndex", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "Boundary accessories require assetId and a valid linkIndex"
            ) from exc

        asset_data = LoadCatalogue.CATALOGUE_BY_ID.get(asset_id)
        if asset_data is None:
            raise ValueError(
                f"Catalogue boundary asset ID {asset_id} was not found"
            )
        if link_index < 0 or link_index >= len(asset_data.get("links", ())):
            raise ValueError(
                "Boundary accessory linkIndex is outside its asset links"
            )

        raw_sides = raw_accessory.get("sides", [])
        if not isinstance(raw_sides, list) or not raw_sides:
            raise ValueError(
                "Boundary accessories require a non-empty sides list"
            )
        sides = tuple(str(side).strip().lower() for side in raw_sides)
        if any(side not in GRID_SIDES for side in sides):
            raise ValueError("Boundary accessory sides must be grid sides")

        accessories.append({
            "assetId": asset_id,
            "assetData": asset_data,
            "linkIndex": link_index,
            "sides": sides,
            "label": str(raw_accessory.get("label", "boundary items")),
        })

    return accessories


def _placement_touches_grid_side(placement, side, columns, rows):
    module = placement["module"]
    if side == "left":
        return placement["column"] == 0
    if side == "right":
        return placement["column"] + module["columns"] == columns
    if side == "bottom":
        return placement["row"] == 0
    return placement["row"] + module["rows"] == rows


def _grid_boundary_accessory_sites(placements, columns, rows, accessory):
    accessory_link = accessory["assetData"]["links"][accessory["linkIndex"]]
    accessory_type = accessory_link["type"]
    seen = set()

    for placement in placements:
        module = placement["module"]
        module_asset = LoadCatalogue.CATALOGUE_BY_ID[module["assetId"]]
        for side in accessory["sides"]:
            if not _placement_touches_grid_side(
                placement,
                side,
                columns,
                rows,
            ):
                continue
            for deck_link_index in module["links"][side]:
                site_key = (id(placement), deck_link_index)
                if site_key in seen:
                    continue
                seen.add(site_key)
                deck_type = module_asset["links"][deck_link_index]["type"]
                if are_link_types_compatible(accessory_type, deck_type):
                    yield placement, deck_link_index


def _add_grid_boundary_accessories(
    placements,
    columns,
    rows,
    accessories,
    imported_objects,
):
    total_count = 0
    connection_count = 0
    label_counts = {}

    for accessory in accessories:
        for placement, deck_link_index in _grid_boundary_accessory_sites(
            placements,
            columns,
            rows,
            accessory,
        ):
            deck_obj = placement["object"]
            deck_link = Connections.get_link(deck_obj, deck_link_index)
            if deck_link is None:
                raise RuntimeError("Unable to find a boundary deck link")
            if Connections.is_link_connected(deck_link):
                continue

            asset_id = accessory["assetId"]
            imported = LoadCatalogue.import_catalogue_asset(asset_id)
            imported_objects.extend(imported)
            accessory_obj = _single_stagehand_object(imported, asset_id)
            accessory_link_index = accessory["linkIndex"]
            accessory_link = Connections.get_link(
                accessory_obj,
                accessory_link_index,
            )
            if accessory_link is None:
                raise RuntimeError("Unable to find a boundary accessory link")
            if not Connections.align_object_link_to_target(
                accessory_obj,
                accessory_link_index,
                deck_obj,
                deck_link_index,
            ):
                raise RuntimeError("Unable to align a boundary accessory")
            if not Connections.connect_links(
                accessory_obj,
                accessory_link_index,
                deck_obj,
                deck_link_index,
            ):
                raise RuntimeError("Unable to connect a boundary accessory")

            total_count += 1
            connection_count += 1
            label = accessory["label"]
            label_counts[label] = label_counts.get(label, 0) + 1

    return total_count, connection_count, label_counts


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
    row_dimension_name = str(settings.get("rowDimensionName", "height"))
    rows = _requested_grid_unit_count(
        requested_height,
        cell_height,
        row_dimension_name,
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
    try:
        placements = _pack_grid(
            columns,
            rows,
            modules,
            max_items,
        )
    except GridPackingError as exc:
        suggestion = _nearest_packable_grid_dimensions(
            columns,
            rows,
            modules,
            max_items,
            max_cells,
            cell_width,
            cell_height,
            row_dimension_name,
        )
        raise ValueError(
            f"Le dimensioni richieste di {requested_width:g}m x "
            f"{requested_height:g}m non sono realizzabili esattamente. "
            f"{suggestion}"
        ) from exc
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
    boundary_accessories = _enabled_grid_boundary_accessories(
        settings,
        parameters,
    )
    configured_accessory_count = sum(
        1
        for accessory in boundary_accessories
        for _site in _grid_boundary_accessory_sites(
            placements,
            columns,
            rows,
            accessory,
        )
    )
    if (
        len(placements)
        + configured_support_count
        + configured_accessory_count
        > max_items
    ):
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
        _accessory_count, accessory_connections, accessory_labels = (
            _add_grid_boundary_accessories(
                placements,
                columns,
                rows,
                boundary_accessories,
                imported_objects,
            )
        )
        connection_count += accessory_connections

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
    accessory_message = "".join(
        f" and {count} {label}"
        for label, count in accessory_labels.items()
    )

    return (
        imported_objects,
        f"Added {len(placements)} modules{support_message}{accessory_message} "
        f"({actual_width:g}m x {actual_height:g}m, "
        f"{connection_count} connections)",
    )


def _normalized_litec_family(definition, parameters):
    settings = definition.get("settings", {})
    resource_name = str(settings.get("familyResource", "")).strip()
    if not resource_name:
        raise ValueError("The Litec structure builder requires a familyResource")

    families = RECIPE_RESOURCES.get(resource_name)
    if not isinstance(families, dict) or not families:
        raise ValueError(f"Recipe resource '{resource_name}' was not found")

    family_key = str(parameters.get("family", "")).strip().upper()
    raw_family = families.get(family_key)
    if not isinstance(raw_family, dict):
        raise ValueError(f"Unknown Litec family '{family_key}'")

    try:
        cube_asset_id = int(raw_family["cubeAssetId"])
        cube_size = float(raw_family["cubeSize"])
        segment_start_link = int(raw_family.get("segmentStartLink", 0))
        segment_end_link = int(raw_family.get("segmentEndLink", 1))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"Litec family '{family_key}' has invalid cube or segment settings"
        ) from exc

    cube_asset = LoadCatalogue.CATALOGUE_BY_ID.get(cube_asset_id)
    if cube_asset is None:
        raise ValueError(f"Catalogue asset ID {cube_asset_id} was not found")
    if cube_size <= 0.0:
        raise ValueError(f"Litec family '{family_key}' cubeSize must be positive")

    raw_cube_links = raw_family.get("cubeLinks")
    required_cube_links = (
        "bottom",
        "xNegative",
        "xPositive",
        "yNegative",
        "yPositive",
    )
    if not isinstance(raw_cube_links, dict):
        raise ValueError(f"Litec family '{family_key}' requires cubeLinks")

    cube_links = {}
    for link_name in required_cube_links:
        try:
            link_index = int(raw_cube_links[link_name])
            cube_asset["links"][link_index]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Litec family '{family_key}' cube link '{link_name}' is invalid"
            ) from exc
        cube_links[link_name] = link_index

    raw_segments = raw_family.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError(f"Litec family '{family_key}' requires straight segments")

    segments = []
    seen_lengths = set()
    for raw_segment in raw_segments:
        try:
            asset_id = int(raw_segment["assetId"])
            length = float(raw_segment["length"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Litec family '{family_key}' contains an invalid segment"
            ) from exc

        millimeters = int(round(length * 1000.0))
        if millimeters <= 0 or abs(length - (millimeters / 1000.0)) > 0.000001:
            raise ValueError(
                f"Litec segment asset {asset_id} length must use whole millimetres"
            )
        if millimeters in seen_lengths:
            raise ValueError(
                f"Litec family '{family_key}' repeats segment length {length:g}m"
            )

        asset = LoadCatalogue.CATALOGUE_BY_ID.get(asset_id)
        if asset is None:
            raise ValueError(f"Catalogue asset ID {asset_id} was not found")
        try:
            asset["links"][segment_start_link]
            asset["links"][segment_end_link]
        except (IndexError, TypeError) as exc:
            raise ValueError(
                f"Litec segment asset {asset_id} has invalid connection links"
            ) from exc

        seen_lengths.add(millimeters)
        segments.append(
            {
                "assetId": asset_id,
                "length": length,
                "millimeters": millimeters,
            }
        )

    segments.sort(key=lambda segment: segment["millimeters"], reverse=True)
    return {
        "key": family_key,
        "label": str(raw_family.get("label", family_key)),
        "cubeAssetId": cube_asset_id,
        "cubeSize": cube_size,
        "cubeLinks": cube_links,
        "segmentStartLink": segment_start_link,
        "segmentEndLink": segment_end_link,
        "segments": segments,
    }


def _litec_straight_length(
    requested,
    cube_size,
    cube_count,
    measurement_mode,
    dimension_name,
):
    if requested <= 0.0:
        raise ValueError(f"Structure {dimension_name} must be greater than zero")

    if measurement_mode == "INTERNAL":
        return requested
    if measurement_mode != "EXTERNAL":
        raise ValueError("Measurements must be either INTERNAL or EXTERNAL")

    cube_thickness = cube_size * cube_count
    straight_length = requested - cube_thickness
    if straight_length <= 0.0:
        raise ValueError(
            f"External {dimension_name} must be greater than the total "
            f"cube thickness of {cube_thickness:g}m"
        )
    return straight_length


def _litec_segment_plan(
    straight_length,
    family,
    dimension_name,
    requested,
    measurement_mode,
    max_run,
):
    if straight_length > max_run + 0.000001:
        raise ValueError(
            f"Structure {dimension_name} exceeds the recipe limit of {max_run:g}m"
        )

    target_millimeters = straight_length * 1000.0
    target = int(round(target_millimeters))
    minimum_segment = min(
        segment["millimeters"] for segment in family["segments"]
    )
    search_limit = min(
        int(round(max_run * 1000.0)),
        ceil(target_millimeters) + minimum_segment,
    )

    if target <= 0:
        plan = None
        best = [None]
    else:
        best = [None] * (search_limit + 1)
        best[0] = ()
        for total in range(1, search_limit + 1):
            winner = None
            winner_lengths = None
            for segment in family["segments"]:
                length = segment["millimeters"]
                if length > total or best[total - length] is None:
                    continue
                candidate = tuple(
                    sorted(
                        best[total - length] + (segment,),
                        key=lambda item: item["millimeters"],
                        reverse=True,
                    )
                )
                candidate_lengths = tuple(
                    item["millimeters"] for item in candidate
                )
                if (
                    winner is None
                    or len(candidate) < len(winner)
                    or (
                        len(candidate) == len(winner)
                        and candidate_lengths > winner_lengths
                    )
                ):
                    winner = candidate
                    winner_lengths = candidate_lengths
            best[total] = winner

        is_whole_millimeter = (
            abs(straight_length - (target / 1000.0)) <= 0.000001
        )
        plan = best[target] if is_whole_millimeter else None

    if plan is None:
        lower_start = min(int(target_millimeters), len(best) - 1)
        lower = next(
            (total for total in range(lower_start, 0, -1) if best[total]),
            None,
        )
        upper_start = max(1, ceil(target_millimeters))
        upper = next(
            (
                total
                for total in range(upper_start, len(best))
                if best[total]
            ),
            None,
        )

        measurement_offset = requested - straight_length
        alternatives = []
        if lower is not None:
            value = round((lower / 1000.0) + measurement_offset, 6)
            alternatives.append(f"{value:g}m (più piccola)")
        if upper is not None:
            value = round((upper / 1000.0) + measurement_offset, 6)
            alternatives.append(f"{value:g}m (più grande)")

        dimension_label = {
            "width": "La larghezza",
            "height": "L'altezza",
            "depth": "La profondità",
        }.get(dimension_name, dimension_name)
        mode_label = "interna" if measurement_mode == "INTERNAL" else "esterna"
        if len(alternatives) == 2:
            suggestion = (
                "Le misure realizzabili più vicine sono "
                f"{alternatives[0]} oppure {alternatives[1]}."
            )
        elif alternatives:
            suggestion = f"La misura realizzabile più vicina è {alternatives[0]}."
        else:
            suggestion = "Non ci sono misure alternative entro i limiti della ricetta."

        raise ValueError(
            f"{dimension_label} {mode_label} richiesta di {requested:g}m "
            f"non è realizzabile esattamente con {family['label']}. "
            f"{suggestion}"
        )
    return plan

def _place_litec_cubes(context, family, offsets, imported_objects):
    cubes = []
    structure_matrix = None
    for offset in offsets:
        asset_id = family["cubeAssetId"]
        imported = LoadCatalogue.import_catalogue_asset(asset_id)
        imported_objects.extend(imported)
        cube = _single_stagehand_object(imported, asset_id)

        if structure_matrix is None:
            structure_matrix = cube.matrix_world.copy()
            structure_matrix.translation = context.scene.cursor.location

        cube.matrix_world = structure_matrix.copy()
        cube.matrix_world.translation = (
            structure_matrix.translation
            + structure_matrix.to_quaternion() @ Vector(offset)
        )
        cubes.append(cube)
    return cubes


def _connect_litec_pair(obj_a, link_index_a, obj_b, link_index_b):
    link_a = Connections.get_link(obj_a, link_index_a)
    link_b = Connections.get_link(obj_b, link_index_b)
    if link_a is None or link_b is None:
        raise RuntimeError("Unable to find a Litec connection link")
    if not are_link_types_compatible(link_a.type, link_b.type):
        raise RuntimeError("The selected Litec components are not compatible")
    if not Connections.links_are_aligned(
        obj_a,
        link_index_a,
        obj_b,
        link_index_b,
    ):
        raise RuntimeError("Unable to align the Litec structure exactly")
    if not Connections.connect_links(
        obj_a,
        link_index_a,
        obj_b,
        link_index_b,
    ):
        raise RuntimeError("Unable to connect the Litec structure")


def _add_litec_run(
    start_obj,
    start_link_index,
    plan,
    family,
    imported_objects,
    target_obj=None,
    target_link_index=None,
):
    current_obj = start_obj
    current_link_index = start_link_index
    connection_count = 0

    for segment in plan:
        asset_id = segment["assetId"]
        imported = LoadCatalogue.import_catalogue_asset(asset_id)
        imported_objects.extend(imported)
        segment_obj = _single_stagehand_object(imported, asset_id)
        segment_start_link = family["segmentStartLink"]

        if not Connections.align_object_link_to_target(
            segment_obj,
            segment_start_link,
            current_obj,
            current_link_index,
        ):
            raise RuntimeError("Unable to position a Litec straight segment")
        _connect_litec_pair(
            segment_obj,
            segment_start_link,
            current_obj,
            current_link_index,
        )
        connection_count += 1
        current_obj = segment_obj
        current_link_index = family["segmentEndLink"]

    if target_obj is not None:
        if target_link_index is None:
            raise RuntimeError("A target Litec link index is required")
        _connect_litec_pair(
            current_obj,
            current_link_index,
            target_obj,
            target_link_index,
        )
        connection_count += 1

    return connection_count


def build_litec_structure(context, definition, parameters):
    """Build a Litec portal or four-legged rectangular ring."""
    settings = definition.get("settings", {})
    structure_type_parameter = str(
        settings.get("structureTypeParameter", "")
    ).strip()
    if structure_type_parameter:
        structure_type = str(
            parameters.get(structure_type_parameter, "")
        ).strip().upper()
    else:
        structure_type = str(settings.get("structureType", "")).strip().upper()
    if structure_type not in {"PORTAL", "RING"}:
        raise ValueError("Litec structure type must be PORTAL or RING")

    family = _normalized_litec_family(definition, parameters)
    measurement_mode = str(parameters.get("measurementMode", "INTERNAL")).upper()
    max_run = float(settings.get("maxRun", 50.0))
    max_items = int(settings.get("maxItems", 500))
    if max_run <= 0.0 or max_items <= 0:
        raise ValueError("Litec structure recipe limits must be positive")

    requested_width = float(parameters["width"])
    requested_height = float(parameters["height"])
    straight_width = _litec_straight_length(
        requested_width,
        family["cubeSize"],
        2,
        measurement_mode,
        "width",
    )
    straight_height = _litec_straight_length(
        requested_height,
        family["cubeSize"],
        1,
        measurement_mode,
        "height",
    )
    width_plan = _litec_segment_plan(
        straight_width,
        family,
        "width",
        requested_width,
        measurement_mode,
        max_run,
    )
    height_plan = _litec_segment_plan(
        straight_height,
        family,
        "height",
        requested_height,
        measurement_mode,
        max_run,
    )

    cube_size = family["cubeSize"]
    cube_links = family["cubeLinks"]
    center_width = straight_width + cube_size
    requested_depth = None
    straight_depth = None
    depth_plan = None

    if structure_type == "PORTAL":
        cube_offsets = (
            (0.0, 0.0, straight_height),
            (center_width, 0.0, straight_height),
        )
        total_items = 2 + len(width_plan) + (2 * len(height_plan))
    else:
        requested_depth = float(parameters["depth"])
        straight_depth = _litec_straight_length(
            requested_depth,
            cube_size,
            2,
            measurement_mode,
            "depth",
        )
        depth_plan = _litec_segment_plan(
            straight_depth,
            family,
            "depth",
            requested_depth,
            measurement_mode,
            max_run,
        )
        center_depth = straight_depth + cube_size
        cube_offsets = (
            (0.0, 0.0, straight_height),
            (center_width, 0.0, straight_height),
            (center_width, center_depth, straight_height),
            (0.0, center_depth, straight_height),
        )
        total_items = (
            4
            + (2 * len(width_plan))
            + (2 * len(depth_plan))
            + (4 * len(height_plan))
        )

    if total_items > max_items:
        raise ValueError(
            f"This structure needs {total_items} items; "
            f"the recipe limit is {max_items}"
        )

    imported_objects = []
    connection_count = 0
    try:
        cubes = _place_litec_cubes(
            context,
            family,
            cube_offsets,
            imported_objects,
        )

        if structure_type == "PORTAL":
            connection_count += _add_litec_run(
                cubes[0],
                cube_links["xPositive"],
                width_plan,
                family,
                imported_objects,
                cubes[1],
                cube_links["xNegative"],
            )
            for cube in cubes:
                connection_count += _add_litec_run(
                    cube,
                    cube_links["bottom"],
                    height_plan,
                    family,
                    imported_objects,
                )
        else:
            for first, second in ((0, 1), (3, 2)):
                connection_count += _add_litec_run(
                    cubes[first],
                    cube_links["xPositive"],
                    width_plan,
                    family,
                    imported_objects,
                    cubes[second],
                    cube_links["xNegative"],
                )
            for first, second in ((0, 3), (1, 2)):
                connection_count += _add_litec_run(
                    cubes[first],
                    cube_links["yPositive"],
                    depth_plan,
                    family,
                    imported_objects,
                    cubes[second],
                    cube_links["yNegative"],
                )
            for cube in cubes:
                connection_count += _add_litec_run(
                    cube,
                    cube_links["bottom"],
                    height_plan,
                    family,
                    imported_objects,
                )
    except Exception:
        _remove_imported_objects(imported_objects)
        raise

    for selected in context.selected_objects:
        selected.select_set(False)
    for obj in imported_objects:
        obj.select_set(True)
    context.view_layer.objects.active = cubes[0]

    mode_label = "internal" if measurement_mode == "INTERNAL" else "external"
    if structure_type == "PORTAL":
        dimensions = f"{requested_width:g}m x {requested_height:g}m"
        structure_label = "portal"
    else:
        dimensions = (
            f"{requested_width:g}m x {requested_depth:g}m x "
            f"{requested_height:g}m"
        )
        structure_label = "ring"

    return (
        imported_objects,
        f"Added {family['label']} {structure_label} ({dimensions} {mode_label}, "
        f"{total_items} items, {connection_count} connections)",
    )


register_builder("litec_structure", build_litec_structure)


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
        for parameter_name, parameter in parameters.items():
            conditions = parameter.get("visibleWhen", {})
            if not isinstance(conditions, dict):
                raise TypeError(
                    f"Recipe parameter '{parameter_name}' visibleWhen must be an object"
                )
            if any(
                getattr(self, dependency_name, None) != expected_value
                for dependency_name, expected_value in conditions.items()
            ):
                continue
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
    global RECIPE_RESOURCES

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

    resources = data.get("resources", {})
    if not isinstance(resources, dict):
        raise TypeError("Recipe resources must be an object")

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

    RECIPE_RESOURCES = resources
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
