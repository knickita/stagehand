import ast
import json
import unittest
from pathlib import Path

from LinkTypes import (
    are_link_types_compatible,
    default_link_allow_rotations,
    get_child_link_rotation_constraint,
    snap_child_link_rotation_degrees,
)


ADDON_DIRECTORY = Path(__file__).resolve().parent


class AudioArrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        catalogue = json.loads(
            (ADDON_DIRECTORY / "Catalogue.json").read_text(encoding="utf-8")
        )
        recipes = json.loads(
            (ADDON_DIRECTORY / "Recipes.json").read_text(encoding="utf-8")
        )
        cls.vio = next(
            item for item in catalogue["items"] if item["uniqueId"] == 1
        )
        cls.recipe = next(
            item for item in recipes["items"] if item["id"] == "vio_array"
        )

    def test_vio_model_and_links(self):
        self.assertTrue(
            (ADDON_DIRECTORY / f"{self.vio['mesh3d']}.glb").is_file()
        )
        self.assertEqual(
            [link["type"] for link in self.vio["links"]],
            [48, 49],
        )
        self.assertEqual(self.vio["links"][0]["posdir"][:3], [0.0, 0.0, 0.0])
        self.assertEqual(
            self.vio["links"][1]["posdir"][:3],
            [0.0, 0.0, -0.26],
        )
        self.assertIn("audioarray", self.vio["tags"])
        self.assertIn("audiosource", self.vio["tags"])

    def test_vio_links_are_compatible_without_roll(self):
        self.assertTrue(are_link_types_compatible(48, 49))
        self.assertTrue(are_link_types_compatible(49, 48))
        self.assertEqual(default_link_allow_rotations(48), "none")
        self.assertEqual(default_link_allow_rotations(49), "none")
        self.assertEqual(
            [link["allowrotations"] for link in self.vio["links"]],
            ["none", "none"],
        )

    def test_vio_child_rotation_constraint(self):
        constraint = get_child_link_rotation_constraint(49, 48)
        self.assertEqual(constraint.axis, "X")
        self.assertEqual(constraint.min_degrees, 0.0)
        self.assertEqual(constraint.max_degrees, 10.0)
        self.assertEqual(constraint.step_degrees, 1.0)
        self.assertIsNone(get_child_link_rotation_constraint(48, 49))

        expected_angles = {
            -1.0: 0.0,
            0.0: 0.0,
            2.49: 2.0,
            2.5: 3.0,
            2.7: 3.0,
            9.6: 10.0,
            11.0: 10.0,
        }
        for requested, expected in expected_angles.items():
            with self.subTest(requested=requested):
                self.assertEqual(
                    snap_child_link_rotation_degrees(49, 48, requested),
                    expected,
                )

    def test_vio_recipe_uses_both_catalogue_links(self):
        settings = self.recipe["settings"]
        self.assertEqual(self.recipe["builder"], "linked_array")
        self.assertEqual(self.recipe["assetId"], self.vio["uniqueId"])
        self.assertEqual(settings["incomingLink"], 0)
        self.assertEqual(settings["outgoingLink"], 1)
        self.assertEqual(
            settings["maxItems"],
            self.recipe["parameters"]["count"]["max"],
        )

    def test_linked_array_builder_is_registered(self):
        source = ast.parse(
            (ADDON_DIRECTORY / "Recipes.py").read_text(encoding="utf-8")
        )
        registered_builders = {
            node.args[0].value
            for node in ast.walk(source)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "register_builder"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        }
        self.assertIn("linked_array", registered_builders)

    def test_audio_array_hierarchy_hooks_are_present(self):
        source = ast.parse(
            (ADDON_DIRECTORY / "Connections.py").read_text(encoding="utf-8")
        )
        function_names = {
            node.name for node in source.body if isinstance(node, ast.FunctionDef)
        }
        self.assertIn("sync_constrained_link_hierarchy", function_names)
        self.assertIn("_enforce_constrained_child_transform", function_names)
        self.assertIn("_ensure_constrained_child_rotation_limit", function_names)


if __name__ == "__main__":
    unittest.main()
