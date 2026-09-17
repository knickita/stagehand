import csv
import json
from collections import Counter
from pathlib import Path


SPECIAL_ITEM_KEYS = ("litecPin", "litecSpigot", "litecSpring")
CUBE_SPECIAL_ITEM_PREFIXES = {9: "litec30Dado", 34: "litec40Dado"}
CUBE_CATEGORY_SUFFIXES = {
    "1 via": "1Way",
    "2 vie dritte": "2WayLine",
    "2 vie ad angolo": "2WayL",
    "3 vie a T": "3WayT",
    "3 vie ad angolo": "3WayL",
    "4 vie a T": "4WayL",
    "4 vie a croce": "4WayCross",
    "5 vie": "5Way",
    "6 vie": "6Way",
}


class RentmanConfigError(ValueError):
    """Raised when the Rentman CSV configuration is invalid."""


def _stagehand_id(raw_id):
    if isinstance(raw_id, bool):
        raise RentmanConfigError(f"Invalid Stagehand item ID: {raw_id!r}")

    try:
        asset_id = int(raw_id)
    except (TypeError, ValueError) as exc:
        raise RentmanConfigError(f"Invalid Stagehand item ID: {raw_id!r}") from exc

    if asset_id < 0 or str(asset_id) != str(raw_id).strip():
        raise RentmanConfigError(f"Invalid Stagehand item ID: {raw_id!r}")
    return asset_id


def _equipment_code(raw_code, stagehand_id):
    if isinstance(raw_code, bool) or raw_code is None:
        raise RentmanConfigError(
            f"Equipment code for Stagehand item {stagehand_id} must not be empty"
        )

    equipment_code = str(raw_code).strip()
    if not equipment_code:
        raise RentmanConfigError(
            f"Equipment code for Stagehand item {stagehand_id} must not be empty"
        )
    if "\n" in equipment_code or "\r" in equipment_code:
        raise RentmanConfigError(
            f"Equipment code for Stagehand item {stagehand_id} cannot contain a line break"
        )
    return equipment_code


def parse_export_config(config):
    if not isinstance(config, dict):
        raise RentmanConfigError("The configuration root must be a JSON object")

    raw_mappings = config.get("itemMappings")
    if not isinstance(raw_mappings, dict) or not raw_mappings:
        raise RentmanConfigError("'itemMappings' must be a non-empty JSON object")

    item_mappings = {}
    equipment_owners = {}
    for raw_stagehand_id, raw_equipment_code in raw_mappings.items():
        stagehand_id = _stagehand_id(raw_stagehand_id)
        if stagehand_id in item_mappings:
            raise RentmanConfigError(
                f"Stagehand item ID {stagehand_id} is mapped more than once"
            )

        equipment_code = _equipment_code(raw_equipment_code, stagehand_id)
        existing_owner = equipment_owners.get(equipment_code)
        if existing_owner is not None:
            raise RentmanConfigError(
                f"Equipment code {equipment_code!r} is mapped from both Stagehand "
                f"items {existing_owner} and {stagehand_id}"
            )

        item_mappings[stagehand_id] = equipment_code
        equipment_owners[equipment_code] = stagehand_id

    raw_special_items = config.get("specialItems")
    if not isinstance(raw_special_items, dict):
        raise RentmanConfigError("'specialItems' must be a JSON object")

    special_items = {}
    for key in SPECIAL_ITEM_KEYS:
        if key not in raw_special_items:
            raise RentmanConfigError(f"Missing specialItems entry: {key}")

    for key, raw_equipment_code in raw_special_items.items():
        equipment_code = _equipment_code(raw_equipment_code, f"specialItems.{key}")
        existing_owner = equipment_owners.get(equipment_code)
        if existing_owner is not None:
            raise RentmanConfigError(
                f"Equipment code {equipment_code!r} is mapped from both "
                f"item {existing_owner} and specialItems.{key}"
            )
        special_items[key] = equipment_code
        equipment_owners[equipment_code] = f"specialItems.{key}"

    return {"itemMappings": item_mappings, "specialItems": special_items}


def load_export_config(filepath):
    path = Path(filepath)
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            raw_config = json.load(handle)
    except FileNotFoundError as exc:
        raise RentmanConfigError(f"Configuration file not found: {path}") from exc
    except OSError as exc:
        raise RentmanConfigError(f"Unable to read configuration file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RentmanConfigError(
            f"Invalid JSON in configuration file at line {exc.lineno}, column {exc.colno}"
        ) from exc

    return parse_export_config(raw_config)


def collect_export_rows(asset_ids, item_mappings, cube_type_entries=None, special_items=None):
    counts = Counter()
    for raw_asset_id in asset_ids:
        asset_id = _stagehand_id(raw_asset_id)
        counts[asset_id] += 1

    missing_ids = sorted(set(counts) - set(item_mappings) - set(CUBE_SPECIAL_ITEM_PREFIXES))
    if missing_ids:
        missing_text = ", ".join(str(asset_id) for asset_id in missing_ids)
        raise RentmanConfigError(
            f"Missing itemMappings entries for Stagehand item IDs: {missing_text}"
        )

    rows = []
    for asset_id, quantity in sorted(counts.items()):
        if asset_id not in CUBE_SPECIAL_ITEM_PREFIXES:
            rows.append((quantity, item_mappings[asset_id]))
            continue

        entries = (cube_type_entries or {}).get(asset_id)
        if entries is None or sum(entry["quantity"] for entry in entries) != quantity:
            raise RentmanConfigError(
                f"Missing or inconsistent cube classifications for Stagehand item {asset_id}"
            )
        rows.extend(collect_cube_export_rows(
            asset_id, entries, item_mappings, special_items or {}
        ))
    return rows


def collect_cube_export_rows(asset_id, entries, item_mappings, special_items):
    rows = []
    for entry in entries:
        category = entry["label"]
        if category == "senza vie":
            equipment_code = item_mappings.get(asset_id)
            if equipment_code is None:
                raise RentmanConfigError(
                    f"Missing itemMappings entry for unconnected cube {asset_id}"
                )
        else:
            suffix = CUBE_CATEGORY_SUFFIXES.get(category)
            if suffix is None:
                raise RentmanConfigError(f"Unknown cube category: {category!r}")
            key = CUBE_SPECIAL_ITEM_PREFIXES[asset_id] + suffix
            equipment_code = special_items.get(key)
            if equipment_code is None:
                raise RentmanConfigError(f"Missing specialItems entry: {key}")
        rows.append((entry["quantity"], equipment_code))
    return rows


def collect_special_item_rows(fastener_totals, special_items):
    pin_and_spring_quantity = (
        fastener_totals["chiodi_coppiglie_tratte"]
        + fastener_totals["chiodi_coppiglie_cubi_basi"]
    )
    quantities = {
        "litecPin": pin_and_spring_quantity,
        "litecSpigot": fastener_totals["ovetti_tratte"],
        "litecSpring": pin_and_spring_quantity,
    }
    return [(quantities[key], special_items[key]) for key in SPECIAL_ITEM_KEYS]


def write_export_csv(filepath, rows):
    path = Path(filepath)
    try:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter=",", lineterminator="\n")
            writer.writerow(("Code", "Quantity", "Remark"))
            writer.writerows(
                (equipment_code, quantity, "")
                for quantity, equipment_code in rows
            )
    except OSError as exc:
        raise OSError(f"Unable to write CSV file: {exc}") from exc
