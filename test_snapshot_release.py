import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
import snapshot_release  # noqa: E402


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = self._dir.name

    def test_a_snapshot_of_this_code_passes_its_check(self):
        directory = snapshot_release.write_snapshot("v1.0.0", self.root)
        self.assertEqual(sorted(os.listdir(directory)), ["config.json", "history.db"])
        self.assertEqual(snapshot_release.check_snapshot("v1.0.0", self.root), [])
        self.assertEqual(snapshot_release.released_versions(self.root), ["v1.0.0"])

    def test_the_snapshot_has_a_miner_in_each_mode(self):
        saved = snapshot_release.snapshot_config()
        self.assertEqual(
            sorted(miner["mode"] for miner in saved["miners"]),
            ["balanced", "efficiency", "max_hashrate"],
        )
        self.assertTrue(saved["setup_done"])

    def test_a_released_snapshot_is_never_replaced(self):
        snapshot_release.write_snapshot("v1.0.0", self.root)
        with self.assertRaises(FileExistsError):
            snapshot_release.write_snapshot("v1.0.0", self.root)

    def test_missing_files_are_reported(self):
        problems = snapshot_release.check_snapshot("v1.0.0", self.root)
        self.assertEqual(len(problems), 2)
        self.assertTrue(all("missing" in problem for problem in problems))
        self.assertEqual(snapshot_release.released_versions(self.root), [])
        self.assertEqual(
            snapshot_release.released_versions(os.path.join(self.root, "none")), []
        )

    def test_a_snapshot_that_no_longer_matches_the_code_is_reported(self):
        directory = snapshot_release.write_snapshot("v1.0.0", self.root)
        config_path = os.path.join(directory, "config.json")
        with open(config_path, encoding="utf-8") as handle:
            saved = json.load(handle)
        saved["voltage_step"] = 99
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(saved, handle)
        history_path = os.path.join(directory, "history.db")
        os.remove(history_path)
        with closing(sqlite3.connect(history_path)) as connection:
            connection.execute("CREATE TABLE samples (ts INTEGER)")
        problems = snapshot_release.check_snapshot("v1.0.0", self.root)
        self.assertEqual(len(problems), 2)
        self.assertIn("not what this code writes", problems[0])
        self.assertIn("different schema", problems[1])


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = mock.patch.object(snapshot_release, "RELEASED", self._dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, *args):
        with mock.patch("builtins.print") as printed:
            code = snapshot_release.main(list(args))
        return code, [call.args[0] for call in printed.call_args_list]

    def test_a_name_that_is_not_a_tag_is_refused(self):
        code, lines = self.run_main("1.0")
        self.assertEqual(code, 2)
        self.assertIn("not a release tag", lines[0])

    def test_write_then_check(self):
        self.assertEqual(self.run_main("--check", "v1.0.0")[0], 1)
        code, lines = self.run_main("v1.0.0")
        self.assertEqual(code, 0)
        self.assertIn("Commit it with the release", lines[0])
        code, lines = self.run_main("--check", "v1.0.0")
        self.assertEqual(code, 0)
        self.assertIn("matches this code", lines[0])
        code, lines = self.run_main("v1.0.0")
        self.assertEqual(code, 1)
        self.assertIn("already exists", str(lines[0]))


if __name__ == "__main__":
    unittest.main()
