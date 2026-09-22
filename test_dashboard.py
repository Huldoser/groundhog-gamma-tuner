import os
import tempfile
import threading
import unittest
from datetime import datetime
from contextlib import contextmanager
from unittest import mock

import config
import dashboard
from dashboard import (
    ALL_AUTOTUNE_FIELDS,
    DashboardApi,
    LOG_LIMIT,
    TunerDashboard,
    blank_miner_row,
    difficulty_title,
    droop_alert,
    format_core_voltage_title,
    format_difficulty,
    format_efficiency,
    format_hash_title,
    format_input_voltage,
    format_learned_wall,
    format_minute_hashrate,
    format_number,
    format_share_title,
    format_shares,
    format_uptime,
    format_version_title,
    limit_level,
    replace_ips_with_names,
    under_limit,
)


@contextmanager
def temp_config(miners=None):
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "config.json")
        old_path = config.CONFIG_FILE
        old_last = config._last_good_config
        config.CONFIG_FILE = path
        config._last_good_config = None
        try:
            saved = config.get_default_config()
            if miners is not None:
                saved["miners"] = miners
            config.save_config(saved)
            yield path
        finally:
            config.CONFIG_FILE = old_path
            config._last_good_config = old_last


class DisplayHelperTests(unittest.TestCase):
    def test_format_number_and_learned_wall(self):
        self.assertEqual(format_number(None, 0), "-")
        self.assertEqual(format_number("12.3%", 1), "12.3")
        self.assertEqual(format_number(12.3, 2), "12.30")
        self.assertEqual(
            format_learned_wall(
                {"wall_type": "silicon", "last_good_freq": 640, "last_good_volt": 1200},
                {},
            ),
            "silicon 640/1200",
        )
        self.assertEqual(format_learned_wall({}, {}), "-")
        self.assertEqual(format_difficulty(49224525), "49.22M")
        self.assertEqual(format_difficulty(2038368), "2.04M")
        self.assertEqual(format_difficulty(999), "999")
        self.assertEqual(format_difficulty(1500), "1.50k")
        self.assertEqual(format_difficulty(None), "-")
        self.assertEqual(format_difficulty("270M"), "-")
        self.assertEqual(format_difficulty(True), "-")
        self.assertEqual(difficulty_title(49224525), "49224525")
        self.assertEqual(difficulty_title("270M"), "")
        self.assertEqual(format_shares(4768, 13), "4768/13")
        self.assertEqual(format_shares(None, 0), "-")

    def test_updated_stamp_uses_the_local_clock(self):
        moment = datetime(2026, 9, 22, 16, 45, 3)
        with mock.patch("dashboard.platform.system", return_value="Linux"):
            self.assertEqual(dashboard.format_local_time(moment), "16:45:03")

        kernel = mock.Mock()

        def get_time(locale, flags, system_time, picture, buffer, size):
            self.assertIsNone(locale)
            self.assertEqual(flags, 0x00000002)
            self.assertIsNone(picture)
            buffer.value = "4:45 PM"
            return 7

        kernel.GetTimeFormatEx.side_effect = get_time
        with mock.patch("dashboard.platform.system", return_value="Windows"), \
                mock.patch("ctypes.WinDLL", return_value=kernel, create=True):
            self.assertEqual(dashboard.format_local_time(moment), "4:45 PM")

        kernel.GetTimeFormatEx.side_effect = None
        kernel.GetTimeFormatEx.return_value = 0
        with mock.patch("dashboard.platform.system", return_value="Windows"), \
                mock.patch("ctypes.WinDLL", return_value=kernel, create=True):
            self.assertEqual(dashboard.format_local_time(moment), "16:45:03")

        app = TunerDashboard()
        app._rows = [{"ip": "10.0.0.8"}]
        with mock.patch("dashboard.format_local_time", return_value="4:45 PM"), \
                mock.patch.object(app, "_apply_one_locked"), \
                mock.patch.object(app, "_note_alert_locked", return_value=None), \
                mock.patch.object(app, "_deliver_alerts"):
            app._apply_results([("10.0.0.8", {})])
        self.assertEqual(app._updated, "4:45 PM")

    def test_network_status_reads_difficulty_and_pool_reachability(self):
        from dashboard import read_network_status

        class Response:
            def __init__(self, url):
                self.status_code = 200
                self.content = b"{}"
                self._url = url

            def raise_for_status(self):
                return None

            def json(self):
                return {"currentDifficulty": 132757073449487.5}

        def fake_get(url, timeout=8):
            return Response(url)

        def fake_connect(address, timeout=8):
            host, port = address
            if host == "public-pool.io":
                self.assertEqual(port, 23330)
                raise TimeoutError("offline")
            self.assertEqual((host, port), ("stratum.ckpool.org", 3336))

            class Sock:
                def close(self):
                    return None

            return Sock()

        status = read_network_status(fake_get, fake_connect)
        self.assertEqual(status["difficulty"], 132757073449487.5)
        self.assertEqual(
            status["pools"],
            [
                {"name": "stratum.ckpool.org", "online": True},
                {"name": "public-pool.io", "online": False},
            ],
        )

    def test_failed_difficulty_fetch_keeps_the_last_value(self):
        app = TunerDashboard()
        with mock.patch("dashboard.read_network_status", return_value={
            "difficulty": 132757073449487.5,
            "pools": [
                {"name": "stratum.ckpool.org", "online": True},
                {"name": "public-pool.io", "online": True},
            ],
        }):
            app._refresh_network()
        self.assertEqual(app.get_snapshot(0)["network"]["difficulty"], "132.76T")
        with mock.patch("dashboard.read_network_status", return_value={
            "difficulty": None,
            "pools": [
                {"name": "stratum.ckpool.org", "online": False},
                {"name": "public-pool.io", "online": True},
            ],
        }):
            app._refresh_network()
        network = app.get_snapshot(0)["network"]
        self.assertEqual(network["difficulty"], "132.76T")
        self.assertEqual(network["difficulty_title"], "132757073449487.5")
        self.assertFalse(network["pools"][0]["online"])
        self.assertTrue(network["pools"][1]["online"])
        self.assertEqual(format_input_voltage(5093.75), "5.09")
        self.assertEqual(format_input_voltage(5.05), "5.05")
        self.assertEqual(format_input_voltage(None), "-")
        self.assertEqual(format_minute_hashrate({"hashRate_1m": 1000.5, "hashRate": 900}), "1000.50")
        self.assertEqual(format_minute_hashrate({"hashRate": 900}), "-")
        self.assertEqual(format_efficiency(18.5, 1000, None), "18.50")
        self.assertEqual(format_efficiency(18.5, 0, None), "-")
        self.assertEqual(format_efficiency(18.5, 1000, "UV"), "-")
        self.assertEqual(format_efficiency(18.5, 1000, "none"), "18.50")
        self.assertEqual(format_uptime(18000), ("5h", 18000))
        self.assertEqual(format_uptime(172800), ("2d", 172800))
        self.assertEqual(format_uptime(45), ("45s", 45))
        self.assertEqual(format_uptime(None), ("-", None))
        self.assertEqual(
            format_hash_title({"hashRate": 1072.24, "hashRate_10m": 1070, "expectedHashrate": 1071}),
            "Live 1072.24 GH/s\n10m 1070.00 GH/s\nExpected 1071.00 GH/s",
        )
        self.assertEqual(format_core_voltage_title(1150, 1144), "Measured 1144 mV, droop 6 mV")
        self.assertEqual(format_core_voltage_title(1150, None), "")
        self.assertFalse(droop_alert(1150, 1110, {"max_droop_mv": 40}))
        self.assertTrue(droop_alert(1150, 1109, {"max_droop_mv": 40}))
        self.assertTrue(droop_alert(1150, 1109, {}))
        self.assertEqual(
            format_share_title({
                "sharesRejectedReasons": [{"message": "Stale", "count": 13}],
                "poolDifficulty": 1000,
                "isUsingFallbackStratum": 1,
            }),
            "Stale 13\nPool difficulty 1000\nFallback pool",
        )
        self.assertEqual(format_share_title({"isUsingFallbackStratum": 0, "poolDifficulty": 0}), "")
        self.assertEqual(format_version_title({"version": "v2.15.1"}), "v2.15.1")
        self.assertEqual(limit_level(65, 68, 3), "")
        self.assertEqual(limit_level(65.1, 68, 3), "warn")
        self.assertEqual(limit_level(68, 68, 3), "warn")
        self.assertEqual(limit_level(68.1, 68, 3), "bad")
        self.assertEqual(limit_level(67, 68, 0), "")
        self.assertEqual(limit_level(70, None, 3), "")
        self.assertEqual(limit_level(None, 68, 3), "")
        self.assertFalse(under_limit(4.9, 4.9))
        self.assertTrue(under_limit(4.89, 4.9))
        self.assertFalse(under_limit(None, 4.9))
        self.assertFalse(under_limit(4.8, None))

    def test_ip_inside_a_nickname_is_left_alone(self):
        text = replace_ips_with_names(
            "Miner-192.168.8.10 at 192.168.8.10",
            {"192.168.8.10": "Miner-192.168.8.10"},
        )
        self.assertEqual(text, "Miner-192.168.8.10 at 192.168.8.10")

    def test_latest_stable_firmware_skips_prereleases(self):
        from dashboard import (
            firmware_update_version,
            latest_stable_firmware,
            read_latest_stable_firmware,
        )

        releases = [
            {"tag_name": "v2.15.3", "prerelease": True, "draft": False},
            {"tag_name": "v2.15.2rc0", "prerelease": False, "draft": False},
            {"tag_name": "v2.14.0b4", "prerelease": True, "draft": False},
            {"tag_name": "v2.16.0", "prerelease": False, "draft": True},
            {"tag_name": "v2.15.1", "prerelease": False, "draft": False},
            {"tag_name": "v2.15.2", "prerelease": False, "draft": False},
        ]
        self.assertEqual(latest_stable_firmware(releases), "v2.15.2")
        self.assertEqual(firmware_update_version("v2.15.1", "v2.15.2"), "v2.15.2")
        self.assertEqual(firmware_update_version("2.15.1", "v2.15.2"), "v2.15.2")
        self.assertEqual(firmware_update_version("v2.15.2", "v2.15.2"), "")
        self.assertEqual(firmware_update_version("v2.15.2-rc1", "v2.15.2"), "")
        self.assertEqual(firmware_update_version("v2.15.3", "v2.15.2"), "")

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return releases

        def fake_get(url, timeout=8):
            self.assertIn("bitaxeorg/ESP-Miner/releases", url)
            self.assertEqual(timeout, 8)
            return Response()

        self.assertEqual(read_latest_stable_firmware(fake_get), "v2.15.2")

        def offline_get(url, timeout=8):
            raise TimeoutError("offline")

        self.assertIsNone(read_latest_stable_firmware(offline_get))

    def test_failed_firmware_fetch_keeps_the_last_tag(self):
        app = TunerDashboard()
        with mock.patch("dashboard.read_latest_stable_firmware", return_value="v2.15.2"):
            app._refresh_firmware()
        self.assertEqual(app._latest_firmware, "v2.15.2")
        with mock.patch("dashboard.read_latest_stable_firmware", return_value=None):
            app._refresh_firmware()
        self.assertEqual(app._latest_firmware, "v2.15.2")
        row = blank_miner_row("Alpha", "10.0.0.8")
        row["name_title"] = "v2.15.1"
        app._rows = [row]
        self.assertEqual(app.get_snapshot(0)["miners"][0]["firmware_update"], "v2.15.2")
        app._rows[0]["name_title"] = "v2.15.2"
        self.assertEqual(app.get_snapshot(0)["miners"][0]["firmware_update"], "")
        app._rows[0]["name_title"] = "v2.15.3"
        self.assertEqual(app.get_snapshot(0)["miners"][0]["firmware_update"], "")

    def test_firmware_check_runs_at_start_and_local_noon(self):
        from dashboard import firmware_check_due

        morning = datetime(2026, 9, 22, 9, 0, 0)
        noon = datetime(2026, 9, 22, 12, 0, 5)
        afternoon = datetime(2026, 9, 22, 15, 0, 0)
        next_noon = datetime(2026, 9, 23, 12, 0, 1)
        self.assertTrue(firmware_check_due(None, morning))
        self.assertFalse(firmware_check_due(morning, morning.replace(minute=1)))
        self.assertTrue(firmware_check_due(morning, noon))
        self.assertFalse(firmware_check_due(noon, noon.replace(minute=30)))
        self.assertFalse(firmware_check_due(noon, afternoon))
        self.assertTrue(firmware_check_due(morning, afternoon))
        self.assertFalse(firmware_check_due(afternoon, afternoon.replace(hour=18)))
        self.assertTrue(firmware_check_due(afternoon, next_noon))

        app = TunerDashboard()
        with mock.patch("dashboard.read_latest_stable_firmware", return_value="v2.15.2") as fetch, \
                mock.patch("dashboard.datetime") as clock:
            clock.now.return_value = morning
            app._refresh_firmware_if_due()
            clock.now.return_value = morning.replace(minute=5)
            app._refresh_firmware_if_due()
            self.assertEqual(fetch.call_count, 1)
            clock.now.return_value = noon
            app._refresh_firmware_if_due()
            clock.now.return_value = afternoon
            app._refresh_firmware_if_due()
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(app._latest_firmware, "v2.15.2")


class SnapshotTests(unittest.TestCase):
    def test_closing_joins_tuner_threads(self):
        app = TunerDashboard()
        started = threading.Event()
        release = threading.Event()

        def worker():
            started.set()
            release.wait(2)

        thread = threading.Thread(target=worker)
        app.threads = [thread]
        app.stop_event = release
        thread.start()
        self.assertTrue(started.wait(1))
        app._on_closing()
        self.assertFalse(thread.is_alive())
        self.assertTrue(release.is_set())
        self.assertTrue(app._closed.is_set())

    def test_live_row_tag_offline_and_log_tail(self):
        app = TunerDashboard()
        app._rows = [blank_miner_row("Alpha", "10.0.0.8")]
        info = {
            "frequency": 640,
            "coreVoltage": 1200,
            "temp": 61.2,
            "vrTemp": 70,
            "hashRate": 12.5,
            "hashRate_1m": 12.5,
            "hashRate_10m": 12.2,
            "expectedHashrate": 13,
            "power": 18.5,
            "voltage": 5093.75,
            "coreVoltageActual": 1144,
            "uptimeSeconds": 18000,
            "version": "v2.15.1",
            "poolDifficulty": 1000,
            "isUsingFallbackStratum": 1,
            "fallbackStratumURL": "solo.ckpool.org",
            "wifiRSSI": -74,
            "power_fault": "none",
            "overheat_mode": 0,
            "sharesRejectedReasons": [{"message": "Stale", "count": 13}],
            "errorPercentage": 0.5,
            "hostname": "Alpha",
            "bestDiff": 49224525,
            "bestSessionDiff": 2038368,
            "sharesAccepted": 4768,
            "sharesRejected": 13,
        }
        status = {
            "phase": "hold",
            "wall_type": "silicon",
            "last_good_freq": 640,
            "last_good_volt": 1200,
            "reason": "holding",
        }
        stored = {
            "nickname": "Alpha",
            "max_temp": 68,
            "max_error_percentage": 2.0,
            "max_droop_mv": 40,
            "last_good_freq": 640,
            "last_good_volt": 1200,
            "wall_type": "silicon",
        }
        settings = {"temp_tolerance": 3, "vr_temp_tolerance": 3}
        with mock.patch("dashboard.get_system_info", return_value=info), \
                mock.patch("dashboard.get_miner_status", return_value=status), \
                mock.patch("dashboard.get_miner_defaults", return_value=stored), \
                mock.patch("dashboard.load_config", return_value=settings):
            app.refresh_once()
        row = app.get_snapshot(0)["miners"][0]
        self.assertEqual(row["tag"], "hold")
        self.assertEqual(row["asic_level"], "")
        self.assertEqual(row["vr_level"], "")
        self.assertFalse(row["error_alert"])
        self.assertFalse(row["watts_alert"])
        self.assertFalse(row["vin_alert"])
        self.assertEqual(row["freq"], "640")
        self.assertEqual(row["mv"], "1200")
        self.assertEqual(row["mv_title"], "Measured 1144 mV, droop 56 mV")
        self.assertTrue(row["mv_alert"])
        self.assertEqual(row["vin"], "5.09")
        self.assertEqual(row["asic"], "61.2")
        self.assertEqual(row["hash"], "12.50")
        self.assertEqual(row["hash_title"], "Live 12.50 GH/s\n10m 12.20 GH/s\nExpected 13.00 GH/s")
        self.assertEqual(row["jth"], "1480.00")
        self.assertEqual(row["up"], "5h")
        self.assertEqual(row["up_seconds"], 18000)
        self.assertEqual(row["name_title"], "v2.15.1")
        self.assertEqual(row["shares_title"], "Stale 13\nPool difficulty 1000\nFallback pool")
        self.assertEqual(row["error"], "0.50%")
        self.assertEqual(row["setpoint"], "silicon 640/1200")
        self.assertEqual(row["reason"], "holding")
        self.assertEqual(row["pool"], "solo.ckpool.org")
        self.assertTrue(row["fallback"])
        self.assertEqual(row["wifi"], "-74")
        self.assertTrue(row["wifi_weak"])
        self.assertFalse(row["power_fault"])
        self.assertFalse(row["overheat"])
        self.assertEqual(row["best_exact"], 49224525)
        self.assertEqual(row["best"], "49.22M")
        self.assertEqual(row["best_title"], "49224525")
        self.assertEqual(row["session"], "2.04M")
        self.assertEqual(row["session_title"], "2038368")
        self.assertEqual(row["shares"], "4768/13")
        self.assertNotEqual(app.get_snapshot(0)["updated"], "--:--:--")

        info["temp"] = 80
        status["phase"] = "climb"
        with mock.patch("dashboard.get_system_info", return_value=info), \
                mock.patch("dashboard.get_miner_status", return_value=status), \
                mock.patch("dashboard.get_miner_defaults", return_value=stored), \
                mock.patch("dashboard.load_config", return_value=settings):
            app.refresh_once()
        hot = app.get_snapshot(0)["miners"][0]
        self.assertEqual(hot["tag"], "alert")
        self.assertEqual(hot["asic_level"], "bad")

        app._rows[0]["freq"] = "500"
        with mock.patch(
            "dashboard.get_system_info",
            return_value="Error fetching system info from 10.0.0.8: timed out",
        ):
            app.refresh_once()
        offline = app.get_snapshot(0)["miners"][0]
        self.assertEqual(offline["phase"], "offline")
        self.assertEqual(offline["tag"], "alert")
        self.assertEqual(offline["freq"], "500")
        self.assertEqual(offline["reason"], "")

        cursor = app.get_snapshot(0)["log"][-1]["id"] if app.get_snapshot(0)["log"] else 0
        with mock.patch("dashboard.get_miners", return_value=[{"ip": "10.1.1.5", "nickname": "Alpha"}]):
            app.log_message("heat on 10.1.1.5", "warning")
            app.log_message("settled", "success")
        tail = app.get_snapshot(cursor)
        self.assertEqual([line["level"] for line in tail["log"]], ["warning", "success"])
        self.assertIn("heat on Alpha", tail["log"][0]["text"])
        self.assertNotIn("10.1.1.5", tail["log"][0]["text"])
        self.assertEqual(app.get_snapshot(tail["log"][-1]["id"])["log"], [])

        controls = app.get_snapshot(0)["controls"]
        self.assertEqual(controls["status"], "idle")
        self.assertEqual(controls["status_label"], "Idle")
        self.assertTrue(controls["reset_enabled"])
        self.assertNotIn("start_label", controls)
        self.assertIn("525 MHz", app.get_snapshot(0)["prompts"]["baseline"])

    def test_row_flags_readings_near_or_past_a_limit(self):
        app = TunerDashboard()
        app._rows = [blank_miner_row("Alpha", "10.0.0.8")]
        info = {
            "temp": 66,
            "vrTemp": 86,
            "power": 21,
            "voltage": 4.8,
            "errorPercentage": 2.5,
        }
        stored = {
            "max_temp": 68,
            "max_vr_temp": 88,
            "max_watts": 20,
            "max_error_percentage": 2,
            "min_input_voltage": 4.9,
        }
        settings = {"temp_tolerance": 3, "vr_temp_tolerance": 3}
        with mock.patch("dashboard.get_system_info", return_value=info), \
                mock.patch("dashboard.get_miner_status", return_value={"phase": "hold"}), \
                mock.patch("dashboard.get_miner_defaults", return_value=stored), \
                mock.patch("dashboard.load_config", return_value=settings):
            app.refresh_once()
        row = app.get_snapshot(0)["miners"][0]
        self.assertEqual(row["asic_level"], "warn")
        self.assertEqual(row["vr_level"], "warn")
        self.assertTrue(row["watts_alert"])
        self.assertTrue(row["error_alert"])
        self.assertTrue(row["vin_alert"])

        info.update({"temp": 60, "vrTemp": 80, "power": 18, "voltage": 5.1, "errorPercentage": 0.4})
        with mock.patch("dashboard.get_system_info", return_value=info), \
                mock.patch("dashboard.get_miner_status", return_value={"phase": "hold"}), \
                mock.patch("dashboard.get_miner_defaults", return_value=stored), \
                mock.patch("dashboard.load_config", return_value=settings):
            app.refresh_once()
        clear = app.get_snapshot(0)["miners"][0]
        self.assertEqual(clear["asic_level"], "")
        self.assertEqual(clear["vr_level"], "")
        self.assertFalse(clear["watts_alert"])
        self.assertFalse(clear["error_alert"])
        self.assertFalse(clear["vin_alert"])

    def test_activity_log_drops_lines_past_the_limit(self):
        app = TunerDashboard()
        extra = 50
        written = LOG_LIMIT + extra
        for index in range(written):
            app.log_message(f"line {index}")
        self.assertEqual(len(app._log), LOG_LIMIT)
        kept_ids = [line["id"] for line in app._log]
        self.assertEqual(kept_ids[0], extra + 1)
        self.assertEqual(kept_ids[-1], written)
        self.assertNotIn(1, kept_ids)
        stale = app.get_snapshot(0)
        self.assertLessEqual(len(stale["log"]), LOG_LIMIT)
        self.assertEqual(stale["log"][0]["id"], extra + 1)

    def test_table_rereads_miners_every_five_seconds(self):
        app = TunerDashboard()
        self.assertEqual(app._poll_interval(), 5)
        app.running = True
        self.assertEqual(app._poll_interval(), 5)
        self.assertFalse(hasattr(app, "refresh_miner"))

    def test_start_with_no_enabled_miners_stays_idle(self):
        with temp_config():
            app = TunerDashboard()
            result = app.start_autotuner()
            self.assertFalse(result["ok"])
            self.assertFalse(app.running)
            self.assertIn("No miners are enabled", result["message"])

    def test_remove_stops_only_that_miner_thread(self):
        first = config.new_miner_record("BM1370 601", "10.0.0.8", "Alpha", config.get_default_config())
        second = config.new_miner_record("BM1370 601", "10.0.0.9", "Beta", config.get_default_config())
        events = {}

        def fake_monitor(*args, **kwargs):
            events[args[0]] = kwargs["stop_event"]
            kwargs["stop_event"].wait(2)

        with temp_config([first, second]):
            app = TunerDashboard()
            with mock.patch("dashboard.monitor_and_adjust", fake_monitor):
                started = app.start_autotuner()
                self.assertTrue(started["ok"])
                self.assertEqual(set(events), {"10.0.0.8", "10.0.0.9"})
                removed = app.remove_miner_address("10.0.0.8")
                self.assertTrue(removed["ok"])
                self.assertTrue(events["10.0.0.8"].wait(1))
                self.assertFalse(events["10.0.0.9"].is_set())
                app._signal_miner_stop("10.0.0.9")
                for thread in app.threads:
                    thread.join(timeout=2)

    def test_run_control_follows_tuner_state(self):
        app = TunerDashboard()
        idle = app.get_snapshot(0)["controls"]
        self.assertEqual(idle["status"], "idle")
        self.assertTrue(idle["reset_enabled"])

        app.running = True
        running = app.get_snapshot(0)["controls"]
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["status_label"], "Running")
        self.assertFalse(running["reset_enabled"])

        app.running = False
        app._stop_in_progress = True
        stopping = app.get_snapshot(0)["controls"]
        self.assertEqual(stopping["status"], "stopping")
        self.assertEqual(stopping["status_label"], "Stopping")
        self.assertFalse(stopping["reset_enabled"])

        app._stop_in_progress = False
        app._baseline_reset_running = True
        resetting = app.get_snapshot(0)["controls"]
        self.assertEqual(resetting["status"], "resetting")
        self.assertEqual(resetting["status_label"], "Resetting")
        self.assertFalse(resetting["reset_enabled"])

        app._baseline_reset_running = False
        app._start_pending = True
        pending = app.reset_baseline()
        self.assertFalse(pending["ok"])
        self.assertFalse(app.get_snapshot(0)["controls"]["reset_enabled"])

    def test_finished_stop_marks_tuning_phase_stopped(self):
        import autotune

        held_ip = "10.9.9.1"
        skipped_ip = "10.9.9.2"
        live_ip = "10.9.9.3"
        gate = threading.Event()
        live = None

        def wait_for_gate():
            gate.wait()

        try:
            autotune._publish_status(
                held_ip, phase="hold", last_good_freq=640, last_good_volt=1200, wall_type="silicon",
            )
            autotune._publish_status(skipped_ip, phase="skipped", last_good_freq=525)
            autotune._publish_status(live_ip, phase="climb", last_good_freq=600, last_good_volt=1150)
            app = TunerDashboard()
            held = threading.Thread(target=lambda: None)
            held.miner_ip = held_ip
            held.start()
            held.join()
            skipped = threading.Thread(target=lambda: None)
            skipped.miner_ip = skipped_ip
            skipped.start()
            skipped.join()
            live = threading.Thread(target=wait_for_gate)
            live.miner_ip = live_ip
            live.start()
            app._rows = [
                {"ip": held_ip, "phase": "hold", "tag": "hold", "asic": "60", "error": "1%"},
                {"ip": skipped_ip, "phase": "skipped", "tag": "idle", "asic": "60", "error": "1%"},
                {"ip": live_ip, "phase": "climb", "tag": "climb", "asic": "60", "error": "1%"},
            ]
            app.threads = [held, skipped, live]
            app.running = True
            app._stop_in_progress = True
            app._finish_stop()
            held_status = autotune.get_miner_status(held_ip)
            self.assertEqual(held_status["phase"], "stopped")
            self.assertEqual(held_status["last_good_freq"], 640)
            self.assertEqual(held_status["last_good_volt"], 1200)
            self.assertEqual(held_status["wall_type"], "silicon")
            self.assertEqual(autotune.get_miner_status(skipped_ip)["phase"], "skipped")
            self.assertEqual(autotune.get_miner_status(live_ip)["phase"], "climb")
            self.assertEqual(app._rows[0]["phase"], "stopped")
            self.assertEqual(app._rows[0]["tag"], "idle")
            self.assertEqual(app._rows[1]["phase"], "skipped")
            self.assertEqual(app._rows[2]["phase"], "climb")
            self.assertIn(live, app.threads)
            controls = app.get_snapshot(0)["controls"]
            self.assertEqual(controls["status"], "stopping")
            self.assertFalse(controls["reset_enabled"])
            refused = app.start_autotuner()
            self.assertFalse(refused["ok"])
            gate.set()
            live.join(timeout=2)
            settled = app.get_snapshot(0)["controls"]
            self.assertEqual(settled["status"], "idle")
            self.assertTrue(settled["reset_enabled"])
        finally:
            gate.set()
            if live is not None:
                live.join(timeout=2)
            autotune._clear_miner_status(held_ip)
            autotune._clear_miner_status(skipped_ip)
            autotune._clear_miner_status(live_ip)

    def test_blank_limit_disables_the_miner_and_frequency_is_clamped(self):
        miner = {"ip": "10.0.0.8", "nickname": "Alpha", "enabled": True, "type": "BM1370 601"}
        with temp_config([miner]):
            app = TunerDashboard()
            fields = {field: "10" for field in ALL_AUTOTUNE_FIELDS}
            fields["max_freq"] = "5000"
            fields["min_input_voltage"] = "4.9"
            fields["max_error_percentage"] = "2"
            saved = app.save_autotuner_settings([
                {"ip": "10.0.0.8", "enabled": True, "fields": fields},
            ])
            self.assertTrue(saved["ok"])
            stored = config.get_miners()[0]
            self.assertEqual(stored["max_freq"], dashboard.HARD_MAX_FREQ)
            self.assertEqual(stored["max_droop_mv"], 10)
            self.assertTrue(stored["enabled"])

            fields["max_temp"] = ""
            cleared = app.save_autotuner_settings([
                {"ip": "10.0.0.8", "enabled": True, "fields": fields},
            ])
            self.assertTrue(cleared["ok"])
            stored = config.get_miners()[0]
            self.assertEqual(stored["max_temp"], "")
            self.assertFalse(stored["enabled"])

    def test_global_settings_save_vr_tolerance_and_ceiling_soak(self):
        with temp_config():
            app = TunerDashboard()
            settings = app.get_global_settings()["settings"]
            self.assertEqual(settings["vr_temp_tolerance"], 3)
            self.assertEqual(settings["ceiling_soak_seconds"], 1800)
            settings["vr_temp_tolerance"] = 4
            settings["ceiling_soak_seconds"] = 900
            saved = app.save_global_settings(settings)
            self.assertTrue(saved["ok"])
            stored = config.load_config()
            self.assertEqual(stored["vr_temp_tolerance"], 4)
            self.assertEqual(stored["ceiling_soak_seconds"], 900)

    def test_subnet_fleet_and_pool(self):
        self.assertEqual(dashboard.subnet_range_for("192.168.8.40"), ("192.168.8.1", "192.168.8.254"))
        self.assertEqual(dashboard.subnet_range_for(""), ("", ""))
        self.assertEqual(dashboard.subnet_range_for("not-an-ip"), ("", ""))
        self.assertFalse(dashboard.wifi_is_weak(-44))
        self.assertTrue(dashboard.wifi_is_weak(-70))
        host, fallback = dashboard.pool_host({"stratumURL": "stratum+tcp://public-pool.io:23330"})
        self.assertEqual(host, "public-pool.io")
        self.assertFalse(fallback)
        rows = [
            {"phase": "climb", "hash": "1000.00", "watts": "20.00", "freq": "600"},
            {"phase": "hold", "hash": "500.00", "watts": "15.00", "freq": "500"},
            {"phase": "offline", "hash": "900.00", "watts": "10.00", "freq": "400"},
        ]
        summary = dashboard.fleet_summary(rows)
        self.assertEqual(summary["online"], 2)
        self.assertEqual(summary["offline"], 1)
        self.assertEqual(summary["hold"], 1)
        self.assertNotIn("climb", summary)
        self.assertEqual(summary["jth"], "23.33")

    def test_refresh_fills_fleet(self):
        app = TunerDashboard()
        app._rows = [blank_miner_row("Alpha", "10.0.0.8")]
        info = {"frequency": 640, "temp": 60, "hashRate_1m": 1000, "power": 20}
        stored = {"nickname": "Alpha"}
        with mock.patch("dashboard.get_system_info", return_value=info), \
                mock.patch("dashboard.get_miner_status", return_value={"phase": "hold", "reason": "holding"}), \
                mock.patch("dashboard.get_miner_defaults", return_value=stored), \
                mock.patch("dashboard.load_config", return_value={}):
            app.refresh_once()
        snapshot = app.get_snapshot(0)
        self.assertNotIn("history", snapshot)
        self.assertNotIn("odds", snapshot)
        self.assertEqual(snapshot["fleet"]["online"], 1)
        self.assertEqual(snapshot["fleet"]["hash"], "1000.00")
        self.assertEqual(snapshot["fleet"]["hold"], 1)
        self.assertIn("start", snapshot["scan_range"])

    def test_background_notice_for_offline_power_fault_and_overheat(self):
        app = TunerDashboard()
        app._rows = [blank_miner_row("Alpha", "10.0.0.8")]
        app._focused = False
        stored = {"nickname": "Alpha"}
        with mock.patch("dashboard.show_windows_toast") as toast, \
                mock.patch("dashboard.get_system_info", return_value="timed out"):
            app.refresh_once()
        toast.assert_called_once_with("Groundhog Gamma Tuner", "Alpha is offline.")
        with mock.patch("dashboard.show_windows_toast") as toast, \
                mock.patch("dashboard.get_system_info", return_value="timed out"):
            app.refresh_once()
        toast.assert_not_called()

        fault = {"power_fault": "UV", "temp": 40, "hashRate_1m": 10, "frequency": 500}
        with mock.patch("dashboard.show_windows_toast") as toast, \
                mock.patch("dashboard.get_system_info", return_value=fault), \
                mock.patch("dashboard.get_miner_status", return_value={"phase": "hold"}), \
                mock.patch("dashboard.get_miner_defaults", return_value=stored), \
                mock.patch("dashboard.load_config", return_value={}):
            app.refresh_once()
        toast.assert_called_once_with("Groundhog Gamma Tuner", "Alpha reported a power fault.")
        row = app.get_snapshot(0)["miners"][0]
        self.assertTrue(row["power_fault"])
        self.assertEqual(row["reason"], "power fault")
        self.assertEqual(row["tag"], "alert")

        hot = {"overheat_mode": 1, "power_fault": "none", "temp": 40, "hashRate_1m": 10, "frequency": 500}
        with mock.patch("dashboard.show_windows_toast") as toast, \
                mock.patch("dashboard.get_system_info", return_value=hot), \
                mock.patch("dashboard.get_miner_status", return_value={"phase": "hold"}), \
                mock.patch("dashboard.get_miner_defaults", return_value=stored), \
                mock.patch("dashboard.load_config", return_value={}):
            app.refresh_once()
        toast.assert_called_once_with("Groundhog Gamma Tuner", "Alpha is in overheat mode.")
        self.assertEqual(app.get_snapshot(0)["miners"][0]["reason"], "overheat mode")

        app._focused = True
        with mock.patch("dashboard.show_windows_toast") as toast, \
                mock.patch("dashboard.get_system_info", return_value="timed out"):
            app.refresh_once()
        toast.assert_not_called()
        self.assertEqual(app.get_snapshot(0, focused=False)["miners"][0]["phase"], "offline")
        self.assertFalse(app._focused)
        DashboardApi(app).get_snapshot(0, True)
        self.assertTrue(app._focused)


if __name__ == "__main__":
    unittest.main()
