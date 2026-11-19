"""Dimension geometry regressions. Run with Blender's Python for mathutils."""

import ast
import colorsys
from contextlib import contextmanager
import itertools
import math
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import sys
sys.path.insert(0, str(Path(__file__).parent))
import DimensionLayout

try:
    from mathutils import Matrix, Quaternion, Vector
except ImportError:
    Matrix = Vector = None


@unittest.skipIf(Matrix is None, "Requires Blender's mathutils")
class GeneralDimensionTests(unittest.TestCase):
    def setUp(self):
        @contextmanager
        def database_transaction():
            yield

        path = Path(__file__).with_name("PdfDrawings.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        nodes += [node for node in tree.body if isinstance(node, ast.ClassDef)
                  and node.name == "_StructureSegment"]
        self.ns = {
            "math": math, "Matrix": Matrix, "Quaternion": Quaternion, "Vector": Vector,
            "DIMENSION_DUPLICATE_TOLERANCE": 0.01,
            "colorsys": colorsys,
            "DimensionLayout": DimensionLayout,
            "Connections": SimpleNamespace(database_transaction=database_transaction),
        }
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in {
                    "PDF_DIMENSION_COLOR", "PDF_STRUCTURE_DIMENSION_COLORS",
                    "STRUCTURE_HORIZONTAL_TAG", "STRUCTURE_VERTICAL_TAG",
                } for target in node.targets
            ):
                exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), self.ns)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), self.ns)
        self.structure_rotation = self.ns['_structure_rotation']
        self.ns.update({
            "_is_structure_object": lambda obj: obj.is_structure,
            "_connected_structure_groups": lambda objects: [[obj] for obj in objects],
            "_structure_rotation": lambda group: group[0].rotation,
            "_build_structure_segments": lambda group: [
                self.ns["_StructureSegment"](group, quote_axis="X")
            ],
            "_layher_center_span": lambda *args: None,
            "_object_uid": lambda obj: obj.name_full,
        })
        self.scene = SimpleNamespace(render=SimpleNamespace(resolution_x=1200, resolution_y=850))

    def object(self, name, position, angle=0, is_structure=True):
        rotation = Matrix.Rotation(angle, 3, "Z")
        return SimpleNamespace(
            name_full=name, rotation=rotation, is_structure=is_structure,
            matrix_world=Matrix.Translation(Vector(position)) @ rotation.to_4x4(),
            bound_box=list(itertools.product((-2.0, 2.0), (-0.2, 0.2), (-0.2, 0.2))),
        )

    def project(self, candidates, view, allowed=None):
        direction, up = self.ns["_view_direction_and_up"](view, Matrix.Identity(3))
        rotation = self.ns["_camera_rotation_from_direction"](direction, up)
        camera = SimpleNamespace(
            rotation_euler=rotation, location=Vector((0, -20, 10)),
            data=SimpleNamespace(ortho_scale=20),
        )
        data = self.ns["_view_dimension_data"](
            self.scene, camera, Vector((0, 0, 0)), candidates, view,
            Matrix.Identity(3), allowed_dimension_candidate_indices=allowed,
        )
        return data["axes"] if data is not None else []

    def test_general_reuses_individual_candidates_and_ignores_nonstructures(self):
        objects = [self.object("A", (0, 0, 0)), self.object("B", (5, 2, 0), math.pi / 4)]
        candidates = self.ns["_build_general_dimension_candidates"](
            objects + [self.object("light", (20, 0, 0), is_structure=False)]
        )
        self.assertEqual([c["index"] for c in candidates], [0, 1])
        self.assertNotEqual(candidates[0]["color"], candidates[1]["color"])
        for obj, candidate in zip(objects, candidates):
            individual = self.ns["_build_dimension_candidates"](
                self.ns["_build_structure_segments"]([obj]), obj.rotation,
                color=self.ns["_structure_dimension_color"](candidate["structure_index"]),
            )[0]
            for key in ("axis", "value", "segment", "span", "is_overall", "color"):
                self.assertEqual(candidate[key], individual[key])
        self.assertEqual(self.ns["_build_general_dimension_candidates"]([]), [])

    def test_equal_dimensions_on_different_structures_are_preserved(self):
        candidates = self.ns["_build_general_dimension_candidates"]([
            self.object("A", (0, 0, 0)), self.object("B", (0, 0, 3)),
        ])
        self.assertEqual(len(self.project(candidates, "Front")), 2)
        self.assertEqual(len(self.project(candidates, "Iso", {1})), 1)

    def test_rotated_dimensions_use_world_projection_and_actual_length(self):
        candidates = self.ns["_build_general_dimension_candidates"]([
            self.object("A", (0, 0, 0)), self.object("B", (5, 0, 0), math.pi / 2),
        ])
        self.assertEqual([a["candidate"]["index"] for a in self.project(candidates, "Front")], [0])
        self.assertEqual([a["candidate"]["index"] for a in self.project(candidates, "Left")], [1])
        top_axes = self.project(candidates, "Top")
        self.assertEqual(len(top_axes), 2)
        for axis in top_axes:
            self.assertAlmostEqual(axis["value"], 4.0, places=5)
            self.assertAlmostEqual((axis["p2"] - axis["p1"]).length, 4.0, places=5)

    def test_joint_dimensions_follow_each_connected_run_axis(self):
        joint = self.object("joint", (0, 0, 0))
        runs = [self.object(name, (0, 0, 0)) for name in ("left", "right", "up")]
        neighbors = {"joint": runs, **{run.name_full: [joint] for run in runs}}
        self.ns.update({
            "_is_truss_joint_object": lambda obj: obj is joint,
            "_connected_truss_neighbors": lambda obj, visible: neighbors[obj.name_full],
            "_truss_quote_axis": lambda objects, rotation: (
                "Z" if objects[0].name_full == "up" else "X"
            ),
        })
        segments = self.ns["_build_truss_segments"]([joint] + runs)
        joint_segments = [segment for segment in segments if segment[0] is joint]
        self.assertEqual([segment.quote_axis for segment in joint_segments], ["X", "Z"])
        self.assertEqual(len(segments), 5)
        self.assertFalse(any(getattr(segment, "is_overall", False) for segment in segments))

    def test_equal_joint_dimensions_are_not_merged_on_individual_pages(self):
        objects = [self.object("joint A", (0, 0, 0)), self.object("joint B", (0, 0, 3))]
        self.ns["_is_truss_joint_object"] = lambda obj: True
        segments = [self.ns["_StructureSegment"]([obj], quote_axis="X") for obj in objects]
        candidates = self.ns["_build_dimension_candidates"](segments, Matrix.Identity(3))
        self.assertTrue(all(c["is_joint"] for c in candidates))
        self.assertEqual(len(self.project(candidates, "Front")), 2)
        self.assertEqual(len(self.project(candidates, "Iso")), 2)

    def test_dimensions_repeat_in_each_visible_view(self):
        objects = [self.object("A", (0, 0, 0)), self.object("B", (5, 0, 0), math.pi / 2)]
        candidates = self.ns["_build_general_dimension_candidates"](objects)
        expected = {'Front': [0], 'Left': [1], 'Top': [0, 1], 'Iso': [0, 1]}
        for view in ('Front', 'Left', 'Top', 'Iso', 'Front'):
            axes = self.project(candidates, view)
            self.assertEqual([a['candidate']['index'] for a in axes], expected[view])
            for axis in axes:
                self.assertAlmostEqual(axis['value'], 4.0, places=5)

    def test_duplicates_are_removed_independently_within_each_view(self):
        obj = self.object('A', (0, 0, 0))
        for candidates in (
            self.ns['_build_general_dimension_candidates']([obj]),
            self.ns['_build_dimension_candidates'](
                self.ns['_build_structure_segments']([obj]), obj.rotation,
            ),
        ):
            duplicated = candidates + [dict(candidates[0], index=1)]
            for view in ('Front', 'Top', 'Iso'):
                axes = self.project(duplicated, view)
                self.assertEqual(len(axes), 1)
                self.assertEqual(axes[0]['candidate']['index'], 0)

    def test_disabled_dimensions_remain_empty_in_all_views(self):
        for view in ('Front', 'Left', 'Top', 'Iso'):
            self.assertEqual(self.project([], view), [])

    def vertical_candidates(self, second_height=3):
        objects = [self.object('left', (-8, 0, 3)), self.object('right', (8, 2, second_height))]
        for obj in objects:
            obj.bound_box = list(itertools.product((-.145, .145), (-.145, .145), (-3, 3)))
        segments = [self.ns['_StructureSegment']([obj], quote_axis='Z') for obj in objects]
        return self.ns['_build_dimension_candidates'](segments, Matrix.Identity(3))

    def test_iso_deduplicates_equal_heights_despite_depth_shift(self):
        candidates = self.vertical_candidates()
        for view in ('Front', 'Iso', 'Front'):
            axes = self.project(candidates, view)
            self.assertEqual(len(axes), 1)
            self.assertAlmostEqual(axes[0]['value'], 6.0)

    def test_iso_preserves_equal_lengths_at_different_heights(self):
        self.assertEqual(len(self.project(self.vertical_candidates(second_height=4), 'Iso')), 2)

    def test_iso_deduplication_follows_rotated_structure_axis(self):
        rotation = Matrix.Rotation(.4, 3, 'X')
        segments = []
        for candidate in self.vertical_candidates():
            obj = candidate['segment'][0]
            obj.matrix_world = Matrix.Translation(Vector((10, 5, 2))) @ rotation.to_4x4() @ obj.matrix_world
            segments.append(self.ns['_StructureSegment']([obj], quote_axis='Z'))
        candidates = self.ns['_build_dimension_candidates'](segments, rotation)
        candidates = [dict(c, structure_rotation=rotation) for c in candidates]
        axes = self.project(candidates, 'Iso')
        self.assertEqual(len(axes), 1)
        self.assertAlmostEqual(axes[0]['value'], 6.0, places=5)

    def test_iso_preserves_same_span_on_different_structures(self):
        candidates = [dict(c, structure_index=i) for i, c in enumerate(self.vertical_candidates())]
        self.assertEqual(len(self.project(candidates, 'Iso')), 2)

    def test_iso_preserves_equal_lengths_on_different_axes(self):
        obj = self.object('box', (0, 0, 0))
        obj.bound_box = list(itertools.product((-3, 3), (-3, 3), (-3, 3)))
        segments = [self.ns['_StructureSegment']([obj], quote_axis=axis) for axis in ('X', 'Y', 'Z')]
        candidates = self.ns['_build_dimension_candidates'](segments, Matrix.Identity(3))
        self.assertEqual(len(self.project(candidates, 'Iso')), 3)

    def test_render_solves_layout_once_even_when_camera_fit_changes(self):
        camera_data = SimpleNamespace(ortho_scale=20)
        camera = SimpleNamespace(data=camera_data)
        rendered_image = SimpleNamespace(size=(1, 1), pixels=[1, 1, 1, 1])
        scene = SimpleNamespace(collection=SimpleNamespace(objects=SimpleNamespace(link=Mock())),
                                render=SimpleNamespace(engine='BLENDER_WORKBENCH'))
        context = SimpleNamespace(scene=scene, view_layer=SimpleNamespace(update=Mock()))
        create_dimensions = Mock(return_value=([object()], None))
        fit = Mock(return_value=(True, Vector((1, 2, 3))))
        self.ns.update({
            'time': time, 'Path': Path, 'PDF_WORKBENCH_OUTLINE_INK_GAIN': 1,
            'bpy': SimpleNamespace(
                data=SimpleNamespace(cameras=SimpleNamespace(new=Mock(return_value=camera_data), remove=Mock()),
                                     objects=SimpleNamespace(new=Mock(return_value=camera), remove=Mock()),
                                     images=SimpleNamespace(load=Mock(return_value=rendered_image), remove=Mock())),
                ops=SimpleNamespace(render=SimpleNamespace(render=Mock())),
            ),
            '_set_camera_view': Mock(), '_expand_camera_for_dimensions': Mock(),
            '_view_dimension_data': Mock(return_value={'axes': []}),
            '_create_dimension_render_objects': create_dimensions,
            '_remove_dimension_render_objects': Mock(),
            '_fit_camera_to_projected_objects': fit,
            '_image_pixels_to_pdf_rgb': Mock(return_value='image'),
        })
        result = self.ns['_render_view'](context, 'Front', Vector((0, 0, 0)), [], [], Matrix.Identity(3), '.')
        self.assertEqual(result, 'image')
        create_dimensions.assert_called_once()
        fit.assert_called_once()

    def test_camera_fit_keeps_annotations_inside_actual_frame(self):
        low, high = Vector((-10, -8, 0)), Vector((16, 12, 0))
        self.ns['_projected_box'] = Mock(return_value=(low, high))
        for aspect in (1200 / 850, 850 / 1200):
            data = SimpleNamespace(ortho_scale=20)
            data.view_frame = lambda scene: [Vector((x * data.ortho_scale / 2,
                                                     y * data.ortho_scale / (2 * aspect), -1))
                                             for x, y in itertools.product((-1, 1), repeat=2)]
            camera = SimpleNamespace(data=data, rotation_euler=Matrix.Identity(3).to_euler(),
                                     location=Vector((0, 0, 20)))
            changed, center = self.ns['_fit_camera_to_projected_objects'](
                self.scene, camera, [object()], Vector((0, 0, 0)),
            )
            self.assertTrue(changed)
            frame = data.view_frame(self.scene)
            for axis in (0, 1):
                self.assertGreaterEqual(low[axis] - center[axis], min(p[axis] for p in frame))
                self.assertLessEqual(high[axis] - center[axis], max(p[axis] for p in frame))

    def test_structure_orientation_ignores_layher_brace_slope(self):
        for yaw, tilt in ((0, 0), (.3, 0), (.3, .2)):
            rotation = Matrix.Rotation(yaw, 3, 'Z') @ Matrix.Rotation(tilt, 3, 'X')
            post = self.object('post', (0, 0, 0))
            post.matrix_world = rotation.to_4x4()
            post.bound_box = list(itertools.product((-.03, .03), (-.03, .03), (0, 2)))
            rail = self.object('rail', (0, 0, 0))
            rail.matrix_world = rotation.to_4x4()
            rail.bound_box = list(itertools.product((0, 2.57), (-.03, .03), (-.03, .03)))
            brace = self.object('brace', (0, 0, 0))
            brace.matrix_world = (rotation @ Matrix.Rotation(.55, 3, 'X')).to_4x4()
            brace.bound_box = list(itertools.product((-.03, .03), (-.03, .03), (0, 10)))
            self.ns['_has_tag'] = lambda obj, tag: (
                (obj is post and tag == 'structure-vertical')
                or (obj is rail and tag == 'structure-horizontal')
            )
            for members in ([brace, rail, post], [post, rail, brace], [brace, post]):
                actual = self.structure_rotation(members)
                for i in range(3):
                    for j in range(3):
                        self.assertAlmostEqual(actual[i][j], rotation[i][j], places=5)
                for view in ('Front', 'Left'):
                    direction, up = self.ns['_view_direction_and_up'](view, actual)
                    camera = self.ns['_camera_rotation_from_direction'](direction, up).to_matrix()
                    projected_post = camera.inverted() @ (rotation @ Vector((0, 0, 1)))
                    self.assertAlmostEqual(projected_post.x, 0, places=5)

    def test_untagged_structure_keeps_existing_orientation_fallback(self):
        obj = self.object('truss', (0, 0, 0), angle=.3)
        self.ns['_has_tag'] = lambda obj, tag: False
        actual = self.structure_rotation([obj])
        for i in range(3):
            for j in range(3):
                self.assertAlmostEqual(actual[i][j], obj.rotation[i][j], places=5)


if __name__ == "__main__":
    unittest.main(argv=[__file__])
