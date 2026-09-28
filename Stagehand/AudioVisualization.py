"""GPU viewport overlay for coherent sound-pressure visualization."""

from math import radians, tan

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from .RegistrationUtils import (
    safe_define_property,
    safe_register_class,
    safe_remove_property,
    safe_unregister_class,
)


AUDIO_SOURCE_TAG = "audiosource"
SOURCE_TEXTURE_FIELDS = (
    "sourcePositionSpl",
    "sourceForwardHorizontal",
    "sourceRightVertical",
    "sourceAudio",
    "sourceNearPlane",
)
SOURCE_TEXTURE_ROWS_PER_BLOCK = len(SOURCE_TEXTURE_FIELDS)
_draw_handler = None
_shader = None
_audio_data_ubo = None
_audio_data_texture = None
_audio_data_texture_size = None
_audio_data_texture_values = None
_mesh_batches = {}
_last_draw_error = ""


VERTEX_SHADER = '''
void main()
{
    vec4 world = modelMatrix * vec4(position, 1.0);
    worldPosition = world.xyz;
    vec4 clipPosition = viewProjectionMatrix * world;
    // Keep the overlay just in front of the original surface in depth-space.
    // This avoids z-fighting without moving or modifying the actual mesh.
    clipPosition.z -= 0.00001 * clipPosition.w;
    gl_Position = clipPosition;
}
'''


FRAGMENT_SHADER = f'''
#define REFERENCE_PRESSURE 0.00002
#define PI 3.14159265358979323846

float log10Safe(float value)
{{
    return log(max(value, 0.000000000001)) / log(10.0);
}}

vec4 sourceData(int index, int row)
{{
    int textureWidth = int(audioData.textureWidth);
    int blockIndex = index / textureWidth;
    int textureX = index - blockIndex * textureWidth;
    int textureY = blockIndex * {SOURCE_TEXTURE_ROWS_PER_BLOCK} + row;
    return texelFetch(audioDataTexture, ivec2(textureX, textureY), 0);
}}

float directivityFactor(int index, vec3 offset)
{{
    vec4 audio = sourceData(index, 3);
    if (audio.w > 0.5)
        return 1.0;

    vec4 forwardData = sourceData(index, 1);
    vec4 rightData = sourceData(index, 2);
    vec3 forward = normalize(forwardData.xyz);
    vec3 right = normalize(rightData.xyz);
    vec3 up = normalize(cross(right, forward));
    float forwardDistance = dot(offset, forward);
    float nearDistance = audio.z;
    if (forwardDistance < nearDistance)
        return 0.0;

    vec2 nearSize = sourceData(index, 4).xy;
    float farDistance = 10.0;
    float interpolationDistance = max(farDistance - nearDistance, 0.001);
    float horizontalSlope = (
        forwardData.w * farDistance - nearSize.x * 0.5
    ) / interpolationDistance;
    float verticalSlope = (
        rightData.w * farDistance - nearSize.y * 0.5
    ) / interpolationDistance;
    float allowedHorizontal = max(
        0.0,
        nearSize.x * 0.5 + (forwardDistance - nearDistance) * horizontalSlope
    );
    float allowedVertical = max(
        0.0,
        nearSize.y * 0.5 + (forwardDistance - nearDistance) * verticalSlope
    );

    if (abs(dot(offset, right)) > allowedHorizontal)
        return 0.0;
    if (abs(dot(offset, up)) > allowedVertical)
        return 0.0;
    return 1.0;
}}

vec3 gradientColor(int index)
{{
    if (index <= 0) return vec3(0.85, 0.00, 0.00);
    if (index == 1) return vec3(1.00, 0.18, 0.00);
    if (index == 2) return vec3(1.00, 0.55, 0.00);
    if (index == 3) return vec3(1.00, 0.92, 0.00);
    if (index == 4) return vec3(0.25, 0.85, 0.10);
    if (index == 5) return vec3(0.00, 0.78, 0.58);
    if (index == 6) return vec3(0.00, 0.62, 1.00);
    if (index == 7) return vec3(0.10, 0.25, 0.92);
    if (index == 8) return vec3(0.30, 0.08, 0.62);
    return vec3(0.06, 0.02, 0.16);
}}

void main()
{{
    float pressureReal = 0.0;
    float pressureImaginary = 0.0;

    for (int sourceIndex = 0; sourceIndex < int(audioData.sourceCount); ++sourceIndex)
    {{
        vec4 positionSpl = sourceData(sourceIndex, 0);
        vec3 offset = worldPosition - positionSpl.xyz;
        float distanceMeters = max(length(offset), 0.01);
        float actualSpl = positionSpl.w - 20.0 * log10Safe(distanceMeters);
        float pressure = REFERENCE_PRESSURE * pow(10.0, actualSpl / 20.0);
        float dispersion = directivityFactor(sourceIndex, offset);
        vec4 audio = sourceData(sourceIndex, 3);
        float phase = 2.0 * PI * (
            distanceMeters + audio.x
        ) * audioData.frequency / max(audioData.soundSpeed, 0.001);
        pressureReal += pressure * sin(phase) * audio.y * dispersion;
        pressureImaginary += pressure * cos(phase) * audio.y * dispersion;
    }}

    float totalPressure = sqrt(
        pressureReal * pressureReal + pressureImaginary * pressureImaginary
    );
    float totalSpl = 20.0 * log10Safe(totalPressure / REFERENCE_PRESSURE);
    float gradientValue = clamp(
        (audioData.maxDisplayedSpl - totalSpl)
            / max(audioData.gradientStepDb, 0.001),
        0.0,
        9.0
    );
    int lowerIndex = int(floor(gradientValue));
    int upperIndex = min(lowerIndex + 1, 9);
    vec3 color = gradientColor(lowerIndex);
    if (audioData.smoothGradient > 0.5)
        color = mix(color, gradientColor(upperIndex), fract(gradientValue));
    FragColor = vec4(color, audioData.overlayOpacity);
}}
'''


def _tag_view3d_redraw():
    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is None:
        return
    for window in window_manager.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _audio_setting_updated(_self, _context):
    _tag_view3d_redraw()


def _has_audio_source_tag(obj):
    stagehand = getattr(obj, "stagehand", None)
    if stagehand is None or not stagehand.is_stagehand_object:
        return False
    return any(
        str(tag_item.value).strip().lower() == AUDIO_SOURCE_TAG
        for tag_item in stagehand.tags
    )


def _visible_audio_sources(context):
    sources = []
    for obj in context.scene.objects:
        if not _has_audio_source_tag(obj):
            continue
        if obj.stagehand.audioMuted:
            continue
        sources.append(obj)
    return sorted(sources, key=lambda obj: obj.name_full)


def audio_source_counts(context):
    sources = [
        obj
        for obj in context.scene.objects
        if _has_audio_source_tag(obj)
    ]
    active_count = sum(
        1 for obj in sources if not obj.stagehand.audioMuted
    )
    return active_count, len(sources)


def _source_uniform_data(context):
    depsgraph = context.evaluated_depsgraph_get()
    source_data = {
        "sourcePositionSpl": [],
        "sourceForwardHorizontal": [],
        "sourceRightVertical": [],
        "sourceAudio": [],
        "sourceNearPlane": [],
    }

    for source_obj in _visible_audio_sources(context):
        evaluated_obj = source_obj.evaluated_get(depsgraph)
        matrix_world = evaluated_obj.matrix_world
        rotation = matrix_world.to_quaternion()
        stagehand = source_obj.stagehand
        source_position = matrix_world @ Vector(stagehand.audioSourceOffset)
        forward = (rotation @ Vector((0.0, -1.0, 0.0))).normalized()
        right = (rotation @ Vector((1.0, 0.0, 0.0))).normalized()
        horizontal_angle = min(float(stagehand.audioHorizontalDispersion), 179.8)
        vertical_angle = min(float(stagehand.audioVerticalDispersion), 179.8)

        source_data["sourcePositionSpl"].append((
            source_position.x,
            source_position.y,
            source_position.z,
            float(stagehand.audioSplAtOneMeter + stagehand.audioAttenuation),
        ))
        source_data["sourceForwardHorizontal"].append((
            forward.x,
            forward.y,
            forward.z,
            tan(radians(horizontal_angle) * 0.5),
        ))
        source_data["sourceRightVertical"].append((
            right.x,
            right.y,
            right.z,
            tan(radians(vertical_angle) * 0.5),
        ))
        source_data["sourceAudio"].append((
            float(stagehand.audioDelayMeters),
            -1.0 if stagehand.audioReversePolarity else 1.0,
            float(stagehand.audioNearDispersionDistance),
            1.0 if stagehand.audioOmnidirectional else 0.0,
        ))
        source_data["sourceNearPlane"].append((
            float(stagehand.audioDispersionPlaneSize[0]),
            float(stagehand.audioDispersionPlaneSize[1]),
            0.0,
            0.0,
        ))

    return len(source_data["sourcePositionSpl"]), source_data


def _ensure_shader():
    global _shader
    if _shader is None:
        vertex_interface = gpu.types.GPUStageInterfaceInfo(
            "stagehand_audio_interface"
        )
        vertex_interface.smooth('VEC3', "worldPosition")

        shader_info = gpu.types.GPUShaderCreateInfo()
        shader_info.push_constant('MAT4', "viewProjectionMatrix")
        shader_info.push_constant('MAT4', "modelMatrix")
        shader_info.typedef_source('''
struct AudioData {
    float sourceCount;
    float textureWidth;
    float frequency;
    float soundSpeed;
    float maxDisplayedSpl;
    float gradientStepDb;
    float overlayOpacity;
    float smoothGradient;
};
''')
        shader_info.uniform_buf(0, "AudioData", "audioData")
        shader_info.sampler(0, 'FLOAT_2D', "audioDataTexture")

        shader_info.vertex_in(0, 'VEC3', "position")
        shader_info.vertex_out(vertex_interface)
        shader_info.fragment_out(0, 'VEC4', "FragColor")
        shader_info.vertex_source(VERTEX_SHADER)
        shader_info.fragment_source(FRAGMENT_SHADER)
        _shader = gpu.shader.create_from_info(shader_info)
        del vertex_interface
        del shader_info
    return _shader


def _mesh_batch(obj, shader):
    mesh = getattr(obj, "data", None)
    if mesh is None or not hasattr(mesh, "vertices"):
        return None
    mesh.calc_loop_triangles()
    cache_key = (
        mesh.as_pointer(),
        len(mesh.vertices),
        len(mesh.loop_triangles),
    )
    cached = _mesh_batches.get(obj.name_full)
    if (
        cached is not None
        and cached[0] == cache_key
        and not getattr(mesh, "is_updated_data", False)
    ):
        return cached[1]

    positions = [tuple(vertex.co) for vertex in mesh.vertices]
    indices = [tuple(triangle.vertices) for triangle in mesh.loop_triangles]
    if not positions or not indices:
        return None
    batch = batch_for_shader(
        shader,
        'TRIS',
        {"position": positions},
        indices=indices,
    )
    _mesh_batches[obj.name_full] = (cache_key, batch)
    return batch


def _update_audio_data_buffer(scene, source_count, texture_width):
    global _audio_data_ubo

    values = [
        float(source_count),
        float(texture_width),
        float(scene.stagehand_audio_frequency),
        float(scene.stagehand_audio_sound_speed),
        float(scene.stagehand_audio_max_spl),
        float(scene.stagehand_audio_gradient_step),
        float(scene.stagehand_audio_overlay_opacity),
        1.0 if scene.stagehand_audio_smooth_gradient else 0.0,
    ]

    buffer = gpu.types.Buffer('FLOAT', len(values), values)
    if _audio_data_ubo is None:
        _audio_data_ubo = gpu.types.GPUUniformBuf(buffer)
    else:
        _audio_data_ubo.update(buffer)
    return _audio_data_ubo


def _update_audio_data_texture(source_count, source_data):
    global _audio_data_texture
    global _audio_data_texture_size
    global _audio_data_texture_values

    maximum_texture_size = gpu.capabilities.max_texture_size_get()
    texture_width = min(source_count, maximum_texture_size)
    block_count = (source_count + texture_width - 1) // texture_width
    texture_height = block_count * SOURCE_TEXTURE_ROWS_PER_BLOCK
    if texture_height > maximum_texture_size:
        raise RuntimeError(
            f"The GPU cannot store {source_count} audio sources in its maximum "
            f"{maximum_texture_size} x {maximum_texture_size} data texture"
        )

    texture_size = (texture_width, texture_height)
    values = [0.0] * (texture_width * texture_height * 4)
    for source_index in range(source_count):
        texture_x = source_index % texture_width
        block_index = source_index // texture_width
        for field_index, field_name in enumerate(SOURCE_TEXTURE_FIELDS):
            texture_y = (
                block_index * SOURCE_TEXTURE_ROWS_PER_BLOCK + field_index
            )
            value_offset = (texture_y * texture_width + texture_x) * 4
            values[value_offset:value_offset + 4] = source_data[field_name][source_index]

    if (
        _audio_data_texture is None
        or _audio_data_texture_size != texture_size
        or _audio_data_texture_values != values
    ):
        buffer = gpu.types.Buffer('FLOAT', len(values), values)
        _audio_data_texture = gpu.types.GPUTexture(
            texture_size,
            format='RGBA32F',
            data=buffer,
        )
        _audio_data_texture_size = texture_size
        _audio_data_texture_values = values

    return _audio_data_texture, texture_width


def _draw_audio_visualization():
    global _last_draw_error

    context = bpy.context
    scene = getattr(context, "scene", None)
    region_data = getattr(context, "region_data", None)
    if (
        scene is None
        or region_data is None
        or not getattr(scene, "stagehand_audio_visualization_enabled", False)
    ):
        return

    try:
        source_count, source_data = _source_uniform_data(context)
        if source_count == 0:
            return
        shader = _ensure_shader()
        audio_texture, texture_width = _update_audio_data_texture(
            source_count,
            source_data,
        )
        shader.bind()
        shader.uniform_block(
            "audioData",
            _update_audio_data_buffer(scene, source_count, texture_width),
        )
        shader.uniform_sampler("audioDataTexture", audio_texture)
        shader.uniform_float(
            "viewProjectionMatrix",
            region_data.perspective_matrix,
        )
        depsgraph = context.evaluated_depsgraph_get()
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.depth_mask_set(False)
        gpu.state.face_culling_set('NONE')
        for obj in scene.objects:
            if obj.type != 'MESH' or obj.hide_viewport or obj.hide_get():
                continue
            if _has_audio_source_tag(obj):
                continue
            if not obj.visible_get(view_layer=context.view_layer):
                continue
            batch = _mesh_batch(obj, shader)
            if batch is None:
                continue
            evaluated_obj = obj.evaluated_get(depsgraph)
            shader.uniform_float("modelMatrix", evaluated_obj.matrix_world)
            batch.draw(shader)
        _last_draw_error = ""
    except Exception as exc:
        message = f"Stagehand audio visualization draw failed: {exc}"
        if message != _last_draw_error:
            print(message)
            _last_draw_error = message
    finally:
        gpu.state.face_culling_set('NONE')
        gpu.state.depth_test_set('NONE')
        gpu.state.depth_mask_set(True)
        gpu.state.blend_set('NONE')


class STAGEHAND_OT_toggle_audio_visualization(bpy.types.Operator):
    bl_idname = "stagehand.toggle_audio_visualization"
    bl_label = "Toggle Audio Visualization"
    bl_description = "Enable or disable the GPU sound-pressure overlay"
    bl_options = {'REGISTER'}

    def execute(self, context):
        enabled = not context.scene.stagehand_audio_visualization_enabled
        if enabled:
            try:
                _ensure_shader()
            except Exception as exc:
                self.report({'ERROR'}, f"Unable to create the audio shader: {exc}")
                return {'CANCELLED'}
            _mesh_batches.clear()
        context.scene.stagehand_audio_visualization_enabled = enabled
        _tag_view3d_redraw()
        state = "enabled" if enabled else "disabled"
        source_count = len(_visible_audio_sources(context))
        self.report(
            {'INFO'},
            f"Audio visualization {state} ({source_count} active sources)",
        )
        return {'FINISHED'}


classes = (STAGEHAND_OT_toggle_audio_visualization,)


def register():
    global _draw_handler

    safe_define_property(
        bpy.types.Scene,
        "stagehand_audio_visualization_enabled",
        bpy.props.BoolProperty(
            name="Audio Visualization",
            default=False,
            update=_audio_setting_updated,
        ),
    )
    safe_define_property(
        bpy.types.Scene,
        "stagehand_audio_frequency",
        bpy.props.FloatProperty(
            name="Frequency",
            default=100.0,
            min=20.0,
            max=20000.0,
            update=_audio_setting_updated,
        ),
    )
    safe_define_property(
        bpy.types.Scene,
        "stagehand_audio_sound_speed",
        bpy.props.FloatProperty(
            name="Sound Speed",
            default=343.0,
            min=1.0,
            max=1000.0,
            update=_audio_setting_updated,
        ),
    )
    safe_define_property(
        bpy.types.Scene,
        "stagehand_audio_max_spl",
        bpy.props.FloatProperty(
            name="Maximum Displayed SPL",
            default=150.0,
            min=1.0,
            max=220.0,
            update=_audio_setting_updated,
        ),
    )
    safe_define_property(
        bpy.types.Scene,
        "stagehand_audio_gradient_step",
        bpy.props.FloatProperty(
            name="Gradient Step",
            default=6.0,
            min=0.1,
            max=60.0,
            update=_audio_setting_updated,
        ),
    )
    safe_define_property(
        bpy.types.Scene,
        "stagehand_audio_smooth_gradient",
        bpy.props.BoolProperty(
            name="Smooth Gradient",
            default=True,
            update=_audio_setting_updated,
        ),
    )
    safe_define_property(
        bpy.types.Scene,
        "stagehand_audio_overlay_opacity",
        bpy.props.FloatProperty(
            name="Overlay Opacity",
            default=0.75,
            min=0.0,
            max=1.0,
            subtype='FACTOR',
            update=_audio_setting_updated,
        ),
    )
    for cls in classes:
        safe_register_class(cls)
    if _draw_handler is None:
        _draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            _draw_audio_visualization,
            (),
            'WINDOW',
            'POST_VIEW',
        )


def unregister():
    global _draw_handler
    global _shader
    global _audio_data_ubo
    global _audio_data_texture
    global _audio_data_texture_size
    global _audio_data_texture_values
    global _last_draw_error

    if _draw_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, 'WINDOW')
        _draw_handler = None
    _shader = None
    _audio_data_ubo = None
    _audio_data_texture = None
    _audio_data_texture_size = None
    _audio_data_texture_values = None
    _mesh_batches.clear()
    _last_draw_error = ""
    for cls in reversed(classes):
        safe_unregister_class(cls)
    for property_name in (
        "stagehand_audio_overlay_opacity",
        "stagehand_audio_smooth_gradient",
        "stagehand_audio_gradient_step",
        "stagehand_audio_max_spl",
        "stagehand_audio_sound_speed",
        "stagehand_audio_frequency",
        "stagehand_audio_visualization_enabled",
    ):
        safe_remove_property(bpy.types.Scene, property_name)
