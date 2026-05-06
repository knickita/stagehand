import math
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import bpy
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix

from .AddStagehandObject import apply_stagehand_catalogue_data
from .RegistrationUtils import safe_register_class, safe_unregister_class


MVR_XML_NAME = "GeneralSceneDescription.xml"
MVR_UNIT_SCALE = 0.001
DEFAULT_TRUSS_LENGTH = 1.0
DEFAULT_TRUSS_WIDTH = 0.35


def _read_mvr_xml(filepath):
    path = Path(filepath)
    suffix = path.suffix.lower()

    if suffix == ".xml":
        return path.read_bytes()

    if suffix != ".mvr":
        raise ValueError("Select an .mvr file or a GeneralSceneDescription.xml file")

    with zipfile.ZipFile(path, "r") as archive:
        for name in archive.namelist():
            if Path(name).name == MVR_XML_NAME:
                return archive.read(name)

    raise FileNotFoundError(f"{MVR_XML_NAME} was not found inside {path.name}")


def _parse_matrix(text):
    rows = re.findall(r"\{([^{}]+)\}", text or "")
    if len(rows) != 4:
        raise ValueError(f"Unsupported MVR matrix: {text!r}")

    values = []
    for row in rows:
        values.append([float(value.strip()) for value in row.split(",")])

    if any(len(row) != 3 for row in values):
        raise ValueError(f"Unsupported MVR matrix: {text!r}")

    local_x_axis, local_y_axis, local_z_axis = values[:3]
    translation = [value * MVR_UNIT_SCALE for value in values[3]]
    return Matrix(
        (
            (local_x_axis[0], local_y_axis[0], local_z_axis[0], translation[0]),
            (local_x_axis[1], local_y_axis[1], local_z_axis[1], translation[1]),
            (local_x_axis[2], local_y_axis[2], local_z_axis[2], translation[2]),
            (0.0, 0.0, 0.0, 1.0),
        )
    )


def _symbol_definitions(root):
    return {
        symdef.get("uuid"): symdef.get("name", "")
        for symdef in root.iter("Symdef")
        if symdef.get("uuid")
    }


def _first_symbol_name(truss, symdefs):
    symbol = next(truss.iter("Symbol"), None)
    if symbol is None:
        return ""

    return symdefs.get(symbol.get("symdef"), "")


def _truss_length_from_symbol(symbol_name):
    match = re.search(r"(\d{3})(?!.*\d)", symbol_name or "")
    if match is None:
        return DEFAULT_TRUSS_LENGTH

    return int(match.group(1)) / 100.0


def _truss_width_from_symbol(symbol_name):
    upper_name = (symbol_name or "").upper()
    if "40" in upper_name:
        return 0.40
    if "30" in upper_name:
        return 0.30
    return DEFAULT_TRUSS_WIDTH


def _catalogue_family_from_symbol(symbol_name):
    upper_name = (symbol_name or "").upper()
    if upper_name.startswith(("QX30", "FX30")):
        return "QX30"
    if upper_name.startswith(("QX40", "QH40")):
        return "QX40"
    return ""


def _catalogue_lookup_by_name():
    from . import LoadCatalogue

    if not LoadCatalogue.CATALOGUE_BY_ID:
        LoadCatalogue.reload_catalogue_operators()

    return {
        asset_data.get("name", "").lower(): asset_data
        for asset_data in LoadCatalogue.CATALOGUE_BY_ID.values()
    }


def _asset_for_symbol(symbol_name, catalogue_by_name):
    family = _catalogue_family_from_symbol(symbol_name)
    if not family:
        return None

    match = re.search(r"(\d{3})(?!.*\d)", symbol_name or "")
    if match is None:
        return None

    length_cm = int(match.group(1))
    return catalogue_by_name.get(f"litec{family}-{length_cm}cm".lower())


def _asset_truss_length(asset_data, fallback_symbol_name):
    link_positions = []
    for link in asset_data.get("links", []):
        pos_dir = link.get("posdir", ())
        if len(pos_dir) >= 3:
            link_positions.append(float(pos_dir[2]))

    if len(link_positions) >= 2:
        return max(link_positions) - min(link_positions)

    return _truss_length_from_symbol(fallback_symbol_name)


def _move_objects_to_collection(objects, collection):
    for obj in objects:
        if obj.name not in collection.objects.keys():
            collection.objects.link(obj)

        for existing_collection in list(obj.users_collection):
            if existing_collection != collection:
                existing_collection.objects.unlink(obj)


def _apply_import_transform(objects, target_matrix, asset_length):
    if not objects:
        return

    # Stagehand Litec assets are authored along local Z; MVR truss symbols run along local X.
    # The extra local-Z roll matches the MVR truss section orientation after the length axis is aligned.
    stagehand_to_mvr = (
        Matrix.Rotation(math.radians(90.0), 4, "Y")
        @ Matrix.Rotation(math.radians(-90.0), 4, "Z")
    )
    center_to_stagehand_origin = Matrix.Translation((0.0, 0.0, -asset_length * 0.5))
    root_inverse = objects[0].matrix_world.inverted()

    for obj in objects:
        relative_matrix = root_inverse @ obj.matrix_world
        obj.matrix_world = (
            target_matrix
            @ stagehand_to_mvr
            @ center_to_stagehand_origin
            @ relative_matrix
        )


def _tag_mvr_object(obj, truss, symbol_name):
    obj["mvr_uuid"] = truss.get("uuid", "")
    obj["mvr_symbol"] = symbol_name


def _import_catalogue_truss(asset_data, target_matrix, collection, truss, symbol_name):
    from . import LoadCatalogue

    objects = LoadCatalogue._import_asset(asset_data)
    _move_objects_to_collection(objects, collection)
    _apply_import_transform(
        objects,
        target_matrix,
        _asset_truss_length(asset_data, symbol_name),
    )

    for obj in objects:
        _tag_mvr_object(obj, truss, symbol_name)

    return objects


def _create_placeholder_truss(target_matrix, collection, truss, symbol_name):
    length = _truss_length_from_symbol(symbol_name)
    width = _truss_width_from_symbol(symbol_name)

    bpy.ops.mesh.primitive_cube_add(size=1.0)
    obj = bpy.context.object
    obj.name = symbol_name or "MVR Truss"
    obj.matrix_world = target_matrix @ Matrix.Diagonal((length, width, width, 1.0))
    _move_objects_to_collection([obj], collection)

    apply_stagehand_catalogue_data(obj)
    obj.stagehand.catalogueName = symbol_name or "MVR Truss"
    for tag_value in ("structure", "truss", "mvr"):
        tag_item = obj.stagehand.tags.add()
        tag_item.value = tag_value

    _tag_mvr_object(obj, truss, symbol_name)
    return [obj]


def _create_import_collection(context, filepath):
    base_name = Path(filepath).stem or "MVR Import"
    collection = bpy.data.collections.new(f"MVR Import - {base_name}")
    context.scene.collection.children.link(collection)
    return collection


def import_mvr_structure(context, filepath):
    root = ElementTree.fromstring(_read_mvr_xml(filepath))
    symdefs = _symbol_definitions(root)
    catalogue_by_name = _catalogue_lookup_by_name()
    collection = _create_import_collection(context, filepath)

    imported_objects = []
    matched_count = 0
    placeholder_count = 0

    for truss in root.iter("Truss"):
        matrix_text = truss.findtext("Matrix")
        if not matrix_text:
            continue

        target_matrix = _parse_matrix(matrix_text)
        symbol_name = _first_symbol_name(truss, symdefs)
        asset_data = _asset_for_symbol(symbol_name, catalogue_by_name)

        if asset_data is None:
            objects = _create_placeholder_truss(target_matrix, collection, truss, symbol_name)
            placeholder_count += 1
        else:
            objects = _import_catalogue_truss(
                asset_data,
                target_matrix,
                collection,
                truss,
                symbol_name,
            )
            matched_count += 1

        imported_objects.extend(objects)

    bpy.ops.object.select_all(action="DESELECT")
    for obj in imported_objects:
        obj.select_set(True)
    if imported_objects:
        context.view_layer.objects.active = imported_objects[0]

    return {
        "trusses": matched_count + placeholder_count,
        "catalogue": matched_count,
        "placeholders": placeholder_count,
        "objects": len(imported_objects),
    }


class STAGEHAND_OT_import_mvr_structure(bpy.types.Operator, ImportHelper):
    bl_idname = "stagehand.import_mvr_structure"
    bl_label = "Import MVR Structure"
    bl_description = "Import truss structure from an MVR file"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".mvr"
    filter_glob: bpy.props.StringProperty(
        default="*.mvr;*.xml",
        options={'HIDDEN'},
    )

    def execute(self, context):
        try:
            result = import_mvr_structure(context, self.filepath)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        self.report(
            {'INFO'},
            (
                f"Imported {result['trusses']} MVR trusses "
                f"({result['catalogue']} catalogue, {result['placeholders']} placeholders)"
            ),
        )
        return {'FINISHED'}


classes = (
    STAGEHAND_OT_import_mvr_structure,
)


def register():
    for cls in classes:
        safe_register_class(cls)


def unregister():
    for cls in reversed(classes):
        safe_unregister_class(cls)
