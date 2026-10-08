"""config.py edge cases: odd replies, odd files, and failed writes."""

import json
import os
import tempfile
import unittest
from unittest import mock

import config

GAMMA_INFO = {"ASICModel": "BM1370", "boardVersion": "601", "hostname": "goose"}


class _Reply:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class TempConfigCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "config.json")
        saved = (config.CONFIG_FILE, config._last_good_config, config._config_corrupt)
        self.addCleanup(self._restore, saved)
        config.CONFIG_FILE = self.path
        config._last_good_config = None
        config._config_corrupt = False

    def _restore(self, saved):
        config.CONFIG_FILE, config._last_good_config, config._config_corrupt = saved
        self._dir.cleanup()

    def save(self, miners=()):
        saved = config.get_default_config()
        saved["miners"] = [
            config.new_miner_record("BM1370 601", ip, name, saved)
            for ip, name in miners
        ]
        config.save_config(saved)


class NamingTests(unittest.TestCase):
    def test_a_type_from_half_a_reply_uses_what_is_there(self):
        self.assertEqual(config.miner_type_from_info({"ASICModel": "BM1370"}), "BM1370")
        self.assertEqual(config.miner_type_from_info({"boardVersion": "601"}), "601")
        self.assertEqual(config.miner_type_from_info({}), "Unknown")

    def test_a_missing_reply_names_the_miner_by_its_address(self):
        self.assertEqual(
            config.miner_name_from_info(None, "10.0.0.4"), "Miner-10.0.0.4"
        )
        self.assertIsNone(config.adopted_hostname("Miner-10.0.0.4", "10.0.0.4", None))

    def test_a_hostname_equal_to_the_placeholder_is_not_adopted(self):
        info = {"hostname": "Miner-10.0.0.4"}
        self.assertIsNone(config.adopted_hostname("Miner-10.0.0.4", "10.0.0.4", info))


class VersionHelperTests(unittest.TestCase):
    def test_odd_saved_versions_count_as_the_oldest(self):
        self.assertEqual(config._saved_config_version(["not", "a", "dict"]), 0)
        self.assertEqual(config._saved_config_version({"config_version": "x"}), 0)
        stamped = {"limits_version": "x"}
        self.assertTrue(config._record_limits_version(stamped))
        self.assertEqual(stamped["limits_version"], config.LIMITS_VERSION)

    def test_helpers_leave_anything_but_a_config_alone(self):
        self.assertFalse(config._record_limits_version(None))
        self.assertFalse(config._record_boards(None))
        self.assertFalse(config._drop_retired_keys(None))
        self.assertTrue(config.internet_switch_on(None, "pool_check_enabled"))

    def test_a_broken_miner_entry_is_skipped_by_the_upgrade(self):
        saved = {"miners": ["oops", {"ip": "a"}]}
        self.assertTrue(config._record_boards(saved))
        self.assertEqual(saved["miners"][0], "oops")
        self.assertEqual(saved["miners"][1]["board"], "601")


class DataFolderTests(unittest.TestCase):
    def test_a_packaged_app_creates_its_data_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "new", "config.json")
            config.ensure_data_dir(path, frozen=False)
            self.assertFalse(os.path.isdir(os.path.dirname(path)))
            config.ensure_data_dir(path, frozen=True)
            self.assertTrue(os.path.isdir(os.path.dirname(path)))
            with mock.patch.object(config.sys, "frozen", True, create=True):
                config.ensure_data_dir(path)


class FileTests(TempConfigCase):
    def test_a_file_removed_between_the_check_and_the_read_is_recreated(self):
        with mock.patch("config.os.path.exists", return_value=True):
            loaded = config.load_config()
        self.assertEqual(loaded, config.get_default_config())
        self.assertTrue(os.path.exists(self.path))

    def test_a_failed_write_cleans_up_and_reports(self):
        with (
            mock.patch("config.os.replace", side_effect=OSError("disk full")),
            mock.patch("config.os.remove", side_effect=OSError("gone")),
            self.assertRaises(OSError),
        ):
            config._write_config(config.get_default_config())

    def test_modify_reports_a_refused_change_and_a_refused_save(self):
        self.save()
        self.assertIsNone(config.modify_config(lambda saved: False))
        with mock.patch("config.save_config", return_value=False):
            self.assertFalse(config.modify_config(lambda saved: None))


class MinerListTests(TempConfigCase):
    def test_defaults_are_found_past_other_miners(self):
        self.save([("10.0.0.1", "one"), ("10.0.0.2", "two")])
        self.assertEqual(config.get_miner_defaults("10.0.0.2")["nickname"], "two")
        self.assertEqual(config.get_miner_defaults("10.0.0.9"), {})

    def test_adding_twice_and_removing_an_unknown_miner_change_nothing(self):
        self.save([("10.0.0.1", "one")])
        before = config.load_config()
        with mock.patch("builtins.print") as printed:
            config.add_miner("BM1370 601", "10.0.0.1", "again")
            config.remove_miner("10.0.0.9")
            config.update_miner("10.0.0.9", {"nickname": "ghost"})
        self.assertEqual(config.load_config(), before)
        lines = [call.args[0] for call in printed.call_args_list]
        self.assertIn("Error: Miner with IP 10.0.0.1 already exists.", lines)
        self.assertIn("Error: Miner with IP 10.0.0.9 not found.", lines)
        self.assertIn("Error: Miner 10.0.0.9 not found.", lines)


class ScanTests(TempConfigCase):
    def scan(self, replies, **kwargs):
        def get(url, timeout=None):
            reply = replies.get(url.split("/")[2])
            if reply is None:
                raise config.requests.exceptions.ConnectionError("no answer")
            return reply

        with (
            mock.patch("config.requests.get", side_effect=get),
            mock.patch("builtins.print"),
        ):
            return config.detect_miners("10.0.0.1", "10.0.0.3", **kwargs)

    def test_a_bad_range_finds_nothing(self):
        with mock.patch("builtins.print") as printed:
            self.assertEqual(config.detect_miners("10.0.0.1", "nope"), [])
        printed.assert_called_once_with("Error: Invalid IP range provided.")

    def test_an_error_reply_is_skipped(self):
        self.save()
        found = self.scan(
            {"10.0.0.1": _Reply({}, status=500), "10.0.0.2": _Reply(GAMMA_INFO)}
        )
        self.assertEqual([miner["ip"] for miner in found], ["10.0.0.2"])

    def test_a_miner_added_during_the_scan_is_not_added_twice(self):
        self.save()

        def add_during_scan(index, total, ip):
            if index == total:
                config.modify_config(
                    lambda saved: saved["miners"].append(
                        config.new_miner_record("BM1370 601", "10.0.0.2", "typed")
                    )
                )

        found = self.scan({"10.0.0.2": _Reply(GAMMA_INFO)}, on_progress=add_during_scan)
        self.assertEqual(found, [])
        miners = config.load_config()["miners"]
        self.assertEqual([miner["nickname"] for miner in miners], ["typed"])

    def test_a_scan_that_cannot_save_reports_nothing_found(self):
        self.save()
        with mock.patch("config.save_config", return_value=False):
            found = self.scan({"10.0.0.2": _Reply(GAMMA_INFO)})
        self.assertEqual(found, [])
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["miners"], [])


if __name__ == "__main__":
    unittest.main()
