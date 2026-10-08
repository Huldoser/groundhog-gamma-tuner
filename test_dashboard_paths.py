"""Every guard, fallback, and error path of the dashboard and its page API."""

import sqlite3
import sys
import threading
import types
import unittest
from unittest import mock

import config
import dashboard
from dashboard import ALL_AUTOTUNE_FIELDS, DashboardApi, TunerDashboard
from test_dashboard import blank_miner_row, temp_config

GAMMA_INFO = {"ASICModel": "BM1370", "boardVersion": "601", "hostname": "goose"}


def _gamma(ip="10.0.0.8", name="Alpha", **changes):
    miner = config.new_miner_record("BM1370 601", ip, name, config.get_default_config())
    miner.update(changes)
    return miner


class _InlineThread:
    """A thread that runs its target as soon as it starts."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


class _Thread:
    """A tuner thread the dashboard only asks about."""

    def __init__(self, ip, alive=True):
        self.miner_ip = ip
        self.alive = alive

    def is_alive(self):
        return self.alive

    def start(self):
        pass

    def join(self, timeout=None):
        pass


class HelperTests(unittest.TestCase):
    def test_a_start_below_its_min_is_an_order_error(self):
        fields = {"min_freq": 500, "start_freq": 400, "max_freq": 900}
        self.assertEqual(
            dashboard.limit_order_error(fields, "Alpha"),
            "Alpha: Frequency start must be at or above min.",
        )

    def test_plain_numbers_ignore_blanks_and_other_types(self):
        self.assertIsNone(dashboard._plain_number("   "))
        self.assertIsNone(dashboard._plain_number([1]))

    def test_share_counts_round_or_keep_their_fraction(self):
        self.assertEqual(dashboard.format_shares(10.6, 2), "11/2")
        self.assertEqual(dashboard._count_text(2.5), "2.5")

    def test_missing_replies_have_empty_titles(self):
        self.assertEqual(dashboard.format_hash_title(None), "")
        self.assertEqual(dashboard.format_share_title(None), "")
        self.assertEqual(dashboard.format_version_title(None), "")
        self.assertEqual(dashboard.pool_host(None), ("", False))
        self.assertIsNone(dashboard.wifi_reading(None))
        self.assertEqual(dashboard.pool_targets(None), [])

    def test_flags_read_text_numbers_and_anything_else(self):
        self.assertFalse(dashboard._flag_set(" false "))
        self.assertTrue(dashboard._flag_set("1"))
        self.assertTrue(dashboard._flag_set(object()))

    def test_reject_reasons_skip_broken_entries(self):
        info = {
            "sharesRejectedReasons": [
                "oops",
                {"message": "", "count": 2},
                {"message": "Stale", "count": None},
                {"message": "Duplicate", "count": 3},
            ]
        }
        self.assertEqual(dashboard.format_share_title(info), "Duplicate 3")

    def test_an_ipv6_address_has_no_scan_range(self):
        self.assertEqual(dashboard.subnet_range_for("fe80::1"), ("", ""))

    def test_local_address_falls_back_to_blank(self):
        with mock.patch("dashboard.socket.socket", side_effect=OSError):
            self.assertEqual(dashboard.local_ipv4(), "")
        sock = mock.Mock()
        sock.connect.side_effect = OSError
        sock.close.side_effect = OSError
        with mock.patch("dashboard.socket.socket", return_value=sock):
            self.assertEqual(dashboard.local_ipv4(), "")
        sock = mock.Mock()
        sock.getsockname.return_value = ("127.0.1.1", 0)
        with mock.patch("dashboard.socket.socket", return_value=sock):
            self.assertEqual(dashboard.local_ipv4(), "")

    def test_the_fleet_counts_trimming_miners(self):
        summary = dashboard.fleet_summary([{"phase": "trim", "hash": 1000}])
        self.assertEqual(summary["trim"], 1)

    def test_odds_and_waits_read_naturally(self):
        self.assertEqual(dashboard._one_in(0.75), "75%")
        self.assertEqual(dashboard._wait_text(3 * 86400), "3 days")
        self.assertEqual(dashboard._wait_text(7200), "2.0 hours")

    def test_an_unknown_alert_has_no_message(self):
        self.assertEqual(dashboard.alert_message("Alpha", "unknown"), "")

    def test_a_port_check_survives_a_failing_close(self):
        sock = mock.Mock()
        sock.close.side_effect = OSError
        self.assertTrue(dashboard.stratum_port_open("h", 1, connect=lambda *a: sock))

    def test_a_connection_without_close_still_counts(self):
        self.assertTrue(
            dashboard.stratum_port_open("h", 1, connect=lambda *a: object())
        )

    def test_a_failed_difficulty_read_is_blank(self):
        def get(url, timeout=None):
            raise OSError("offline")

        status = dashboard.read_network_status(get=get)
        self.assertEqual(status, {"difficulty": None, "pools": []})

    def test_release_lists_skip_broken_and_older_entries(self):
        self.assertEqual(dashboard.latest_stable_firmware(None), "")
        releases = [
            "oops",
            {"tag_name": "v2.15.3"},
            {"tag_name": "v2.14.0"},
        ]
        self.assertEqual(dashboard.latest_stable_firmware(releases), "v2.15.3")

    def test_tolerances_and_switches_from_odd_values(self):
        self.assertEqual(dashboard._configured_tolerance(None, "temp_tolerance"), 3)
        self.assertTrue(dashboard._as_bool(" Yes "))
        self.assertFalse(dashboard._as_bool("off"))
        self.assertEqual(dashboard._coerce_log_id("x"), 0)


class WindowTests(unittest.TestCase):
    def test_the_window_opens_the_local_page(self):
        window = mock.Mock()
        window.events.closing = mock.MagicMock()
        webview = types.ModuleType("webview")
        webview.create_window = mock.Mock(return_value=window)
        webview.start = mock.Mock()
        app = TunerDashboard()
        with (
            mock.patch.dict(sys.modules, {"webview": webview}),
            mock.patch.object(app, "start_background") as background,
        ):
            app.run()
        background.assert_called_once()
        url = webview.create_window.call_args.kwargs["url"]
        self.assertTrue(url.endswith("index.html"))
        self.assertTrue(dashboard.os.path.isabs(url))
        webview.start.assert_called_once_with(app._on_ready)
        self.assertIs(app._window, window)

    def test_without_pywebview_the_app_says_what_to_install(self):
        app = TunerDashboard()
        with (
            mock.patch.dict(sys.modules, {"webview": None}),
            mock.patch("builtins.print"),
            self.assertRaises(SystemExit) as stopped,
        ):
            app.run()
        self.assertIn("pip install", str(stopped.exception.code))

    def test_the_window_opens_maximized_when_it_can(self):
        app = TunerDashboard()
        app._on_ready()
        app._window = mock.Mock()
        app._window.maximize.side_effect = RuntimeError("no")
        app._on_ready()
        app._window.maximize.assert_called_once()

    def test_background_work_starts_once(self):
        app = TunerDashboard()
        with (
            mock.patch("dashboard.threading.Thread") as thread,
            mock.patch.object(app, "load_rows") as load,
        ):
            app.start_background()
            app.start_background()
        load.assert_called_once()
        self.assertEqual(thread.call_count, 2)

    def test_a_failed_fullscreen_toggle_is_reported(self):
        app = TunerDashboard()
        app._window = mock.Mock()
        app._window.toggle_fullscreen.side_effect = RuntimeError("no screen")
        self.assertEqual(app.set_fullscreen(True)["message"], "no screen")


class LoopTests(unittest.TestCase):
    def once(self, app):
        app._closed = mock.Mock()
        app._closed.is_set.side_effect = [False, True]
        app._closed.wait.return_value = False

    def test_the_network_loop_survives_every_failure(self):
        app = TunerDashboard()
        self.once(app)
        with (
            mock.patch("dashboard.load_config", side_effect=OSError),
            mock.patch.object(app, "_refresh_network", side_effect=RuntimeError),
            mock.patch.object(app, "_refresh_firmware_if_due", side_effect=OSError),
            mock.patch.object(app, "_refresh_weather_if_due", side_effect=OSError),
        ):
            app._network_loop()
        app._closed.wait.assert_called_once()

    def test_a_closed_window_stops_the_loops_before_they_start(self):
        app = TunerDashboard()
        app._closed.set()
        with mock.patch.object(app, "_refresh_network") as refresh:
            app._network_loop()
            app._display_loop()
        refresh.assert_not_called()

    def test_the_display_loop_survives_a_failed_read(self):
        app = TunerDashboard()
        self.once(app)
        app._wake = mock.Mock()
        with mock.patch.object(app, "refresh_once", side_effect=RuntimeError):
            app._display_loop()
        app._wake.wait.assert_called_once_with(dashboard.STATUS_REFRESH_SECONDS)

    def test_a_refresh_runs_once_at_a_time_and_only_with_miners(self):
        app = TunerDashboard()
        with mock.patch("dashboard.get_system_info") as read:
            app.refresh_once()
            app._rows = [blank_miner_row("Alpha", "10.0.0.8")]
            app._status_refresh_running = True
            app.refresh_once()
        read.assert_not_called()


class WeatherAndHistoryTests(unittest.TestCase):
    def setUp(self):
        self.place = {"name": "Moose Jaw", "latitude": 50.4, "longitude": -105.5}

    def test_a_failed_read_tries_again_in_two_minutes(self):
        with temp_config():
            config.modify_config(
                lambda saved: saved.update(location=self.place, weather_enabled=True)
            )
            app = TunerDashboard()
            with mock.patch(
                "dashboard.weather.read_current_weather", return_value=None
            ):
                app._refresh_weather_if_due(now=10_000.0)
            self.assertEqual(
                app._weather_checked,
                10_000.0 - dashboard.weather.WEATHER_REFRESH_SECONDS + 120,
            )

    def test_backfill_stops_on_a_database_error_or_nothing_to_fill(self):
        app = TunerDashboard()
        with mock.patch(
            "dashboard.history.oldest_missing_weather", side_effect=sqlite3.Error
        ):
            app._backfill_weather(self.place, 1_000_000.0)
        with (
            mock.patch("dashboard.history.oldest_missing_weather", return_value=None),
            mock.patch("dashboard.weather.read_hourly_weather") as hourly,
        ):
            app._backfill_weather(self.place, 1_000_000.0)
        hourly.assert_not_called()
        with (
            mock.patch(
                "dashboard.history.oldest_missing_weather", return_value=999_000.0
            ),
            mock.patch("dashboard.weather.read_hourly_weather", return_value=[]),
            mock.patch(
                "dashboard.history.fill_missing_weather", side_effect=sqlite3.Error
            ),
        ):
            app._backfill_weather(self.place, 1_000_000.0)
        with (
            mock.patch(
                "dashboard.history.oldest_missing_weather", return_value=999_000.0
            ),
            mock.patch("dashboard.weather.read_hourly_weather", return_value=[]),
            mock.patch("dashboard.history.fill_missing_weather", return_value=0),
        ):
            app._backfill_weather(self.place, 1_000_000.0)
        self.assertFalse(any("Added outdoor" in line["text"] for line in app._log))

    def test_history_write_errors_are_logged(self):
        app = TunerDashboard()
        app._rows = [blank_miner_row("Alpha", "10.0.0.8")]
        app._history = mock.Mock()
        app._history.observe.side_effect = sqlite3.Error("locked")
        app._history.forget = mock.Mock()
        with mock.patch("dashboard.get_miner_status", return_value={}):
            app._record_history([("10.0.0.8", {})])
        with mock.patch(
            "dashboard.history.record_event", side_effect=sqlite3.Error("locked")
        ):
            app._note_fresh_start(["10.0.0.8"])
        texts = [line["text"] for line in app._log]
        self.assertTrue(any("History sample was not saved" in text for text in texts))
        self.assertTrue(any("was not marked in the history" in text for text in texts))

    def test_an_unreadable_history_is_reported(self):
        with temp_config():
            app = TunerDashboard()
            with mock.patch(
                "dashboard.history.period_samples", side_effect=sqlite3.Error("x")
            ):
                result = app.get_history({"metric": "efficiency"})
            self.assertFalse(result["ok"])
            self.assertIn("could not be read", result["message"])


class StartStopTests(unittest.TestCase):
    def test_start_waits_for_a_baseline_reset(self):
        app = TunerDashboard()
        app._baseline_reset_running = True
        result = app.start_autotuner()
        self.assertEqual(result["notice"]["title"], "Reset in Progress")

    def test_miners_missing_limits_are_skipped_with_a_notice(self):
        ready = _gamma("10.0.0.8", enabled=True)
        incomplete = _gamma("10.0.0.9", "Beta", enabled=True, max_freq="")
        with temp_config([ready, incomplete]):
            app = TunerDashboard()
            with mock.patch.object(
                app,
                "_miner_thread",
                return_value=(threading.Event(), _Thread("10.0.0.8")),
            ):
                result = app.start_autotuner()
            self.assertTrue(result["ok"])
            self.assertEqual(result["notice"]["title"], "Some miners skipped")
            self.assertIn("10.0.0.9: Missing max_freq", result["notice"]["message"])

    def test_no_complete_miner_means_no_start(self):
        with temp_config([_gamma(enabled=True, max_freq="")]):
            result = TunerDashboard().start_autotuner()
        self.assertEqual(result["notice"]["title"], "Incomplete Settings")

    def test_a_damaged_config_stops_the_confirmation(self):
        supra = config.new_miner_record(
            "BM1368 402",
            "10.0.0.2",
            "Supra",
            config.get_default_config(),
            board=dashboard.board_for_info(
                {"ASICModel": "BM1368", "boardVersion": "402"}
            ),
        )
        supra["enabled"] = True
        with temp_config([supra]):
            app = TunerDashboard()
            with mock.patch("dashboard.modify_config", return_value=False):
                result = app.start_autotuner(confirmed=True)
        self.assertEqual(result["notice"]["title"], "Config file damaged")

    def test_a_stop_during_start_leaves_the_miners_stopped(self):
        with temp_config([_gamma(enabled=True)]):
            app = TunerDashboard()
            event = threading.Event()
            thread = _Thread("10.0.0.8")
            thread.start = lambda: setattr(app, "_stop_in_progress", True)
            with mock.patch.object(app, "_miner_thread", return_value=(event, thread)):
                result = app.start_autotuner()
        self.assertTrue(result["ok"])
        self.assertTrue(event.is_set())
        self.assertFalse(app.running)

    def test_stop_twice_or_with_nothing_running_is_fine(self):
        app = TunerDashboard()
        app.running = True
        self.assertEqual(app.stop_autotuner(), {"ok": True})
        self.assertFalse(app.running)
        app._stop_in_progress = True
        self.assertEqual(app.stop_autotuner(), {"ok": True})


class BaselineTests(unittest.TestCase):
    def test_a_second_reset_waits_for_the_first(self):
        app = TunerDashboard()
        app._baseline_reset_running = True
        self.assertEqual(app.reset_baseline()["notice"]["title"], "Reset in Progress")
        with temp_config([_gamma()]):
            self.assertEqual(
                app.reset_miner_baseline("10.0.0.8")["notice"]["title"],
                "Reset in Progress",
            )
            app._baseline_reset_running = False
            app._restart_all_running = True
            self.assertEqual(
                app.reset_miner_baseline("10.0.0.8")["notice"]["title"],
                "Restart in Progress",
            )

    def test_a_reset_needs_a_miner(self):
        with temp_config():
            app = TunerDashboard()
            self.assertEqual(app.reset_baseline()["notice"]["title"], "No Miners Found")
            self.assertFalse(app._baseline_reset_running)
            self.assertEqual(
                app.reset_miner_baseline("")["notice"]["title"], "No Selection"
            )


class SetupAndLocationTests(unittest.TestCase):
    def test_setup_refuses_odd_answers_and_a_damaged_config(self):
        app = TunerDashboard()
        self.assertFalse(app.complete_setup(None)["ok"])
        with temp_config():
            with mock.patch("dashboard.modify_config", return_value=False):
                result = app.complete_setup({"cooling": "stock", "goal": "balance"})
            self.assertEqual(result["notice"]["title"], "Config file damaged")
            done = app.complete_setup({"cooling": "stock", "goal": "balance"})
            self.assertTrue(done["ok"])
            self.assertEqual(config.load_config()["supply_watts"], "")
            self.assertFalse(app._wake.is_set())

    def test_a_location_that_cannot_be_saved_or_cleared_is_reported(self):
        place = {"name": "Moose Jaw", "latitude": 50.4, "longitude": -105.5}
        app = TunerDashboard()
        with mock.patch.object(app, "_store_location", return_value=False):
            self.assertEqual(
                app.save_location(place)["notice"]["title"], "Config file damaged"
            )
        with mock.patch("dashboard.modify_config", return_value=False):
            self.assertEqual(
                app.clear_location()["notice"]["title"], "Config file damaged"
            )

    def test_one_section_set_for_two_miners_on_the_same_board(self):
        with temp_config([_gamma("10.0.0.8"), _gamma("10.0.0.9", "Beta")]):
            sections = TunerDashboard().get_hardware_limits()["sections"]
        self.assertFalse(any(":" in section["title"] for section in sections))


class ScanTests(unittest.TestCase):
    def start(self, app, found=None, error=None, cancel=False):
        def detect(start, end, on_progress=None, should_cancel=None):
            on_progress(1, 2, start)
            if cancel:
                app._scan_cancel.set()
            if error is not None:
                raise error
            return found or []

        with (
            mock.patch("dashboard.detect_miners", side_effect=detect),
            mock.patch("dashboard.threading.Thread", _InlineThread),
            mock.patch.object(app, "load_rows"),
        ):
            return app.start_scan("10.0.0.1", "10.0.0.2")

    def test_bad_ranges_are_refused(self):
        app = TunerDashboard()
        self.assertIn("valid", app.start_scan("10.0.0.1", "nope")["message"])
        self.assertIn("after", app.start_scan("10.0.0.9", "10.0.0.1")["message"])

    def test_a_scan_reports_what_it_added(self):
        app = TunerDashboard()
        self.assertEqual(self.start(app, found=[{"ip": "10.0.0.1"}]), {"ok": True})
        self.assertFalse(app._scan_running)
        self.assertIsNone(app._scan)
        self.assertTrue(any("Added 1 miner" in line["text"] for line in app._log))

    def test_a_cancelled_or_failed_scan_says_so(self):
        app = TunerDashboard()
        self.start(app, cancel=True)
        self.start(app, error=RuntimeError("socket"))
        texts = [line["text"] for line in app._log]
        self.assertTrue(any(text.endswith("Scan cancelled.") for text in texts))
        self.assertTrue(any("Scan failed: socket" in text for text in texts))

    def test_one_scan_at_a_time_and_cancel(self):
        app = TunerDashboard()
        self.assertEqual(app.cancel_scan(), {"ok": True, "running": False})
        app._scan_running = True
        app._scan = {"message": "Checking"}
        self.assertEqual(
            app.start_scan("10.0.0.1", "10.0.0.2")["message"],
            "A scan is already running.",
        )
        self.assertEqual(app.cancel_scan(), {"ok": True, "running": True})
        self.assertEqual(app._scan["message"], "Stopping scan...")
        self.assertTrue(app._scan_cancel.is_set())
        app._scan = None
        self.assertEqual(app.cancel_scan(), {"ok": True, "running": True})

    def test_progress_after_the_scan_ended_is_ignored(self):
        app = TunerDashboard()

        def detect(start, end, on_progress=None, should_cancel=None):
            app._scan = None
            on_progress(1, 2, start)
            return []

        with (
            mock.patch("dashboard.detect_miners", side_effect=detect),
            mock.patch("dashboard.threading.Thread", _InlineThread),
            mock.patch.object(app, "load_rows"),
        ):
            self.assertTrue(app.start_scan("10.0.0.1", "10.0.0.2")["ok"])


class MinerEditTests(unittest.TestCase):
    def test_remove_needs_a_saved_miner(self):
        with temp_config([_gamma()]):
            app = TunerDashboard()
            self.assertEqual(
                app.remove_miner_address("")["notice"]["title"], "No Selection"
            )
            self.assertIn("not found", app.remove_miner_address("10.0.0.9")["message"])

    def test_edit_refuses_what_it_cannot_save(self):
        with temp_config([_gamma("10.0.0.8"), _gamma("10.0.0.9", "Beta")]):
            app = TunerDashboard()
            self.assertIn("required", app.edit_miner("10.0.0.8", "A", "")["message"])
            self.assertIn(
                "not found", app.edit_miner("10.0.0.7", "A", "10.0.0.7")["message"]
            )
            self.assertIn(
                "already exists", app.edit_miner("10.0.0.8", "A", "10.0.0.9")["message"]
            )
            with mock.patch("dashboard.get_system_info", return_value="No reply"):
                self.assertEqual(
                    app.edit_miner("10.0.0.8", "A", "10.0.0.5")["message"], "No reply"
                )
            with mock.patch.object(app, "_apply_miner_edit", return_value=False):
                result = app.edit_miner("10.0.0.8", "A", "10.0.0.8")
            self.assertEqual(result["notice"]["title"], "Config file damaged")

    def test_an_edit_of_an_unknown_address_changes_nothing(self):
        with temp_config([_gamma("10.0.0.8"), _gamma("10.0.0.9", "Beta")]):
            app = TunerDashboard()
            before = config.load_config()
            self.assertTrue(app._apply_miner_edit("10.0.0.7", "X", "10.0.0.7", "t"))
            self.assertEqual(config.load_config(), before)
            with mock.patch("dashboard.modify_config", return_value=False):
                self.assertFalse(
                    app._apply_miner_edit("10.0.0.8", "X", "10.0.0.8", "t")
                )

    def test_a_miner_without_a_name_keeps_its_address_in_the_log(self):
        with temp_config([_gamma(nickname="")]):
            self.assertEqual(TunerDashboard()._miner_names(), {})

    def test_a_placeholder_name_takes_the_hostname(self):
        with temp_config([_gamma(nickname="Miner-10.0.0.8")]):
            app = TunerDashboard()
            row = {"name": "Miner-10.0.0.8"}
            app._maybe_adopt_hostname("10.0.0.8", GAMMA_INFO, row)
            self.assertEqual(row["name"], "goose")
            self.assertEqual(config.get_miner_defaults("10.0.0.8")["nickname"], "goose")


class RestartTests(unittest.TestCase):
    def test_restart_needs_a_selection_and_reports_success(self):
        app = TunerDashboard()
        self.assertEqual(app.restart_miner("")["notice"]["title"], "No Selection")
        with mock.patch(
            "dashboard.restart_bitaxe", return_value="10.0.0.8 -> Restarting"
        ):
            result = app.restart_miner("10.0.0.8")
        self.assertTrue(result["ok"])
        self.assertEqual(result["notice"]["title"], "Restart Triggered")


class SettingsTests(unittest.TestCase):
    def test_global_settings_refuse_odd_input(self):
        with temp_config():
            app = TunerDashboard()
            self.assertFalse(app.save_global_settings(None)["ok"])
            settings = app.get_global_settings()["settings"]
            del settings["voltage_step"]
            self.assertFalse(app.save_global_settings(settings)["ok"])

    def test_a_page_without_mode_or_switches_keeps_them(self):
        with temp_config():
            app = TunerDashboard()
            settings = app.get_global_settings()["settings"]
            settings["default_mode"] = "unknown"
            for key in ("weather_enabled", *config.INTERNET_SWITCHES):
                settings.pop(key)
            config.modify_config(lambda saved: saved.update(pool_check_enabled=True))
            self.assertTrue(app.save_global_settings(settings)["ok"])
            saved = config.load_config()
            self.assertEqual(
                saved["default_mode"], config.get_default_config()["default_mode"]
            )
            self.assertTrue(saved["pool_check_enabled"])

    def test_autotuner_settings_refuse_odd_rows(self):
        app = TunerDashboard()
        with temp_config():
            self.assertEqual(
                app.get_autotuner_settings()["notice"]["title"], "No Miners Found"
            )
        self.assertFalse(app.save_autotuner_settings(None)["ok"])
        self.assertFalse(app.save_autotuner_settings(["row"])["ok"])

    def test_a_row_for_an_unknown_miner_or_the_same_mode_is_skipped(self):
        miner = _gamma()
        fields = {field: str(miner[field]) for field in ALL_AUTOTUNE_FIELDS}
        with temp_config([miner]):
            app = TunerDashboard()
            rows = [
                {
                    "ip": "10.0.0.8",
                    "enabled": True,
                    "fields": fields,
                    "mode": miner["mode"],
                },
                {"ip": "10.0.0.9", "enabled": True, "fields": fields},
            ]
            self.assertTrue(app.save_autotuner_settings(rows)["ok"])
            self.assertEqual(
                [m["ip"] for m in config.load_config()["miners"]], ["10.0.0.8"]
            )
            with mock.patch("dashboard.modify_config", return_value=False):
                result = app.save_autotuner_settings(rows)
            self.assertEqual(result["notice"]["title"], "Config file damaged")


class BackgroundTests(unittest.TestCase):
    def test_a_removed_miner_loses_its_alert(self):
        with temp_config([_gamma()]):
            app = TunerDashboard()
            app._alerts = {"10.0.0.8": "offline", "10.0.0.9": "offline"}
            app.load_rows()
        self.assertNotIn("10.0.0.9", app._alerts)
        self.assertEqual(app._alerts["10.0.0.8"], "offline")

    def test_an_unknown_log_level_is_info(self):
        app = TunerDashboard()
        app.log_message("hello", "loud")
        self.assertEqual(app._log[-1]["level"], "info")

    def test_a_notification_failure_is_quiet(self):
        with mock.patch("dashboard.notify", side_effect=RuntimeError):
            TunerDashboard()._deliver_alerts(["Alpha is offline."])

    def test_a_miner_removed_while_its_flag_is_checked_is_left_alone(self):
        app = TunerDashboard()
        app._rows = [blank_miner_row("Alpha", "10.0.0.8")]
        reply = {"overheat_mode": 1, "temp": 40, "vrTemp": 40, "power": 3}

        def defaults(ip):
            app._rows = []
            return _gamma()

        with (
            mock.patch("dashboard.get_miner_defaults", side_effect=defaults),
            mock.patch("dashboard.overheat_ready_to_clear", return_value=True),
            mock.patch("dashboard.patch_system") as patch,
        ):
            app._clear_cooled_overheat([("10.0.0.8", reply)])
        patch.assert_not_called()

    def test_threads_for_miners_enabled_mid_session(self):
        live = _gamma("10.0.0.7", "Live", enabled=True)
        off = _gamma("10.0.0.8", "Off", enabled=False)
        incomplete = _gamma("10.0.0.9", "Bare", enabled=True, max_freq="")
        with temp_config([live, off, incomplete]):
            app = TunerDashboard()
            app.running = True
            app.threads = [_Thread("10.0.0.7")]
            with mock.patch.object(app, "_miner_thread") as make:
                app._start_miners_if_running(["10.0.0.7", "10.0.0.8", "10.0.0.9"])
            make.assert_not_called()

    def test_a_stop_while_threads_start_is_honoured(self):
        with temp_config([_gamma(enabled=True)]):
            app = TunerDashboard()
            app.running = True
            app.stop_event = threading.Event()
            app.stop_event.set()
            with mock.patch.object(
                app,
                "_miner_thread",
                return_value=(threading.Event(), _Thread("10.0.0.8")),
            ):
                app._start_miners_if_running(["10.0.0.8"])
            self.assertEqual(app.threads, [])

            app.stop_event = threading.Event()
            event = threading.Event()
            thread = _Thread("10.0.0.8")
            thread.start = app.stop_event.set
            with mock.patch.object(app, "_miner_thread", return_value=(event, thread)):
                app._start_miners_if_running(["10.0.0.8"])
            self.assertTrue(event.is_set())

    def test_stop_signals_without_a_shared_event(self):
        app = TunerDashboard()
        event = threading.Event()
        app._miner_stops = {"10.0.0.8": event}
        app._signal_all_stops()
        self.assertTrue(event.is_set())

    def test_a_newer_tuner_keeps_its_status(self):
        app = TunerDashboard()
        app.threads = [_Thread("10.0.0.8")]
        with mock.patch("dashboard._publish_status") as publish:
            app._publish_stopped_phases(
                [_Thread("10.0.0.8", False), _Thread("", False)]
            )
        publish.assert_not_called()


class PageApiTests(unittest.TestCase):
    def test_every_page_call_reaches_the_dashboard(self):
        inner = mock.Mock()
        api = DashboardApi(inner)
        calls = {
            "start_autotuner": (True,),
            "stop_autotuner": (),
            "reset_baseline": (),
            "get_setup_state": (),
            "complete_setup": ({"a": 1},),
            "get_location": (),
            "search_location": ("Moose Jaw",),
            "detect_location": (),
            "start_scan": ("10.0.0.1", "10.0.0.2"),
            "cancel_scan": (),
            "remove_miner_address": ("10.0.0.8",),
            "edit_miner": ("10.0.0.8", "A", "10.0.0.8", "2026-10-01"),
            "restart_miner": ("10.0.0.8",),
            "get_global_settings": (),
            "save_global_settings": ({"x": 1},),
            "get_autotuner_settings": (),
            "save_autotuner_settings": ([],),
            "set_fullscreen": (True,),
        }
        for name, args in calls.items():
            with self.subTest(name):
                getattr(api, name)(*args)
                getattr(inner, name).assert_called_once_with(*args)


if __name__ == "__main__":
    unittest.main()
