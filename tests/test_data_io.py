"""Exercise the real data dependencies without a browser or network access."""

import tempfile
import unittest
from io import StringIO
from pathlib import Path

import pandas as pd

from tools.scrapers import ais_positions, movement


class DataDependencyTests(unittest.TestCase):
    def test_html_tables_can_be_read_with_each_supported_parser(self):
        # pandas alone imports successfully even when read_html dependencies
        # are missing. Test both its primary parser and fallback parser.
        html = "<table><tr><th>Place</th></tr><tr><td>Tokyo</td></tr></table>"
        for flavor in ("lxml", "bs4"):
            with self.subTest(flavor=flavor):
                frame = pd.read_html(StringIO(html), flavor=flavor)[0]
                self.assertEqual(frame.to_dict("records"), [{"Place": "Tokyo"}])

    def test_movement_csvs_merge_different_columns_and_filter_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.csv"
            second = Path(directory) / "second.csv"
            first.write_text(
                "LLI NO,Status,Name\n2,Live,Ship A\n1,Live,Ship B\n3,Dead,Ship C\n",
                encoding="utf-8",
            )
            second.write_text(
                "LLI NO,Status,Flag\n2,Live,JP\n4,Live,GB\n,Live,US\n",
                encoding="utf-8",
            )

            result = movement.load_live_llinos_from_vessel_files([first, second])

            self.assertEqual(result["targets"], [1, 2, 4])
            self.assertEqual(result["target_count"], 3)
            self.assertEqual(result["live_vessel_df"].height, 4)
            self.assertEqual(result["source_summary"]["selected_rows"].to_list(), [2, 2])

    def test_ais_split_csvs_supply_vessel_types_and_filter_status(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "bulk.csv").write_text(
                "LLI NO,Status,Name\n2,Live,Ship A\n1,Live,Ship B\n3,Dead,Ship C\n",
                encoding="utf-8",
            )
            (Path(directory) / "tanker.csv").write_text(
                "LLI NO,Status,Flag\n4,Live,JP\n",
                encoding="utf-8",
            )

            result = ais_positions.load_ais_vessel_source({
                "source_mode": "split_files",
                "dir_vessel": directory,
                "vessel_type_list": ["bulk", "tanker"],
                "file_template": "{vessel_type}.csv",
            })

            self.assertEqual(result["llino_list_dict"], {"bulk": [1, 2], "tanker": [4]})
            self.assertEqual(result["target_vessel_df"].height, 3)


if __name__ == "__main__":
    unittest.main()
