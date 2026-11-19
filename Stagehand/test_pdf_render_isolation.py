"""PDF render copies must stay outside Stagehand's automatic mesh repair."""

from pathlib import Path
import sys
import unittest

try:
    import bpy
except ImportError:
    bpy = None


@unittest.skipIf(bpy is None, "Requires Blender")
class PdfRenderIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import Stagehand
        cls.addon = Stagehand
        cls.registered = not hasattr(bpy.types.Object, 'stagehand')
        if cls.registered:
            Stagehand.register()
        cls.pdf = Stagehand.PdfDrawings
        cls.connections = Stagehand.Connections

    @classmethod
    def tearDownClass(cls):
        if cls.registered:
            cls.addon.unregister()

    def setUp(self):
        self.existing = {name: set(getattr(bpy.data, name))
                         for name in ('objects', 'meshes', 'materials')}
        mesh = bpy.data.meshes.new('Isolation original mesh')
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
        material = bpy.data.materials.new('Isolation original dark material')
        material.diffuse_color = (.01, .02, .03, 1)
        mesh.materials.append(material)
        self.original = bpy.data.objects.new('Isolation original', mesh)
        bpy.context.scene.collection.objects.link(self.original)
        self.original.stagehand.is_stagehand_object = True
        self.original.stagehand.asset_id = 1
        self.original.stagehand.uid = 'pdf-isolation-original'
        self.copies, self.material, self.hidden = self.pdf._create_line_render_objects(
            bpy.context.scene, [self.original])

    def tearDown(self):
        self.pdf._remove_line_render_objects(self.copies, self.material, self.hidden)
        for name in self.existing:
            added = set(getattr(bpy.data, name)) - self.existing[name]
            if added:
                bpy.data.batch_remove(ids=added)

    def test_copies_have_no_live_stagehand_identity(self):
        copy = self.copies[0]
        self.assertFalse(self.connections.is_stagehand_object(copy))
        self.assertEqual(copy.stagehand.uid, '')
        self.assertTrue(self.original.stagehand.is_stagehand_object)
        self.assertEqual(self.original.stagehand.uid, 'pdf-isolation-original')
        self.assertNotEqual(copy.data, self.original.data)
        self.assertEqual(copy.data.materials[0], self.material)

    def test_forced_duplicate_repair_does_not_relink_render_meshes(self):
        copy = self.copies[0]
        original_mesh = self.original.data
        original_material = original_mesh.materials[0]
        copy_mesh = copy.data
        for _ in range(3):
            self.connections.mark_duplicate_repair_needed()
            self.assertEqual(self.connections._repair_duplicate_ids_if_needed(), 0)
            self.assertEqual(copy.data, copy_mesh)
            self.assertEqual(copy.data.materials[0], self.material)
            self.assertEqual(self.original.data, original_mesh)
            self.assertEqual(self.original.data.materials[0], original_material)
            self.assertEqual(self.original.stagehand.uid, 'pdf-isolation-original')


if __name__ == '__main__':
    unittest.main()
