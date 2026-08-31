from pathlib import Path

import bpy
from bpy_extras.io_utils import ExportHelper

from .RegistrationUtils import safe_register_class, safe_unregister_class
from .RentmanCsv import (
    RentmanConfigError,
    collect_export_rows,
    load_export_config,
    write_export_csv,
)


def _default_config_path():
    return Path(__file__).resolve().parent / "RentmanExport.json"


def _project_name():
    blend_path = bpy.data.filepath
    return Path(blend_path).stem if blend_path else "Stagehand"


def _csv_filename_for_project(project_name):
    sanitized = "".join(
        character if character.isalnum() or character in (" ", "-", "_") else "_"
        for character in project_name
    ).strip()
    return f"{'_'.join(sanitized.split()) or 'stagehand'}_rentman.csv"


def _stagehand_asset_ids():
    for obj in bpy.data.objects:
        stagehand = getattr(obj, "stagehand", None)
        if stagehand is None or not getattr(stagehand, "is_stagehand_object", False):
            continue
        yield int(getattr(stagehand, "asset_id", -1))


class STAGEHAND_OT_export_rentman_csv(bpy.types.Operator, ExportHelper):
    bl_idname = "stagehand.export_rentman_csv"
    bl_label = "Export Rentman CSV"
    bl_description = "Export Stagehand item quantities using the Rentman ID mapping"
    bl_options = {'REGISTER'}

    filename_ext = ".csv"
    filter_glob: bpy.props.StringProperty(
        default="*.csv",
        options={'HIDDEN'},
    )
    config_filepath: bpy.props.StringProperty(
        name="Configuration",
        description="JSON file containing column names and Stagehand-to-Rentman item mappings",
        subtype='FILE_PATH',
    )

    def draw(self, context):
        self.layout.prop(self, "config_filepath")

    def invoke(self, context, event):
        if not self.filepath:
            blend_path = bpy.data.filepath
            base_directory = Path(blend_path).parent if blend_path else Path.home()
            self.filepath = str(base_directory / _csv_filename_for_project(_project_name()))
        if not self.config_filepath:
            self.config_filepath = str(_default_config_path())
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        try:
            config_path = bpy.path.abspath(self.config_filepath)
            config = load_export_config(config_path)
            asset_ids = list(_stagehand_asset_ids())
            if not asset_ids:
                self.report({'ERROR'}, "No Stagehand objects found")
                return {'CANCELLED'}

            rows = collect_export_rows(asset_ids, config["itemMappings"])
            output_path = bpy.path.ensure_ext(bpy.path.abspath(self.filepath), self.filename_ext)
            write_export_csv(output_path, rows)
        except (RentmanConfigError, OSError, TypeError, ValueError) as exc:
            print(f"Stagehand Rentman CSV export failed: {exc}")
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        self.report(
            {'INFO'},
            f"Rentman CSV created with {len(rows)} item types and {sum(row[0] for row in rows)} items",
        )
        return {'FINISHED'}


classes = (
    STAGEHAND_OT_export_rentman_csv,
)


def register():
    for cls in classes:
        safe_register_class(cls)


def unregister():
    for cls in reversed(classes):
        safe_unregister_class(cls)
