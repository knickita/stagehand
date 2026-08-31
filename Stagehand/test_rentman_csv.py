import tempfile
import unittest
from pathlib import Path

from RentmanCsv import (
    RentmanConfigError,
    collect_export_rows,
    load_export_config,
    parse_export_config,
    write_export_csv,
)


class RentmanCsvTests(unittest.TestCase):
    def test_groups_items_and_applies_mapping(self):
        config = parse_export_config({
            "itemMappings": {"7": "RM-22", "2": 901},
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

    def test_default_config_is_valid(self):
        directory = Path(__file__).resolve().parent
        config = load_export_config(directory / "RentmanExport.json")

        self.assertTrue(config["itemMappings"])


if __name__ == "__main__":
    unittest.main()
