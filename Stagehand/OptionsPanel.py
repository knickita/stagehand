bl_info = {
    "name": "Stagehand Options Panel",
    "author": "nick",
    "version": (0, 0, 1),
    "blender": (2, 80, 0),
    "location": "3D Viewport > Sidebar > Stagehand options panel",
    "description": "Stagehand options panel",
    "category": "Development",
}

import bpy

from . import Alerts
from .RegistrationUtils import (
    safe_define_property,
    safe_register_class,
    safe_remove_property,
    safe_unregister_class,
)


POWER_LINES_OBJECT_NAME = "Stagehand Power Lines"
CABLE_ANCHOR_POINTS_OBJECT_NAME = "Stagehand Cable Anchor Points"


def _regenerate_power_lines(_scene, context):
    if context is None or bpy.data.objects.get(POWER_LINES_OBJECT_NAME) is None:
        return

    try:
        bpy.ops.stagehand.generate_power_lines()
    except Exception as exc:
        print(f"Unable to regenerate power lines after cable setting change: {exc}")


class StageHandOptionsPanel(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Stagehand"
    bl_label = "Stagehand"

    def draw(self, context):
        layout = self.layout

        Alerts.draw_alerts(layout, context)

        box = layout.box()
        box.label(text="Cable")
        box.prop(context.scene, "stagehand_cable_draw_faces", text="Draw Face")
        box.prop(context.scene, "stagehand_cable_color", text="Color")
        anchor_points = bpy.data.objects.get(CABLE_ANCHOR_POINTS_OBJECT_NAME)
        anchor_text = (
            "Hide Anchor Points"
            if anchor_points is not None and not anchor_points.hide_viewport and not anchor_points.hide_get()
            else "Show Anchor Points"
        )
        box.operator("stagehand.toggle_cable_anchor_points", text=anchor_text)


def register():
    safe_define_property(
        bpy.types.Scene,
        "stagehand_cable_draw_faces",
        bpy.props.EnumProperty(
            name="Cable Draw Face",
            description="Choose whether generated cables include every face or only the visible outer shell",
            items=(
                ('VISIBLE', "Only Visible", "Generate only the visible outer cable faces"),
                ('ALL', "All", "Generate every cable face, including internal faces"),
            ),
            default='VISIBLE',
            update=_regenerate_power_lines,
        ),
    )
    safe_define_property(
        bpy.types.Scene,
        "stagehand_cable_color",
        bpy.props.EnumProperty(
            name="Cable Color",
            description="Choose whether generated cables are black or colored by powerline",
            items=(
                ('BLACK', "Black", "Draw all generated cables in black"),
                ('POWERLINES', "Color Powerlines", "Give every powerline a different vertex color"),
            ),
            default='POWERLINES',
            update=_regenerate_power_lines,
        ),
    )
    safe_register_class(StageHandOptionsPanel)


def unregister():
    safe_unregister_class(StageHandOptionsPanel)
    safe_remove_property(bpy.types.Scene, "stagehand_cable_color")
    safe_remove_property(bpy.types.Scene, "stagehand_cable_draw_faces")


if __name__ == "__main__" or __name__ == "<run_path>":
    register()
