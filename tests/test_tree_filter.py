import json
from pathlib import Path
import shutil
import tempfile
import unittest

import tmux_tree_filter as tree


DATA = (
    "%1\t$1\t* ▾ work (2min ago)\twork\n"
    "%1\t@1\t    ├─ * 0: cleanup\t0: cleanup\n"
    "%2\t@2\t    └─   1: login\t1: login\n"
    "%4\t$3\t  ▾ frontend (3min ago)\tfrontend\n"
    "%4\t@4\t    └─   0: other\t0: other\n"
    "%3\t$2\t  ▾ backend (1min ago)\tbackend\n"
    "%3\t@3\t    └─   0: login docs\t0: login docs\n"
)


@unittest.skipUnless(shutil.which("fzf"), "fzf unavailable")
class WindowTreeTests(unittest.TestCase):
    def rows(self, query):
        return [line.split("\t") for line in tree.filter_window_rows(DATA, query).splitlines()]

    def test_child_match_restores_parent_and_retargets_preview(self):
        rows = self.rows("'login")
        self.assertEqual([r[1] for r in rows], ["$1", "@2", "$2", "@3"])
        self.assertEqual(rows[0][0], "%2")
        self.assertIn("work", rows[0][2])
        self.assertIn("└─", rows[1][2])

    def test_session_match_keeps_entire_group(self):
        rows = self.rows("'work")
        self.assertEqual([r[1] for r in rows], ["$1", "@1", "@2"])
        self.assertIn("├─", rows[1][2])
        self.assertIn("└─", rows[2][2])

    def test_input_order_preserved_not_query_order_or_score(self):
        rows = self.rows("'docs | 'other | 'login")
        self.assertEqual([r[1] for r in rows], ["$1", "@2", "$3", "@4", "$2", "@3"])

    def test_header_and_child_keep_distinct_delete_targets(self):
        rows = self.rows("'login !docs")
        self.assertEqual(rows[0][:2], ["%2", "$1"])
        self.assertEqual(rows[1][:2], ["%2", "@2"])

    def test_only_names_and_window_indices_match_not_heading_time_or_ids(self):
        for query in ("'ago", "'min", "'$1", "'%1"):
            with self.subTest(query=query):
                self.assertEqual(self.rows(query), [])
        self.assertEqual([r[1] for r in self.rows("'1:")], ["$1", "@2"])

    def test_empty_query_restores_original_tree_and_missing_query_is_empty(self):
        self.assertEqual(tree.filter_window_rows(DATA, ""), DATA)
        self.assertEqual(tree.filter_window_rows(DATA, "absent-task"), "")
        self.assertEqual(tree.filter_window_rows("", "login"), "")

    def test_cached_filter_retains_load_time_without_rerunning_producer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "windows.json"
            initial = tree.window_output(path, "", ["printf", "%s", DATA])
            self.assertTrue(initial.startswith("\t\tLoaded in "))
            cached = path.read_text()
            filtered = tree.window_output(path, "'login")
            self.assertEqual(initial.splitlines()[0], filtered.splitlines()[0])
            self.assertEqual(len(filtered.splitlines()), 5)  # Metric + two parent/child pairs.
            self.assertEqual(path.read_text(), cached)
            self.assertEqual(json.loads(cached)["rows"], DATA)

    def test_metric_format(self):
        self.assertEqual(tree.load_label(0.1234), "Loaded in 123 ms")
        self.assertEqual(tree.load_label(1.234), "Loaded in 1.23 s")


if __name__ == "__main__":
    unittest.main()
