"""Test PDF raster colors without importing the Blender add-on."""

import ast
from pathlib import Path
import unittest

try:
    import numpy as np
except ImportError:
    np = None


def load_conversion_functions():
    source_path = Path(__file__).with_name("PdfDrawings.py")
    source = ast.parse(source_path.read_text(encoding="utf-8"))
    names = {
        "PDF_NEUTRAL_COLOR_TOLERANCE",
        "PDF_WORKBENCH_OUTLINE_INK_GAIN",
        "_image_pixels_to_pdf_rgb",
        "_image_pixels_to_pdf_rgb_numpy",
        "_image_pixels_to_pdf_rgb_python",
    }
    nodes = [
        node for node in source.body
        if (isinstance(node, ast.FunctionDef) and node.name in names)
        or (isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in names
            for target in node.targets
        ))
    ]
    namespace = {"np": np}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace


class PdfDrawingColorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conversion = load_conversion_functions()

    def converters(self):
        yield self.conversion["_image_pixels_to_pdf_rgb_python"]
        if np is not None:
            yield self.conversion["_image_pixels_to_pdf_rgb_numpy"]

    def test_neutral_outline_is_black_but_white_and_blue_are_unchanged(self):
        pixels = [
            0.75, 0.75, 0.75, 1.0,
            0.0, 0.0, 0.0, 0.25,
            1.0, 1.0, 1.0, 1.0,
            0.0, 0.0, 1.0, 1.0,
            0.0, 0.0, 1.0, 0.5,
            0.0, 0.0, 0.0, 0.0,
        ]
        expected = bytes([
            0, 0, 0, 0, 0, 0, 255, 255, 255,
            0, 0, 255, 127, 127, 255, 255, 255, 255,
        ])
        for convert in self.converters():
            with self.subTest(converter=convert.__name__):
                self.assertEqual(convert((6, 1), pixels, 4.0)["data"], expected)

    def test_antialiased_edge_keeps_intermediate_tones(self):
        for convert in self.converters():
            result = convert((1, 1), [0.875, 0.875, 0.875, 1.0], 4.0)
            self.assertEqual(result["data"], bytes([127, 127, 127]))

    def test_default_conversion_does_not_apply_workbench_correction(self):
        for convert in self.converters():
            result = convert((1, 1), [0.75, 0.75, 0.75, 1.0])
            self.assertEqual(result["data"], bytes([191, 191, 191]))

    def test_rows_are_still_flipped(self):
        pixels = [0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
        for convert in self.converters():
            self.assertEqual(
                convert((1, 2), pixels, 4.0)["data"],
                bytes([255, 255, 255, 0, 0, 255]),
            )


if __name__ == "__main__":
    unittest.main()
