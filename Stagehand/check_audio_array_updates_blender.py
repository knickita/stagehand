"""Blender regression check for idle array dependency-graph updates."""
import json
import math
import sys
import time
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from Stagehand import AddStagehandObject as objects, Connections as connections

objects.register()
if '--' in sys.argv:
    bpy.ops.wm.open_mainfile(filepath=sys.argv[sys.argv.index('--') + 1])
asset = next(a for a in json.loads((ROOT / 'Catalogue.json').read_text())['items'] if a['uniqueId'] == 1)
array = []
for i in range(2):
    obj = bpy.data.objects.new(f'Array regression {i}', bpy.data.meshes.new(f'Array regression mesh {i}'))
    bpy.context.scene.collection.objects.link(obj)
    objects.apply_stagehand_catalogue_data(obj, asset)
    obj.location.z = -0.26 * i
    array.append(obj)
bpy.context.view_layer.update()
parent, child = array
assert connections._sync_constrained_connection_parenting(parent, 1, child, 0)
parent.location.x = 10
bpy.context.view_layer.update()
events = []
def observe(scene, depsgraph):
    events.append(len(list(depsgraph.updates)))
bpy.app.handlers.depsgraph_update_post.append(observe)
start = time.perf_counter()
for _ in range(5):
    connections._enforce_constrained_child_transform(child)
    bpy.context.view_layer.update()
print('ARRAY_IDLE_UPDATES', events, 'elapsed', time.perf_counter() - start, flush=True)
assert not events, 'Enforcing an already valid array keeps invalidating the depsgraph'
child.rotation_euler.x += math.radians(2.7)
connections._enforce_constrained_child_transform(child)
rest = child[connections.AUDIO_ARRAY_REST_ROTATION_KEY][0]
assert abs(math.degrees(child.rotation_euler.x - rest) - 3) < 0.0001
child.rotation_euler.x += math.radians(20)
connections._enforce_constrained_child_transform(child)
assert abs(math.degrees(child.rotation_euler.x - rest) - 10) < 0.0001
child.location.x += 3
connections._enforce_constrained_child_transform(child)
assert tuple(child.location) == tuple(child[connections.AUDIO_ARRAY_REST_LOCATION_KEY])
assert tuple(child.lock_location) == (True, True, True)
assert tuple(child.lock_rotation) == (False, True, True)
child.rotation_euler.x -= math.radians(30)
connections._enforce_constrained_child_transform(child)
assert abs(child.rotation_euler.x - rest) < 0.0001
bpy.context.view_layer.update()
events.clear()
# Exercise the actual handler, including the scene membership scan.
connections._mark_stagehand_object_membership_changes()
bpy.app.handlers.depsgraph_update_post.append(connections.stagehand_depsgraph_update_post)
try:
    parent.location.x += 5
    bpy.context.view_layer.update()
    assert abs(child.matrix_world.translation.x - parent.matrix_world.translation.x) < 0.0001
    events.clear()
    for _ in range(5):
        bpy.context.view_layer.update()
    assert not events, ('Array handler did not settle', events)
finally:
    bpy.app.handlers.depsgraph_update_post.remove(connections.stagehand_depsgraph_update_post)
bpy.app.handlers.depsgraph_update_post.remove(observe)
print('ARRAY_UPDATE_CHECK_OK', flush=True)
