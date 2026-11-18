"""Deterministic 2D dimension placement with explicit geometric conflicts."""

import math


def add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def mul(a, scale):
    return (a[0] * scale, a[1] * scale)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def cross(a, b, c):
    ab, ac = sub(b, a), sub(c, a)
    return ab[0] * ac[1] - ab[1] * ac[0]


def convex_hull(points):
    points = sorted(set(tuple(p[:2]) for p in points))
    if len(points) <= 2:
        return points
    def half(sequence):
        result = []
        for p in sequence:
            while len(result) > 1 and cross(result[-2], result[-1], p) <= 0:
                result.pop()
            result.append(p)
        return result
    return half(points)[:-1] + half(reversed(points))[:-1]


def segments_intersect(first, second, epsilon=1e-8):
    a, b = first
    c, d = second
    if (max(a[0], b[0]) + epsilon < min(c[0], d[0])
            or max(c[0], d[0]) + epsilon < min(a[0], b[0])
            or max(a[1], b[1]) + epsilon < min(c[1], d[1])
            or max(c[1], d[1]) + epsilon < min(a[1], b[1])):
        return False
    return (cross(a, b, c) * cross(a, b, d) <= epsilon ** 2
            and cross(c, d, a) * cross(c, d, b) <= epsilon ** 2)


def point_inside(point, polygon):
    if len(polygon) < 3:
        return False
    return all(cross(polygon[i - 1], polygon[i], point) >= -1e-8
               for i in range(len(polygon)))


def segments_cross(first, second, epsilon=1e-8):
    """Interior crossing; meeting at a shared datum is not a crossing."""
    a, b = first
    c, d = second
    return (cross(a, b, c) * cross(a, b, d) < -epsilon ** 2
            and cross(c, d, a) * cross(c, d, b) < -epsilon ** 2)


def segment_hits_polygon(segment, polygon):
    return (any(point_inside(p, polygon) for p in segment)
            or any(segments_intersect(segment, (polygon[i - 1], polygon[i]))
                   for i in range(len(polygon))))


def polygons_overlap(first, second):
    return (any(point_inside(p, second) for p in first)
            or any(segment_hits_polygon((first[i - 1], first[i]), second)
                   for i in range(len(first))))


def pair_conflicts(first, second):
    # Extension/extension crossings are deliberately allowed.
    count = int(polygons_overlap(first['label_box'], second['label_box']))
    for lines, other in ((first, second), (second, first)):
        count += sum(segment_hits_polygon(s, other['label_box'])
                     for s in lines['main'] + lines['extensions'])
        count += sum(segments_cross(a, b)
                     for a in lines['main'][:1] for b in other['extensions'])
    count += sum(segments_intersect(a, b)
                 for a in first['main'] for b in second['main'])
    return count


def _placement(feature, edge, normal, level, text_size):
    p1, p2 = edge
    vector = sub(p2, p1)
    length = math.hypot(*vector)
    direction = mul(vector, 1.0 / length)
    q1 = add(p1, mul(normal, level - dot(p1, normal)))
    q2 = add(p2, mul(normal, level - dot(p2, normal)))
    label = add(mul(add(q1, q2), 0.5), mul(normal, text_size * 0.8))
    half_width = feature['text_width'] * 0.5 + text_size * 0.15
    half_height = text_size * 0.65
    box = convex_hull([
        add(label, add(mul(direction, x * half_width), mul(normal, y * half_height)))
        for x in (-1, 1) for y in (-1, 1)
    ])
    gap, overshoot, tick = text_size * 0.3, text_size * 0.2, text_size * 0.28
    extensions = [(add(p, mul(normal, gap)), add(q, mul(normal, overshoot)))
                  for p, q in ((p1, q1), (p2, q2))]
    main = [(q1, q2)] + [(sub(q, mul(normal, tick)), add(q, mul(normal, tick)))
                         for q in (q1, q2)]
    return dict(label=label, label_box=box, main=main, extensions=extensions,
                direction=direction, offset=level - dot(mul(add(p1, p2), 0.5), normal),
                feature=feature, normal=normal)


def place_dimensions(features, obstacles, text_size):
    """Search uniform perimeter lanes, then improve all placements together.

    Hard conflicts precede distance and grouping preferences in the objective.
    Unsatisfiable dense projections retain all dimensions and report conflicts.
    """
    alternatives = []
    step, clearance = text_size * 1.8, text_size * 1.1
    for feature in features:
        p1, p2 = feature['edges'][0]
        direction = sub(p2, p1)
        length = math.hypot(*direction)
        if length < 1e-8:
            continue
        normal = (-direction[1] / length, direction[0] / length)
        midpoint = mul(add(p1, p2), 0.5)
        if dot(sub(midpoint, feature['center']), normal) < 0:
            normal = mul(normal, -1)
        choices = []
        for side in (1, -1):
            outward = mul(normal, side)
            edge = max(feature['edges'], key=lambda e: dot(mul(add(*e), 0.5), outward))
            boundary = max(dot(p, outward) for p in feature['perimeter'])
            for lane in range(max(12, min(48, len(features) * 2))):
                placement = _placement(feature, edge, outward, boundary + clearance + lane * step, text_size)
                object_hits = sum(
                    polygons_overlap(placement['label_box'], polygon)
                    or any(segment_hits_polygon(s, polygon) for s in placement['main'])
                    for polygon in obstacles
                )
                # Crossing another object with an extension is a softer penalty:
                # an interior feature sometimes has no unobstructed route out.
                extension_hits = sum(
                    segment_hits_polygon(s, polygon)
                    for s in placement['extensions'] for polygon in obstacles
                )
                placement['object_hits'] = object_hits
                placement['extension_hits'] = extension_hits
                placement['preference'] = placement['offset'] / step + (0.3 if side < 0 else 0)
                choices.append(placement)
        alternatives.append(choices)

    conflict_cache = {}

    def conflicts_between(first, second):
        key = tuple(sorted((id(first), id(second))))
        if key not in conflict_cache:
            conflict_cache[key] = pair_conflicts(first, second)
        return conflict_cache[key]

    def score(choice, others):
        conflicts = sum(conflicts_between(choice, other) for other in others)
        return (choice['object_hits'], conflicts, choice['extension_hits'], choice['preference'])

    # Short dimensions first prevents their extensions crossing a larger outer dimension.
    order = sorted(range(len(alternatives)), key=lambda i: (
        math.dist(*alternatives[i][0]['feature']['edges'][0]),
        alternatives[i][0]['feature']['group'], i,
    ))

    def total_score(chosen):
        values = list(chosen.values())
        return (
            sum(c['object_hits'] for c in values),
            sum(
                conflicts_between(a, b) for i, a in enumerate(values) for b in values[i + 1:]
            ),
            sum(c['extension_hits'] for c in values),
            sum(c['preference'] for c in values),
        )

    best_solution = None
    orders = [order, list(reversed(order)), sorted(order, key=lambda i: (
        alternatives[i][0]['feature']['group'], order.index(i),
    ))]
    for order in orders:
        chosen = {}
        for i in order:
            chosen[i] = min(alternatives[i], key=lambda c: score(c, chosen.values()))
        for _ in range(4):
            changed = False
            for i in order:
                others = [c for j, c in chosen.items() if j != i]
                best = min(alternatives[i], key=lambda c: score(c, others))
                if score(best, others) < score(chosen[i], others):
                    chosen[i] = best
                    changed = True
            if not changed:
                break
        if best_solution is None or total_score(chosen) < total_score(best_solution):
            best_solution = chosen
        if total_score(best_solution)[:2] == (0, 0):
            break
    # Escape local minima that require moving two conflicting dimensions together.
    if best_solution:
        for _ in range(3):
            improved = False
            for i in range(len(alternatives)):
                for j in range(i + 1, len(alternatives)):
                    if not conflicts_between(best_solution[i], best_solution[j]):
                        continue
                    others = [c for k, c in best_solution.items() if k not in (i, j)]
                    first_choices = sorted(alternatives[i], key=lambda c: score(c, others))[:8]
                    second_choices = sorted(alternatives[j], key=lambda c: score(c, others))[:8]
                    def pair_score(pair):
                        a, b = pair
                        sa, sb = score(a, others), score(b, others)
                        return (sa[0] + sb[0], sa[1] + sb[1] + conflicts_between(a, b),
                                sa[2] + sb[2], sa[3] + sb[3])
                    pair = min(((a, b) for a in first_choices for b in second_choices), key=pair_score)
                    if pair_score(pair) < pair_score((best_solution[i], best_solution[j])):
                        best_solution[i], best_solution[j] = pair
                        improved = True
            if not improved:
                break
    result = [best_solution[i] for i in range(len(alternatives))]
    conflicts = sum(c['object_hits'] for c in result) + sum(
        conflicts_between(a, b) for i, a in enumerate(result) for b in result[i + 1:]
    )
    return result, conflicts
