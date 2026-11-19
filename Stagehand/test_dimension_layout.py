import math
import unittest
from unittest.mock import patch

import DimensionLayout as layout


class DimensionLayoutTests(unittest.TestCase):
    def feature(self, p1, p2, width=1.0, group=0, perimeter=None):
        return dict(edges=[(p1, p2)], center=(0, 0), text_width=width, group=group,
                    perimeter=perimeter or [(-5, -2), (5, -2), (5, 2), (-5, 2)])

    def test_intersection_handles_crossing_collinear_and_disjoint_segments(self):
        self.assertTrue(layout.segments_intersect(((0, 0), (2, 2)), ((0, 2), (2, 0))))
        self.assertTrue(layout.segments_intersect(((0, 0), (2, 0)), ((1, 0), (3, 0))))
        self.assertFalse(layout.segments_intersect(((0, 0), (1, 0)), ((2, 0), (3, 0))))

    def test_extension_crossings_are_allowed_but_dimension_crossings_are_not(self):
        a = dict(main=[], extensions=[((0, 0), (2, 2))], label_box=[(5, 5), (6, 5), (6, 6), (5, 6)])
        b = dict(main=[], extensions=[((0, 2), (2, 0))], label_box=[(8, 8), (9, 8), (9, 9), (8, 9)])
        self.assertEqual(layout.pair_conflicts(a, b), 0)
        a['main'] = a['extensions']
        a['extensions'] = []
        self.assertGreater(layout.pair_conflicts(a, b), 0)

    def test_nested_dimensions_stay_outside_and_labels_are_centered(self):
        features = [self.feature((-5, 2), (5, 2)), self.feature((-2, 2), (2, 2))]
        placements, conflicts = layout.place_dimensions(features, [features[0]['perimeter']], .3)
        self.assertEqual(conflicts, 0)
        self.assertEqual(len(placements), 2)
        for placement in placements:
            self.assertFalse(layout.polygons_overlap(placement['label_box'], features[0]['perimeter']))
            q1, q2 = placement['main'][0]
            self.assertAlmostEqual(placement['label'][0], (q1[0] + q2[0]) / 2)
            for source, extension in zip(placement['feature']['edges'][0], placement['extensions']):
                self.assertAlmostEqual(math.dist(source, extension[0]), .3 * .3)

    def test_short_adjacent_dimensions_do_not_overlap(self):
        features = [self.feature((x, 2), (x + .29, 2), width=1.2) for x in (-1, 0, 1)]
        placements, conflicts = layout.place_dimensions(features, [features[0]['perimeter']], .3)
        self.assertEqual(conflicts, 0)
        self.assertEqual(len(placements), 3)

    def test_empty_and_degenerate_dimensions(self):
        self.assertEqual(layout.place_dimensions([], [], .3), ([], 0))
        self.assertEqual(layout.place_dimensions([self.feature((0, 0), (0, 0))], [], .3), ([], 0))

    def test_placement_avoids_other_objects_and_is_deterministic(self):
        feature = self.feature((-5, 2), (5, 2))
        obstacles = [feature['perimeter'], [(-6, 2.1), (6, 2.1), (6, 4), (-6, 4)]]
        placements, conflicts = layout.place_dimensions([feature], obstacles, .3)
        self.assertEqual(conflicts, 0)
        self.assertEqual((placements, conflicts), layout.place_dimensions([feature], obstacles, .3))
        for polygon in obstacles:
            self.assertFalse(layout.polygons_overlap(placements[0]['label_box'], polygon))
            self.assertFalse(any(layout.segment_hits_polygon(s, polygon) for s in placements[0]['main']))

    def test_diagonal_text_center_and_uniform_lanes(self):
        feature = self.feature((-2, -2), (2, 2))
        placements, conflicts = layout.place_dimensions([feature], [feature['perimeter']], .3)
        self.assertEqual(conflicts, 0)
        placement = placements[0]
        a, b = placement['main'][0]
        midpoint = layout.mul(layout.add(a, b), .5)
        self.assertAlmostEqual(layout.dot(layout.sub(placement['label'], midpoint), placement['direction']), 0)
        level = layout.dot(a, placement['normal'])
        boundary = max(layout.dot(p, placement['normal']) for p in feature['perimeter'])
        lane = (level - boundary - .3 * 1.1) / (.3 * 1.8)
        self.assertAlmostEqual(lane, round(lane))

    def test_shortest_exit_does_not_detour_to_avoid_another_dimension(self):
        feature = self.feature((-5, 2), (5, 2))
        feature['shortest_exit'] = True
        blocker = self.feature((-3, -1), (6, -1),
                               perimeter=[(-3, -2), (6, -2), (6, -1), (-3, -1)])
        blocker['shortest_exit'] = True
        obstacles = [feature['perimeter'], blocker['perimeter'],
                     [(-4, -5), (6, -5), (6, -2), (-4, -2)]]
        placements, _ = layout.place_dimensions([feature, blocker], obstacles, .3)
        self.assertTrue(all(p['normal'][1] > 0 for p in placements))
        self.assertAlmostEqual(placements[0]['offset'], .3 * 1.1)

    def test_shortest_exit_can_switch_sides_to_avoid_geometry(self):
        feature = self.feature((-4, 1.8), (4, 1.8))
        feature['shortest_exit'] = True
        roof = [(-6, 2.01), (6, 2.01), (6, 20), (-6, 20)]
        placements, conflicts = layout.place_dimensions([feature], [feature['perimeter'], roof], .3)
        self.assertEqual(conflicts, 0)
        self.assertLess(placements[0]['normal'][1], 0)

    def test_budget_exhaustion_keeps_every_dimension_and_bounds_work(self):
        features = [self.feature((x * .1, 2), (x * .1 + .29, 2)) for x in range(18)]
        with patch.object(layout, 'pair_conflicts', wraps=layout.pair_conflicts) as comparisons:
            placements, conflicts = layout.place_dimensions(features, [], .3, refinement_budget=0)
        count = len(features)
        self.assertLessEqual(comparisons.call_count, 24 * count * (count - 1) // 2 + count * count)
        self.assertEqual([p['feature'] for p in placements], features)
        self.assertEqual(conflicts, sum(layout.pair_conflicts(a, b)
                                       for i, a in enumerate(placements) for b in placements[i + 1:]))

    def test_far_apart_annotations_skip_detailed_geometry(self):
        a = layout._placement(self.feature((0, 0), (1, 0)), ((0, 0), (1, 0)), (0, 1), 1, .3)
        b = layout._placement(self.feature((100, 0), (101, 0)), ((100, 0), (101, 0)), (0, 1), 1, .3)
        with patch.object(layout, 'polygons_overlap', side_effect=AssertionError('Unnecessary geometry check')):
            self.assertEqual(layout.pair_conflicts(a, b), 0)


if __name__ == '__main__':
    unittest.main()
