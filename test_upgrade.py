"""Updating the app never changes a value the user saved.

Each fixture is config.json as an earlier version wrote it. This version may
add settings and drop retired ones, but every saved value stays as it was, and
the file is kept as config.backup-v<N>.json before it is rewritten.
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import config
import dashboard
import history
from dashboard import TunerDashboard

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
import snapshot_release  # noqa: E402

# Upstream bitaxe-temp-monitor, before this fork. Blank caps meant "use defaults".
UPSTREAM = {
    "voltage_step": 10,
    "frequency_step": 5,
    "monitor_interval": 5,
    "default_target_temp": 50,
    "temp_tolerance": 2,
    "refresh_interval": 5,
    "enforce_safe_pairing": True,
    "daily_reset_enabled": False,
    "daily_reset_time": "03:00",
    "miners": [
        {
            "nickname": "attic",
            "type": "BM1370 601",
            "ip": "192.168.1.20",
            "min_freq": "",
            "max_freq": 600,
            "start_freq": 525,
            "min_volt": "",
            "max_volt": 1200,
            "start_volt": 1150,
            "max_temp": 60,
            "max_watts": "",
            "max_vr_temp": "",
            "target_hashrate": "",
        }
    ],
}

# The fork's first month: learned setpoints and soak settings, no versions yet.
FORK_SEPTEMBER = {
    "voltage_step": 10,
    "frequency_step": 5,
    "monitor_interval": 5,
    "default_target_temp": 68,
    "temp_tolerance": 3,
    "vr_temp_tolerance": 3,
    "refresh_interval": 180,
    "ceiling_soak_seconds": 1800,
    "daily_reset_enabled": False,
    "daily_reset_time": "03:00",
    "flatline_detection_enabled": True,
    "flatline_hashrate_repeat_count": 7,
    "miners": [
        {
            "nickname": "garage",
            "type": "BM1370 601",
            "ip": "192.168.1.21",
            "enabled": True,
            "last_good_freq": 1010,
            "last_good_volt": 1290,
            "wall_type": "heat",
            "wall_timestamp": 1758500000,
            "target_hashrate": "",
            "min_freq": 400,
            "max_freq": 1050,
            "start_freq": 900,
            "min_volt": 1000,
            "max_volt": 1350,
            "start_volt": 1250,
            "max_temp": 66,
            "max_watts": 38,
            "max_vr_temp": 85,
            "min_input_voltage": 4.85,
            "max_error_percentage": 1.5,
            "max_droop_mv": 35,
        }
    ],
}

# After the 601 caps were first raised (limits 2) and boards were recorded (config 1).
BOARDS_RECORDED = {
    "voltage_step": 5,
    "frequency_step": 10,
    "monitor_interval": 3,
    "default_target_temp": 64,
    "temp_tolerance": 2,
    "vr_temp_tolerance": 4,
    "refresh_interval": 240,
    "fast_start": False,
    "continue_from_live": False,
    "limits_version": 2,
    "config_version": 1,
    "location": {"name": "Moose Jaw", "latitude": 50.4, "longitude": -105.5},
    "miners": [
        {
            "nickname": "shed",
            "type": "BM1370 601",
            "ip": "192.168.1.22",
            "board": "601",
            "enabled": False,
            "repasted_on": "2026-10-01",
            "min_freq": 450,
            "max_freq": 975,
            "start_freq": 700,
            "min_volt": 1050,
            "max_volt": 1300,
            "start_volt": 1200,
            "max_temp": 64,
            "max_watts": 32,
            "max_vr_temp": 80,
            "min_input_voltage": 4.8,
            "max_error_percentage": 2.5,
            "max_droop_mv": 45,
            "max_core_amps": 24.5,
        }
    ],
}

# Modes and the first-run setup existed, but not the internet switches.
MODES_ADDED = {
    "voltage_step": 10,
    "frequency_step": 5,
    "monitor_interval": 5,
    "default_target_temp": 70,
    "temp_tolerance": 3,
    "vr_temp_tolerance": 3,
    "refresh_interval": 180,
    "fast_start": True,
    "continue_from_live": True,
    "limits_version": 3,
    "config_version": 2,
    "default_mode": "efficiency",
    "setup_done": True,
    "weather_enabled": False,
    "supply_watts": 30.0,
    "flatline_detection_enabled": False,
    "flatline_hashrate_repeat_count": 5,
    "miners": [
        {
            "nickname": "office",
            "type": "BM1368 402",
            "ip": "192.168.1.23",
            "board": "402",
            "mode": "efficiency",
            "enabled": True,
            "experimental_ok": True,
            "repasted_on": "",
            "min_freq": 400,
            "max_freq": 600,
            "start_freq": 490,
            "min_volt": 1100,
            "max_volt": 1166,
            "start_volt": 1166,
            "max_temp": 62,
            "max_watts": 28,
            "max_vr_temp": 80,
            "min_input_voltage": 4.9,
            "max_error_percentage": 2.0,
            "max_droop_mv": 40,
            "max_core_amps": 22.0,
        }
    ],
}

OLDER_FORMATS = {
    "upstream": UPSTREAM,
    "fork September": FORK_SEPTEMBER,
    "boards recorded": BOARDS_RECORDED,
    "modes added": MODES_ADDED,
}

# Keys an update may add or move forward. Everything else must stay as saved.
VERSION_KEYS = {"config_version", "limits_version"}


class ConfigFileCase(unittest.TestCase):
    """A temporary config.json, and checks on what loading it kept."""

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

    def _write(self, saved):
        raw = json.dumps(saved, indent=2).encode("utf-8")
        with open(self.path, "wb") as handle:
            handle.write(raw)
        return raw

    def _on_disk(self):
        with open(self.path, "rb") as handle:
            return handle.read()

    def _fresh_process(self):
        """Forget what this process parsed, as a new start of the app would."""
        config._last_good_config = None
        config._config_corrupt = False

    def _backups(self):
        return sorted(
            name
            for name in os.listdir(self._dir.name)
            if name.startswith("config.backup-")
        )

    def assert_kept(self, saved, loaded):
        for key, value in saved.items():
            if key == "miners" or key in VERSION_KEYS:
                continue
            if key in config.RETIRED_GLOBAL_KEYS:
                self.assertNotIn(key, loaded)
                continue
            self.assertEqual(loaded.get(key), value, key)
        self.assertEqual(len(loaded["miners"]), len(saved["miners"]))
        for before, after in zip(saved["miners"], loaded["miners"], strict=True):
            for key, value in before.items():
                if key in config.RETIRED_MINER_KEYS:
                    self.assertNotIn(key, after)
                    continue
                self.assertEqual(after.get(key), value, f"{before['ip']} {key}")


class UpgradeTests(ConfigFileCase):
    def test_every_older_format_keeps_every_saved_value(self):
        for name, saved in OLDER_FORMATS.items():
            with self.subTest(name):
                for leftover in self._backups():
                    os.remove(os.path.join(self._dir.name, leftover))
                raw = self._write(saved)
                self._fresh_process()
                loaded = config.load_config()
                self.assert_kept(saved, loaded)
                self.assertEqual(loaded["config_version"], config.CONFIG_VERSION)
                self.assertEqual(loaded["limits_version"], config.LIMITS_VERSION)
                self.assertEqual(json.loads(self._on_disk()), loaded)
                version = saved.get("config_version", 0)
                backup = config.backup_path(version)
                with open(backup, "rb") as handle:
                    self.assertEqual(handle.read(), raw)
                self.assertEqual(self._backups(), [f"config.backup-v{version}.json"])

    def test_settings_older_versions_ran_without_stay_as_they_ran(self):
        self._write(FORK_SEPTEMBER)
        loaded = config.load_config()
        miner = loaded["miners"][0]
        # Before modes, every 601 was tuned the Max hashrate way.
        self.assertEqual((miner["board"], miner["mode"]), ("601", "max_hashrate"))
        self.assertTrue(loaded["setup_done"])
        self.assertTrue(loaded["weather_enabled"])
        for key in config.INTERNET_SWITCHES:
            self.assertTrue(loaded[key], key)

    def test_a_second_start_changes_nothing(self):
        self._write(BOARDS_RECORDED)
        first = config.load_config()
        after_first = self._on_disk()
        self._fresh_process()
        with mock.patch("config._write_config") as write:
            second = config.load_config()
        write.assert_not_called()
        self.assertEqual(second, first)
        self.assertEqual(self._on_disk(), after_first)
        self.assertEqual(self._backups(), ["config.backup-v1.json"])

    def test_an_existing_backup_is_never_overwritten(self):
        backup = config.backup_path(2)
        with open(backup, "wb") as handle:
            handle.write(b"the first copy")
        self._write(MODES_ADDED)
        config.load_config()
        with open(backup, "rb") as handle:
            self.assertEqual(handle.read(), b"the first copy")

    def test_a_backup_that_cannot_be_written_does_not_stop_the_start(self):
        self._write(MODES_ADDED)
        with mock.patch(
            "config.backup_path",
            return_value=os.path.join(self._dir.name, "missing", "copy.json"),
        ):
            loaded = config.load_config()
        self.assert_kept(MODES_ADDED, loaded)
        self.assertEqual(self._backups(), [])

    def test_a_current_config_is_not_rewritten_or_copied(self):
        current = copy.deepcopy(MODES_ADDED)
        current["config_version"] = config.CONFIG_VERSION
        for key in config.INTERNET_SWITCHES:
            current[key] = False
        raw = self._write(current)
        with mock.patch("config._write_config") as write:
            loaded = config.load_config()
        write.assert_not_called()
        self.assertEqual(loaded, current)
        self.assertEqual(self._on_disk(), raw)
        self.assertEqual(self._backups(), [])

    def test_a_newer_versions_settings_survive_this_version(self):
        newer = copy.deepcopy(MODES_ADDED)
        newer["config_version"] = 99
        newer["limits_version"] = 99
        newer["setting_from_the_future"] = {"keep": "me"}
        newer["miners"][0]["field_from_the_future"] = [1, 2, 3]
        raw = self._write(newer)
        loaded = config.load_config()
        self.assertEqual(loaded, newer)
        self.assertEqual(self._on_disk(), raw)
        config.update_miner("192.168.1.23", {"nickname": "den"})
        self._fresh_process()
        again = config.load_config()
        self.assertEqual(again["setting_from_the_future"], {"keep": "me"})
        self.assertEqual(again["miners"][0]["field_from_the_future"], [1, 2, 3])
        self.assertEqual(again["config_version"], 99)
        self.assertEqual(self._backups(), [])

    def test_a_damaged_file_is_never_replaced(self):
        with open(self.path, "wb") as handle:
            handle.write(b'{"miners": [')
        loaded = config.load_config()
        self.assertEqual(loaded, config.get_default_config())
        self.assertTrue(config.config_problem())
        self.assertFalse(config.modify_config(lambda saved: saved.update(x=1)))
        self.assertFalse(config.save_config(config.get_default_config()))
        config.update_miner("192.168.1.23", {"nickname": "den"})
        self.assertEqual(self._on_disk(), b'{"miners": [')
        self.assertEqual(self._backups(), [])

    def test_a_file_notepad_saved_with_a_byte_order_mark_still_loads(self):
        current = copy.deepcopy(MODES_ADDED)
        current["config_version"] = config.CONFIG_VERSION
        for key in config.INTERNET_SWITCHES:
            current[key] = True
        with open(self.path, "wb") as handle:
            handle.write(b"\xef\xbb\xbf" + json.dumps(current).encode("utf-8"))
        self.assertEqual(config.load_config(), current)

    def test_opening_and_saving_each_dialog_changes_no_value(self):
        for name, saved in OLDER_FORMATS.items():
            with self.subTest(name):
                self._write(saved)
                self._fresh_process()
                before = config.load_config()
                app = TunerDashboard()
                with mock.patch.object(app, "_restart_tuning_for", create=True):
                    tuner = app.get_autotuner_settings()
                    rows = [
                        {
                            "ip": row["ip"],
                            "enabled": row["enabled"],
                            "fields": row["fields"],
                        }
                        for row in tuner["miners"]
                    ]
                    self.assertTrue(app.save_autotuner_settings(rows)["ok"])
                    settings = app.get_global_settings()["settings"]
                    result = app.save_global_settings(settings)
                if saved is UPSTREAM:
                    # Upstream tuned every 5 s; this version needs 60. The
                    # dialog asks the user instead of changing it.
                    self.assertIn("Tune interval", result["message"])
                    self.assertEqual(config.load_config()["refresh_interval"], 5)
                else:
                    self.assertTrue(result["ok"], result)
                after = config.load_config()
                for key, value in before.items():
                    if key == "miners":
                        continue
                    self.assertEqual(after.get(key), value, key)
                for old, new in zip(before["miners"], after["miners"], strict=True):
                    for key, value in old.items():
                        if value == "":
                            # A blank cap means "use the default"; the dialog
                            # shows that default and saves it.
                            continue
                        self.assertEqual(new.get(key), value, f"{old['ip']} {key}")

    def test_an_update_never_writes_to_a_miner(self):
        self._write(FORK_SEPTEMBER)
        with (
            mock.patch("autotune.requests.post") as post,
            mock.patch("autotune.requests.patch") as patch,
            mock.patch("dashboard.requests.get"),
        ):
            config.load_config()
            TunerDashboard().load_rows()
        post.assert_not_called()
        patch.assert_not_called()


class ReleasedVersionTests(ConfigFileCase):
    """Every release's snapshot (tools/snapshot_release.py) loads with this code.

    Until the first release there are none, and breaking changes are allowed.
    After it, a change that fails here needs a migration, not a new snapshot.
    """

    def assert_release_still_loads(self, directory):
        with open(os.path.join(directory, "config.json"), "rb") as handle:
            raw = handle.read()
        saved = json.loads(raw)
        with open(self.path, "wb") as handle:
            handle.write(raw)
        self._fresh_process()
        loaded = config.load_config()
        self.assert_kept(saved, loaded)
        self._fresh_process()
        with mock.patch("config._write_config") as write:
            self.assertEqual(config.load_config(), loaded)
        write.assert_not_called()

        db = os.path.join(self._dir.name, "history.db")
        shutil.copyfile(os.path.join(directory, "history.db"), db)
        samples = history.load_samples(path=db)
        self.assertTrue(samples)
        newer = dict(samples[-1], ts=samples[-1]["ts"] + history.SAMPLE_SECONDS)
        history.record_samples([newer], db)
        again = history.load_samples(path=db)
        self.assertEqual(len(again), len(samples) + 1)
        self.assertTrue(history.last_events("reset", db))
        self.assertTrue(history.summarize(again, "good_hashrate"))

    def test_every_released_version_still_loads(self):
        versions = snapshot_release.released_versions()
        if not versions:
            self.skipTest("No release yet. Breaking changes are allowed until then.")
        for version in versions:
            with self.subTest(version):
                directory = os.path.join(snapshot_release.RELEASED, version)
                self.assert_release_still_loads(directory)

    def test_a_snapshot_of_this_code_loads_as_a_release_would(self):
        with tempfile.TemporaryDirectory() as root:
            directory = snapshot_release.write_snapshot("v9.9.9", root)
            self.assert_release_still_loads(directory)


class DashboardModuleTests(unittest.TestCase):
    def test_the_dashboard_uses_this_config_module(self):
        self.assertIs(dashboard.load_config, config.load_config)


if __name__ == "__main__":
    unittest.main()
