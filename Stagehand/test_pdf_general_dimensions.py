"""Dimension geometry regressions. Run with Blender's Python for mathutils."""

import ast
import colorsys
import itertools
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
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
        }
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in {
                    "PDF_DIMENSION_COLOR", "PDF_STRUCTURE_DIMENSION_COLORS",
                } for target in node.targets
            ):
                exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), self.ns)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), self.ns)
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


if __name__ == "__main__":
    unittest.main(argv=[__file__])
