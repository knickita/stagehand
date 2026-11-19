import bpy
import json
from mathutils import Vector
from pathlib import Path

objects = []
for obj in bpy.context.scene.objects:
    if obj.type != 'MESH':
        continue
    points = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    props = obj.get('stagehand')
    objects.append({
        'name': obj.name,
        'visible': obj.visible_get(),
        'collections': [c.name for c in obj.users_collection],
        'bounds': [list(p) for p in points],
        'location': list(obj.matrix_world.translation),
        'rotation': list(obj.matrix_world.to_euler()),
        'properties': props.to_dict() if hasattr(props, 'to_dict') else str(props),
    })
out = Path('D:/Repositories/stagehand/Stagehand/tmp/stage_perimeter_scene.json')
out.write_text(json.dumps({'units': bpy.context.scene.unit_settings.system, 'scale': bpy.context.scene.unit_settings.scale_length, 'objects': objects}, default=str), encoding='utf-8')
print('EXTRACTED', len(objects), 'mesh objects to', out)
