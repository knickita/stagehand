import math
import os
import tempfile
import zlib
from pathlib import Path

import bpy
from bpy_extras.io_utils import ExportHelper
from mathutils import Vector


PAGE_WIDTH = 842.0
PAGE_HEIGHT = 595.0
PAGE_MARGIN = 36.0
PAGE_GUTTER = 18.0
TITLE_HEIGHT = 32.0
RENDER_WIDTH = 1200
RENDER_HEIGHT = 850
CAMERA_FIT_MARGIN = 1.65
WHITE = (1.0, 1.0, 1.0)
BLACK = (0.0, 0.0, 0.0)
OPAQUE_WHITE = (1.0, 1.0, 1.0, 1.0)


def _visible_mesh_objects(context):
    return [
        obj
        for obj in context.scene.objects
        if obj.type == 'MESH' and obj.visible_get()
    ]


def _object_bounds(objects):
    min_corner = Vector((math.inf, math.inf, math.inf))
    max_corner = Vector((-math.inf, -math.inf, -math.inf))

    for obj in objects:
        for corner in obj.bound_box:
            world_corner = obj.matrix_world @ Vector(corner)
            min_corner.x = min(min_corner.x, world_corner.x)
            min_corner.y = min(min_corner.y, world_corner.y)
            min_corner.z = min(min_corner.z, world_corner.z)
            max_corner.x = max(max_corner.x, world_corner.x)
            max_corner.y = max(max_corner.y, world_corner.y)
            max_corner.z = max(max_corner.z, world_corner.z)

    center = (min_corner + max_corner) * 0.5
    dimensions = max_corner - min_corner
    return center, dimensions


def _projected_bounds(objects, camera_rotation):
    min_corner = Vector((math.inf, math.inf, math.inf))
    max_corner = Vector((-math.inf, -math.inf, -math.inf))
    world_to_camera_rotation = camera_rotation.inverted()

    for obj in objects:
        for corner in obj.bound_box:
            projected = world_to_camera_rotation @ (obj.matrix_world @ Vector(corner))
            min_corner.x = min(min_corner.x, projected.x)
            min_corner.y = min(min_corner.y, projected.y)
            min_corner.z = min(min_corner.z, projected.z)
            max_corner.x = max(max_corner.x, projected.x)
            max_corner.y = max(max_corner.y, projected.y)
            max_corner.z = max(max_corner.z, projected.z)

    return max_corner - min_corner


def _set_camera_view(scene, camera, center, objects, view_direction):
    direction = view_direction.normalized()
    camera_rotation = direction.to_track_quat('-Z', 'Y').to_euler()
    projected_dimensions = _projected_bounds(objects, camera_rotation.to_matrix())
    max_dimension = max(projected_dimensions.x, projected_dimensions.y, projected_dimensions.z, 0.1)
    distance = max_dimension * 4.0
    render = scene.render
    frame_aspect = render.resolution_x / max(render.resolution_y, 1)
    required_width_scale = projected_dimensions.x / frame_aspect
    required_height_scale = projected_dimensions.y

    camera.location = center - (direction * distance)
    camera.rotation_euler = camera_rotation
    camera.data.type = 'ORTHO'
    camera.data.ortho_scale = max(required_width_scale, required_height_scale, 0.5) * CAMERA_FIT_MARGIN


def _capture_attributes(owner, attribute_names):
    if owner is None:
        return {}

    values = {}
    for attribute_name in attribute_names:
        if hasattr(owner, attribute_name):
            value = getattr(owner, attribute_name)
            if hasattr(value, "__len__") and not isinstance(value, str):
                value = tuple(value)
            values[attribute_name] = value
    return values


def _restore_attributes(owner, values):
    if owner is None:
        return

    for attribute_name, value in values.items():
        try:
            setattr(owner, attribute_name, value)
        except (AttributeError, TypeError):
            pass


def _set_attribute_if_available(owner, attribute_name, value):
    if owner is None or not hasattr(owner, attribute_name):
        return

    try:
        setattr(owner, attribute_name, value)
    except (AttributeError, TypeError):
        pass


def _set_render_engine(scene):
    for engine in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "BLENDER_RENDER"):
        try:
            scene.render.engine = engine
            return
        except TypeError:
            continue


def _configure_line_render(scene, view_layer):
    shading = getattr(scene.display, "shading", None)
    _set_attribute_if_available(shading, "type", 'SOLID')
    _set_attribute_if_available(shading, "color_type", 'MATERIAL')
    _set_attribute_if_available(shading, "background_type", 'VIEWPORT')
    _set_attribute_if_available(shading, "background_color", WHITE)
    _set_attribute_if_available(shading, "light", 'FLAT')
    _set_attribute_if_available(shading, "show_xray", True)
    _set_attribute_if_available(shading, "xray_alpha", 1.0)
    _set_attribute_if_available(shading, "show_wireframes", True)
    _set_attribute_if_available(shading, "wireframe_opacity", 1.0)

    world = scene.world
    if world is not None:
        world.color = WHITE

    scene.render.use_freestyle = True
    scene.render.film_transparent = True
    scene.render.image_settings.color_mode = 'RGBA'
    if hasattr(scene, "eevee"):
        scene.eevee.taa_render_samples = 16

    freestyle_settings = view_layer.freestyle_settings
    if not freestyle_settings.linesets:
        bpy.ops.scene.freestyle_lineset_add()

    for line_set in freestyle_settings.linesets:
        line_set.select_silhouette = True
        line_set.select_border = True
        line_set.select_crease = True
        line_set.select_edge_mark = True
        line_set.select_material_boundary = False
        line_set.select_contour = True
        line_set.visibility = 'VISIBLE'
        line_set.linestyle.color = (0.0, 0.0, 0.0)
        line_set.linestyle.thickness = 1.2

    try:
        scene.view_settings.view_transform = 'Standard'
        scene.view_settings.look = 'None'
        scene.view_settings.exposure = 0.0
        scene.view_settings.gamma = 1.0
    except TypeError:
        pass


def _capture_freestyle_settings(scene, view_layer):
    freestyle_settings = view_layer.freestyle_settings
    line_sets = []

    for line_set in freestyle_settings.linesets:
        line_sets.append({
            "line_set": line_set,
            "values": _capture_attributes(
                line_set,
                (
                    "select_silhouette",
                    "select_border",
                    "select_crease",
                    "select_edge_mark",
                    "select_material_boundary",
                    "select_contour",
                    "visibility",
                ),
            ),
            "linestyle": _capture_attributes(line_set.linestyle, ("color", "thickness")),
        })

    return {
        "use_freestyle": scene.render.use_freestyle,
        "line_set_count": len(freestyle_settings.linesets),
        "line_sets": line_sets,
    }


def _restore_freestyle_settings(scene, view_layer, state):
    scene.render.use_freestyle = state["use_freestyle"]
    freestyle_settings = view_layer.freestyle_settings

    while len(freestyle_settings.linesets) > state["line_set_count"]:
        freestyle_settings.linesets.remove(freestyle_settings.linesets[-1])

    for line_set_state in state["line_sets"]:
        line_set = line_set_state["line_set"]
        try:
            _restore_attributes(line_set, line_set_state["values"])
            _restore_attributes(line_set.linestyle, line_set_state["linestyle"])
        except ReferenceError:
            pass


def _create_white_material():
    material = bpy.data.materials.new("Stagehand PDF White Surface")
    material.diffuse_color = OPAQUE_WHITE
    material.use_nodes = True

    nodes = material.node_tree.nodes
    nodes.clear()

    output_node = nodes.new(type="ShaderNodeOutputMaterial")
    emission_node = nodes.new(type="ShaderNodeEmission")
    emission_node.inputs["Color"].default_value = OPAQUE_WHITE
    emission_node.inputs["Strength"].default_value = 1.0
    material.node_tree.links.new(emission_node.outputs["Emission"], output_node.inputs["Surface"])

    return material


def _create_line_render_objects(scene, objects):
    white_material = _create_white_material()
    temporary_objects = []
    original_hide_render = []

    for obj in objects:
        original_hide_render.append((obj, obj.hide_render))
        obj.hide_render = True

        line_obj = obj.copy()
        line_obj.data = obj.data.copy()
        line_obj.animation_data_clear()
        line_obj.name = f"Stagehand PDF Line {obj.name}"
        line_obj.parent = None
        line_obj.matrix_world = obj.matrix_world.copy()
        line_obj.data.materials.clear()
        line_obj.data.materials.append(white_material)
        line_obj.hide_render = False

        scene.collection.objects.link(line_obj)
        temporary_objects.append(line_obj)

    return temporary_objects, white_material, original_hide_render


def _remove_line_render_objects(temporary_objects, white_material, original_hide_render):
    for obj, hide_render in original_hide_render:
        try:
            obj.hide_render = hide_render
        except ReferenceError:
            pass

    for obj in temporary_objects:
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)

    if white_material is not None and white_material.users == 0:
        bpy.data.materials.remove(white_material)


def _render_view(context, view_name, center, objects, temp_directory):
    scene = context.scene
    camera_data = bpy.data.cameras.new(f"Stagehand PDF {view_name} Camera Data")
    camera = bpy.data.objects.new(f"Stagehand PDF {view_name} Camera", camera_data)
    scene.collection.objects.link(camera)

    view_direction = {
        "Front": Vector((0.0, -1.0, 0.0)),
        "Left": Vector((1.0, 0.0, 0.0)),
        "Top": Vector((0.0, 0.0, -1.0)),
        "Iso": Vector((-1.0, -1.0, -0.75)),
    }[view_name]

    _set_camera_view(scene, camera, center, objects, view_direction)

    output_path = Path(temp_directory) / f"{view_name.lower()}.png"
    try:
        scene.camera = camera
        scene.render.filepath = str(output_path)
        bpy.ops.render.render(write_still=True)

        image = bpy.data.images.load(str(output_path))
        try:
            return _image_to_pdf_rgb(image)
        finally:
            bpy.data.images.remove(image)
    finally:
        bpy.data.objects.remove(camera, do_unlink=True)
        bpy.data.cameras.remove(camera_data)


def _image_to_pdf_rgb(image):
    width, height = image.size
    pixels = list(image.pixels)
    data = bytearray(width * height * 3)
    target = 0

    for y in range(height - 1, -1, -1):
        row_start = y * width * 4
        for x in range(width):
            pixel_start = row_start + x * 4
            alpha = pixels[pixel_start + 3]
            red = pixels[pixel_start] * alpha + (1.0 - alpha)
            green = pixels[pixel_start + 1] * alpha + (1.0 - alpha)
            blue = pixels[pixel_start + 2] * alpha + (1.0 - alpha)
            data[target] = max(0, min(255, int(red * 255.0)))
            data[target + 1] = max(0, min(255, int(green * 255.0)))
            data[target + 2] = max(0, min(255, int(blue * 255.0)))
            target += 3

    return {
        "width": width,
        "height": height,
        "data": bytes(data),
    }


def _pdf_escape(value):
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_stream(data):
    if isinstance(data, str):
        data = data.encode("latin-1")
    return b"<< /Length " + str(len(data)).encode("ascii") + b" >>\nstream\n" + data + b"\nendstream"


def _pdf_image_stream(image):
    compressed = zlib.compress(image["data"])
    header = (
        f"<< /Type /XObject /Subtype /Image /Width {image['width']} "
        f"/Height {image['height']} /ColorSpace /DeviceRGB "
        f"/BitsPerComponent 8 /Filter /FlateDecode /Length {len(compressed)} >>\n"
    ).encode("ascii")
    return header + b"stream\n" + compressed + b"\nendstream"


def _write_pdf(filepath, rendered_views):
    labels = ("Front", "Left", "Top", "Iso")
    slot_width = (PAGE_WIDTH - (PAGE_MARGIN * 2.0) - PAGE_GUTTER) / 2.0
    slot_height = (PAGE_HEIGHT - (PAGE_MARGIN * 2.0) - TITLE_HEIGHT - PAGE_GUTTER) / 2.0

    content = [
        "q",
        "BT /F1 18 Tf 36 566 Td (Stagehand PDF Drawings) Tj ET",
    ]

    for index, label in enumerate(labels):
        column = index % 2
        row = 1 - (index // 2)
        x = PAGE_MARGIN + (slot_width + PAGE_GUTTER) * column
        y = PAGE_MARGIN + (slot_height + PAGE_GUTTER) * row
        image = rendered_views[label]

        scale = min(slot_width / image["width"], (slot_height - 18.0) / image["height"])
        draw_width = image["width"] * scale
        draw_height = image["height"] * scale
        draw_x = x + (slot_width - draw_width) * 0.5
        draw_y = y + (slot_height - 18.0 - draw_height) * 0.5

        content.extend([
            f"BT /F1 10 Tf {x:.2f} {y + slot_height - 10.0:.2f} Td ({_pdf_escape(label)}) Tj ET",
            f"{x:.2f} {y:.2f} {slot_width:.2f} {slot_height:.2f} re S",
            "q",
            f"{draw_width:.2f} 0 0 {draw_height:.2f} {draw_x:.2f} {draw_y:.2f} cm",
            f"/Im{index + 1} Do",
            "Q",
        ])

    content.append("Q")
    content_stream = "\n".join(content)

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 595] "
            b"/Resources << /Font << /F1 4 0 R >> "
            b"/XObject << /Im1 6 0 R /Im2 7 0 R /Im3 8 0 R /Im4 9 0 R >> >> "
            b"/Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _pdf_stream(content_stream),
    ]

    for label in labels:
        objects.append(_pdf_image_stream(rendered_views[label]))

    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")

    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))

    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )

    with open(filepath, "wb") as handle:
        handle.write(output)


class STAGEHAND_OT_generate_pdf_drawings(bpy.types.Operator, ExportHelper):
    bl_idname = "stagehand.generate_pdf_drawings"
    bl_label = "Generate PDF Drawings"
    bl_description = "Generate a PDF page with front, left, top, and isometric scene views"
    bl_options = {'REGISTER'}

    filename_ext = ".pdf"
    filter_glob: bpy.props.StringProperty(
        default="*.pdf",
        options={'HIDDEN'},
    )

    def invoke(self, context, event):
        if not self.filepath:
            blend_path = bpy.data.filepath
            base_directory = Path(blend_path).parent if blend_path else Path.home()
            self.filepath = str(base_directory / "stagehand_pdf_drawings.pdf")

        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        objects = _visible_mesh_objects(context)
        if not objects:
            self.report({'ERROR'}, "No visible mesh objects found for PDF drawings")
            return {'CANCELLED'}

        center, dimensions = _object_bounds(objects)
        scene = context.scene

        original_camera = scene.camera
        original_filepath = scene.render.filepath
        original_resolution = (scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage)
        original_format = scene.render.image_settings.file_format
        original_color_mode = scene.render.image_settings.color_mode
        original_engine = scene.render.engine
        original_film_transparent = scene.render.film_transparent
        original_freestyle = _capture_freestyle_settings(scene, context.view_layer)
        original_world_color = scene.world.color[:] if scene.world is not None else None
        original_view_settings = _capture_attributes(
            scene.view_settings,
            ("view_transform", "look", "exposure", "gamma"),
        )
        original_shading = _capture_attributes(
            getattr(scene.display, "shading", None),
            (
                "type",
                "color_type",
                "single_color",
                "background_type",
                "background_color",
                "light",
                "show_xray",
                "xray_alpha",
                "show_wireframes",
                "wireframe_opacity",
            ),
        )

        rendered_views = {}
        temporary_line_objects = []
        white_material = None
        original_hide_render = []

        try:
            scene.render.resolution_x = RENDER_WIDTH
            scene.render.resolution_y = RENDER_HEIGHT
            scene.render.resolution_percentage = 100
            scene.render.image_settings.file_format = 'PNG'

            _set_render_engine(scene)
            _configure_line_render(scene, context.view_layer)
            temporary_line_objects, white_material, original_hide_render = _create_line_render_objects(
                scene,
                objects,
            )

            with tempfile.TemporaryDirectory() as temp_directory:
                for view_name in ("Front", "Left", "Top", "Iso"):
                    rendered_views[view_name] = _render_view(context, view_name, center, objects, temp_directory)

            _write_pdf(self.filepath, rendered_views)
        except Exception as exc:
            self.report({'ERROR'}, f"Unable to generate PDF drawings: {exc}")
            return {'CANCELLED'}
        finally:
            _remove_line_render_objects(temporary_line_objects, white_material, original_hide_render)
            scene.camera = original_camera
            scene.render.filepath = original_filepath
            scene.render.resolution_x = original_resolution[0]
            scene.render.resolution_y = original_resolution[1]
            scene.render.resolution_percentage = original_resolution[2]
            scene.render.image_settings.file_format = original_format
            scene.render.image_settings.color_mode = original_color_mode
            scene.render.film_transparent = original_film_transparent
            scene.render.engine = original_engine
            _restore_freestyle_settings(scene, context.view_layer, original_freestyle)
            if scene.world is not None and original_world_color is not None:
                scene.world.color = original_world_color
            _restore_attributes(scene.view_settings, original_view_settings)
            _restore_attributes(getattr(scene.display, "shading", None), original_shading)

        self.report({'INFO'}, f"PDF drawings created: {os.path.basename(self.filepath)}")
        return {'FINISHED'}


classes = (
    STAGEHAND_OT_generate_pdf_drawings,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
