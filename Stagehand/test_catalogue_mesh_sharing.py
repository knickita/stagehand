"""Run: blender --background --factory-startup --python test_catalogue_mesh_sharing.py."""

from pathlib import Path
import sys
import unittest
import uuid

try:
    import bpy
except ImportError:
    raise unittest.SkipTest("Requires Blender's Python runtime")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from Stagehand import AddStagehandObject, Connections, LoadCatalogue as catalogue


class CatalogueMeshSharingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        AddStagehandObject.register()
        catalogue.CATALOGUE_BY_ID = catalogue._load_catalogue()

    @classmethod
    def tearDownClass(cls):
        AddStagehandObject.unregister()

    def setUp(self):
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in list(bpy.data.meshes):
            bpy.data.meshes.remove(mesh)
        Connections.mark_duplicate_repair_needed()

    def tearDown(self):
        if bpy.app.timers.is_registered(Connections.dirty_connection_refresh_timer):
            bpy.app.timers.unregister(Connections.dirty_connection_refresh_timer)

    def import_asset(self):
        return catalogue.import_catalogue_asset(1)[0]

    def duplicate(self, obj, linked=False):
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.duplicate(linked=linked)
        return bpy.context.object

    def repair(self):
        Connections.mark_duplicate_repair_needed()
        Connections._repair_duplicate_ids_if_needed()

    def test_repeated_imports_share_one_mesh(self):
        first = self.import_asset()
        for _ in range(10):
            instance = self.import_asset()
            self.assertEqual(first.data, instance.data)
            self.assertNotEqual(first.stagehand.uid, instance.stagehand.uid)
        self.assertEqual(len(bpy.data.meshes), 1)

    def test_normal_duplicate_reuses_mesh_and_keeps_object_state(self):
        original = self.import_asset()
        duplicate = self.duplicate(original)
        self.assertNotEqual(original.data, duplicate.data)
        self.assertEqual(len(bpy.data.meshes), 2)
        duplicate.location = (4, 5, 6)
        duplicate.scale = (2, 3, 4)
        bpy.ops.object.select_all(action='DESELECT')
        duplicate.stagehand.audioDelayMeters = 7
        self.repair()
        self.assertEqual(original.data, duplicate.data)
        self.assertEqual(len(bpy.data.meshes), 1)
        self.assertEqual(tuple(duplicate.location), (4, 5, 6))
        self.assertEqual(tuple(duplicate.scale), (2, 3, 4))
        self.assertEqual(duplicate.stagehand.audioDelayMeters, 7)
        self.assertEqual(original.stagehand.audioDelayMeters, 0)
        self.assertNotEqual(original.stagehand.uid, duplicate.stagehand.uid)
        self.assertNotEqual(original.stagehand.links[0].uid, duplicate.stagehand.links[0].uid)

    def test_linked_duplicate_stays_shared(self):
        original = self.import_asset()
        duplicate = self.duplicate(original, linked=True)
        self.repair()
        self.assertEqual(original.data, duplicate.data)
        self.assertEqual(len(bpy.data.meshes), 1)

    def test_automatic_membership_refresh_repairs_duplication(self):
        original = self.import_asset()
        Connections._mark_stagehand_object_membership_changes(delay=0, auto_connect=False)
        Connections._process_dirty_connection_refresh()
        duplicate = self.duplicate(original)
        Connections.stagehand_depsgraph_update_post(bpy.context.scene, bpy.context.evaluated_depsgraph_get())
        Connections._process_dirty_connection_refresh()
        self.assertEqual(original.data, duplicate.data)
        self.assertEqual(len(bpy.data.meshes), 1)

    def test_existing_unique_uid_copy_is_repaired(self):
        original = self.import_asset()
        duplicate = self.duplicate(original)
        duplicate.stagehand.uid = 'already-repaired-uid'
        self.repair()
        self.assertEqual(original.data, duplicate.data)
        self.assertEqual(duplicate.stagehand.uid, 'already-repaired-uid')

    def test_legacy_single_mesh_copy_without_part_metadata(self):
        original = self.import_asset()
        duplicate = self.duplicate(original)
        duplicate.stagehand.uid = 'legacy-copy'
        del duplicate[catalogue.INSTANCE_PART_KEY]
        del duplicate[catalogue.INSTANCE_MESH_PATH_KEY]
        self.repair()
        self.assertEqual(original.data, duplicate.data)
        self.assertIn(catalogue.INSTANCE_PART_KEY, duplicate)

    def test_duplicate_without_cache_uses_original_part(self):
        original = self.import_asset()
        template = catalogue._get_cached_template(catalogue.CATALOGUE_BY_ID[1])
        catalogue._discard_cached_template(1, template)
        duplicate = self.duplicate(original)
        self.repair()
        self.assertEqual(original.data, duplicate.data)
        self.assertEqual(len(bpy.data.meshes), 1)

    def test_mesh_used_by_non_catalogue_object_is_not_removed(self):
        original = self.import_asset()
        duplicate = self.duplicate(original)
        old_mesh = duplicate.data
        unrelated = bpy.data.objects.new('Not Stagehand', old_mesh)
        bpy.context.scene.collection.objects.link(unrelated)
        self.repair()
        self.assertEqual(original.data, duplicate.data)
        self.assertEqual(unrelated.data, old_mesh)
        self.assertEqual(len(bpy.data.meshes), 2)

    def test_fake_user_mesh_is_preserved(self):
        original = self.import_asset()
        duplicate = self.duplicate(original)
        old_mesh = duplicate.data
        old_mesh.use_fake_user = True
        self.repair()
        self.assertEqual(original.data, duplicate.data)
        self.assertTrue(old_mesh.use_fake_user)
        self.assertEqual(len(bpy.data.meshes), 2)

    def multipart_asset(self):
        asset = {'uniqueId': 99999, 'name': 'Multipart', 'mesh3d': 'test/multipart'}
        catalogue.CATALOGUE_BY_ID[asset['uniqueId']] = asset
        root = bpy.data.objects.new('Root', None)
        parts = [root]
        for name, coordinate in (('Base', 1), ('Head', 2)):
            mesh = bpy.data.meshes.new(name)
            mesh.from_pydata([(coordinate, 0, 0)], [], [])
            obj = bpy.data.objects.new(name, mesh)
            obj.parent = root
            parts.append(obj)
        for obj in parts:
            bpy.context.scene.collection.objects.link(obj)
        template = catalogue._prepare_cached_template(parts, asset)
        return catalogue._instantiate_cached_template(template, asset)

    def test_multipart_copies_share_corresponding_meshes(self):
        root, base, head = self.multipart_asset()
        copy_base = self.duplicate(base)
        copy_head = self.duplicate(head)
        copy_base.name = 'Renamed Base'
        copy_head.name = 'Renamed Head'
        copy_base.stagehand.uid = 'unique-base'
        copy_head.stagehand.uid = 'unique-head'
        self.repair()
        self.assertEqual(copy_base.data, base.data)
        self.assertEqual(copy_head.data, head.data)
        self.assertNotEqual(base.data, head.data)
        self.assertEqual(copy_head.parent, root)
        self.assertEqual(len(bpy.data.meshes), 2)

    def test_ambiguous_legacy_multipart_is_not_collapsed(self):
        _, base, head = self.multipart_asset()
        duplicate = self.duplicate(head)
        duplicate.stagehand.uid = 'legacy-head'
        del duplicate[catalogue.INSTANCE_PART_KEY]
        del duplicate[catalogue.INSTANCE_MESH_PATH_KEY]
        old_mesh = duplicate.data
        self.repair()
        self.assertEqual(duplicate.data, old_mesh)
        self.assertNotEqual(base.data, head.data)

    def test_different_catalogue_ids_are_not_merged(self):
        first = self.import_asset()
        other = catalogue.import_catalogue_asset(68)[0]
        duplicate = self.duplicate(other)
        self.repair()
        self.assertEqual(other.data, duplicate.data)
        self.assertNotEqual(first.data, other.data)

    def test_unassigned_stagehand_mesh_is_not_merged(self):
        original = self.import_asset()
        duplicate = self.duplicate(original)
        duplicate.stagehand.asset_id = 0
        self.repair()
        self.assertNotEqual(original.data, duplicate.data)

    def test_negative_variant_id_shares_its_own_mesh(self):
        asset = dict(catalogue.CATALOGUE_BY_ID[1], uniqueId=-99999, parentId=1)
        catalogue.CATALOGUE_BY_ID[asset['uniqueId']] = asset
        original = catalogue.import_catalogue_asset(asset['uniqueId'])[0]
        duplicate = self.duplicate(original)
        self.repair()
        self.assertEqual(original.data, duplicate.data)
        self.assertEqual(len(bpy.data.meshes), 1)

    def test_previous_model_path_is_not_replaced_by_new_cache(self):
        original = self.import_asset()
        duplicate = self.duplicate(original)
        duplicate.stagehand.uid = 'older-model'
        duplicate[catalogue.INSTANCE_MESH_PATH_KEY] = 'Models/previous-version'
        old_mesh = duplicate.data
        self.repair()
        self.assertEqual(duplicate.data, old_mesh)
        self.assertNotEqual(original.data, duplicate.data)

    def test_save_reload_preserves_cache_and_repairs_existing_copy(self):
        original = self.import_asset()
        duplicate = self.duplicate(original)
        duplicate.stagehand.uid = 'saved-copy'
        names = original.name, duplicate.name
        directory = Path(__file__).resolve().parent / f'stagehand-mesh-{uuid.uuid4().hex}'
        directory.mkdir()
        try:
            path = str(directory / 'mesh_sharing.blend')
            bpy.ops.wm.save_as_mainfile(filepath=path)
            bpy.ops.wm.open_mainfile(filepath=path)
        finally:
            for file in directory.iterdir():
                file.unlink()
            directory.rmdir()
        Connections.stagehand_undo_redo_post(None)
        Connections._process_dirty_connection_refresh()
        original, duplicate = (bpy.data.objects[name] for name in names)
        self.assertEqual(original.data, duplicate.data)
        self.assertEqual(original.data, self.import_asset().data)
        self.assertEqual(len(bpy.data.meshes), 1)

    def test_undo_redo_preserves_mesh_sharing(self):
        original = self.import_asset()
        original_name = original.name
        bpy.context.preferences.edit.use_global_undo = True
        bpy.ops.ed.undo_push(message='Before duplicate')
        duplicate_name = self.duplicate(original).name
        bpy.ops.ed.undo_push(message='Duplicate')
        # The real timer repairs after Blender has recorded the duplicate step.
        self.repair()
        bpy.ops.ed.undo()
        self.assertNotIn(duplicate_name, bpy.data.objects)
        bpy.ops.ed.redo()
        Connections.stagehand_undo_redo_post(None)
        Connections._process_dirty_connection_refresh()
        self.assertEqual(bpy.data.objects[original_name].data, bpy.data.objects[duplicate_name].data)
        self.assertEqual(len(bpy.data.meshes), 1)


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
