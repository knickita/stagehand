import ast
import tempfile
import unittest
from pathlib import Path

from RentmanCsv import (
    CUBE_CATEGORY_SUFFIXES,
    CUBE_SPECIAL_ITEM_PREFIXES,
    RentmanConfigError,
    collect_export_rows,
    collect_special_item_rows,
    load_export_config,
    parse_export_config,
    write_export_csv,
)


class RentmanCsvTests(unittest.TestCase):
    def test_groups_items_and_applies_mapping(self):
        config = parse_export_config({
            "itemMappings": {"7": "RM-22", "2": 901},
            "specialItems": {"litecPin": "526", "litecSpigot": "527", "litecSpring": "528"},
        })

        rows = collect_export_rows([7, 2, 7, 7, 2], config["itemMappings"])

        self.assertEqual(rows, [(2, "901"), (3, "RM-22")])

    def test_rejects_missing_mapping(self):
        with self.assertRaisesRegex(RentmanConfigError, "IDs: 8"):
            collect_export_rows([7, 8], {7: "RM-22"})

    def test_rejects_duplicate_equipment_codes(self):
        with self.assertRaisesRegex(RentmanConfigError, "mapped from both"):
            parse_export_config({
                "itemMappings": {"1": "RM-1", "2": "RM-1"},
            })

    def test_writes_expected_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.csv"
            write_export_csv(path, [(2, "100"), (1, "RM,200")])

            self.assertEqual(
                path.read_text(encoding="utf-8"),
                'Code,Quantity,Remark\n100,2,\n"RM,200",1,\n',
            )

    def test_special_item_quantities_match_pdf_totals(self):
        rows = collect_special_item_rows(
            {
                "ovetti_tratte": 8,
                "chiodi_coppiglie_tratte": 16,
                "chiodi_coppiglie_cubi_basi": 12,
            },
            {"litecPin": "526", "litecSpigot": "527", "litecSpring": "528"},
        )

        self.assertEqual(rows, [(28, "526"), (8, "527"), (28, "528")])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.csv"
            write_export_csv(path, [(2, "900")] + rows)
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "Code,Quantity,Remark\n900,2,\n526,28,\n527,8,\n528,28,\n",
            )

    def test_special_items_are_included_even_when_quantities_are_zero(self):
        rows = collect_special_item_rows(
            {
                "ovetti_tratte": 0,
                "chiodi_coppiglie_tratte": 0,
                "chiodi_coppiglie_cubi_basi": 0,
            },
            {"litecPin": "526", "litecSpigot": "527", "litecSpring": "528"},
        )
        self.assertEqual(rows, [(0, "526"), (0, "527"), (0, "528")])

    def test_rejects_missing_special_item(self):
        with self.assertRaisesRegex(RentmanConfigError, "litecSpring"):
            parse_export_config({
                "itemMappings": {"2": "900"},
                "specialItems": {"litecPin": "526", "litecSpigot": "527"},
            })

    def test_rejects_missing_special_items_section(self):
        with self.assertRaisesRegex(RentmanConfigError, "specialItems"):
            parse_export_config({"itemMappings": {"2": "900"}})

    def test_rejects_empty_special_item_code(self):
        with self.assertRaisesRegex(RentmanConfigError, "must not be empty"):
            parse_export_config({
                "itemMappings": {"2": "900"},
                "specialItems": {"litecPin": "", "litecSpigot": "527", "litecSpring": "528"},
            })

    def test_rejects_duplicate_special_item_code(self):
        with self.assertRaisesRegex(RentmanConfigError, "mapped from both"):
            parse_export_config({
                "itemMappings": {"2": "900"},
                "specialItems": {"litecPin": "526", "litecSpigot": "526", "litecSpring": "528"},
            })

    def test_rejects_special_item_code_already_used_by_a_catalogue_item(self):
        with self.assertRaisesRegex(RentmanConfigError, "mapped from both"):
            parse_export_config({
                "itemMappings": {"2": "526"},
                "specialItems": {"litecPin": "526", "litecSpigot": "527", "litecSpring": "528"},
            })

    def test_default_config_is_valid(self):
        directory = Path(__file__).resolve().parent
        config = load_export_config(directory / "RentmanExport.json")

        self.assertTrue(config["itemMappings"])
        required_keys = {"litecPin", "litecSpigot", "litecSpring"}
        required_keys.update(
            prefix + suffix
            for prefix in CUBE_SPECIAL_ITEM_PREFIXES.values()
            for suffix in CUBE_CATEGORY_SUFFIXES.values()
        )
        self.assertTrue(required_keys.issubset(config["specialItems"]))

    def test_maps_every_cube_category_for_both_sizes(self):
        for asset_id, prefix in CUBE_SPECIAL_ITEM_PREFIXES.items():
            for category, suffix in CUBE_CATEGORY_SUFFIXES.items():
                with self.subTest(asset_id=asset_id, category=category):
                    rows = collect_export_rows(
                        [asset_id, asset_id],
                        {asset_id: "GENERIC"},
                        {asset_id: [{"label": category, "quantity": 2}]},
                        {prefix + suffix: "CATEGORY"},
                    )
                    self.assertEqual(rows, [(2, "CATEGORY")])

    def test_cube_export_uses_the_categories_classified_by_the_pdf(self):
        directory = Path(__file__).resolve().parent
        # The classifier uses only builtins: test the real PDF function without Blender.
        pdf_tree = ast.parse((directory / "PdfDrawings.py").read_text(encoding="utf-8"))
        classifier_node = next(
            node for node in pdf_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_cube_category"
        )
        namespace = {}
        exec(compile(ast.Module(body=[classifier_node], type_ignores=[]), "PdfDrawings.py", "exec"), namespace)
        classify = namespace["_cube_category"]
        config = load_export_config(directory / "RentmanExport.json")
        cases = [
            ([0], "1Way"),
            ([0, 1], "2WayLine"),
            ([0, 2], "2WayL"),
            ([0, 1, 2], "3WayT"),
            ([0, 2, 4], "3WayL"),
            ([0, 1, 2, 4], "4WayL"),
            ([0, 1, 2, 3], "4WayCross"),
            ([0, 1, 2, 3, 4], "5Way"),
            ([0, 1, 2, 3, 4, 5], "6Way"),
        ]
        for asset_id, prefix in CUBE_SPECIAL_ITEM_PREFIXES.items():
            for active_indexes, suffix in cases:
                with self.subTest(asset_id=asset_id, active_indexes=active_indexes):
                    rows = collect_export_rows(
                        [asset_id], config["itemMappings"],
                        {asset_id: [{"label": classify(active_indexes), "quantity": 1}]},
                        config["specialItems"],
                    )
                    self.assertEqual(rows, [(1, config["specialItems"][prefix + suffix])])

    def test_cube_rows_replace_generic_rows_without_double_counting(self):
        rows = collect_export_rows(
            [2, 9, 9, 9, 34],
            {2: "900", 9: "2656", 34: "2672"},
            {
                9: [{"label": "1 via", "quantity": 1}, {"label": "2 vie ad angolo", "quantity": 2}],
                34: [{"label": "4 vie a croce", "quantity": 1}],
            },
            {"litec30Dado1Way": "5741", "litec30Dado2WayL": "5743", "litec40Dado4WayCross": "8253"},
        )
        self.assertEqual(rows, [(1, "900"), (1, "5741"), (2, "5743"), (1, "8253")])
        self.assertEqual(sum(quantity for quantity, _code in rows), 5)

    def test_connected_cubes_do_not_require_generic_mapping(self):
        rows = collect_export_rows(
            [9], {}, {9: [{"label": "1 via", "quantity": 1}]},
            {"litec30Dado1Way": "5741"},
        )
        self.assertEqual(rows, [(1, "5741")])

    def test_unconnected_cubes_use_generic_mapping(self):
        rows = collect_export_rows(
            [9, 34], {9: "2656", 34: "2672"},
            {
                9: [{"label": "senza vie", "quantity": 1}],
                34: [{"label": "senza vie", "quantity": 1}],
            }, {},
        )
        self.assertEqual(rows, [(1, "2656"), (1, "2672")])

    def test_rejects_missing_cube_category_code(self):
        with self.assertRaisesRegex(RentmanConfigError, "litec30Dado2WayL"):
            collect_export_rows(
                [9], {9: "2656"}, {9: [{"label": "2 vie ad angolo", "quantity": 1}]}, {},
            )

    def test_rejects_inconsistent_cube_counts(self):
        with self.assertRaisesRegex(RentmanConfigError, "inconsistent cube classifications"):
            collect_export_rows(
                [9, 9], {9: "2656"}, {9: [{"label": "1 via", "quantity": 1}]},
                {"litec30Dado1Way": "5741"},
            )


if __name__ == "__main__":
    unittest.main()
