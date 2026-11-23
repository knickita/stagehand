"""Run with Blender --factory-startup --background --python this_file.py."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from Stagehand import AddStagehandObject as objects, AudioVisualization as audio

objects.register()
gpu.init()
asset = next(a for a in json.loads((ROOT / 'Catalogue.json').read_text())['items'] if a['uniqueId'] == 68)
sources = []
for name in ('filter A', 'filter B'):
    obj = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(obj)
    objects.apply_stagehand_catalogue_data(obj, asset)
    obj.stagehand.audioSourceOffset = (0, 0, 0)
    obj.location.z = 1
    sources.append(obj)
a, b = sources

# Constraints apply even to scripted edits of inactive objects.
a.stagehand.audioLowpass = 100
a.stagehand.audioHighpass = 150
assert a.stagehand.audioHighpass == 100
a.stagehand.audioLowpass = 50
assert a.stagehand.audioLowpass == 100
b.stagehand.audioLowpass = 200
for obj in bpy.context.selected_objects:
    obj.select_set(False)
for obj in sources:
    obj.select_set(True)
bpy.context.view_layer.objects.active = a
a.stagehand.audioHighpass = 80
assert b.stagehand.audioHighpass == 80
a.stagehand.audioLowpass = 50
assert a.stagehand.audioLowpass == b.stagehand.audioLowpass == 80

# Catalogue loading must reset a previously narrow band reliably.
objects.apply_stagehand_catalogue_data(a, asset)
assert (a.stagehand.audioHighpass, a.stagehand.audioLowpass) == (20, 20000)
bpy.ops.object.select_all(action='DESELECT')
b.stagehand.audioMuted = True
a.stagehand.audioHighpass = 150
a.stagehand.audioLowpass = 200
bpy.context.view_layer.update()
count, data = audio._source_uniform_data(bpy.context)
assert count == 1 and data['sourceNearPlane'][0][2:] == (150, 200)
texture, width = audio._update_audio_data_texture(count, data)
shader = audio._ensure_shader()
batch = batch_for_shader(shader, 'TRIS', {'position': [(-1,-1,0), (3,-1,0), (-1,3,0)]})
settings = SimpleNamespace(stagehand_audio_frequency=80, stagehand_audio_sound_speed=343,
    stagehand_audio_max_spl=100, stagehand_audio_gradient_step=6,
    stagehand_audio_overlay_opacity=1, stagehand_audio_smooth_gradient=True)
offscreen = gpu.types.GPUOffScreen(16, 16)
try:
    with offscreen.bind():
        gpu.state.viewport_set(0, 0, 16, 16)
        gpu.state.depth_test_set('NONE')
        gpu.state.blend_set('NONE')
        for frequency, expected in ((80, (0.06,0.02,0.16)), (150, (0.85,0,0)),
                                    (200, (0.85,0,0)), (201, (0.06,0.02,0.16))):
            settings.stagehand_audio_frequency = frequency
            shader.bind()
            shader.uniform_float('modelMatrix', Matrix.Identity(4))
            shader.uniform_float('viewProjectionMatrix', Matrix.Identity(4))
            shader.uniform_block('audioData', audio._update_audio_data_buffer(settings, count, width))
            shader.uniform_sampler('audioDataTexture', texture)
            batch.draw(shader)
            pixel = gpu.state.active_framebuffer_get().read_color(8,8,1,1,4,0,'FLOAT')
            values = pixel.to_list()[0][0]
            assert all(abs(values[i]-expected[i]) < 0.01 for i in range(3)), (frequency, values)
finally:
    offscreen.free()
print('AUDIO_FILTERS_OK: constraints, multi-edit, catalogue reset, GPU boundaries and rejection')
