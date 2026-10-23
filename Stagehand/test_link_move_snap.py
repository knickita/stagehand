"""Run with Blender: blender --background --python test_link_move_snap.py."""

from pathlib import Path
from types import MethodType, SimpleNamespace
import sys
import unittest
from unittest.mock import MagicMock, patch

try:
    from mathutils import Matrix, Quaternion, Vector
    import bpy
except ImportError:
    raise unittest.SkipTest("Requires Blender's Python runtime")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from Stagehand import SnapLogics as snap
from Stagehand.LinkTypes import StagehandLinkType


def make_link(x=0.0, y=0.0, link_type=StagehandLinkType.LITEC30, cylindrical=False):
    return SimpleNamespace(
        posDir=(x, y, 0.0, 0.0, 0.0, 0.0, 1.0), type=link_type,
        cylindricalType=cylindrical, length=10.0, displayRadius=0.1,
    )


def make_object(name, links):
    return SimpleNamespace(
        name_full=name, matrix_world=Matrix.Identity(4),
        stagehand=SimpleNamespace(links=links, is_stagehand_object=True),
    )


def make_operator(obj):
    operator = SimpleNamespace(
        source_link=(obj, 0), moving_objects=[obj],
        initial_matrices={obj.name_full: obj.matrix_world.copy()},
        _selecting_source=True,
        _snap_target=None,
    )
    for name in (
        '_start_preview', '_stop_preview', '_draw_source_link', '_build_selection_preview', '_draw_snap_target',
        '_restore_initial_transforms', '_modal', 'modal', 'cancel',
        'invoke', '_select_source_link', '_begin_snap_move', '_translate_objects', '_apply_current_motion',
    ):
        setattr(operator, name, MethodType(getattr(snap.STAGEHAND_OT_link_move_mode, name), operator))
    return operator


class LinkMoveSnapTests(unittest.TestCase):
    def setUp(self):
        self.context = SimpleNamespace(
            region=SimpleNamespace(type='WINDOW'),
            region_data=SimpleNamespace(view_rotation=Quaternion()), window=object(),
            area=SimpleNamespace(type='VIEW_3D', tag_redraw=MagicMock()),
            window_manager=MagicMock(),
        )
        projection = patch.object(
            snap.view3d_utils, 'location_3d_to_region_2d',
            side_effect=lambda region, data, point: Vector((point.x, point.y)),
        )
        projection.start()
        self.addCleanup(projection.stop)

    def test_picks_nearest_source_across_selected_objects(self):
        first = make_object('first', [make_link(0.0), make_link(50.0)])
        second = make_object('second', [make_link(80.0)])
        event = SimpleNamespace(mouse_region_x=49.0, mouse_region_y=0.0)
        self.assertEqual(snap._pick_source_link(self.context, event, [first, second]), (first, 1))

    def test_picks_pipe_near_its_middle(self):
        obj = make_object('pipe', [
            make_link(link_type=StagehandLinkType.PIPE, cylindrical=True),
            make_link(2.0, 8.0),
        ])
        event = SimpleNamespace(mouse_region_x=0.0, mouse_region_y=8.0)
        self.assertEqual(snap._pick_source_link(self.context, event, [obj]), (obj, 0))

    def test_holding_l_only_selects_links_without_moving_or_scanning(self):
        obj = make_object('source', [make_link(), make_link(50.0)])
        operator = make_operator(obj)
        operator._start_preview = MagicMock(side_effect=lambda context: setattr(operator, '_area', context.area))
        self.context.selected_objects = [obj]
        with patch.object(snap.Connections, 'prune_stale_connections') as prune, patch.object(snap, '_ScreenLinkSpatialIndex') as index:
            result = operator.invoke(self.context, SimpleNamespace(
                type='L', value='PRESS', mouse_region_x=0.0, mouse_region_y=0.0,
            ))
            self.assertEqual(result, {'RUNNING_MODAL'})
            for event_type, mouse_x in (('MOUSEMOVE', 49.0), ('L', 49.0), ('MOUSEMOVE', 50.0)):
                result = operator.modal(self.context, SimpleNamespace(
                    type=event_type, value='PRESS', mouse_region_x=mouse_x, mouse_region_y=0.0,
                ))
                self.assertEqual(result, {'RUNNING_MODAL'})
                self.assertEqual(obj.matrix_world, Matrix.Identity(4))
                self.assertEqual(operator.source_link, (obj, 1))
            prune.assert_not_called()
            index.assert_not_called()

    def test_release_starts_motion_at_release_position_and_reselection_does_not_jump(self):
        obj = make_object('source', [make_link(), make_link(50.0)])
        operator = make_operator(obj)
        operator._area = self.context.area
        index = SimpleNamespace(cells_by_type={}, target_count=0)
        with (
            patch.object(snap.Connections, 'prune_stale_connections'),
            patch.object(snap, '_connection_graph_from_database', return_value={}),
            patch.object(snap, '_ScreenLinkSpatialIndex', return_value=index) as build_index,
            patch.object(snap, 'SNAP_PROFILE', False),
            patch.object(snap, '_screen_to_plane_point', side_effect=lambda context, event, *args: Vector((event.mouse_region_x, event.mouse_region_y, 0.0))),
        ):
            for event_type, value, mouse_x, expected_x in (
                ('L', 'RELEASE', 50.0, 0.0),
                ('MOUSEMOVE', 'NOTHING', 51.0, 1.0),
                ('L', 'PRESS', 1.0, 1.0),
                ('MOUSEMOVE', 'NOTHING', 20.0, 1.0),
                ('L', 'RELEASE', 20.0, 1.0),
                ('MOUSEMOVE', 'NOTHING', 21.0, 2.0),
            ):
                result = operator.modal(self.context, SimpleNamespace(
                    type=event_type, value=value, mouse_region_x=mouse_x, mouse_region_y=0.0,
                ))
                self.assertEqual(result, {'RUNNING_MODAL'})
                self.assertAlmostEqual(obj.matrix_world.translation.x, expected_x)
            self.assertEqual(build_index.call_count, 2)
            self.assertEqual(operator.modal(self.context, SimpleNamespace(type='ESC', value='PRESS')), {'CANCELLED'})
            self.assertEqual(obj.matrix_world, Matrix.Identity(4))

    def test_click_while_selecting_does_not_move_or_refresh_connections(self):
        operator = make_operator(make_object('source', [make_link()]))
        operator._area = self.context.area
        with patch.object(snap.Connections, 'refresh_connections_for_objects') as refresh:
            result = operator.modal(self.context, SimpleNamespace(type='LEFTMOUSE', value='PRESS'))
        self.assertEqual(result, {'RUNNING_MODAL'})
        refresh.assert_not_called()

    def test_snap_keeps_the_chosen_source_when_another_link_is_closer(self):
        obj = make_object('moving', [make_link(), make_link(50.0)])
        target = make_object('target', [make_link(50.0)])
        center, rotation = snap._link_transform(target, target.stagehand.links[0])
        index = SimpleNamespace(
            target_count=1,
            iter_candidates=MagicMock(return_value=[(0, (target, 0, target.stagehand.links[0], center, rotation))]),
        )
        with patch.object(snap.Connections, 'link_alignment_metrics', return_value=(1.0, 0.0)) as metrics:
            for position in (0.0, 5.0):
                obj.matrix_world = Matrix.Translation((position, 0.0, 0.0))
                profile = {}
                pair = snap._find_best_snap_pair(self.context, (obj, 0), index, profile)
                self.assertEqual(pair[1], 0)
                self.assertEqual(profile['moving_links'], 1)
                self.assertEqual(metrics.call_args.args[1], 0)

    def test_target_index_contains_only_compatible_types(self):
        source = make_object('hook', [make_link(link_type=StagehandLinkType.HOOK)])
        target = make_object('target', [make_link(link_type=StagehandLinkType.PIPE), make_link()])
        fake_bpy = SimpleNamespace(data=SimpleNamespace(objects=[source, target]))
        with patch.object(snap, 'bpy', fake_bpy), patch.object(snap, '_is_link_available_cached', return_value=True):
            index = snap._ScreenLinkSpatialIndex(
                self.context, [source], connections={}, source_link=source.stagehand.links[0],
            )
        self.assertEqual(index.target_count, 1)
        self.assertEqual(set(index.cells_by_type), {int(StagehandLinkType.PIPE)})

    def test_sphere_is_an_overlay_with_a_one_second_pulse_and_cleans_up(self):
        obj = make_object('source', [make_link()])
        operator = make_operator(obj)
        fake_gpu = MagicMock()
        fake_gpu.state.blend_get.return_value = 'NONE'
        fake_gpu.state.depth_test_get.return_value = 'LESS_EQUAL'
        fake_gpu.state.depth_mask_get.return_value = True
        fake_gpu.state.line_width_get.return_value = 1.0
        fake_bpy = SimpleNamespace(context=self.context, types=MagicMock())
        with patch.object(snap, 'gpu', fake_gpu), patch.object(snap, 'bpy', fake_bpy), patch.object(snap, 'batch_for_shader') as batch:
            operator._start_preview(self.context)
            try:
                triangles = batch.call_args_list[1].args[2]['pos']
                for axis in range(3):
                    self.assertAlmostEqual(max(point[axis] for point in triangles), 1.0, places=5)
                    self.assertAlmostEqual(min(point[axis] for point in triangles), -1.0, places=5)
                alphas = []
                for elapsed in (0.0, 0.5, 1.0):
                    with patch.object(snap.time, 'perf_counter', return_value=operator._preview_started_at + elapsed):
                        operator._draw_source_link()
                    alphas.append(operator._shader.uniform_float.call_args.args[1][3])
                scale = fake_gpu.matrix.multiply_matrix.call_args.args[0].to_scale()
                self.assertLess((scale - Vector((0.1, 0.1, 0.1))).length, 1e-6)
                self.assertAlmostEqual(alphas[0], alphas[2])
                self.assertLess(alphas[1], alphas[0])
                fake_gpu.state.depth_test_set.assert_any_call('NONE')
                self.assertEqual(fake_gpu.state.depth_test_set.call_args.args, ('LESS_EQUAL',))
                self.assertEqual(fake_gpu.state.depth_mask_set.call_args.args, (True,))
            finally:
                operator._stop_preview()
            operator._stop_preview()
            fake_bpy.types.SpaceView3D.draw_handler_remove.assert_called_once()
            self.context.window_manager.event_timer_remove.assert_called_once()
            self.assertFalse(any(active is operator for active in snap._active_move_operators))

    def test_pulse_timer_does_not_run_snap_or_connection_checks(self):
        operator = make_operator(make_object('source', [make_link()]))
        operator._area = self.context.area
        operator._pulse_timer = object()
        operator._apply_current_motion = MagicMock()
        with patch.object(snap.Connections, 'refresh_connections_for_objects') as refresh:
            result = operator.modal(self.context, SimpleNamespace(type='TIMER'))
        self.assertEqual(result, {'RUNNING_MODAL'})
        self.context.area.tag_redraw.assert_called_once()
        operator._apply_current_motion.assert_not_called()
        refresh.assert_not_called()

    def test_other_links_are_visible_only_during_selection_and_preview_is_cached(self):
        obj = make_object('source', [make_link(), make_link(2.0), make_link(4.0)])
        operator = make_operator(obj)
        fake_gpu = MagicMock()
        fake_bpy = SimpleNamespace(context=self.context, types=MagicMock())
        with (
            patch.object(snap, 'gpu', fake_gpu),
            patch.object(snap, 'bpy', fake_bpy),
            patch.object(snap, 'batch_for_shader', side_effect=lambda *args: MagicMock()) as batch,
        ):
            operator._start_preview(self.context)
            try:
                operator._draw_source_link()
                vertices = batch.call_args.args[2]['pos']
                self.assertAlmostEqual(min(vertex.x for vertex in vertices), 1.9, places=5)
                self.assertAlmostEqual(max(vertex.x for vertex in vertices), 4.1, places=5)
                self.assertEqual(len(vertices), 2 * len(operator._sphere_outline_vertices))
                other_batch = operator._selection_batch
                other_batch.draw.assert_called_once()
                operator._shader.uniform_float.assert_any_call('color', (*snap.LinkMode.SPHERE_COLOR[:3], 0.65))

                operator._draw_source_link()
                self.assertEqual(batch.call_count, 3)
                self.assertEqual(other_batch.draw.call_count, 2)

                operator._selecting_source = False
                operator._draw_source_link()
                self.assertEqual(other_batch.draw.call_count, 2)

                obj.matrix_world = Matrix.Translation((10.0, 0.0, 0.0))
                operator.modal(self.context, SimpleNamespace(
                    type='L', value='PRESS', mouse_region_x=12.0, mouse_region_y=0.0,
                ))
                operator._draw_source_link()
                self.assertEqual(operator.source_link, (obj, 1))
                self.assertEqual(batch.call_count, 4)
                vertices = batch.call_args.args[2]['pos']
                self.assertAlmostEqual(min(vertex.x for vertex in vertices), 9.9, places=5)
                self.assertAlmostEqual(max(vertex.x for vertex in vertices), 14.1, places=5)
                self.assertFalse(any(11.8 < vertex.x < 12.2 for vertex in vertices))
            finally:
                operator._stop_preview()

    def test_snap_target_tracks_the_current_match_and_clears_when_lost_or_reselecting(self):
        obj = make_object('source', [make_link()])
        target_a = make_object('first target', [make_link(2.0)])
        target_b = make_object('second target', [make_link(4.0)])
        operator = make_operator(obj)
        operator._area = self.context.area
        operator._selecting_source = False
        operator.movement_matrices = operator.initial_matrices
        operator.plane_origin = Vector((0.0, 0.0, 0.0))
        operator.plane_normal = Vector((0.0, 0.0, 1.0))
        operator.start_plane_point = Vector((0.0, 0.0, 0.0))
        operator.target_index = object()
        operator.last_profile_report_at = 0.0
        event = SimpleNamespace(type='MOUSEMOVE', mouse_region_x=0.0, mouse_region_y=0.0)
        pairs = [
            (obj, 0, obj.stagehand.links[0], Vector((0, 0, 0)), target, 0, target.stagehand.links[0], Vector((x, 0, 0)))
            for target, x in ((target_a, 2.0), (target_b, 4.0))
        ]
        with (
            patch.object(snap, '_find_best_snap_pair', side_effect=[*pairs, None, pairs[0]]),
            patch.object(snap, '_screen_to_plane_point', return_value=Vector((0, 0, 0))),
            patch.object(snap, 'SNAP_PROFILE', False),
        ):
            for target in (target_a, target_b, None, target_a):
                operator._apply_current_motion(self.context, event)
                if target is None:
                    self.assertIsNone(operator._snap_target)
                else:
                    self.assertIs(operator._snap_target[0], target)
                    self.assertEqual(operator._snap_target[1], 0)
            operator.modal(self.context, SimpleNamespace(
                type='L', value='PRESS', mouse_region_x=0.0, mouse_region_y=0.0,
            ))
            self.assertIsNone(operator._snap_target)

    def test_red_glow_uses_the_other_objects_link_transform_and_radius(self):
        obj = make_object('source', [make_link()])
        obj.stagehand.links[0].displayRadius = 0.4
        target = make_object('target', [make_link(2.0, 1.0)])
        target.stagehand.links[0].displayRadius = 0.025
        target.matrix_world = Matrix.Translation((3.0, 4.0, 5.0))
        target_center = Vector((5.0, 5.0, 5.0))
        operator = make_operator(obj)
        fake_gpu = MagicMock()
        fake_bpy = SimpleNamespace(context=self.context, types=MagicMock())
        with patch.object(snap, 'gpu', fake_gpu), patch.object(snap, 'bpy', fake_bpy), patch.object(snap, 'batch_for_shader'):
            operator._start_preview(self.context)
            try:
                operator._selecting_source = False
                operator._snap_target = (target, 0)
                for source_position in (0.0, 10.0):
                    obj.matrix_world = Matrix.Translation((source_position, 0, 0))
                    fake_gpu.matrix.multiply_matrix.reset_mock()
                    operator._draw_source_link()
                    transforms = fake_gpu.matrix.multiply_matrix.call_args_list
                    self.assertLess((transforms[1].args[0].translation - target_center).length, 1e-6)
                    self.assertAlmostEqual(transforms[-1].args[0].to_scale().x, 0.025)
                    self.assertEqual(operator._shader.uniform_float.call_args.args[1], (*snap.SNAP_TARGET_COLOR, 1.0))

                operator._shader.uniform_float.reset_mock()
                operator._selecting_source = True
                operator._draw_source_link()
                colors = [call.args[1][:3] for call in operator._shader.uniform_float.call_args_list]
                self.assertNotIn(snap.SNAP_TARGET_COLOR, colors)

                operator._shader.uniform_float.reset_mock()
                operator._selecting_source = False
                operator._snap_target = (obj, 0)
                operator._draw_source_link()
                self.assertIsNone(operator._snap_target)
                colors = [call.args[1][:3] for call in operator._shader.uniform_float.call_args_list]
                self.assertNotIn(snap.SNAP_TARGET_COLOR, colors)
            finally:
                operator._stop_preview()
            self.assertIsNone(operator._snap_target)

    def test_pipe_target_glow_outlines_the_other_objects_entire_link(self):
        obj = make_object('source', [make_link()])
        target = make_object('pipe', [make_link(link_type=StagehandLinkType.PIPE, cylindrical=True)])
        target.stagehand.links[0].displayRadius = 0.025
        target.matrix_world = Matrix.Translation((3.0, 4.0, 5.0))
        operator = make_operator(obj)
        fake_gpu = MagicMock()
        fake_bpy = SimpleNamespace(context=self.context, types=MagicMock())
        with (
            patch.object(snap, 'gpu', fake_gpu),
            patch.object(snap, 'bpy', fake_bpy),
            patch.object(snap, 'batch_for_shader', side_effect=lambda *args: MagicMock()) as batch,
        ):
            operator._start_preview(self.context)
            try:
                operator._selecting_source = False
                operator._snap_target = (target, 0)
                operator._draw_source_link()
                vertices = batch.call_args.args[2]['pos']
                self.assertAlmostEqual(min(vertex.y for vertex in vertices), 0.0)
                self.assertAlmostEqual(max(vertex.y for vertex in vertices), 10.0)
                self.assertAlmostEqual(max(vertex.x for vertex in vertices), 0.025)
                transform = fake_gpu.matrix.multiply_matrix.call_args.args[0]
                self.assertLess((transform.translation - target.matrix_world.translation).length, 1e-6)
                operator._draw_source_link()
                self.assertEqual(batch.call_count, 3)
            finally:
                operator._stop_preview()


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
