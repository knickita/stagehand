"""Data-driven recipes for complex Stagehand systems.

Recipes.json describes the UI, catalogue assets, and builder settings. This
module owns the executable builder functions. New reusable algorithms can be
added with register_builder(name, function) and selected by name in JSON.
Keeping executable code out of JSON makes recipe files safe and easy to edit.
"""

import json
from math import ceil, radians
from pathlib import Path
from time import perf_counter

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
            f"Catalogue asset {asset_id} must import exactly one Stagehand object; "
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
    return_endpoint=False,
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

    if return_endpoint:
        return connection_count, current_obj, current_link_index
    return connection_count


def _add_litec_heavy_base(
    leg_obj,
    leg_link_index,
    base_asset_id,
    base_link_index,
    imported_objects,
):
    imported = LoadCatalogue.import_catalogue_asset(base_asset_id)
    imported_objects.extend(imported)
    base_obj = _single_stagehand_object(imported, base_asset_id)
    if not Connections.align_object_link_to_target(
        base_obj,
        base_link_index,
        leg_obj,
        leg_link_index,
    ):
        raise RuntimeError("Unable to position a Litec heavy base")
    _connect_litec_pair(
        base_obj,
        base_link_index,
        leg_obj,
        leg_link_index,
    )
    return base_obj


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
    add_heavy_base = bool(parameters.get("heavyBase", False))
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
    heavy_base_asset_id = int(settings.get("heavyBaseAssetId", 10))
    heavy_base_link = int(settings.get("heavyBaseLink", 0))
    if add_heavy_base:
        heavy_base_asset = LoadCatalogue.CATALOGUE_BY_ID.get(heavy_base_asset_id)
        if heavy_base_asset is None:
            raise ValueError(
                f"Catalogue asset ID {heavy_base_asset_id} was not found"
            )
        try:
            heavy_base_asset["links"][heavy_base_link]
        except (IndexError, TypeError) as exc:
            raise ValueError(
                f"Heavy base link {heavy_base_link} is invalid for asset "
                f"{heavy_base_asset_id}"
            ) from exc
    center_width = straight_width + cube_size
    requested_depth = None
    straight_depth = None
    depth_plan = None

    if structure_type == "PORTAL":
        leg_count = 2
        cube_offsets = (
            (0.0, 0.0, straight_height),
            (center_width, 0.0, straight_height),
        )
        total_items = 2 + len(width_plan) + (2 * len(height_plan))
    else:
        leg_count = 4
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

    heavy_base_count = leg_count if add_heavy_base else 0
    total_items += heavy_base_count

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

        def add_vertical_leg(cube):
            result = _add_litec_run(
                cube,
                cube_links["bottom"],
                height_plan,
                family,
                imported_objects,
                return_endpoint=add_heavy_base,
            )
            if not add_heavy_base:
                return result

            run_connections, leg_obj, leg_link_index = result
            _add_litec_heavy_base(
                leg_obj,
                leg_link_index,
                heavy_base_asset_id,
                heavy_base_link,
                imported_objects,
            )
            return run_connections + 1

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
                connection_count += add_vertical_leg(cube)
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
                connection_count += add_vertical_leg(cube)
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
        f"{total_items} items, {connection_count} connections"
        f"{f', {heavy_base_count} heavy bases' if heavy_base_count else ''})",
    )


register_builder("litec_structure", build_litec_structure)


def _normalized_selvoline_settings(definition, parameters):
    settings = definition.get("settings", {})
    try:
        module_size = float(settings.get("moduleSize", 2.0))
        max_items = int(settings.get("maxItems", 2000))
        post_asset_ids = {
            str(key): int(value)
            for key, value in settings["postAssetIds"].items()
        }
        asset_settings = {
            "xBeamAssetId": int(settings["xBeamAssetId"]),
            "yBeamAssetId": int(settings["yBeamAssetId"]),
            "bearerAssetId": int(settings["bearerAssetId"]),
            "boardAssetId": int(settings["boardAssetId"]),
        }
        post_links = {
            str(key): int(value)
            for key, value in settings["postLinks"].items()
        }
        x_beam_bearer_links = tuple(
            int(value) for value in settings["xBeamBearerLinks"]
        )
        bearer_board_links = tuple(
            int(value) for value in settings["bearerBoardLinks"]
        )
        railing_assets = {
            str(key): int(value)
            for key, value in settings["railingAssets"].items()
        }
        railing_links = settings["railingLinks"]
        single_railing_links = tuple(
            int(value) for value in railing_links["single"]
        )
        double_positive_railing_links = tuple(
            int(value) for value in railing_links["doublePositive"]
        )
        double_negative_railing_links = tuple(
            int(value) for value in railing_links["doubleNegative"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid Selvoline recipe settings") from exc

    height_key = str(parameters.get("stageHeight", ""))
    post_asset_id = post_asset_ids.get(height_key)
    if post_asset_id is None:
        raise ValueError("Selvoline stage height must be 50cm or 150cm")

    railings_mode = str(parameters.get("railings", "NONE"))
    if railings_mode not in {"NONE", "THREE_SIDES", "FOUR_SIDES"}:
        raise ValueError("Invalid Selvoline railings option")

    asset_ids = {post_asset_id, *asset_settings.values(), *railing_assets.values()}
    missing_asset_ids = sorted(
        asset_id
        for asset_id in asset_ids
        if asset_id not in LoadCatalogue.CATALOGUE_BY_ID
    )
    if missing_asset_ids:
        raise ValueError(
            "Selvoline recipe references missing catalogue assets: "
            + ", ".join(str(asset_id) for asset_id in missing_asset_ids)
        )
    if module_size <= 0.0 or max_items <= 0:
        raise ValueError("Invalid Selvoline recipe limits")
    if len(x_beam_bearer_links) != 2 or len(bearer_board_links) != 4:
        raise ValueError("Invalid Selvoline connection layout")
    if not all(
        len(link_group) == 3
        for link_group in (
            single_railing_links,
            double_positive_railing_links,
            double_negative_railing_links,
        )
    ):
        raise ValueError("Invalid Selvoline railing connection layout")

    return {
        "moduleSize": module_size,
        "maxItems": max_items,
        "postAssetId": post_asset_id,
        **asset_settings,
        "postLinks": post_links,
        "beamStartLink": int(settings.get("beamStartLink", 0)),
        "beamEndLink": int(settings.get("beamEndLink", 1)),
        "xBeamBearerLinks": x_beam_bearer_links,
        "bearerStartLink": int(settings.get("bearerStartLink", 0)),
        "bearerEndLink": int(settings.get("bearerEndLink", 1)),
        "bearerBoardLinks": bearer_board_links,
        "boardLink": int(settings.get("boardLink", 0)),
        "railingsMode": railings_mode,
        "railingAssets": railing_assets,
        "railingLinks": {
            "postBase": int(railing_links.get("postBase", 0)),
            "single": single_railing_links,
            "doublePositive": double_positive_railing_links,
            "doubleNegative": double_negative_railing_links,
            "railStart": int(railing_links.get("railStart", 0)),
            "railEnd": int(railing_links.get("railEnd", 1)),
        },
    }


def _selvoline_module_count(value, dimension_name, module_size):
    try:
        dimension = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Selvoline {dimension_name} must be a number") from exc

    module_count = round(dimension / module_size)
    if dimension > 0.0 and abs(dimension - module_count * module_size) <= 0.0001:
        return module_count, module_count * module_size

    lower_count = max(1, int(dimension // module_size))
    if lower_count * module_size >= dimension - 0.0001:
        lower_count = max(1, lower_count - 1)
    upper_count = max(1, lower_count + 1)
    raise ValueError(
        f"Selvoline {dimension_name} must be a multiple of {module_size:g}m. "
        f"Available dimensions: {lower_count * module_size:g}m or "
        f"{upper_count * module_size:g}m"
    )


def _import_selvoline_object(asset_id, imported_objects):
    imported = LoadCatalogue.import_catalogue_asset(asset_id)
    imported_objects.extend(imported)
    return _single_stagehand_object(imported, asset_id)


def _connect_selvoline_pair(obj_a, link_index_a, obj_b, link_index_b):
    link_a = Connections.get_link(obj_a, link_index_a)
    link_b = Connections.get_link(obj_b, link_index_b)
    if link_a is None or link_b is None:
        raise RuntimeError("Unable to find a Selvoline connection link")
    if not are_link_types_compatible(link_a.type, link_b.type):
        raise RuntimeError("The selected Selvoline components are not compatible")
    if not Connections.links_are_aligned(obj_a, link_index_a, obj_b, link_index_b):
        raise RuntimeError("Unable to align the Selvoline structure exactly")
    if not Connections.connect_links(obj_a, link_index_a, obj_b, link_index_b):
        raise RuntimeError("Unable to connect the Selvoline structure")


def _add_selvoline_between(
    asset_id,
    start_obj,
    start_link_index,
    end_obj,
    end_link_index,
    component_start_link,
    component_end_link,
    imported_objects,
):
    component = _import_selvoline_object(asset_id, imported_objects)
    if not Connections.align_object_link_to_target(
        component,
        component_start_link,
        start_obj,
        start_link_index,
    ):
        raise RuntimeError("Unable to position a Selvoline component")
    _connect_selvoline_pair(
        component,
        component_start_link,
        start_obj,
        start_link_index,
    )
    _connect_selvoline_pair(
        component,
        component_end_link,
        end_obj,
        end_link_index,
    )
    return component


def _selvoline_railing_runs(width_count, depth_count, mode):
    if mode == "NONE":
        return []

    runs = [
        {
            "name": "back",
            "coordinates": [
                (x_index, depth_count)
                for x_index in range(width_count + 1)
            ],
            "postLink": "yPositive",
            "startPost": "rightPost",
            "endPost": "leftPost",
            "positiveLinks": "doublePositive",
            "negativeLinks": "doubleNegative",
        },
        {
            "name": "left",
            "coordinates": [
                (0, y_index)
                for y_index in range(depth_count + 1)
            ],
            "postLink": "xNegative",
            "startPost": "rightPost",
            "endPost": "leftPost",
            "positiveLinks": "doublePositive",
            "negativeLinks": "doubleNegative",
        },
        {
            "name": "right",
            "coordinates": [
                (width_count, y_index)
                for y_index in range(depth_count + 1)
            ],
            "postLink": "xPositive",
            "startPost": "leftPost",
            "endPost": "rightPost",
            "positiveLinks": "doubleNegative",
            "negativeLinks": "doublePositive",
        },
    ]
    if mode == "FOUR_SIDES":
        runs.append(
            {
                "name": "front",
                "coordinates": [
                    (x_index, 0)
                    for x_index in range(width_count + 1)
                ],
                "postLink": "yNegative",
                "startPost": "leftPost",
                "endPost": "rightPost",
                "positiveLinks": "doubleNegative",
                "negativeLinks": "doublePositive",
            }
        )
    return runs


def _add_selvoline_railing_run(
    run,
    posts,
    settings,
    imported_objects,
):
    railing_assets = settings["railingAssets"]
    railing_links = settings["railingLinks"]
    post_link_index = settings["postLinks"][run["postLink"]]
    coordinates = run["coordinates"]
    mounts = []

    for node_index, coordinates_at_node in enumerate(coordinates):
        if node_index == 0:
            mount_asset_id = railing_assets[run["startPost"]]
            positive_links = railing_links["single"]
            negative_links = None
        elif node_index == len(coordinates) - 1:
            mount_asset_id = railing_assets[run["endPost"]]
            positive_links = None
            negative_links = railing_links["single"]
        else:
            mount_asset_id = railing_assets["doublePost"]
            positive_links = railing_links[run["positiveLinks"]]
            negative_links = railing_links[run["negativeLinks"]]

        mount = _import_selvoline_object(mount_asset_id, imported_objects)
        if not Connections.align_object_link_to_target(
            mount,
            railing_links["postBase"],
            posts[coordinates_at_node],
            post_link_index,
        ):
            raise RuntimeError(
                f"Unable to position a Selvoline railing post on the {run['name']} side"
            )
        _connect_selvoline_pair(
            mount,
            railing_links["postBase"],
            posts[coordinates_at_node],
            post_link_index,
        )
        mounts.append(
            {
                "object": mount,
                "positiveLinks": positive_links,
                "negativeLinks": negative_links,
            }
        )

    rail_assets = (
        railing_assets["toeBoard"],
        railing_assets["centerRail"],
        railing_assets["highRail"],
    )
    rail_count = 0
    for segment_index in range(len(mounts) - 1):
        start_mount = mounts[segment_index]
        end_mount = mounts[segment_index + 1]
        for rail_asset_id, start_link, end_link in zip(
            rail_assets,
            start_mount["positiveLinks"],
            end_mount["negativeLinks"],
        ):
            _add_selvoline_between(
                rail_asset_id,
                start_mount["object"],
                start_link,
                end_mount["object"],
                end_link,
                railing_links["railStart"],
                railing_links["railEnd"],
                imported_objects,
            )
            rail_count += 1

    return len(mounts), rail_count


def build_selvoline_stage(context, definition, parameters):
    """Build a modular Selvoline stage with one bearer and four boards per bay."""
    settings = _normalized_selvoline_settings(definition, parameters)
    module_size = settings["moduleSize"]
    width_count, width = _selvoline_module_count(
        parameters.get("width"), "width", module_size
    )
    depth_count, depth = _selvoline_module_count(
        parameters.get("depth"), "depth", module_size
    )

    post_count = (width_count + 1) * (depth_count + 1)
    x_beam_count = width_count * (depth_count + 1)
    y_beam_count = depth_count * (width_count + 1)
    bearer_count = width_count * depth_count
    board_count = bearer_count * len(settings["bearerBoardLinks"])
    railing_runs = _selvoline_railing_runs(
        width_count,
        depth_count,
        settings["railingsMode"],
    )
    railing_post_count = sum(len(run["coordinates"]) for run in railing_runs)
    railing_segment_count = sum(
        len(run["coordinates"]) - 1
        for run in railing_runs
    )
    railing_component_count = railing_segment_count * 3
    total_items = (
        post_count
        + x_beam_count
        + y_beam_count
        + bearer_count
        + board_count
        + railing_post_count
        + railing_component_count
    )
    if total_items > settings["maxItems"]:
        raise ValueError(
            f"This Selvoline stage needs {total_items} items; "
            f"the recipe limit is {settings['maxItems']}"
        )

    imported_objects = []
    posts = {}
    x_beams = {}
    connection_count = 0
    structure_matrix = None
    post_links = settings["postLinks"]

    try:
        for y_index in range(depth_count + 1):
            for x_index in range(width_count + 1):
                post = _import_selvoline_object(settings["postAssetId"], imported_objects)
                if structure_matrix is None:
                    structure_matrix = post.matrix_world.copy()
                    structure_matrix.translation = context.scene.cursor.location
                post.matrix_world = structure_matrix.copy()
                post.matrix_world.translation = (
                    structure_matrix.translation
                    + structure_matrix.to_quaternion()
                    @ Vector((x_index * module_size, y_index * module_size, 0.0))
                )
                posts[(x_index, y_index)] = post

        for y_index in range(depth_count + 1):
            for x_index in range(width_count):
                x_beams[(x_index, y_index)] = _add_selvoline_between(
                    settings["xBeamAssetId"],
                    posts[(x_index, y_index)], post_links["xPositive"],
                    posts[(x_index + 1, y_index)], post_links["xNegative"],
                    settings["beamStartLink"], settings["beamEndLink"],
                    imported_objects,
                )
                connection_count += 2

        for x_index in range(width_count + 1):
            for y_index in range(depth_count):
                _add_selvoline_between(
                    settings["yBeamAssetId"],
                    posts[(x_index, y_index)], post_links["yPositive"],
                    posts[(x_index, y_index + 1)], post_links["yNegative"],
                    settings["beamStartLink"], settings["beamEndLink"],
                    imported_objects,
                )
                connection_count += 2

        front_bearer_link, back_bearer_link = settings["xBeamBearerLinks"]
        for y_index in range(depth_count):
            for x_index in range(width_count):
                bearer = _add_selvoline_between(
                    settings["bearerAssetId"],
                    x_beams[(x_index, y_index)], front_bearer_link,
                    x_beams[(x_index, y_index + 1)], back_bearer_link,
                    settings["bearerStartLink"], settings["bearerEndLink"],
                    imported_objects,
                )
                connection_count += 2

                for bearer_board_link in settings["bearerBoardLinks"]:
                    board = _import_selvoline_object(
                        settings["boardAssetId"], imported_objects
                    )
                    if not Connections.align_object_link_to_target(
                        board, settings["boardLink"], bearer, bearer_board_link
                    ):
                        raise RuntimeError("Unable to position a Selvoline board")
                    _connect_selvoline_pair(
                        board, settings["boardLink"], bearer, bearer_board_link
                    )
                    connection_count += 1

        for railing_run in railing_runs:
            added_posts, added_rails = _add_selvoline_railing_run(
                railing_run,
                posts,
                settings,
                imported_objects,
            )
            connection_count += added_posts + (added_rails * 2)
    except Exception:
        _remove_imported_objects(imported_objects)
        raise

    for selected in context.selected_objects:
        selected.select_set(False)
    for obj in imported_objects:
        obj.select_set(True)
    context.view_layer.objects.active = posts[(0, 0)]

    height_label = "50cm" if str(parameters["stageHeight"]) == "H50" else "150cm"
    railings_label = {
        "NONE": "senza mancorrenti",
        "THREE_SIDES": "mancorrenti su 3 lati",
        "FOUR_SIDES": "mancorrenti su 4 lati",
    }[settings["railingsMode"]]
    return (
        imported_objects,
        f"Aggiunto palco Selvoline {width:g}m x {depth:g}m, "
        f"altezza {height_label}, {railings_label} ({total_items} elementi, "
        f"{connection_count} connessioni)",
    )


register_builder("selvoline_stage", build_selvoline_stage)


def _layher_asset_link(asset_id, link_index, label):
    asset = LoadCatalogue.CATALOGUE_BY_ID.get(asset_id)
    if asset is None:
        raise ValueError(f"Catalogue asset ID {asset_id} was not found")
    try:
        asset["links"][link_index]
    except (IndexError, TypeError) as exc:
        raise ValueError(
            f"Layher {label} link {link_index} is invalid for asset {asset_id}"
        ) from exc


def _normalized_layher_settings(definition, parameters):
    settings = definition.get("settings", {})
    try:
        base_asset_id = int(settings["baseAssetId"])
        vertical_asset_id = int(settings["verticalAssetId"])
        vertical_height = float(settings["verticalModuleHeight"])
        base_vertical_link = int(settings["baseVerticalLink"])
        vertical_bottom_link = int(settings["verticalBottomLink"])
        vertical_top_link = int(settings["verticalTopLink"])
        horizontal_start_link = int(settings["horizontalStartLink"])
        horizontal_end_link = int(settings["horizontalEndLink"])
        diagonal_lower_link = int(settings["diagonalLowerLink"])
        diagonal_upper_link = int(settings["diagonalUpperLink"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("The Layher recipe settings are incomplete") from exc

    if vertical_height <= 0.0:
        raise ValueError("Layher verticalModuleHeight must be positive")

    cardinal_directions = (
        "xNegative",
        "xPositive",
        "yNegative",
        "yPositive",
    )
    diagonal_directions = (
        "positivePositive",
        "negativePositive",
        "positiveNegative",
        "negativeNegative",
    )

    def link_mapping(setting_name, required_keys):
        raw_mapping = settings.get(setting_name)
        if not isinstance(raw_mapping, dict):
            raise ValueError(f"The Layher recipe requires {setting_name}")
        mapping = {}
        for key in required_keys:
            try:
                mapping[key] = int(raw_mapping[key])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"The Layher link '{setting_name}.{key}' is invalid"
                ) from exc
        return mapping

    base_links = link_mapping("baseRosetteLinks", cardinal_directions)
    vertical_links = link_mapping(
        "verticalTopRosetteLinks",
        cardinal_directions,
    )
    base_diagonal_links = link_mapping(
        "baseDiagonalLinks",
        diagonal_directions,
    )
    vertical_diagonal_links = link_mapping(
        "verticalTopDiagonalLinks",
        diagonal_directions,
    )

    raw_horizontal_modules = settings.get("horizontalModules")
    if not isinstance(raw_horizontal_modules, dict) or not raw_horizontal_modules:
        raise ValueError("The Layher recipe requires horizontalModules")

    def horizontal_module(parameter_name):
        module_key = str(parameters.get(parameter_name, ""))
        raw_module = raw_horizontal_modules.get(module_key)
        if not isinstance(raw_module, dict):
            raise ValueError(f"Unknown Layher module '{module_key}'")
        try:
            asset_id = int(raw_module["assetId"])
            diagonal_asset_id = int(raw_module["diagonalAssetId"])
            length = float(raw_module["length"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Layher module '{module_key}' is invalid") from exc
        if length <= 0.0:
            raise ValueError(f"Layher module '{module_key}' length must be positive")
        _layher_asset_link(asset_id, horizontal_start_link, "horizontal start")
        _layher_asset_link(asset_id, horizontal_end_link, "horizontal end")
        _layher_asset_link(
            diagonal_asset_id,
            diagonal_lower_link,
            "diagonal lower",
        )
        _layher_asset_link(
            diagonal_asset_id,
            diagonal_upper_link,
            "diagonal upper",
        )
        return {
            "key": module_key,
            "assetId": asset_id,
            "diagonalAssetId": diagonal_asset_id,
            "length": length,
        }

    _layher_asset_link(base_asset_id, base_vertical_link, "base vertical")
    _layher_asset_link(vertical_asset_id, vertical_bottom_link, "vertical bottom")
    _layher_asset_link(vertical_asset_id, vertical_top_link, "vertical top")
    for direction in cardinal_directions:
        _layher_asset_link(
            base_asset_id,
            base_links[direction],
            f"base {direction}",
        )
        _layher_asset_link(
            vertical_asset_id,
            vertical_links[direction],
            f"vertical {direction}",
        )
    for direction in diagonal_directions:
        _layher_asset_link(
            base_asset_id,
            base_diagonal_links[direction],
            f"base diagonal {direction}",
        )
        _layher_asset_link(
            vertical_asset_id,
            vertical_diagonal_links[direction],
            f"vertical diagonal {direction}",
        )

    return {
        "baseAssetId": base_asset_id,
        "verticalAssetId": vertical_asset_id,
        "verticalModuleHeight": vertical_height,
        "baseVerticalLink": base_vertical_link,
        "verticalBottomLink": vertical_bottom_link,
        "verticalTopLink": vertical_top_link,
        "horizontalStartLink": horizontal_start_link,
        "horizontalEndLink": horizontal_end_link,
        "diagonalLowerLink": diagonal_lower_link,
        "diagonalUpperLink": diagonal_upper_link,
        "baseRosetteLinks": base_links,
        "verticalTopRosetteLinks": vertical_links,
        "baseDiagonalLinks": base_diagonal_links,
        "verticalTopDiagonalLinks": vertical_diagonal_links,
        "widthModule": horizontal_module("widthModule"),
        "depthModule": horizontal_module("depthModule"),
        "maxItems": int(settings.get("maxItems", 1000)),
        "maxModuleCount": int(settings.get("maxModuleCount", 50)),
    }

def _connect_layher_pair(obj_a, link_index_a, obj_b, link_index_b):
    link_a = Connections.get_link(obj_a, link_index_a)
    link_b = Connections.get_link(obj_b, link_index_b)
    if link_a is None or link_b is None:
        raise RuntimeError("Unable to find a Layher connection link")
    if not are_link_types_compatible(link_a.type, link_b.type):
        raise RuntimeError("The selected Layher components are not compatible")
    if not Connections.links_are_aligned(
        obj_a,
        link_index_a,
        obj_b,
        link_index_b,
    ):
        raise RuntimeError("Unable to align the Layher structure exactly")
    if not Connections.connect_links(
        obj_a,
        link_index_a,
        obj_b,
        link_index_b,
    ):
        raise RuntimeError("Unable to connect the Layher structure")


class _LayherConsoleProfiler:
    def __init__(self, width_count, height_count, depth_count):
        self.started_at = perf_counter()
        self.width_count = width_count
        self.height_count = height_count
        self.depth_count = depth_count
        self.current_phase = None
        self.current_phase_started_at = None
        self.phases = []
        self.imports = {}

    def start_phase(self, name):
        self._finish_current_phase()
        self.current_phase = name
        self.current_phase_started_at = perf_counter()

    def _finish_current_phase(self):
        if self.current_phase is None:
            return
        elapsed = perf_counter() - self.current_phase_started_at
        self.phases.append((self.current_phase, elapsed))
        self.current_phase = None
        self.current_phase_started_at = None

    def record_import(self, asset_id, elapsed):
        entry = self.imports.setdefault(asset_id, {"calls": 0, "elapsed": 0.0})
        entry["calls"] += 1
        entry["elapsed"] += elapsed

    def print_report(self, status, total_items):
        self._finish_current_phase()
        total_elapsed = perf_counter() - self.started_at
        import_elapsed = sum(item["elapsed"] for item in self.imports.values())
        other_elapsed = max(0.0, total_elapsed - import_elapsed)

        print("\n[Stagehand][Layher profiler]")
        print(f"Stato: {status}")
        print(
            "Moduli: "
            f"larghezza={self.width_count}, altezza={self.height_count}, "
            f"profondita={self.depth_count} | elementi={total_items}"
        )
        print(f"Tempo totale: {total_elapsed:.3f} s")
        print("Fasi:")
        for name, elapsed in self.phases:
            percentage = elapsed / total_elapsed * 100.0 if total_elapsed else 0.0
            print(f"  {name:<16} {elapsed:8.3f} s  ({percentage:5.1f}%)")

        import_percentage = (
            import_elapsed / total_elapsed * 100.0 if total_elapsed else 0.0
        )
        print(
            f"Cache/istanze:    {import_elapsed:8.3f} s  "
            f"({import_percentage:5.1f}%)"
        )
        print("Dettaglio cache/istanze:")
        for asset_id, item in sorted(
            self.imports.items(),
            key=lambda pair: pair[1]["elapsed"],
            reverse=True,
        ):
            asset = LoadCatalogue.CATALOGUE_BY_ID.get(asset_id, {})
            asset_name = asset.get("name", "asset sconosciuto")
            average = item["elapsed"] / item["calls"]
            print(
                f"  #{asset_id:<3} {asset_name:<28} "
                f"{item['calls']:4d} chiamate  "
                f"totale {item['elapsed']:8.3f} s  media {average:.4f} s"
            )
        print(
            "Altro (posizionamento, connessioni e aggiornamenti scena): "
            f"{other_elapsed:.3f} s"
        )
        print("[Stagehand][Layher profiler fine]\n")


def _import_layher_object(asset_id, imported_objects, profiler=None):
    started_at = perf_counter()
    try:
        imported = LoadCatalogue.import_catalogue_asset(asset_id)
    finally:
        if profiler is not None:
            profiler.record_import(asset_id, perf_counter() - started_at)
    imported_objects.extend(imported)
    return _single_stagehand_object(imported, asset_id)


def _add_layher_horizontal(
    module,
    start_obj,
    start_link_index,
    end_obj,
    end_link_index,
    settings,
    imported_objects,
    profiler=None,
):
    horizontal = _import_layher_object(
        module["assetId"],
        imported_objects,
        profiler,
    )
    start_link = settings["horizontalStartLink"]
    end_link = settings["horizontalEndLink"]
    if not Connections.align_object_link_to_target(
        horizontal,
        start_link,
        start_obj,
        start_link_index,
    ):
        raise RuntimeError("Unable to position a Layher horizontal")
    _connect_layher_pair(
        horizontal,
        start_link,
        start_obj,
        start_link_index,
    )
    _connect_layher_pair(
        horizontal,
        end_link,
        end_obj,
        end_link_index,
    )
    return horizontal


def _add_layher_diagonal(
    module,
    lower_obj,
    lower_link_index,
    upper_obj,
    upper_link_index,
    settings,
    imported_objects,
    profiler=None,
):
    diagonal = _import_layher_object(
        module["diagonalAssetId"],
        imported_objects,
        profiler,
    )
    lower_link = settings["diagonalLowerLink"]
    upper_link = settings["diagonalUpperLink"]
    if not Connections.align_object_link_to_target(
        diagonal,
        lower_link,
        lower_obj,
        lower_link_index,
    ):
        raise RuntimeError("Unable to position a Layher diagonal")
    _connect_layher_pair(
        diagonal,
        lower_link,
        lower_obj,
        lower_link_index,
    )
    _connect_layher_pair(
        diagonal,
        upper_link,
        upper_obj,
        upper_link_index,
    )
    return diagonal

def build_layher_grid(context, definition, parameters):
    """Build a three-dimensional Layher frame from bases, standards, and ledgers."""
    settings = _normalized_layher_settings(definition, parameters)
    try:
        width_count = int(parameters["widthCount"])
        height_count = int(parameters["heightCount"])
        depth_count = int(parameters["depthCount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Layher module counts must be integers") from exc

    counts = {
        "width": width_count,
        "height": height_count,
        "depth": depth_count,
    }
    for dimension_name, count in counts.items():
        if count <= 0:
            raise ValueError(f"Layher {dimension_name} module count must be positive")
        if count > settings["maxModuleCount"]:
            raise ValueError(
                f"Layher {dimension_name} module count exceeds the recipe limit "
                f"of {settings['maxModuleCount']}"
            )

    node_count = (width_count + 1) * (depth_count + 1)
    base_count = node_count
    vertical_count = node_count * height_count
    width_horizontal_count = (
        width_count * (depth_count + 1) * (height_count + 1)
    )
    depth_horizontal_count = (
        depth_count * (width_count + 1) * (height_count + 1)
    )
    horizontal_count = width_horizontal_count + depth_horizontal_count
    width_diagonal_count = (
        width_count * (depth_count + 1) * height_count
    )
    depth_diagonal_count = (
        depth_count * (width_count + 1) * height_count
    )
    diagonal_count = width_diagonal_count + depth_diagonal_count
    total_items = (
        base_count + vertical_count + horizontal_count + diagonal_count
    )
    if total_items > settings["maxItems"]:
        raise ValueError(
            f"This Layher structure needs {total_items} items; "
            f"the recipe limit is {settings['maxItems']}"
        )

    width_step = settings["widthModule"]["length"]
    depth_step = settings["depthModule"]["length"]
    imported_objects = []
    bases = {}
    verticals = {}
    connection_count = 0
    structure_matrix = None
    profiler = _LayherConsoleProfiler(
        width_count,
        height_count,
        depth_count,
    )

    profiler.start_phase("Basette")
    try:
        for y_index in range(depth_count + 1):
            for x_index in range(width_count + 1):
                base = _import_layher_object(
                    settings["baseAssetId"],
                    imported_objects,
                    profiler,
                )
                if structure_matrix is None:
                    structure_matrix = base.matrix_world.copy()
                    structure_matrix.translation = context.scene.cursor.location
                base.matrix_world = structure_matrix.copy()
                base.matrix_world.translation = (
                    structure_matrix.translation
                    + structure_matrix.to_quaternion()
                    @ Vector((x_index * width_step, y_index * depth_step, 0.0))
                )
                bases[(x_index, y_index)] = base

        profiler.start_phase("Montanti")
        for y_index in range(depth_count + 1):
            for x_index in range(width_count + 1):
                current_obj = bases[(x_index, y_index)]
                current_link = settings["baseVerticalLink"]
                for level in range(height_count):
                    vertical = _import_layher_object(
                        settings["verticalAssetId"],
                        imported_objects,
                        profiler,
                    )
                    bottom_link = settings["verticalBottomLink"]
                    if not Connections.align_object_link_to_target(
                        vertical,
                        bottom_link,
                        current_obj,
                        current_link,
                    ):
                        raise RuntimeError("Unable to position a Layher vertical")
                    _connect_layher_pair(
                        vertical,
                        bottom_link,
                        current_obj,
                        current_link,
                    )
                    connection_count += 1
                    verticals[(x_index, y_index, level)] = vertical
                    current_obj = vertical
                    current_link = settings["verticalTopLink"]

        def rosette_site(x_index, y_index, level, direction):
            if level == 0:
                return (
                    bases[(x_index, y_index)],
                    settings["baseRosetteLinks"][direction],
                )
            return (
                verticals[(x_index, y_index, level - 1)],
                settings["verticalTopRosetteLinks"][direction],
            )

        def diagonal_site(x_index, y_index, level, direction):
            if level == 0:
                return (
                    bases[(x_index, y_index)],
                    settings["baseDiagonalLinks"][direction],
                )
            return (
                verticals[(x_index, y_index, level - 1)],
                settings["verticalTopDiagonalLinks"][direction],
            )

        profiler.start_phase("Correnti")
        for level in range(height_count + 1):
            for y_index in range(depth_count + 1):
                for x_index in range(width_count):
                    start_obj, start_link = rosette_site(
                        x_index,
                        y_index,
                        level,
                        "xPositive",
                    )
                    end_obj, end_link = rosette_site(
                        x_index + 1,
                        y_index,
                        level,
                        "xNegative",
                    )
                    _add_layher_horizontal(
                        settings["widthModule"],
                        start_obj,
                        start_link,
                        end_obj,
                        end_link,
                        settings,
                        imported_objects,
                        profiler,
                    )
                    connection_count += 2

            for x_index in range(width_count + 1):
                for y_index in range(depth_count):
                    start_obj, start_link = rosette_site(
                        x_index,
                        y_index,
                        level,
                        "yPositive",
                    )
                    end_obj, end_link = rosette_site(
                        x_index,
                        y_index + 1,
                        level,
                        "yNegative",
                    )
                    _add_layher_horizontal(
                        settings["depthModule"],
                        start_obj,
                        start_link,
                        end_obj,
                        end_link,
                        settings,
                        imported_objects,
                        profiler,
                    )
                    connection_count += 2

        profiler.start_phase("Diagonali")
        diagonal_orientation_cycle = (
            (True, True),
            (True, False),
            (False, False),
            (False, True),
        )
        for level in range(height_count):
            x_positive, y_positive = diagonal_orientation_cycle[
                level % len(diagonal_orientation_cycle)
            ]

            for y_index in range(depth_count + 1):
                for x_index in range(width_count):
                    if x_positive:
                        lower_coordinates = (x_index, y_index)
                        upper_coordinates = (x_index + 1, y_index)
                        lower_direction = "positivePositive"
                        upper_direction = "negativePositive"
                    else:
                        lower_coordinates = (x_index + 1, y_index)
                        upper_coordinates = (x_index, y_index)
                        lower_direction = "negativeNegative"
                        upper_direction = "positiveNegative"

                    lower_obj, lower_link = diagonal_site(
                        *lower_coordinates,
                        level,
                        lower_direction,
                    )
                    upper_obj, upper_link = diagonal_site(
                        *upper_coordinates,
                        level + 1,
                        upper_direction,
                    )
                    _add_layher_diagonal(
                        settings["widthModule"],
                        lower_obj,
                        lower_link,
                        upper_obj,
                        upper_link,
                        settings,
                        imported_objects,
                        profiler,
                    )
                    connection_count += 2

            for x_index in range(width_count + 1):
                for y_index in range(depth_count):
                    if y_positive:
                        lower_coordinates = (x_index, y_index)
                        upper_coordinates = (x_index, y_index + 1)
                        lower_direction = "negativePositive"
                        upper_direction = "negativeNegative"
                    else:
                        lower_coordinates = (x_index, y_index + 1)
                        upper_coordinates = (x_index, y_index)
                        lower_direction = "positiveNegative"
                        upper_direction = "positivePositive"

                    lower_obj, lower_link = diagonal_site(
                        *lower_coordinates,
                        level,
                        lower_direction,
                    )
                    upper_obj, upper_link = diagonal_site(
                        *upper_coordinates,
                        level + 1,
                        upper_direction,
                    )
                    _add_layher_diagonal(
                        settings["depthModule"],
                        lower_obj,
                        lower_link,
                        upper_obj,
                        upper_link,
                        settings,
                        imported_objects,
                        profiler,
                    )
                    connection_count += 2
    except Exception as exc:
        profiler.print_report(
            f"errore: {type(exc).__name__}: {exc}",
            total_items,
        )
        _remove_imported_objects(imported_objects)
        raise

    profiler.start_phase("Selezione")
    for selected in context.selected_objects:
        selected.select_set(False)
    for obj in imported_objects:
        obj.select_set(True)
    context.view_layer.objects.active = bases[(0, 0)]
    profiler.print_report("completato", total_items)

    width = width_count * width_step
    depth = depth_count * depth_step
    module_height = height_count * settings["verticalModuleHeight"]
    return (
        imported_objects,
        f"Aggiunta struttura Layher {width_count}x{height_count}x{depth_count} "
        f"moduli ({width:g}m x {depth:g}m x {module_height:g}m più basette, "
        f"{total_items} elementi, {diagonal_count} diagonali, "
        f"{connection_count} connessioni)",
    )


register_builder("layher_grid", build_layher_grid)

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
        if "step" in parameter:
            options["step"] = int(parameter["step"])
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
            with Connections.database_transaction():
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
