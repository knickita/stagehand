"""Stage perimeter candidates and projection, using Blender's mathutils."""

import ast
import itertools
import math
from pathlib import Path
from types import SimpleNamespace
import unittest

import DimensionLayout
try:
    from mathutils import Matrix, Vector
except ImportError:
    Matrix = None


@unittest.skipIf(Matrix is None, "Requires Blender's mathutils")
class StagePerimeterTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name('PdfDrawings.py')
        tree = ast.parse(path.read_text(encoding='utf-8'))
        names = {'_stage_perimeter_candidates', '_project_dimension_candidate', '_camera_point',
                 '_segment_local_box', '_normalize_2d', '_dimension_axis_vector',
                 '_same_projected_point', '_same_projected_dimension', '_projected_dimensions_are_redundant'}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        self.ns = dict(math=math, Matrix=Matrix, Vector=Vector, DimensionLayout=DimensionLayout,
                       STAGE_DECK_THICKNESS_BY_TAG={'sixtema-stage-deck': .085},
                       DIMENSION_DUPLICATE_TOLERANCE=.01,
                       _has_tag=lambda obj, tag: tag in obj.tags)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), self.ns)

    def decks(self, rectangles, rotation=None, z=1):
        rotation = rotation if rotation is not None else Matrix.Identity(3)
        result = []
        for x0, y0, x1, y1 in rectangles:
            result.append(SimpleNamespace(
                matrix_world=rotation.to_4x4() @ Matrix.Translation(Vector((x0, y0, z - .085))),
                bound_box=list(itertools.product((0, x1-x0), (0, y1-y0), (0, .085))),
                tags={'sixtema-stage-deck'},
            ))
        return result

    def candidates(self, decks, rotation=None):
        result = self.ns['_stage_perimeter_candidates'](decks, rotation if rotation is not None else Matrix.Identity(3), (0, 0, 1, 1))
        for i, candidate in enumerate(result or []):
            candidate['index'] = i
        return result

    def test_rectangle_keeps_existing_two_axis_dimensions(self):
        self.assertIsNone(self.candidates(self.decks([(0, 0, 2, 1), (2, 0, 4, 1)])))

    def test_t_shape_uses_real_world_edges_and_lengths(self):
        candidates = self.candidates(self.decks([(-4, 0, 20, 4), (0, 4, 16, 8)], z=.485))
        self.assertEqual(len(candidates), 8)
        self.assertEqual(sorted(round(c['value'], 4) for c in candidates), [4]*6 + [16, 24])
        for c in candidates:
            self.assertAlmostEqual((c['world_edge'][1] - c['world_edge'][0]).length, c['value'], places=4)
            self.assertAlmostEqual(c['world_edge'][0].z, .485, places=4)

    def test_rotated_assembly_preserves_lengths_and_concave_edges(self):
        rotation = Matrix.Rotation(.73, 3, 'Z')
        candidates = self.candidates(self.decks([(-4, 0, 20, 4), (0, 4, 16, 8)], rotation), rotation)
        self.assertEqual(len(candidates), 8)
        self.assertEqual(sorted(round(c['value'], 4) for c in candidates), [4]*6 + [16, 24])

    def test_level_boundaries_are_not_merged(self):
        decks = self.decks([(0, 0, 4, 4)], z=.5) + self.decks([(0, 4, 4, 8)], z=1)
        candidates = self.candidates(decks)
        self.assertEqual(len(candidates), 8)
        self.assertEqual({round(c['world_edge'][0].z, 3) for c in candidates}, {.5, 1})

    def test_off_grid_panels_do_not_create_invented_rectangular_edges(self):
        decks = self.decks([(-4, 0, 20, 4), (0, 4, 16, 8)])
        decks[1].matrix_world = Matrix.Rotation(.4, 4, 'Z') @ decks[1].matrix_world
        self.assertIsNone(self.candidates(decks))

    def test_hole_boundary_is_included(self):
        candidates = self.candidates(self.decks([(0, 0, 4, 1), (0, 3, 4, 4), (0, 1, 1, 3), (3, 1, 4, 3)]))
        self.assertEqual(len(candidates), 8)
        self.assertEqual(sorted(c['value'] for c in candidates), [2]*4 + [4]*4)

    def test_equal_edges_are_distinct_in_top_and_iso(self):
        candidates = self.candidates(self.decks([(-4, 0, 20, 4), (0, 4, 16, 8)]))
        for view, rotation in [('Top', Matrix.Identity(3)), ('Iso', Matrix.Rotation(.8, 3, 'X') @ Matrix.Rotation(.6, 3, 'Z'))]:
            projected = [self.ns['_project_dimension_candidate'](c, rotation, Vector((0, 0, 0)), Matrix.Identity(3), Vector((0, 0, 0))) for c in candidates]
            for i, a in enumerate(projected):
                for b in projected[i+1:]:
                    self.assertFalse(self.ns['_projected_dimensions_are_redundant'](a, b, .01, view))
            self.assertTrue(self.ns['_projected_dimensions_are_redundant'](projected[0], projected[0], .01, view))

    def test_height_anchors_use_real_legs_including_rotated_stage(self):
        for angle in (0, .73):
            assembly = Matrix.Rotation(angle, 3, 'Z')
            legs = []
            for x, y in ((-3.94, .06), (19.94, .06), (15.94, 7.94), (.06, 7.94)):
                legs.append(SimpleNamespace(
                    matrix_world=assembly.to_4x4() @ Matrix.Translation(Vector((x, y, 0))),
                    bound_box=list(itertools.product((-.045, .045), (-.045, .045), (0, .4))),
                ))
            candidate = dict(axis='Z', value=.485, segment=legs, span=(0, .485), anchor_to_legs=True)
            for camera in (Matrix.Rotation(math.pi / 2, 3, 'X'),
                           Matrix.Rotation(1.1, 3, 'X') @ Matrix.Rotation(.8, 3, 'Z')):
                center = Vector((3, 2, 1))
                projected = self.ns['_project_dimension_candidate'](
                    candidate, camera, center, assembly, Vector((0, 0, 0)))
                allowed = []
                for leg in legs:
                    for x, y in itertools.product((-.045, .045), repeat=2):
                        points = []
                        for z in (0, .485):
                            point = camera.inverted() @ (leg.matrix_world @ Vector((x, y, z)) - center)
                            point.z = 0
                            points.append(point)
                        allowed.append(points)
                self.assertAlmostEqual(projected['value'], .485)
                self.assertEqual(len(projected['edge_options']), 2)
                for a, b in projected['edge_options'] + [(projected['p1'], projected['p2'])]:
                    a, b = Vector((a.x, a.y, 0)), Vector((b.x, b.y, 0))
                    self.assertTrue(any((a - p).length < .0001 and (b - q).length < .0001
                                        for p, q in allowed))

    def test_generic_vertical_dimensions_keep_legacy_box_anchors(self):
        objects = self.decks([(0, 0, 2, 1)])
        projected = self.ns['_project_dimension_candidate'](
            dict(axis='Z', value=.085, segment=objects, span=None),
            Matrix.Rotation(1.1, 3, 'X'), Vector((0, 0, 0)), Matrix.Identity(3), Vector((0, 0, 0)))
        self.assertEqual(len(projected['edge_options']), 4)


if __name__ == '__main__':
    unittest.main()
