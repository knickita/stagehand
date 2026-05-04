import bpy

from .mesh import build_power_lines_mesh
from .scene import generate_power_solution
from .solver import PowerSolverError
from ..RegistrationUtils import safe_register_class, safe_unregister_class


class STAGEHAND_OT_generate_power_lines(bpy.types.Operator):
    bl_idname = "stagehand.generate_power_lines"
    bl_label = "Generate Power Lines"
    bl_description = "Calculate cable routes and create one mesh containing all generated power lines"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            result = generate_power_solution(context)
            _obj, link_count, node_count, vertex_count, face_count = build_power_lines_mesh(
                context,
                result.solver,
            )
        except PowerSolverError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, f"Unable to generate power lines: {exc}")
            return {'CANCELLED'}

        message = (
            f"Generated {link_count} cable spans, {node_count} cable nodes, "
            f"{vertex_count} vertices, {face_count} faces"
        )
        if result.warnings:
            message += f" ({'; '.join(result.warnings)})"
        self.report({'INFO'}, message)
        return {'FINISHED'}


classes = (
    STAGEHAND_OT_generate_power_lines,
)


def register():
    for cls in classes:
        safe_register_class(cls)


def unregister():
    for cls in reversed(classes):
        safe_unregister_class(cls)
