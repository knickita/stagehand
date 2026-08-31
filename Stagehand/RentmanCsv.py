import csv
import json
from collections import Counter
from pathlib import Path


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

    return {"itemMappings": item_mappings}


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


def collect_export_rows(asset_ids, item_mappings):
    counts = Counter()
    for raw_asset_id in asset_ids:
        asset_id = _stagehand_id(raw_asset_id)
        counts[asset_id] += 1

    missing_ids = sorted(set(counts) - set(item_mappings))
    if missing_ids:
        missing_text = ", ".join(str(asset_id) for asset_id in missing_ids)
        raise RentmanConfigError(
            f"Missing itemMappings entries for Stagehand item IDs: {missing_text}"
        )

    return [
        (quantity, item_mappings[asset_id])
        for asset_id, quantity in sorted(counts.items())
    ]


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
