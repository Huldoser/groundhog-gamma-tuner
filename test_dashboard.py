import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from unittest import mock

import config
import dashboard
from dashboard import (
    ALL_AUTOTUNE_FIELDS,
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
    replace_ips_with_names,
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

    def test_ip_inside_a_nickname_is_left_alone(self):
        text = replace_ips_with_names(
            "Miner-192.168.8.10 at 192.168.8.10",
            {"192.168.8.10": "Miner-192.168.8.10"},
        )
        self.assertEqual(text, "Miner-192.168.8.10 at 192.168.8.10")


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
        with mock.patch("dashboard.get_system_info", return_value=info), \
                mock.patch("dashboard.get_miner_status", return_value=status), \
                mock.patch("dashboard.get_miner_defaults", return_value=stored):
            app.refresh_once()
        row = app.get_snapshot(0)["miners"][0]
        self.assertEqual(row["tag"], "hold")
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
                mock.patch("dashboard.get_miner_defaults", return_value=stored):
            app.refresh_once()
        self.assertEqual(app.get_snapshot(0)["miners"][0]["tag"], "alert")

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
        self.assertTrue(controls["start_enabled"])
        self.assertFalse(controls["stop_enabled"])
        self.assertIn("525 MHz", app.get_snapshot(0)["prompts"]["baseline"])

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

    def test_add_miner_keeps_only_a_gamma_601(self):
        with temp_config():
            app = TunerDashboard()
            with mock.patch(
                "dashboard.get_system_info",
                return_value={"ASICModel": "BM1366", "boardVersion": "302"},
            ):
                rejected = app.add_miner_address("other", "10.0.0.4")
            self.assertFalse(rejected["ok"])
            self.assertEqual(config.get_miners(), [])

            with mock.patch(
                "dashboard.get_system_info",
                return_value={"ASICModel": "BM1370", "boardVersion": "601", "hostname": "gamma-a"},
            ):
                added = app.add_miner_address("", "10.0.0.9")
            self.assertTrue(added["ok"])
            stored = config.get_miners()
            self.assertEqual(stored[0]["ip"], "10.0.0.9")
            self.assertEqual(stored[0]["type"], "BM1370 601")
            self.assertEqual(stored[0]["nickname"], "gamma-a")
            self.assertEqual(app.get_snapshot(0)["miners"][0]["name"], "gamma-a")

    def test_start_with_no_enabled_miners_stays_idle(self):
        with temp_config():
            app = TunerDashboard()
            result = app.start_autotuner()
            self.assertFalse(result["ok"])
            self.assertFalse(app.running)
            self.assertIn("No miners are enabled", result["message"])

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
            self.assertTrue(stored["enabled"])

            fields["max_temp"] = ""
            cleared = app.save_autotuner_settings([
                {"ip": "10.0.0.8", "enabled": True, "fields": fields},
            ])
            self.assertTrue(cleared["ok"])
            stored = config.get_miners()[0]
            self.assertEqual(stored["max_temp"], "")
            self.assertFalse(stored["enabled"])


if __name__ == "__main__":
    unittest.main()
