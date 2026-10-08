import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest import mock

import config
import dashboard
import desktop
import history
from dashboard import (
    ALL_AUTOTUNE_FIELDS,
    LOG_LIMIT,
    DashboardApi,
    TunerDashboard,
    blank_miner_row,
    difficulty_title,
    droop_alert,
    format_core_voltage_title,
    format_difficulty,
    format_efficiency,
    format_hash_title,
    format_hashrate,
    format_input_voltage,
    format_log_line,
    format_minute_hashrate,
    format_number,
    format_share_title,
    format_shares,
    format_uptime,
    format_version_title,
    limit_level,
    live_limit,
    parse_repaste_date,
    replace_ips_with_names,
    under_limit,
)


@contextmanager
def temp_config(miners=None):
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "config.json")
        old_path = config.CONFIG_FILE
        old_last = config._last_good_config
        old_corrupt = config._config_corrupt
        config.CONFIG_FILE = path
        config._last_good_config = None
        config._config_corrupt = False
        try:
            saved = config.get_default_config()
            if miners is not None:
                saved["miners"] = miners
            config.save_config(saved)
            yield path
        finally:
            config.CONFIG_FILE = old_path
            config._last_good_config = old_last
            config._config_corrupt = old_corrupt


class DisplayHelperTests(unittest.TestCase):
    def test_over_limit_reads_text_numbers(self):
        self.assertTrue(dashboard.over_limit("15.2", 15))
        self.assertFalse(dashboard.over_limit("0.4", "2"))
        self.assertFalse(dashboard.over_limit("n/a", 2))
        self.assertFalse(dashboard.over_limit(None, 2))

    def test_format_number_and_live_limit(self):
        self.assertEqual(format_number(None, 0), "-")
        self.assertEqual(format_number("12.3%", 1), "12.3")
        self.assertEqual(format_number(12.3, 2), "12.30")
        for wall, label in (
            ("silicon", "chip errors"),
            ("hash", "low hashrate"),
            ("thermal", "temperature"),
            ("power", "power"),
            ("reject", "rejected shares"),
            ("input", "input sag"),
            ("current", "core current"),
        ):
            self.assertEqual(live_limit({"wall_type": wall}), label)
        self.assertEqual(live_limit({}), "")
        self.assertEqual(live_limit(None), "")
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
        with mock.patch("desktop.platform.system", return_value="Linux"):
            self.assertEqual(desktop.format_local_time(moment), "16:45:03")

        kernel = mock.Mock()

        def get_time(locale, flags, system_time, picture, buffer, size):
            self.assertIsNone(locale)
            self.assertEqual(flags, 0x00000002)
            self.assertIsNone(picture)
            buffer.value = "4:45 PM"
            return 7

        kernel.GetTimeFormatEx.side_effect = get_time
        with (
            mock.patch("desktop.platform.system", return_value="Windows"),
            mock.patch("ctypes.WinDLL", return_value=kernel, create=True),
        ):
            self.assertEqual(desktop.format_local_time(moment), "4:45 PM")

        kernel.GetTimeFormatEx.side_effect = None
        kernel.GetTimeFormatEx.return_value = 0
        with (
            mock.patch("desktop.platform.system", return_value="Windows"),
            mock.patch("ctypes.WinDLL", return_value=kernel, create=True),
        ):
            self.assertEqual(desktop.format_local_time(moment), "16:45:03")

        app = TunerDashboard()
        app._rows = [{"ip": "10.0.0.8"}]
        with (
            mock.patch("dashboard.format_local_time", return_value="4:45 PM"),
            mock.patch.object(app, "_apply_one_locked"),
            mock.patch.object(app, "_note_alert_locked", return_value=None),
            mock.patch.object(app, "_deliver_alerts"),
        ):
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

        status = read_network_status(
            fake_get,
            fake_connect,
            [("stratum.ckpool.org", 3336), ("public-pool.io", 23330)],
        )
        self.assertEqual(status["difficulty"], 132757073449487.5)
        self.assertEqual(
            status["pools"],
            [
                {"name": "stratum.ckpool.org", "online": True},
                {"name": "public-pool.io", "online": False},
            ],
        )

    def test_block_odds_scale_with_hashrate(self):
        odds = dashboard.block_odds("7200.00", 132757073449487.5)
        self.assertAlmostEqual(odds["mean_seconds"] / (365 * 86400), 2511, delta=1)
        self.assertAlmostEqual(odds["year"], 1 / 2511.6, delta=1e-6)
        self.assertLess(odds["day"], odds["month"])
        self.assertLess(odds["month"], odds["year"])
        doubled = dashboard.block_odds(14400, 132757073449487.5)
        self.assertAlmostEqual(doubled["mean_seconds"] * 2, odds["mean_seconds"])
        self.assertIsNone(dashboard.block_odds("-", 132757073449487.5))
        self.assertIsNone(dashboard.block_odds(0, 132757073449487.5))
        self.assertIsNone(dashboard.block_odds(7200, None))

    def test_format_block_odds(self):
        stat, title = dashboard.format_block_odds(
            dashboard.block_odds(7200, 132757073449487.5)
        )
        self.assertEqual(stat, "1 in 2.51k")
        self.assertEqual(
            title,
            "Chance to find a block solo\n"
            "Day 1 in 916.58k\n"
            "Month 1 in 30.55k\n"
            "Year 1 in 2.51k (0.040%)\n"
            "Average wait about 2,511 years",
        )
        self.assertEqual(dashboard.format_block_odds(None), ("-", ""))

    def test_failed_difficulty_fetch_keeps_the_last_value(self):
        app = TunerDashboard()
        with mock.patch(
            "dashboard.read_network_status",
            return_value={
                "difficulty": 132757073449487.5,
                "pools": [
                    {"name": "stratum.ckpool.org", "online": True},
                    {"name": "public-pool.io", "online": True},
                ],
            },
        ):
            app._refresh_network()
        self.assertEqual(app.get_snapshot(0)["network"]["difficulty"], "132.76T")
        with mock.patch(
            "dashboard.read_network_status",
            return_value={
                "difficulty": None,
                "pools": [
                    {"name": "stratum.ckpool.org", "online": False},
                    {"name": "public-pool.io", "online": True},
                ],
            },
        ):
            app._refresh_network()
        network = app.get_snapshot(0)["network"]
        self.assertEqual(network["difficulty"], "132.76T")
        self.assertEqual(network["difficulty_title"], "132757073449487.5")
        self.assertFalse(network["pools"][0]["online"])
        self.assertTrue(network["pools"][1]["online"])
        self.assertEqual(format_input_voltage(5093.75), "5.09")
        self.assertEqual(format_input_voltage(5.05), "5.05")
        self.assertEqual(format_input_voltage(None), "-")
        self.assertEqual(
            format_minute_hashrate({"hashRate_1m": 1000.5, "hashRate": 900}), "1000.50"
        )
        self.assertEqual(format_minute_hashrate({"hashRate": 900}), "-")
        self.assertEqual(format_efficiency(18.5, 1000, None), "18.50")
        self.assertEqual(format_efficiency(18.5, 0, None), "-")
        self.assertEqual(format_efficiency(18.5, 1000, "UV"), "-")
        self.assertEqual(format_efficiency(18.5, 1000, "none"), "18.50")
        self.assertEqual(format_uptime(18000), ("5h", 18000))
        self.assertEqual(format_uptime(18720), ("5h 12m", 18720))
        self.assertEqual(format_uptime(3661), ("1h 1m", 3661))
        self.assertEqual(format_uptime(172800), ("2d", 172800))
        self.assertEqual(format_uptime(183600), ("2d 3h", 183600))
        self.assertEqual(format_uptime(174600), ("2d 30m", 174600))
        self.assertEqual(format_uptime(90), ("1m", 90))
        self.assertEqual(format_uptime(45), ("45s", 45))
        self.assertEqual(format_uptime(None), ("-", None))
        self.assertEqual(format_hashrate(0.4), "0.40 GH/s")
        self.assertEqual(format_hashrate(12.5), "12.50 GH/s")
        self.assertEqual(format_hashrate(999.4), "999.40 GH/s")
        self.assertEqual(format_hashrate(1000), "1.00 TH/s")
        self.assertEqual(format_hashrate(1000.5), "1.00 TH/s")
        self.assertEqual(format_hashrate(1072.24), "1.07 TH/s")
        self.assertEqual(format_hashrate(None), "-")
        self.assertEqual(
            format_hash_title(
                {"hashRate": 1072.24, "hashRate_10m": 1070, "expectedHashrate": 1071}
            ),
            "Live 1.07 TH/s\n10m 1.07 TH/s\nExpected 1.07 TH/s",
        )
        self.assertEqual(
            format_hash_title(
                {"hashRate": 0.5, "hashRate_10m": 1200, "expectedHashrate": 1}
            ),
            "Live 0.50 GH/s\n10m 1.20 TH/s\nExpected 1.00 GH/s",
        )
        self.assertEqual(
            format_core_voltage_title(1150, 1144), "Measured 1144 mV, droop 6 mV"
        )
        self.assertEqual(format_core_voltage_title(1150, None), "")
        self.assertFalse(droop_alert(1150, 1110, {"max_droop_mv": 40}))
        self.assertTrue(droop_alert(1150, 1109, {"max_droop_mv": 40}))
        self.assertTrue(droop_alert(1150, 1109, {}))
        self.assertEqual(
            format_share_title(
                {
                    "sharesRejectedReasons": [{"message": "Stale", "count": 13}],
                    "poolDifficulty": 1000,
                    "isUsingFallbackStratum": 1,
                }
            ),
            "Stale 13\nPool difficulty 1000\nFallback pool",
        )
        self.assertEqual(
            format_share_title({"isUsingFallbackStratum": 0, "poolDifficulty": 0}), ""
        )
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

    def test_log_line_stamps_the_date_and_brackets_the_hostname(self):
        moment = datetime(2026, 9, 22, 18, 16, 5)
        self.assertEqual(
            format_log_line("goose -> holding for good hashrate.", moment),
            "[2026-09-22 18:16:05] [goose] holding for good hashrate.",
        )
        self.assertEqual(
            format_log_line("Loaded 2 miners.", moment),
            "[2026-09-22 18:16:05] Loaded 2 miners.",
        )

    def test_ip_inside_a_nickname_is_left_alone(self):
        text = replace_ips_with_names(
            "Miner-192.168.1.10 at 192.168.1.10",
            {"192.168.1.10": "Miner-192.168.1.10"},
        )
        self.assertEqual(text, "Miner-192.168.1.10 at 192.168.1.10")

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
        with mock.patch(
            "dashboard.read_latest_stable_firmware", return_value="v2.15.2"
        ):
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
        with (
            mock.patch(
                "dashboard.read_latest_stable_firmware", return_value="v2.15.2"
            ) as fetch,
            mock.patch("dashboard.datetime") as clock,
        ):
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


class RepasteDateTests(unittest.TestCase):
    def test_a_repaste_date_is_today_or_earlier(self):
        today = datetime(2026, 10, 5).date()
        self.assertEqual(parse_repaste_date("2026-10-04", today), ("2026-10-04", ""))
        self.assertEqual(parse_repaste_date(" 2026-10-05 ", today), ("2026-10-05", ""))
        self.assertEqual(parse_repaste_date("", today), ("", ""))
        self.assertEqual(parse_repaste_date(None, today), ("", ""))
        self.assertIn("future", parse_repaste_date("2026-10-06", today)[1])
        self.assertIn("2026-10-04", parse_repaste_date("Oct 4", today)[1])


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
            "reason": "holding",
        }
        stored = {
            "nickname": "Alpha",
            "max_temp": 68,
            "max_error_percentage": 2.0,
            "max_droop_mv": 40,
        }
        settings = {"temp_tolerance": 3, "vr_temp_tolerance": 3}
        with (
            mock.patch("dashboard.get_system_info", return_value=info),
            mock.patch("dashboard.get_miner_status", return_value=status),
            mock.patch("dashboard.get_miner_defaults", return_value=stored),
            mock.patch("dashboard.load_config", return_value=settings),
        ):
            app.refresh_once()
            # One read 56 mV under the setting is a lagging rail, not droop yet.
            self.assertFalse(app.get_snapshot(0)["miners"][0]["mv_alert"])
            for _ in range(dashboard.DROOP_ALERT_READS - 1):
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
        self.assertEqual(row["hash_label"], "12.50 GH/s")
        self.assertEqual(
            row["hash_title"], "Live 12.50 GH/s\n10m 12.20 GH/s\nExpected 13.00 GH/s"
        )
        self.assertEqual(row["jth"], "1480.00")
        self.assertEqual(row["up"], "5h")
        self.assertEqual(row["up_seconds"], 18000)
        self.assertEqual(row["name_title"], "v2.15.1")
        self.assertEqual(
            row["shares_title"], "Stale 13\nPool difficulty 1000\nFallback pool"
        )
        self.assertEqual(row["error"], "0.50%")
        self.assertEqual(row["limit"], "chip errors")
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
        with (
            mock.patch("dashboard.get_system_info", return_value=info),
            mock.patch("dashboard.get_miner_status", return_value=status),
            mock.patch("dashboard.get_miner_defaults", return_value=stored),
            mock.patch("dashboard.load_config", return_value=settings),
        ):
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
            blip = app.get_snapshot(0)["miners"][0]
            self.assertEqual(blip["phase"], "climb")
            self.assertEqual(blip["reason"], "no reply")
            self.assertEqual(blip["freq"], "500")
            for _ in range(dashboard.OFFLINE_AFTER_MISSES - 1):
                app.refresh_once()
        offline = app.get_snapshot(0)["miners"][0]
        self.assertEqual(offline["phase"], "offline")
        self.assertEqual(offline["tag"], "alert")
        self.assertEqual(offline["freq"], "500")
        self.assertEqual(offline["reason"], "")

        cursor = (
            app.get_snapshot(0)["log"][-1]["id"] if app.get_snapshot(0)["log"] else 0
        )
        with mock.patch(
            "dashboard.get_miners",
            return_value=[{"ip": "10.1.1.5", "nickname": "Alpha"}],
        ):
            app.log_message("heat on 10.1.1.5", "warning")
            app.log_message("settled", "success")
        tail = app.get_snapshot(cursor)
        self.assertEqual(
            [line["level"] for line in tail["log"]], ["warning", "success"]
        )
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
        with (
            mock.patch("dashboard.get_system_info", return_value=info),
            mock.patch("dashboard.get_miner_status", return_value={"phase": "hold"}),
            mock.patch("dashboard.get_miner_defaults", return_value=stored),
            mock.patch("dashboard.load_config", return_value=settings),
        ):
            app.refresh_once()
        row = app.get_snapshot(0)["miners"][0]
        self.assertEqual(row["asic_level"], "warn")
        self.assertEqual(row["vr_level"], "warn")
        self.assertTrue(row["watts_alert"])
        self.assertTrue(row["error_alert"])
        self.assertTrue(row["vin_alert"])

        info.update(
            {
                "temp": 60,
                "vrTemp": 80,
                "power": 18,
                "voltage": 5.1,
                "errorPercentage": 0.4,
            }
        )
        with (
            mock.patch("dashboard.get_system_info", return_value=info),
            mock.patch("dashboard.get_miner_status", return_value={"phase": "hold"}),
            mock.patch("dashboard.get_miner_defaults", return_value=stored),
            mock.patch("dashboard.load_config", return_value=settings),
        ):
            app.refresh_once()
        clear = app.get_snapshot(0)["miners"][0]
        self.assertEqual(clear["asic_level"], "")
        self.assertEqual(clear["vr_level"], "")
        self.assertFalse(clear["watts_alert"])
        self.assertFalse(clear["error_alert"])
        self.assertFalse(clear["vin_alert"])

    def test_row_flags_clocks_under_the_saved_floor(self):
        app = TunerDashboard()
        app._rows = [blank_miner_row("Alpha", "10.0.0.8")]
        info = {"frequency": 250, "coreVoltage": 950, "temp": 50, "vrTemp": 50}
        stored = {"min_freq": 400, "min_volt": 1000}
        with (
            mock.patch("dashboard.get_system_info", return_value=info),
            mock.patch("dashboard.get_miner_status", return_value={"phase": "hold"}),
            mock.patch("dashboard.get_miner_defaults", return_value=stored),
            mock.patch("dashboard.load_config", return_value={}),
        ):
            app.refresh_once()
        row = app.get_snapshot(0)["miners"][0]
        self.assertTrue(row["floor_alert"])
        self.assertEqual(row["tag"], "alert")
        self.assertEqual(row["reason"], "under minimum clocks")
        self.assertEqual(dashboard.alert_kind(row), "below_floor")
        self.assertIn("minimum clocks", dashboard.alert_message("Alpha", "below_floor"))

        info.update({"frequency": 400, "coreVoltage": 1000})
        with (
            mock.patch("dashboard.get_system_info", return_value=info),
            mock.patch("dashboard.get_miner_status", return_value={"phase": "hold"}),
            mock.patch("dashboard.get_miner_defaults", return_value=stored),
            mock.patch("dashboard.load_config", return_value={}),
        ):
            app.refresh_once()
        clear = app.get_snapshot(0)["miners"][0]
        self.assertFalse(clear["floor_alert"])
        self.assertEqual(dashboard.alert_kind(clear), "")

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
        first = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        second = config.new_miner_record(
            "BM1370 601", "10.0.0.9", "Beta", config.get_default_config()
        )
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
            autotune._publish_status(held_ip, phase="hold", wall_type="silicon")
            autotune._publish_status(skipped_ip, phase="skipped")
            autotune._publish_status(live_ip, phase="climb")
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
                {
                    "ip": held_ip,
                    "phase": "hold",
                    "tag": "hold",
                    "asic": "60",
                    "error": "1%",
                },
                {
                    "ip": skipped_ip,
                    "phase": "skipped",
                    "tag": "idle",
                    "asic": "60",
                    "error": "1%",
                },
                {
                    "ip": live_ip,
                    "phase": "climb",
                    "tag": "climb",
                    "asic": "60",
                    "error": "1%",
                },
            ]
            app.threads = [held, skipped, live]
            app.running = True
            app._stop_in_progress = True
            app._finish_stop()
            held_status = autotune.get_miner_status(held_ip)
            self.assertEqual(held_status["phase"], "stopped")
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
        miner = {
            "ip": "10.0.0.8",
            "nickname": "Alpha",
            "enabled": True,
            "type": "BM1370 601",
        }
        with temp_config([miner]):
            app = TunerDashboard()
            fields = {field: "10" for field in ALL_AUTOTUNE_FIELDS}
            fields["max_freq"] = "5000"
            fields["min_input_voltage"] = "4.9"
            fields["max_error_percentage"] = "2"
            fields["max_temp"] = "68.5"
            fields["max_watts"] = "50.25"
            fields["max_vr_temp"] = "88.5"
            saved = app.save_autotuner_settings(
                [
                    {"ip": "10.0.0.8", "enabled": True, "fields": fields},
                ]
            )
            self.assertTrue(saved["ok"])
            stored = config.get_miners()[0]
            self.assertEqual(stored["max_freq"], config.HARD_MAX_FREQ)
            self.assertEqual(stored["max_droop_mv"], 10)
            self.assertEqual(stored["max_temp"], 68.5)
            self.assertEqual(stored["max_watts"], 50.25)
            self.assertEqual(stored["max_vr_temp"], 88.5)
            self.assertTrue(stored["enabled"])

            fields["max_temp"] = ""
            cleared = app.save_autotuner_settings(
                [
                    {"ip": "10.0.0.8", "enabled": True, "fields": fields},
                ]
            )
            self.assertTrue(cleared["ok"])
            stored = config.get_miners()[0]
            self.assertEqual(stored["max_temp"], "")
            self.assertFalse(stored["enabled"])

    def test_blank_autotuner_cells_stay_blank(self):
        miner = {
            "ip": "10.0.0.8",
            "nickname": "Alpha",
            "enabled": False,
            "type": "BM1370 601",
            "max_temp": "",
        }
        with temp_config([miner]):
            app = TunerDashboard()
            result = app.get_autotuner_settings()
            self.assertTrue(result["ok"])
            fields = result["miners"][0]["fields"]
            self.assertEqual(fields["max_temp"], "")
            self.assertEqual(fields["min_freq"], "")
            self.assertFalse(result["miners"][0]["enabled"])

    def test_failed_restart_is_not_reported_as_triggered(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        with temp_config([miner]):
            app = TunerDashboard()
            with mock.patch(
                "dashboard.restart_bitaxe",
                return_value="10.0.0.8 -> Error restarting system: down",
            ):
                result = app.restart_miner("10.0.0.8")
        self.assertFalse(result["ok"])
        self.assertEqual(result["notice"]["title"], "Restart Failed")
        self.assertNotEqual(result["notice"]["title"], "Restart Triggered")

    def test_core_current_cap_defaults_for_old_miners_and_never_passes_29(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        # Saved before core current and modes existed: a 601 in Max mode.
        miner.pop("max_core_amps")
        miner.pop("mode")
        with temp_config([miner]):
            app = TunerDashboard()
            row = app.get_autotuner_settings()["miners"][0]
            self.assertEqual(row["fields"]["max_core_amps"], "29.0")
            fields = dict(row["fields"], max_core_amps="35")
            saved = app.save_autotuner_settings(
                [{"ip": "10.0.0.8", "enabled": True, "fields": fields}]
            )
            self.assertTrue(saved["ok"])
            stored = config.get_miners()[0]
            self.assertEqual(stored["max_core_amps"], 29.0)
            self.assertTrue(stored["enabled"])

    def test_core_current_shows_in_the_table_with_its_level(self):
        app = TunerDashboard()
        app._rows = [blank_miner_row("Alpha", "10.0.0.8")]
        stored = {"nickname": "Alpha", "max_core_amps": 28}
        for milliamps, text, level in (
            (20000, "20.0", ""),
            (27500, "27.5", "warn"),
            (28400, "28.4", "bad"),
        ):
            info = {"frequency": 900, "coreVoltage": 1300, "current": milliamps}
            with (
                mock.patch("dashboard.get_system_info", return_value=info),
                mock.patch("dashboard.get_miner_status", return_value={}),
                mock.patch("dashboard.get_miner_defaults", return_value=stored),
                mock.patch("dashboard.load_config", return_value={}),
            ):
                app.refresh_once()
            row = app.get_snapshot(0)["miners"][0]
            self.assertEqual((row["amps"], row["amps_level"]), (text, level))
            self.assertIn(f"{text} A of 28 A", row["watts_title"])

    def test_limits_screen_follows_the_code(self):
        # The fleet's own config: new 601s join in Max hashrate mode.
        fleet = config.get_default_config()
        fleet["default_mode"] = "max_hashrate"
        fleet["miners"] = [_gamma("10.0.0.8", "Alpha")]
        with temp_config():
            config.save_config(fleet)
            result = DashboardApi(TunerDashboard()).get_hardware_limits()
        self.assertTrue(result["ok"])
        rows = {
            row[0]: row[1] for section in result["sections"] for row in section["rows"]
        }
        for section in result["sections"]:
            self.assertTrue(section["title"])
            for row in section["rows"]:
                self.assertEqual(len(row), 3)
                self.assertTrue(all(isinstance(cell, str) for cell in row))
        self.assertEqual(rows["Core current shutdown"], "30 A")
        self.assertEqual(rows["ASIC overheat cutoff"], "75 °C")
        self.assertEqual(
            rows["Core voltage"],
            f"{config.HARD_MIN_VOLT}–{config.HARD_MAX_VOLT} mV",
        )
        self.assertEqual(
            rows["ASIC temperature"], f"{config.GAMMA601_LIMITS['max_temp']} °C"
        )
        self.assertEqual(rows["Core current cap"], "up to 29 A")
        # A fresh install starts new miners in Balanced.
        with temp_config():
            fresh = DashboardApi(TunerDashboard()).get_hardware_limits()
        fresh_rows = {
            row[0]: row[1] for section in fresh["sections"] for row in section["rows"]
        }
        self.assertEqual(fresh_rows["ASIC temperature"], "65 °C")
        self.assertEqual(fresh_rows["Max voltage"], "1250 mV")
        page = os.path.join(os.path.dirname(dashboard.__file__), "web", "index.html")
        with open(page, encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn('data-view="limits"', html)
        self.assertIn('id="limits-view"', html)

    def test_fast_start_is_on_by_default_and_saves(self):
        with temp_config():
            app = TunerDashboard()
            settings = app.get_global_settings()["settings"]
            self.assertTrue(settings["fast_start"])
            settings["fast_start"] = False
            self.assertTrue(app.save_global_settings(settings)["ok"])
            self.assertFalse(config.load_config()["fast_start"])
            self.assertFalse(app.get_global_settings()["settings"]["fast_start"])
            self.assertTrue(settings["continue_from_live"])
            settings["continue_from_live"] = False
            self.assertTrue(app.save_global_settings(settings)["ok"])
            self.assertFalse(config.load_config()["continue_from_live"])
            # A page that does not send a switch leaves it on.
            settings.pop("continue_from_live")
            settings.pop("fast_start")
            self.assertTrue(app.save_global_settings(settings)["ok"])
            self.assertTrue(config.load_config()["fast_start"])
            self.assertTrue(config.load_config()["continue_from_live"])

    def test_global_settings_save_vr_tolerance(self):
        with temp_config():
            app = TunerDashboard()
            settings = app.get_global_settings()["settings"]
            self.assertEqual(settings["vr_temp_tolerance"], 3)
            self.assertNotIn("ceiling_soak_seconds", settings)
            settings["vr_temp_tolerance"] = 4
            saved = app.save_global_settings(settings)
            self.assertTrue(saved["ok"])
            stored = config.load_config()
            self.assertEqual(stored["vr_temp_tolerance"], 4)
            self.assertNotIn("ceiling_soak_seconds", stored)
            settings["monitor_interval"] = 0
            rejected = app.save_global_settings(settings)
            self.assertFalse(rejected["ok"])
            self.assertIn("1 second", rejected["message"])
            settings["monitor_interval"] = 5
            settings["refresh_interval"] = 30
            rejected = app.save_global_settings(settings)
            self.assertFalse(rejected["ok"])
            self.assertIn("60", rejected["message"])

    def test_restart_is_blocked_while_that_miner_is_tuning(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        with temp_config([miner]):
            app = TunerDashboard()

            class _Alive:
                def is_alive(self):
                    return True

            thread = _Alive()
            thread.miner_ip = "10.0.0.8"
            app.threads = [thread]
            app.running = True
            blocked = app.restart_miner("10.0.0.8")
            self.assertFalse(blocked["ok"])
            self.assertFalse(app._controls_locked()["restart_all_enabled"])
            refused = app.restart_all_miners()
            self.assertFalse(refused["ok"])
            self.assertFalse(app._restart_all_running)

    def test_restart_miner_waits_for_baseline_and_restart_all(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        with temp_config([miner]):
            app = TunerDashboard()
            app._baseline_reset_running = True
            blocked = app.restart_miner("10.0.0.8")
            self.assertFalse(blocked["ok"])
            self.assertIn("baseline", blocked["message"])
            app._baseline_reset_running = False
            app._restart_all_running = True
            blocked = app.restart_miner("10.0.0.8")
            self.assertFalse(blocked["ok"])
            self.assertIn("restart", blocked["message"].lower())

    def test_enabling_a_miner_while_running_starts_its_thread(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        miner["enabled"] = False
        with temp_config([miner]):
            app = TunerDashboard()
            fields = {field: str(miner[field]) for field in ALL_AUTOTUNE_FIELDS}
            idle = app.save_autotuner_settings(
                [{"ip": "10.0.0.8", "enabled": True, "fields": fields}]
            )
            self.assertTrue(idle["ok"])
            self.assertEqual(app.threads, [])

            app.running = True
            started = []

            def fake_monitor(*args, **kwargs):
                started.append(args[0])

            with mock.patch("dashboard.monitor_and_adjust", fake_monitor):
                saved = app.save_autotuner_settings(
                    [{"ip": "10.0.0.8", "enabled": True, "fields": fields}]
                )
            self.assertTrue(saved["ok"])
            self.assertEqual(len(app.threads), 1)
            app.threads[0].join(timeout=2)
            self.assertEqual(started, ["10.0.0.8"])
            self.assertEqual(app.threads[0].miner_ip, "10.0.0.8")

    def test_editing_the_ip_while_running_starts_the_new_address(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        with temp_config([miner]):
            app = TunerDashboard()
            app.running = True
            old_stop = threading.Event()
            app._miner_stops["10.0.0.8"] = old_stop
            started = []

            def fake_monitor(*args, **kwargs):
                started.append(args[0])

            info = {"ASICModel": "BM1370", "boardVersion": "601", "hostname": "alpha"}
            with (
                mock.patch("dashboard.get_system_info", return_value=info),
                mock.patch("dashboard.monitor_and_adjust", fake_monitor),
            ):
                result = app.edit_miner("10.0.0.8", "Alpha", "10.0.0.9")
            self.assertTrue(result["ok"])
            self.assertTrue(old_stop.is_set())
            self.assertEqual(config.get_miners()[0]["ip"], "10.0.0.9")
            self.assertEqual(len(app.threads), 1)
            app.threads[0].join(timeout=2)
            self.assertEqual(started, ["10.0.0.9"])
            self.assertEqual(app.threads[0].miner_ip, "10.0.0.9")

    def test_edit_saves_keeps_or_clears_the_repaste_date(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        with temp_config([miner]):
            app = TunerDashboard()
            saved = app.edit_miner("10.0.0.8", "Alpha", "10.0.0.8", "2026-10-04")
            self.assertTrue(saved["ok"])
            self.assertEqual(config.get_miners()[0]["repasted_on"], "2026-10-04")
            self.assertEqual(
                app.get_snapshot()["miners"][0]["repasted_on"], "2026-10-04"
            )
            # An older caller that sends no date keeps the saved one.
            self.assertTrue(app.edit_miner("10.0.0.8", "Alpha", "10.0.0.8")["ok"])
            self.assertEqual(config.get_miners()[0]["repasted_on"], "2026-10-04")
            future = app.edit_miner("10.0.0.8", "Alpha", "10.0.0.8", "2999-01-01")
            self.assertFalse(future["ok"])
            self.assertEqual(config.get_miners()[0]["repasted_on"], "2026-10-04")
            self.assertTrue(app.edit_miner("10.0.0.8", "Alpha", "10.0.0.8", "")["ok"])
            self.assertEqual(config.get_miners()[0]["repasted_on"], "")

    def test_reversed_limits_are_rejected_and_not_saved(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        with temp_config([miner]):
            app = TunerDashboard()
            fields = {field: str(miner[field]) for field in ALL_AUTOTUNE_FIELDS}
            fields["min_freq"] = "900"
            fields["max_freq"] = "400"
            rejected = app.save_autotuner_settings(
                [{"ip": "10.0.0.8", "enabled": True, "fields": fields}]
            )
            self.assertFalse(rejected["ok"])
            self.assertIn("min", rejected["message"])
            self.assertEqual(config.get_miners()[0]["min_freq"], miner["min_freq"])

            fields = {field: str(miner[field]) for field in ALL_AUTOTUNE_FIELDS}
            fields["start_volt"] = "1400"
            fields["max_volt"] = "1200"
            rejected = app.save_autotuner_settings(
                [{"ip": "10.0.0.8", "enabled": True, "fields": fields}]
            )
            self.assertFalse(rejected["ok"])
            self.assertIn("start", rejected["message"])
            self.assertEqual(config.get_miners()[0]["start_volt"], miner["start_volt"])

    def test_config_reads_utf8_with_a_byte_order_mark(self):
        with temp_config() as path:
            with open(path, "w", encoding="utf-8-sig") as file:
                file.write('{"miners": [{"ip": "10.0.0.8", "nickname": "Ångström"}]}')
            config._last_good_config = None
            self.assertEqual(config.get_miners()[0]["nickname"], "Ångström")
            self.assertEqual(config.config_problem(), "")

    def test_config_that_is_not_an_object_is_reported_as_damaged(self):
        with temp_config() as path:
            with open(path, "w", encoding="utf-8") as file:
                file.write("[]")
            config._last_good_config = None
            self.assertEqual(config.get_miners(), [])
            self.assertEqual(config.config_problem(), config.CONFIG_CORRUPT_MESSAGE)

    def test_scan_refuses_a_range_larger_than_the_limit(self):
        with temp_config():
            app = TunerDashboard()
            with mock.patch("dashboard.detect_miners") as detect:
                result = app.start_scan("10.0.0.0", "10.0.255.255")
            self.assertFalse(result["ok"])
            self.assertIn("1024", result["message"])
            detect.assert_not_called()

    def test_reload_keeps_the_last_reading_for_a_saved_miner(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        with temp_config([miner]):
            app = TunerDashboard()
            app.load_rows()
            app._rows[0]["freq"] = "700"
            config.update_miner("10.0.0.8", {"nickname": "Beta"})
            app.load_rows()
            self.assertEqual(app._rows[0]["freq"], "700")
            self.assertEqual(app._rows[0]["name"], "Beta")

    def test_nan_and_inf_limits_are_rejected_and_not_saved(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        with temp_config([miner]):
            app = TunerDashboard()
            for field, text in (
                ("max_temp", "nan"),
                ("max_watts", "inf"),
                ("max_freq", "inf"),
                ("max_volt", "1e999"),
            ):
                fields = {key: str(miner[key]) for key in ALL_AUTOTUNE_FIELDS}
                fields[field] = text
                rejected = app.save_autotuner_settings(
                    [{"ip": "10.0.0.8", "enabled": True, "fields": fields}]
                )
                self.assertFalse(rejected["ok"], field)
                self.assertEqual(config.get_miners()[0][field], miner[field])

    def test_clearing_tune_stops_that_miner(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        with temp_config([miner]):
            app = TunerDashboard()
            event = threading.Event()
            app._miner_stops["10.0.0.8"] = event
            fields = {field: str(miner[field]) for field in ALL_AUTOTUNE_FIELDS}
            saved = app.save_autotuner_settings(
                [{"ip": "10.0.0.8", "enabled": False, "fields": fields}]
            )
            self.assertTrue(saved["ok"])
            self.assertTrue(event.is_set())
            self.assertFalse(config.get_miners()[0]["enabled"])

    def test_turning_a_miner_back_on_before_its_tuner_exits_starts_a_new_one(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        with temp_config([miner]):
            app = TunerDashboard()
            release_old = threading.Event()
            old_event = threading.Event()
            old_thread = threading.Thread(target=release_old.wait, args=(2,))
            old_thread.miner_ip = "10.0.0.8"
            old_thread.start()
            app.running = True
            app.threads = [old_thread]
            app._miner_stops["10.0.0.8"] = old_event
            fields = {field: str(miner[field]) for field in ALL_AUTOTUNE_FIELDS}
            started = []
            new_started = threading.Event()

            def fake_monitor(ip, *_args, **_kwargs):
                started.append((ip, old_thread.is_alive()))
                new_started.set()

            with mock.patch("dashboard.monitor_and_adjust", fake_monitor):
                app.save_autotuner_settings(
                    [{"ip": "10.0.0.8", "enabled": False, "fields": fields}]
                )
                self.assertTrue(old_event.is_set())
                app.save_autotuner_settings(
                    [{"ip": "10.0.0.8", "enabled": True, "fields": fields}]
                )
                self.assertEqual(len(app.threads), 2)
                self.assertFalse(new_started.wait(0.2))
                release_old.set()
                self.assertTrue(new_started.wait(2))
            self.assertEqual(started, [("10.0.0.8", False)])

    def test_corrupt_config_is_reported_and_not_overwritten(self):
        with temp_config():
            path = config.CONFIG_FILE
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{broken")
            config._last_good_config = None
            config._config_corrupt = False
            app = TunerDashboard()
            app.load_rows()
            self.assertTrue(any("damaged" in line["text"] for line in app._log))
            self.assertIn("damaged", app.get_snapshot(0)["config_error"])
            rejected = app.save_global_settings(app.get_global_settings()["settings"])
            self.assertFalse(rejected["ok"])
            with open(path, encoding="utf-8") as handle:
                self.assertTrue(handle.read().startswith("{broken"))

    def test_settings_save_keeps_a_change_made_while_it_waits(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        entered = threading.Event()
        release = threading.Event()
        started = threading.Event()
        real_save = config.save_config

        def save_and_pause(cfg):
            entered.set()
            self.assertTrue(release.wait(2))
            real_save(cfg)

        def learn():
            started.set()
            config.update_miner("10.0.0.8", {"start_freq": 600, "start_volt": 1200})

        with temp_config([miner]):
            app = TunerDashboard()
            fields = {field: str(miner[field]) for field in ALL_AUTOTUNE_FIELDS}
            fields["max_temp"] = "55"

            def saver():
                with mock.patch("config.save_config", save_and_pause):
                    app.save_autotuner_settings(
                        [{"ip": "10.0.0.8", "enabled": True, "fields": fields}]
                    )

            saving = threading.Thread(target=saver)
            saving.start()
            self.assertTrue(entered.wait(2))
            learning = threading.Thread(target=learn)
            learning.start()
            self.assertTrue(started.wait(2))
            time.sleep(0.05)
            release.set()
            saving.join(2)
            learning.join(2)
            self.assertFalse(saving.is_alive())
            self.assertFalse(learning.is_alive())
            stored = config.get_miners()[0]
            self.assertEqual(stored["max_temp"], 55)
            self.assertEqual(stored["start_freq"], 600)
            self.assertEqual(stored["start_volt"], 1200)

    def test_skipped_session_returns_to_idle(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )

        def finished(*args, **kwargs):
            return None

        with temp_config([miner]):
            app = TunerDashboard()
            with mock.patch("dashboard.monitor_and_adjust", finished):
                started = app.start_autotuner()
                self.assertTrue(started["ok"])
                for thread in list(app.threads):
                    thread.join(timeout=2)
                controls = app.get_snapshot(0)["controls"]
                self.assertEqual(controls["status"], "idle")
                self.assertTrue(controls["reset_enabled"])
                again = app.start_autotuner()
                self.assertTrue(again["ok"])
                for thread in list(app.threads):
                    thread.join(timeout=2)

    def test_baseline_reset_staggers_each_miner(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        called = threading.Event()
        seen = {}

        def fake_reset(*args, **kwargs):
            seen["kwargs"] = kwargs
            called.set()

        with temp_config([miner]):
            app = TunerDashboard()
            with mock.patch("dashboard.reset_miners_to_baseline", fake_reset):
                result = app.reset_baseline()
                self.assertTrue(result["ok"])
                self.assertEqual(result["notice"]["title"], "Baseline Reset Started")
                self.assertTrue(called.wait(2))
                # The reset thread still marks the reset in history.db inside
                # this temporary folder. Let it finish before the folder goes.
                deadline = time.time() + 2
                while app._baseline_reset_running and time.time() < deadline:
                    time.sleep(0.01)
                self.assertFalse(app._baseline_reset_running)
        self.assertFalse(seen["kwargs"].get("parallel", False))

    def test_reset_miner_baseline_resets_only_that_miner(self):
        first = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        second = config.new_miner_record(
            "BM1370 601", "10.0.0.9", "Beta", config.get_default_config()
        )
        seen = []

        def fake_reset(miners, log_callback, **kwargs):
            seen.extend(miner["ip"] for miner in miners)
            return 0

        with temp_config([first, second]):
            app = TunerDashboard()
            with mock.patch("dashboard.reset_miners_to_baseline", fake_reset):
                result = DashboardApi(app).reset_miner_baseline("10.0.0.9")
        self.assertTrue(result["ok"])
        self.assertEqual(seen, ["10.0.0.9"])
        self.assertIn("Beta", result["message"])
        self.assertFalse(app._baseline_reset_running)

    def test_reset_miner_baseline_is_blocked_while_that_miner_is_tuning(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        reset = mock.Mock(return_value=0)
        with temp_config([miner]):
            app = TunerDashboard()

            class _Alive:
                def is_alive(self):
                    return True

            thread = _Alive()
            thread.miner_ip = "10.0.0.8"
            app.threads = [thread]
            app.running = True
            with mock.patch("dashboard.reset_miners_to_baseline", reset):
                blocked = app.reset_miner_baseline("10.0.0.8")
                missing = app.reset_miner_baseline("10.0.0.99")
        self.assertFalse(blocked["ok"])
        self.assertIn("Stop the autotuner", blocked["message"])
        self.assertFalse(missing["ok"])
        reset.assert_not_called()

    def test_reset_miner_baseline_reports_a_rejected_write(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        with temp_config([miner]):
            app = TunerDashboard()
            with mock.patch("dashboard.reset_miners_to_baseline", return_value=1):
                result = app.reset_miner_baseline("10.0.0.8")
        self.assertFalse(result["ok"])
        self.assertEqual(result["notice"]["title"], "Reset Failed")

    def test_restart_all_miners_restarts_each_saved_miner(self):
        first = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        second = config.new_miner_record(
            "BM1370 601", "10.0.0.9", "Beta", config.get_default_config()
        )
        called = threading.Event()
        release = threading.Event()
        seen = {}

        def fake_restart(*args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs
            called.set()
            release.wait(2)

        with temp_config([first, second]):
            app = TunerDashboard()
            with mock.patch("dashboard.restart_miners", fake_restart):
                result = DashboardApi(app).restart_all_miners()
                self.assertTrue(result["ok"])
                self.assertNotIn("notice", result)
                self.assertTrue(called.wait(2))
                blocked = app.restart_all_miners()
                self.assertFalse(blocked["ok"])
                self.assertIn("already in progress", blocked["message"])
                release.set()
                for _ in range(40):
                    if not app._restart_all_running:
                        break
                    time.sleep(0.05)
        self.assertEqual(
            [miner["ip"] for miner in seen["args"][0]], ["10.0.0.8", "10.0.0.9"]
        )
        self.assertNotIn("stagger_seconds", seen["kwargs"])
        self.assertFalse(app._restart_all_running)

    def test_restart_all_without_miners_stays_idle(self):
        with temp_config([]):
            app = TunerDashboard()
            result = app.restart_all_miners()
            self.assertFalse(result["ok"])
            self.assertIn("add a miner", result["message"])
            self.assertFalse(app._restart_all_running)
            controls = app.get_snapshot(0)["controls"]
            self.assertEqual(controls["status"], "idle")
            self.assertTrue(controls["restart_all_enabled"])

    def test_restart_all_waits_for_baseline_and_blocks_start(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )
        with temp_config([miner]):
            app = TunerDashboard()
            app._baseline_reset_running = True
            during_reset = app.restart_all_miners()
            self.assertFalse(during_reset["ok"])
            self.assertFalse(app._restart_all_running)

            app._baseline_reset_running = False
            app._restart_all_running = True
            during_restart = app.reset_baseline()
            self.assertFalse(during_restart["ok"])
            self.assertFalse(app._baseline_reset_running)
            started = app.start_autotuner()
            self.assertFalse(started["ok"])
            self.assertIn("restart", started["message"])
            controls = app.get_snapshot(0)["controls"]
            self.assertEqual(controls["status"], "restarting")
            self.assertEqual(controls["status_label"], "Restarting")
            self.assertFalse(controls["reset_enabled"])
            self.assertFalse(controls["restart_all_enabled"])

    def test_subnet_fleet_and_pool(self):
        self.assertEqual(
            dashboard.subnet_range_for("192.168.1.40"), ("192.168.1.1", "192.168.1.254")
        )
        self.assertEqual(dashboard.subnet_range_for(""), ("", ""))
        self.assertEqual(dashboard.subnet_range_for("not-an-ip"), ("", ""))
        self.assertFalse(dashboard.wifi_is_weak(-44))
        self.assertTrue(dashboard.wifi_is_weak(-70))
        host, fallback = dashboard.pool_host(
            {"stratumURL": "stratum+tcp://public-pool.io:23330"}
        )
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
        with (
            mock.patch("dashboard.get_system_info", return_value=info),
            mock.patch(
                "dashboard.get_miner_status",
                return_value={"phase": "hold", "reason": "holding"},
            ),
            mock.patch("dashboard.get_miner_defaults", return_value=stored),
            mock.patch("dashboard.load_config", return_value={}),
        ):
            app.refresh_once()
        snapshot = app.get_snapshot(0)
        self.assertNotIn("history", snapshot)
        self.assertNotIn("odds", snapshot)
        self.assertEqual(snapshot["fleet"]["online"], 1)
        self.assertEqual(snapshot["fleet"]["hash"], "1.00 TH/s")
        self.assertEqual(snapshot["fleet"]["hold"], 1)
        self.assertEqual(snapshot["fleet"]["odds"], "-")
        self.assertEqual(snapshot["fleet"]["odds_title"], "")
        self.assertIn("start", snapshot["scan_range"])
        with mock.patch(
            "dashboard.read_network_status",
            return_value={"difficulty": 132757073449487.5, "pools": []},
        ):
            app._refresh_network()
        fleet = app.get_snapshot(0)["fleet"]
        self.assertEqual(fleet["odds"], "1 in 18.08k")
        self.assertIn("Average wait about 18,081 years", fleet["odds_title"])

    def test_background_notice_for_offline_power_fault_and_overheat(self):
        app = TunerDashboard()
        app._rows = [blank_miner_row("Alpha", "10.0.0.8")]
        app._focused = False
        stored = {"nickname": "Alpha"}
        # A reboot or Wi-Fi blip misses a read or two. That is not offline yet.
        with (
            mock.patch("dashboard.notify") as toast,
            mock.patch("dashboard.get_system_info", return_value="timed out"),
        ):
            for _ in range(dashboard.OFFLINE_AFTER_MISSES - 1):
                app.refresh_once()
        toast.assert_not_called()
        with (
            mock.patch("dashboard.notify") as toast,
            mock.patch("dashboard.get_system_info", return_value="timed out"),
        ):
            app.refresh_once()
        toast.assert_called_once_with("Groundhog Gamma Tuner", "Alpha is offline.")
        with (
            mock.patch("dashboard.notify") as toast,
            mock.patch("dashboard.get_system_info", return_value="timed out"),
        ):
            app.refresh_once()
        toast.assert_not_called()

        fault = {"power_fault": "UV", "temp": 40, "hashRate_1m": 10, "frequency": 500}
        with (
            mock.patch("dashboard.notify") as toast,
            mock.patch("dashboard.get_system_info", return_value=fault),
            mock.patch("dashboard.get_miner_status", return_value={"phase": "hold"}),
            mock.patch("dashboard.get_miner_defaults", return_value=stored),
            mock.patch("dashboard.load_config", return_value={}),
        ):
            app.refresh_once()
        toast.assert_called_once_with(
            "Groundhog Gamma Tuner", "Alpha reported a power fault."
        )
        row = app.get_snapshot(0)["miners"][0]
        self.assertTrue(row["power_fault"])
        self.assertEqual(row["reason"], "power fault")
        self.assertEqual(row["tag"], "alert")

        hot = {
            "overheat_mode": 1,
            "power_fault": "none",
            "temp": 40,
            "hashRate_1m": 10,
            "frequency": 500,
        }
        with (
            mock.patch("dashboard.notify") as toast,
            mock.patch("dashboard.get_system_info", return_value=hot),
            mock.patch("dashboard.get_miner_status", return_value={"phase": "hold"}),
            mock.patch("dashboard.get_miner_defaults", return_value=stored),
            mock.patch("dashboard.load_config", return_value={}),
        ):
            app.refresh_once()
        toast.assert_called_once_with(
            "Groundhog Gamma Tuner", "Alpha is in overheat mode."
        )
        self.assertEqual(app.get_snapshot(0)["miners"][0]["reason"], "overheat mode")

        app._focused = True
        with (
            mock.patch("dashboard.notify") as toast,
            mock.patch("dashboard.get_system_info", return_value="timed out"),
        ):
            for _ in range(dashboard.OFFLINE_AFTER_MISSES):
                app.refresh_once()
        toast.assert_not_called()
        self.assertEqual(
            app.get_snapshot(0, focused=False)["miners"][0]["phase"], "offline"
        )
        self.assertFalse(app._focused)
        DashboardApi(app).get_snapshot(0, True)
        self.assertTrue(app._focused)

    def test_cooled_overheat_is_cleared_and_a_healthy_sample_drops_both_warnings(self):
        app = TunerDashboard()
        app._rows = [blank_miner_row("Alpha", "10.0.0.8")]
        stored = {"nickname": "Alpha", "max_temp": 68, "max_vr_temp": 88}
        status = {"phase": "hold"}
        cool = {
            "overheat_mode": 1,
            "power_fault": "UV",
            "temp": 45,
            "vrTemp": 40,
            "power": 12,
            "hashRate_1m": 10,
            "frequency": 500,
        }
        with (
            mock.patch("dashboard.patch_system", return_value=(True, "")) as patch,
            mock.patch("dashboard.get_system_info", return_value=cool),
            mock.patch("dashboard.get_miner_status", return_value=status),
            mock.patch("dashboard.get_miner_defaults", return_value=stored),
            mock.patch("dashboard.load_config", return_value={}),
        ):
            app.refresh_once()
        patch.assert_called_once_with("10.0.0.8", {"overheat_mode": 0})
        latched = app.get_snapshot(0)["miners"][0]
        self.assertTrue(latched["overheat"])
        self.assertTrue(latched["power_fault"])
        self.assertEqual(latched["tag"], "alert")
        self.assertEqual(latched["reason"], "overheat mode")

        healthy = {
            "overheat_mode": 0,
            "temp": 45,
            "vrTemp": 40,
            "power": 12,
            "hashRate_1m": 10,
            "frequency": 500,
        }
        with (
            mock.patch("dashboard.patch_system", return_value=(True, "")) as patch,
            mock.patch("dashboard.get_system_info", return_value=healthy),
            mock.patch("dashboard.get_miner_status", return_value=status),
            mock.patch("dashboard.get_miner_defaults", return_value=stored),
            mock.patch("dashboard.load_config", return_value={}),
        ):
            app.refresh_once()
        patch.assert_not_called()
        row = app.get_snapshot(0)["miners"][0]
        self.assertFalse(row["overheat"])
        self.assertFalse(row["power_fault"])
        self.assertEqual(row["tag"], "hold")
        self.assertEqual(row["reason"], "")

    def test_failed_overheat_clear_is_logged_once_until_the_flag_reads_clear(self):
        app = TunerDashboard()
        app._rows = [blank_miner_row("Alpha", "10.0.0.8")]
        stored = {"nickname": "Alpha", "max_temp": 68, "max_vr_temp": 88}
        cool = {
            "overheat_mode": 1,
            "temp": 45,
            "vrTemp": 40,
            "power": 12,
            "hashRate_1m": 10,
            "frequency": 500,
        }
        healthy = {
            "overheat_mode": 0,
            "temp": 45,
            "vrTemp": 40,
            "power": 12,
            "hashRate_1m": 10,
            "frequency": 500,
        }

        def warnings():
            return [
                line
                for line in app.get_snapshot(0)["log"]
                if line["level"] == "warning" and "overheat" in line["text"]
            ]

        with (
            mock.patch("dashboard.patch_system", return_value=(False, "down")) as patch,
            mock.patch("dashboard.get_system_info", return_value=cool),
            mock.patch("dashboard.get_miner_status", return_value={"phase": "hold"}),
            mock.patch("dashboard.get_miner_defaults", return_value=stored),
            mock.patch("dashboard.load_config", return_value={}),
        ):
            app.refresh_once()
            app.refresh_once()
        self.assertEqual(patch.call_count, 2)
        self.assertEqual(len(warnings()), 1)
        self.assertIn("Could not clear overheat mode", warnings()[0]["text"])

        with (
            mock.patch("dashboard.patch_system", return_value=(True, "")) as patch,
            mock.patch("dashboard.get_system_info", return_value=healthy),
            mock.patch("dashboard.get_miner_status", return_value={"phase": "hold"}),
            mock.patch("dashboard.get_miner_defaults", return_value=stored),
            mock.patch("dashboard.load_config", return_value={}),
        ):
            app.refresh_once()
        patch.assert_not_called()

        with (
            mock.patch("dashboard.patch_system", return_value=(False, "down")) as patch,
            mock.patch("dashboard.get_system_info", return_value=cool),
            mock.patch("dashboard.get_miner_status", return_value={"phase": "hold"}),
            mock.patch("dashboard.get_miner_defaults", return_value=stored),
            mock.patch("dashboard.load_config", return_value={}),
        ):
            app.refresh_once()
        patch.assert_called_once_with("10.0.0.8", {"overheat_mode": 0})
        self.assertEqual(len(warnings()), 2)

    def test_overheat_clear_skips_an_ip_removed_during_the_poll(self):
        app = TunerDashboard()
        app._rows = [blank_miner_row("Alpha", "10.0.0.8")]
        cool = {
            "overheat_mode": 1,
            "temp": 45,
            "vrTemp": 40,
            "power": 12,
            "hashRate_1m": 10,
            "frequency": 500,
        }

        def fetch(_ip):
            app._rows = []
            return cool

        with (
            mock.patch("dashboard.patch_system", return_value=(True, "")) as patch,
            mock.patch("dashboard.get_system_info", side_effect=fetch),
            mock.patch("dashboard.get_miner_status", return_value={"phase": "hold"}),
            mock.patch("dashboard.get_miner_defaults", return_value={}),
            mock.patch("dashboard.load_config", return_value={}),
        ):
            app.refresh_once()
        patch.assert_not_called()

    def test_failed_baseline_and_restart_all_do_not_log_success(self):
        miner = config.new_miner_record(
            "BM1370 601", "10.0.0.8", "Alpha", config.get_default_config()
        )

        def finished(app, phrase):
            for _ in range(40):
                lines = [
                    line
                    for line in app.get_snapshot(0)["log"]
                    if phrase in line["text"]
                ]
                if lines:
                    return lines[-1]
                time.sleep(0.05)
            return None

        with temp_config([miner]):
            app = TunerDashboard()
            with mock.patch(
                "autotune.set_system_settings",
                return_value="10.0.0.8 -> Error setting system settings: down",
            ):
                started = app.reset_baseline()
                self.assertTrue(started["ok"])
                line = finished(app, "Baseline reset finished")
            self.assertIsNotNone(line)
            self.assertEqual(line["level"], "error")
            self.assertIn("did not accept", line["text"])

        with temp_config([miner]):
            app = TunerDashboard()
            with mock.patch(
                "autotune.restart_bitaxe",
                return_value="10.0.0.8 -> Error restarting system: down",
            ):
                started = app.restart_all_miners()
                self.assertTrue(started["ok"])
                line = finished(app, "Restart of all miners finished")
            self.assertIsNotNone(line)
            self.assertEqual(line["level"], "error")
            self.assertIn("failure", line["text"])


MOOSE_JAW = {
    "name": "Moose Jaw, Saskatchewan, Canada",
    "latitude": 50.40005,
    "longitude": -105.53445,
    "timezone": "America/Regina",
    "source": "search",
}


def _gamma(ip, name):
    return config.new_miner_record("BM1370 601", ip, name, config.get_default_config())


class HistoryScreenTests(unittest.TestCase):
    def test_history_reads_samples_beside_the_config(self):
        with temp_config([_gamma("10.0.0.8", "Alpha"), _gamma("10.0.0.9", "Beta")]):
            now = int(time.time())
            history.record_samples(
                [
                    {
                        "ts": now - 600,
                        "ip": ip,
                        "good_hashrate": rate,
                        "hashrate": rate,
                        "power": 17.0,
                        "frequency": 550,
                        "voltage": 1150,
                        "settled": 1,
                        "outdoor_temp": 9.0,
                        "weather_code": 0,
                    }
                    for ip, rate in (("10.0.0.8", 1000.0), ("10.0.0.9", 900.0))
                ]
            )
            app = TunerDashboard()
            result = DashboardApi(app).get_history(
                {"ip": "unknown", "period": "bogus", "metric": "nope"}
            )
            self.assertTrue(result["ok"])
            self.assertEqual(
                result["filters"],
                {"ip": "", "metric": "good_hashrate", "period": "7d"},
            )
            self.assertEqual(
                [miner["name"] for miner in result["miners"]], ["Alpha", "Beta"]
            )
            self.assertEqual([miner["slot"] for miner in result["miners"]], [0, 1])
            self.assertEqual(result["best"]["value"], 1900.0)
            self.assertEqual(set(result["series"]), {"10.0.0.8", "10.0.0.9"})
            self.assertIsNone(result["location"])
            one = app.get_history({"ip": "10.0.0.9", "period": "24h"})
            self.assertEqual(one["best"]["value"], 900.0)
            self.assertEqual(set(one["series"]), {"10.0.0.9"})

    def test_since_repaste_starts_each_miner_at_its_saved_day(self):
        today = datetime.now().date().isoformat()
        alpha = _gamma("10.0.0.8", "Alpha")
        alpha["repasted_on"] = today
        with temp_config([alpha, _gamma("10.0.0.9", "Beta")]):
            start = history.day_start(today)
            now = int(time.time())
            history.record_samples(
                [
                    {
                        "ts": ts,
                        "ip": ip,
                        "good_hashrate": 1000.0,
                        "hashrate": 1000.0,
                        "settled": 1,
                    }
                    for ts, ip in (
                        (start - 3600, "10.0.0.8"),
                        (now, "10.0.0.8"),
                        (start - 3600, "10.0.0.9"),
                    )
                ]
            )
            result = TunerDashboard().get_history({"period": "repaste"})
            self.assertTrue(result["ok"])
            self.assertEqual(result["filters"]["period"], "repaste")
            self.assertEqual(result["repastes"], {"10.0.0.8": start})
            self.assertEqual(
                [point[0] for point in result["series"]["10.0.0.8"]], [now]
            )
            # A miner with no repaste date keeps all of its samples.
            self.assertEqual(len(result["series"]["10.0.0.9"]), 1)

    def test_baseline_resets_are_marked_as_fresh_starts(self):
        miners = [_gamma("10.0.0.8", "Alpha"), _gamma("10.0.0.9", "Beta")]
        with temp_config(miners):
            app = TunerDashboard()
            with mock.patch("dashboard.reset_miners_to_baseline", return_value=0):
                self.assertTrue(app.reset_miner_baseline("10.0.0.9")["ok"])
                self.assertEqual(set(history.last_events("reset")), {"10.0.0.9"})
                self.assertTrue(app.reset_baseline()["ok"])
                for _ in range(40):
                    if not app._baseline_reset_running:
                        break
                    time.sleep(0.05)
            self.assertEqual(
                set(history.last_events("reset")), {"10.0.0.8", "10.0.0.9"}
            )

    def test_each_poll_feeds_the_history_recorder(self):
        with temp_config([_gamma("10.0.0.8", "Alpha")]):
            app = TunerDashboard()
            app.load_rows()
            clock = {"now": 1_800_000_000.0}
            app._history = history.HistoryRecorder(clock=lambda: clock["now"])
            info = {
                "frequency": 550,
                "coreVoltage": 1150,
                "hashRate": 1000,
                "hashRate_10m": 1000,
                "errorPercentage": 0,
                "temp": 60,
                "vrTemp": 65,
                "power": 17,
                "uptimeSeconds": 9000,
            }
            app._weather_now = {"outdoor_temp": 7.5, "fetched_at": clock["now"]}
            app._apply_results([("10.0.0.8", info)])
            clock["now"] += history.SAMPLE_SECONDS
            app._weather_now["fetched_at"] = clock["now"]
            app._apply_results([("10.0.0.8", info)])
            rows = history.load_samples()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Alpha")
        self.assertEqual(rows[0]["outdoor_temp"], 7.5)
        self.assertEqual(rows[0]["settled"], 1)


class WeatherLocationTests(unittest.TestCase):
    def test_saving_a_place_stores_it_and_reads_the_weather(self):
        with temp_config():
            app = TunerDashboard()
            with mock.patch.object(app, "_refresh_weather_if_due") as refresh:
                saved = DashboardApi(app).save_location(dict(MOOSE_JAW))
                self.assertTrue(saved["ok"])
                for _ in range(40):
                    if refresh.called:
                        break
                    time.sleep(0.02)
            self.assertEqual(config.load_config()["location"], MOOSE_JAW)
            self.assertEqual(app.get_location()["location"], MOOSE_JAW)
            refresh.assert_called_with(force=True)
            rejected = app.save_location({"name": "Mars", "latitude": 200})
            self.assertFalse(rejected["ok"])

    def test_search_failures_come_back_as_notices(self):
        with temp_config():
            app = TunerDashboard()
            with mock.patch("dashboard.weather.search_places", return_value=[]):
                empty = app.search_location("Atlantis")
            self.assertFalse(empty["ok"])
            self.assertIn("Atlantis", empty["message"])
            with mock.patch(
                "dashboard.weather.search_places", side_effect=RuntimeError("down")
            ):
                failed = app.search_location("Moose Jaw")
            self.assertEqual(failed["message"], "down")
            with mock.patch(
                "dashboard.weather.search_places", return_value=[MOOSE_JAW]
            ):
                found = app.search_location("Moose Jaw")
            self.assertEqual(found["places"], [MOOSE_JAW])

    def test_detected_location_is_saved(self):
        with temp_config():
            app = TunerDashboard()
            place = dict(MOOSE_JAW, source="device")
            with (
                mock.patch(
                    "dashboard.weather.detect_device_location", return_value=(place, "")
                ),
                mock.patch.object(app, "_refresh_weather_if_due"),
            ):
                result = app.detect_location()
            self.assertTrue(result["ok"])
            self.assertEqual(config.load_config()["location"]["source"], "device")
            with mock.patch(
                "dashboard.weather.detect_device_location",
                return_value=(None, "Windows blocked the location."),
            ):
                blocked = app.detect_location()
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["notice"]["title"], "Device Location")

    def test_weather_refresh_uses_the_saved_place_and_backfills(self):
        with temp_config():
            config.modify_config(
                lambda saved: saved.update(location=MOOSE_JAW, weather_enabled=True)
            )
            history.record_samples([{"ts": 1_799_999_400, "ip": "10.0.0.8"}])
            app = TunerDashboard()
            current = {"outdoor_temp": 3.5, "weather_code": 71}
            hours = [(1_799_998_200, {"outdoor_temp": 2.0})]
            with (
                mock.patch(
                    "dashboard.weather.read_current_weather", return_value=current
                ) as read_current,
                mock.patch(
                    "dashboard.weather.read_hourly_weather", return_value=hours
                ) as read_hourly,
                mock.patch("dashboard.weather.detect_device_location") as detect,
            ):
                app._refresh_weather_if_due(now=1_800_000_000)
                app._refresh_weather_if_due(now=1_800_000_060)
            read_current.assert_called_once_with(50.40005, -105.53445)
            read_hourly.assert_called_once()
            detect.assert_not_called()
            self.assertEqual(history.load_samples()[0]["outdoor_temp"], 2.0)
            snapshot = app.get_snapshot()
            self.assertEqual(snapshot["weather"]["temp"], 3.5)
            self.assertEqual(snapshot["weather"]["sky"], "snow")
            self.assertEqual(snapshot["weather"]["place"], MOOSE_JAW["name"])

    def test_no_saved_place_reads_nothing_and_never_asks_the_device(self):
        with temp_config():
            config.modify_config(lambda saved: saved.update(weather_enabled=True))
            app = TunerDashboard()
            with (
                mock.patch("dashboard.weather.detect_device_location") as detect,
                mock.patch("dashboard.weather.read_current_weather") as read,
            ):
                app._refresh_weather_if_due(now=1_800_000_000)
                app._refresh_weather_if_due(now=1_800_000_000, force=True)
            detect.assert_not_called()
            read.assert_not_called()
            self.assertIsNone(app.get_snapshot()["weather"])

    def test_forgotten_location_stops_the_weather(self):
        with temp_config():
            config.modify_config(
                lambda saved: saved.update(location=MOOSE_JAW, weather_enabled=True)
            )
            app = TunerDashboard()
            app._weather_now = {"outdoor_temp": -30.0, "weather_code": 71}
            result = DashboardApi(app).clear_location()
            self.assertEqual(result, {"ok": True, "location": None})
            self.assertNotIn("location", config.load_config())
            self.assertIsNone(app.get_location()["location"])
            self.assertIsNone(app.get_snapshot()["weather"])
            with mock.patch("dashboard.weather.read_current_weather") as read:
                app._refresh_weather_if_due(now=1_800_000_000, force=True)
            read.assert_not_called()


class SetupAndPoolTests(unittest.TestCase):
    def test_pools_come_from_what_the_miners_report(self):
        info = {
            "stratumURL": "stratum+tcp://solo.ckpool.org:3333/x",
            "stratumPort": 3333,
            "fallbackStratumURL": "public-pool.io",
            "fallbackStratumPort": "21496",
        }
        self.assertEqual(
            dashboard.pool_targets(info),
            [("solo.ckpool.org", 3333), ("public-pool.io", 21496)],
        )
        self.assertEqual(dashboard.pool_targets({"stratumURL": ""}), [])
        app = TunerDashboard()
        app._rows = [blank_miner_row("Alpha", "10.0.0.8")]
        with (
            mock.patch("dashboard.get_miner_defaults", return_value={}),
            mock.patch("dashboard.load_config", return_value={}),
            mock.patch("dashboard.get_miner_status", return_value={}),
        ):
            app._apply_results([("10.0.0.8", info)])
        with mock.patch(
            "dashboard.read_network_status",
            return_value={"difficulty": None, "pools": []},
        ) as read:
            app._refresh_network()
        self.assertEqual(
            read.call_args.kwargs["pools"],
            [("solo.ckpool.org", 3333), ("public-pool.io", 21496)],
        )

    def test_a_new_config_reads_nothing_from_the_internet(self):
        with temp_config():
            saved = config.load_config()
            self.assertFalse(saved["weather_enabled"])
            for key in config.INTERNET_SWITCHES:
                self.assertFalse(saved[key], key)
            app = TunerDashboard()
            app._pool_targets = {"10.0.0.8": [("solo.ckpool.org", 3333)]}
            app._closed = mock.Mock()
            app._closed.is_set.side_effect = [False, True]
            with (
                mock.patch("dashboard.requests.get") as get,
                mock.patch("dashboard.socket.create_connection") as connect,
                mock.patch("dashboard.weather.read_current_weather") as weather_read,
                mock.patch("dashboard.weather.detect_device_location") as detect,
            ):
                app._network_loop()
            get.assert_not_called()
            connect.assert_not_called()
            weather_read.assert_not_called()
            detect.assert_not_called()
            network = app.get_snapshot(0)["network"]
            self.assertFalse(network["difficulty_enabled"])
            self.assertEqual(network["pools"], [])

    def test_each_switch_turns_on_only_its_own_read(self):
        with temp_config():
            config.modify_config(
                lambda saved: saved.update(
                    network_stats_enabled=False,
                    pool_check_enabled=True,
                    firmware_check_enabled=False,
                )
            )
            app = TunerDashboard()
            app._pool_targets = {"10.0.0.8": [("solo.ckpool.org", 3333)]}
            app._latest_firmware = "v2.15.3"
            app._closed = mock.Mock()
            app._closed.is_set.side_effect = [False, True]
            with (
                mock.patch(
                    "dashboard.read_network_status",
                    return_value={"difficulty": None, "pools": []},
                ) as read,
                mock.patch("dashboard.read_latest_stable_firmware") as firmware,
            ):
                app._network_loop()
            read.assert_called_once_with(
                pools=[("solo.ckpool.org", 3333)], difficulty=False
            )
            firmware.assert_not_called()
            self.assertEqual(app._latest_firmware, "")

    def test_difficulty_switched_off_never_asks_mempool(self):
        get = mock.Mock()
        status = dashboard.read_network_status(get=get, pools=(), difficulty=False)
        get.assert_not_called()
        self.assertEqual(status, {"difficulty": None, "pools": []})

    def test_internet_switches_round_trip_through_global_settings(self):
        with temp_config():
            app = TunerDashboard()
            settings = app.get_global_settings()["settings"]
            for key in config.INTERNET_SWITCHES:
                self.assertFalse(settings[key], key)
            settings.update(network_stats_enabled=True, firmware_check_enabled=True)
            self.assertTrue(app.save_global_settings(settings)["ok"])
            saved = config.load_config()
            self.assertTrue(saved["network_stats_enabled"])
            self.assertFalse(saved["pool_check_enabled"])
            self.assertTrue(saved["firmware_check_enabled"])

    def test_weather_stays_off_until_turned_on(self):
        with temp_config():
            app = TunerDashboard()
            with mock.patch("dashboard.weather.read_current_weather") as read:
                app._refresh_weather_if_due(now=1000.0, force=True)
            read.assert_not_called()

    def test_setup_answers_pick_the_mode(self):
        self.assertEqual(dashboard.setup_mode("stock", "hashrate").key, "balanced")
        self.assertEqual(
            dashboard.setup_mode("custom", "hashrate", acknowledged=False).key,
            "balanced",
        )
        self.assertEqual(
            dashboard.setup_mode("custom", "hashrate", acknowledged=True).key,
            "max_hashrate",
        )
        self.assertEqual(dashboard.setup_mode("stock", "balance").key, "balanced")
        self.assertEqual(
            dashboard.setup_mode("upgraded", "efficiency").key, "efficiency"
        )

    def test_first_run_setup_saves_once(self):
        with temp_config():
            app = TunerDashboard()
            self.assertFalse(app.get_setup_state()["setup_done"])
            refused = app.complete_setup({"cooling": "stock"})
            self.assertFalse(refused["ok"])
            bad_watts = app.complete_setup(
                {"cooling": "stock", "goal": "balance", "supply_watts": "lots"}
            )
            self.assertFalse(bad_watts["ok"])
            done = app.complete_setup(
                {
                    "cooling": "upgraded",
                    "goal": "efficiency",
                    "supply_watts": "30",
                    "weather": True,
                    "pool_check": True,
                }
            )
            self.assertEqual(
                done, {"ok": True, "mode": "efficiency", "mode_name": "Efficiency"}
            )
            saved = config.load_config()
            self.assertTrue(saved["setup_done"])
            self.assertTrue(saved["weather_enabled"])
            # A box left clear keeps its read off.
            self.assertTrue(saved["pool_check_enabled"])
            self.assertFalse(saved["network_stats_enabled"])
            self.assertFalse(saved["firmware_check_enabled"])
            self.assertEqual(saved["default_mode"], "efficiency")
            self.assertTrue(app.get_setup_state()["setup_done"])
            new = config.new_miner_record("x", "1.2.3.4", "n", saved)
            self.assertEqual((new["max_watts"], new["max_volt"]), (30.0, 1150))


class FullscreenTests(unittest.TestCase):
    def test_monitor_rect_covers_the_frame_inset(self):
        monitor = (0, 0, 1920, 1080)
        self.assertEqual(
            desktop.rect_covering_monitor(monitor, (0, 0, 0, 0)),
            (0, 0, 1920, 1080),
        )
        self.assertEqual(
            desktop.rect_covering_monitor(monitor, (8, 8, 8, 8)),
            (-8, -8, 1936, 1096),
        )
        self.assertEqual(
            desktop.frame_inset((0, 0, 1920, 1080), (8, 8, 1912, 1072)),
            (8, 8, 8, 8),
        )

    def test_fullscreen_snaps_only_when_entering_on_windows(self):
        app = TunerDashboard()
        window = mock.Mock()
        app._window = window
        with (
            mock.patch("dashboard.platform.system", return_value="Windows"),
            mock.patch("dashboard.snap_fullscreen_window") as snap,
        ):
            entered = app.set_fullscreen(True)
            self.assertEqual(entered, {"ok": True, "fullscreen": True})
            window.toggle_fullscreen.assert_called_once_with()
            snap.assert_called_once_with(window)

            window.toggle_fullscreen.reset_mock()
            snap.reset_mock()
            repeated = app.set_fullscreen(True)
            self.assertEqual(repeated, {"ok": True, "fullscreen": True})
            window.toggle_fullscreen.assert_not_called()
            snap.assert_not_called()

            left = app.set_fullscreen(False)
            self.assertEqual(left, {"ok": True, "fullscreen": False})
            window.toggle_fullscreen.assert_called_once_with()
            snap.assert_not_called()

        window.toggle_fullscreen.reset_mock()
        with (
            mock.patch("dashboard.platform.system", return_value="Linux"),
            mock.patch("dashboard.snap_fullscreen_window") as snap,
        ):
            entered = app.set_fullscreen(True)
        self.assertEqual(entered, {"ok": True, "fullscreen": True})
        window.toggle_fullscreen.assert_called_once_with()
        snap.assert_not_called()

    def test_fullscreen_stays_on_when_the_snap_fails(self):
        app = TunerDashboard()

        class BrokenWindow:
            def toggle_fullscreen(self):
                return None

            @property
            def native(self):
                raise OSError("snap failed")

        app._window = BrokenWindow()
        with mock.patch("dashboard.platform.system", return_value="Windows"):
            result = app.set_fullscreen(True)
        self.assertEqual(result, {"ok": True, "fullscreen": True})
        self.assertTrue(app._fullscreen)


if __name__ == "__main__":
    unittest.main()
