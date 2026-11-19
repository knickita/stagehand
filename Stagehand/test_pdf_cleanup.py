"""Temporary PDF data cleanup regressions. Run with Blender's Python."""

import ast
from pathlib import Path
import unittest
from unittest.mock import Mock

try:
    import bpy
except ImportError:
    bpy = None


@unittest.skipIf(bpy is None, "Requires Blender")
class PdfCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).with_name("PdfDrawings.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            "_remove_temporary_render_objects", "_remove_line_render_objects",
            "_remove_dimension_render_objects",
        }
        nodes = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
        cls.ns = {"bpy": bpy}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), cls.ns)

    def setUp(self):
        self.collections = ("objects", "meshes", "curves", "materials")
        self.existing = {name: set(getattr(bpy.data, name)) for name in self.collections}

    def tearDown(self):
        for name in self.collections:
            extra = set(getattr(bpy.data, name)) - self.existing[name]
            if extra:
                bpy.data.batch_remove(ids=extra)

    def object(self, name, kind="MESH", data=None):
        if data is None:
            data = (bpy.data.meshes.new(name) if kind == "MESH"
                    else bpy.data.curves.new(name, kind))
        obj = bpy.data.objects.new(name, data)
        bpy.context.scene.collection.objects.link(obj)
        return obj

    def test_mesh_cleanup_restores_visibility_and_removes_only_owned_data(self):
        original = self.object("Original")
        temporary = self.object("Temporary")
        material = bpy.data.materials.new("Temporary material")
        temporary.data.materials.append(material)
        unrelated_mesh = bpy.data.meshes.new("Unrelated orphan mesh")
        unrelated_material = bpy.data.materials.new("Unrelated orphan material")
        original.hide_render = True
        self.ns["_remove_line_render_objects"]([temporary], material, [(original, False)])
        self.assertFalse(original.hide_render)
        self.assertIsNone(bpy.data.objects.get("Temporary"))
        self.assertIsNone(bpy.data.meshes.get("Temporary"))
        self.assertIsNone(bpy.data.materials.get("Temporary material"))
        self.assertEqual(bpy.data.objects.get(original.name), original)
        self.assertEqual(bpy.data.meshes.get(unrelated_mesh.name), unrelated_mesh)
        self.assertEqual(bpy.data.materials.get(unrelated_material.name), unrelated_material)

    def test_curve_and_font_cleanup(self):
        objects = [self.object("Dimension lines", "CURVE"), self.object("Dimension text", "FONT")]
        material = bpy.data.materials.new("Dimension material")
        names = [obj.name for obj in objects]
        for obj in objects:
            obj.data.materials.append(material)
        self.ns["_remove_dimension_render_objects"](objects, (material,))
        for name in names:
            self.assertIsNone(bpy.data.objects.get(name))
            self.assertIsNone(bpy.data.curves.get(name))
        self.assertIsNone(bpy.data.materials.get("Dimension material"))

    def test_mesh_shared_with_original_is_preserved(self):
        original = self.object("Original")
        mesh = original.data
        temporary = self.object("Temporary", data=mesh)
        material = bpy.data.materials.new("Shared material")
        mesh.materials.append(material)
        self.ns["_remove_line_render_objects"]([temporary], material, [])
        self.assertEqual(original.data, mesh)
        self.assertEqual(mesh.users, 1)
        self.assertEqual(mesh.materials[0], material)

    def test_shared_temporary_data_is_removed_once(self):
        first = self.object("Shared temporary")
        second = self.object("Second temporary", data=first.data)
        self.ns["_remove_line_render_objects"]([first, second], None, [])
        self.assertIsNone(bpy.data.meshes.get("Shared temporary"))

    def test_material_used_by_original_is_preserved(self):
        original = self.object("Original")
        temporary = self.object("Temporary", "FONT")
        material = bpy.data.materials.new("Shared material")
        original.data.materials.append(material)
        temporary.data.materials.append(material)
        self.ns["_remove_dimension_render_objects"]([temporary], (material,))
        self.assertEqual(original.data.materials[0], material)
        self.assertIsNone(bpy.data.curves.get("Temporary"))

    def test_fake_users_are_preserved(self):
        temporary = self.object("Retained mesh")
        mesh = temporary.data
        mesh.use_fake_user = True
        material = bpy.data.materials.new("Retained material")
        material.use_fake_user = True
        self.ns["_remove_line_render_objects"]([temporary], material, [])
        self.assertEqual(bpy.data.meshes.get(mesh.name), mesh)
        self.assertEqual(bpy.data.materials.get(material.name), material)

    def test_empty_cleanup(self):
        self.ns["_remove_line_render_objects"]([], None, [])
        self.ns["_remove_dimension_render_objects"]([], None)

    def test_deletion_is_batched_in_dependency_order(self):
        from types import SimpleNamespace
        data = Mock(users=1)
        material = Mock(users=1)
        objects = [Mock(data=data) for _ in range(100)]
        batches = []

        def remove(*, ids):
            batches.append(set(ids))
            if len(batches) == 1:
                data.users = 0
            elif len(batches) == 2:
                material.users = 0

        self.ns["bpy"] = SimpleNamespace(data=SimpleNamespace(batch_remove=remove))
        try:
            self.ns["_remove_temporary_render_objects"](objects, (material,))
        finally:
            self.ns["bpy"] = bpy
        self.assertEqual(batches, [set(objects), {data}, {material}])


if __name__ == "__main__":
    unittest.main()
