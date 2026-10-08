import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import autotune
import boards
import config
import dashboard
from test_autotune import FAST_CONFIG, _start_miner, patched_io
from test_autotune import _info as _session_info
from test_dashboard import temp_config

ULTRA_204 = boards.BOARDS_BY_VERSION["204"]
SUPRA_402 = boards.BOARDS_BY_VERSION["402"]
HEX_302 = boards.BOARDS_BY_VERSION["302"]
GAMMA_HEX = boards.BOARDS_BY_VERSION["1300"]


def _info(version, asic):
    return {"boardVersion": version, "ASICModel": asic}


class BoardTableTests(unittest.TestCase):
    def test_table_holds_every_board_in_axeos_v2_15_3(self):
        self.assertEqual(
            [board.version for board in boards.BOARDS],
            [
                "2.2",
                "102",
                "0.11",
                "201",
                "202",
                "203",
                "204",
                "205",
                "207",
                "302",
                "303",
                "400",
                "401",
                "402",
                "403",
                "600",
                "601",
                "602",
                "603",
                "650",
                "701",
                "702",
                "801",
                "1201",
                "1300",
            ],
        )
        self.assertEqual(len(boards.BOARDS_BY_VERSION), len(boards.BOARDS))

    def test_the_601_keeps_the_fleet_limits(self):
        board = boards.GAMMA_601
        self.assertEqual(board.status, boards.VERIFIED)
        self.assertEqual((config.HARD_MIN_FREQ, config.HARD_MAX_FREQ), (350, 1100))
        self.assertEqual((config.HARD_MIN_VOLT, config.HARD_MAX_VOLT), (1000, 1500))
        self.assertEqual(config.HARD_MAX_CORE_AMPS, 29.0)
        self.assertEqual((config.STOCK_FREQ, config.STOCK_VOLT), (525, 1150))
        self.assertEqual(
            (config.TPS546_IOUT_WARN_A, config.TPS546_IOUT_FAULT_A), (25.0, 30.0)
        )
        self.assertEqual(autotune.ASIC_OFF_POWER_WATTS, 5.5)
        self.assertEqual(autotune.BOARD_POWER_W, 5.0)
        self.assertEqual(autotune.BM1370_HASHRATE_PER_MHZ, 2.04)
        self.assertEqual(autotune.RAMP_MV_PER_MHZ, 0.3)

    def test_only_the_601_is_verified(self):
        self.assertEqual(
            [board.version for board in boards.BOARDS if board.verified], ["601"]
        )

    def test_reply_resolves_to_its_board(self):
        self.assertIs(boards.board_for_info(_info("601", "BM1370")), boards.GAMMA_601)
        self.assertIs(
            boards.board_for_info(_info(" 601 ", " bm1370 ")), boards.GAMMA_601
        )
        naja = boards.board_for_info(_info("1201", "BM1372/BM1373"))
        self.assertEqual(naja.name, "NajaDuo 1201")
        self.assertIs(boards.board_for_info(_info("402", "BM1368")), SUPRA_402)

    def test_unknown_board_or_wrong_asic_is_refused(self):
        self.assertIsNone(boards.board_for_info(_info("999", "BM1370")))
        self.assertIsNone(boards.board_for_info(_info("601", "BM1366")))
        self.assertIsNone(boards.board_for_info(_info("", "")))
        self.assertIsNone(boards.board_for_info("timed out"))
        self.assertIn(
            "not in this tuner's board list",
            boards.board_list_note(_info("999", "BM1370")),
        )
        self.assertIn(
            "puts a BM1370 on it", boards.board_list_note(_info("601", "BM1366"))
        )
        self.assertFalse(config.is_gamma_601(_info("602", "BM1370")))
        self.assertTrue(config.is_gamma_601(_info("601", "BM1370")))

    def test_ds4432u_boards_have_no_core_current_or_regulator_sensor(self):
        ultra = boards.BOARDS_BY_VERSION["204"]
        self.assertFalse(ultra.has_vr_temp)
        self.assertFalse(ultra.has_core_current)
        self.assertEqual(ultra.board_power_w, 0.0)
        self.assertIsNone(ultra.limits.max_core_amps)
        self.assertIsNone(ultra.limits.asic_off_watts)

    def test_experimental_limits_stay_inside_the_asic_presets(self):
        supra = SUPRA_402.limits
        self.assertEqual((supra.min_freq, supra.max_freq), (400, 860))
        self.assertEqual((supra.min_volt, supra.max_volt), (1100, 1300))
        self.assertEqual(supra.max_core_amps, 29.0)
        self.assertEqual(supra.asic_off_watts, 5.5)
        self.assertEqual(GAMMA_HEX.limits.asic_off_watts, 25.5)
        self.assertEqual(GAMMA_HEX.stock_clocks, (690, 1200))

    def test_current_cap_keeps_the_same_share_of_every_regulator(self):
        # 29 of 30 A on the Gamma regulator; bigger regulators keep the share.
        self.assertEqual(boards.GAMMA_601.limits.max_core_amps, 29.0)
        self.assertEqual(boards.BOARDS_BY_VERSION["600"].limits.max_core_amps, 29.0)
        self.assertEqual(boards.BOARDS_BY_VERSION["801"].limits.max_core_amps, 53.0)
        self.assertEqual(GAMMA_HEX.limits.max_core_amps, 154.5)

    def test_input_floor_follows_the_nominal_input(self):
        self.assertEqual(boards.GAMMA_601.default_min_input_voltage, 4.9)
        self.assertEqual(HEX_302.default_min_input_voltage, 11.76)

    def test_unused_fields_name_the_missing_sensors(self):
        self.assertEqual(boards.GAMMA_601.unused_limit_fields, ())
        self.assertEqual(
            ULTRA_204.unused_limit_fields, ("max_vr_temp", "max_core_amps")
        )

    def test_asic_temp_reads_the_hotter_chip(self):
        self.assertEqual(boards.asic_temp({"temp": 61.5, "temp2": 64.0}), 64.0)
        self.assertEqual(boards.asic_temp({"temp": 61.5, "temp2": -1}), 61.5)
        self.assertEqual(boards.asic_temp({"temp": "61.5"}), "61.5")
        self.assertEqual(boards.asic_temp({"temp2": 58}), 58.0)
        self.assertIsNone(boards.asic_temp({}))

    def test_hashrate_per_mhz_counts_every_asic(self):
        self.assertEqual(boards.BOARDS_BY_VERSION["801"].hashrate_per_mhz, 4.08)
        self.assertAlmostEqual(boards.BOARDS_BY_VERSION["1300"].hashrate_per_mhz, 12.24)

    def test_saved_record_names_its_board(self):
        self.assertEqual(boards.board_for_record({"board": "402"}).version, "402")
        self.assertIs(boards.board_for_record({}), boards.GAMMA_601)
        self.assertIs(boards.board_for_record({"board": "junk"}), boards.GAMMA_601)
        self.assertIs(boards.board_for_record(None), boards.GAMMA_601)


class FirmwareTests(unittest.TestCase):
    def test_version_parsing(self):
        self.assertEqual(boards.firmware_version({"version": "v2.15.3"}), (2, 15, 3))
        self.assertEqual(
            boards.firmware_version({"version": "v2.16.0-beta1"}), (2, 16, 0)
        )
        self.assertIsNone(boards.firmware_version({"version": "Unknown"}))
        self.assertIsNone(boards.firmware_version({}))

    def test_policy(self):
        old = boards.firmware_check({"version": "v2.10.2"})
        self.assertIn("older than v2.11.0", old[0])
        self.assertEqual(boards.firmware_check({"version": "v2.15.3"}), ("", ""))
        self.assertEqual(boards.firmware_check({"version": "v2.15.9"}), ("", ""))
        newer = boards.firmware_check({"version": "v2.16.0"})
        self.assertEqual(newer[0], "")
        self.assertIn("newer than v2.15.3", newer[1])
        self.assertIn("Could not read", boards.firmware_check({"version": "x"})[1])
        self.assertEqual(boards.firmware_check({}), ("", ""))

    def test_old_firmware_is_not_tuned(self):
        ip = "10.0.0.7"
        info = {
            "boardVersion": "601",
            "ASICModel": "BM1370",
            "errorPercentage": 0,
            "version": "v2.9.0",
        }
        logs = []
        writes = mock.Mock(return_value="applied")
        self.addCleanup(autotune._clear_miner_status, ip)
        with (
            mock.patch.object(autotune, "load_config", lambda: {"miners": []}),
            mock.patch.object(autotune, "get_system_info", lambda _ip: dict(info)),
            mock.patch.object(autotune, "set_system_settings", writes),
            mock.patch.object(autotune, "patch_system", writes),
        ):
            autotune.monitor_and_adjust(
                ip,
                "BM1370 601",
                0.01,
                lambda message, _level: logs.append(message),
                400,
                800,
                1100,
                1300,
                65,
                30,
                525,
                1150,
                85,
                stop_event=threading.Event(),
            )
        writes.assert_not_called()
        self.assertTrue(any("older than v2.11.0" in line for line in logs), logs)
        self.assertEqual(autotune.get_miner_status(ip)["reason"], "firmware too old")


class BoardLimitTests(unittest.TestCase):
    def test_clamp_uses_the_board_range(self):
        supra = boards.BOARDS_BY_VERSION["402"]
        clamped = autotune.clamp_limits(
            {"min_freq": 300, "max_freq": 1100, "min_volt": 900, "max_volt": 1500},
            supra,
        )
        self.assertEqual((clamped["min_freq"], clamped["max_freq"]), (400, 860))
        self.assertEqual((clamped["min_volt"], clamped["max_volt"]), (1100, 1300))

    def test_core_amps_cap_follows_the_board(self):
        self.assertIsNone(autotune.core_amps_cap({"max_core_amps": 20}, ULTRA_204))
        self.assertEqual(autotune.core_amps_cap({}, GAMMA_HEX), 154.5)
        self.assertEqual(
            autotune.core_amps_cap({"max_core_amps": 200}, GAMMA_HEX), 154.5
        )
        self.assertEqual(autotune.core_amps_cap({"max_core_amps": 35}), 29.0)

    def test_input_current_is_not_core_current(self):
        info = {"current": 3500}
        self.assertEqual(autotune.core_current_amps(info), 3.5)
        self.assertIsNone(autotune.core_current_amps(info, ULTRA_204))

    def test_asic_off_power_needs_a_threshold(self):
        reason = autotune.decide_adjustment(
            **{
                **_decision(power=5.2),
                "asic_off_watts": None,
            }
        )[2]
        self.assertNotIn("offline", reason)
        reason = autotune.decide_adjustment(**_decision(power=5.2))[2]
        self.assertIn("offline", reason)

    def test_settings_cells_clamp_to_the_board(self):
        supra = boards.BOARDS_BY_VERSION["402"]
        ultra = boards.BOARDS_BY_VERSION["204"]
        parse = dashboard.parse_autotuner_value
        self.assertEqual(parse("max_freq", "2000", supra), 860)
        self.assertEqual(parse("max_volt", "1500", supra), 1300)
        self.assertEqual(parse("max_core_amps", "40", supra), 29.0)
        self.assertEqual(parse("max_core_amps", "40", ultra), 40.0)
        self.assertEqual(parse("max_freq", "2000"), 1100)

    def test_limits_screen_fits_the_board(self):
        titles = [section["title"] for section in dashboard.hardware_limits()]
        self.assertIn("Voltage regulator (TPS546)", titles)
        self.assertIn("BM1370 on the Gamma 601", titles)
        self.assertIn("Defaults for a new miner", titles)
        self.assertIn("5 V supply and wiring", titles)
        ultra = dashboard.hardware_limits(ULTRA_204)
        titles = [section["title"] for section in ultra]
        self.assertNotIn("Voltage regulator (TPS546)", titles)
        self.assertNotIn("5 V supply and wiring", titles)
        self.assertIn("BM1366 on the Ultra 204", titles)
        hard = next(s for s in ultra if s["title"] == "Tuner hard limits")
        self.assertNotIn("Core current cap", [row[0] for row in hard["rows"]])
        defaults = next(s for s in ultra if s["title"] == "Defaults for a new miner")
        names = [row[0] for row in defaults["rows"]]
        self.assertNotIn("Regulator temperature", names)
        self.assertNotIn("Core current", names)
        self.assertIn("Nobody has verified", defaults["intro"])
        hex_rows = dashboard.hardware_limits(HEX_302)
        defaults = next(s for s in hex_rows if s["title"] == "Defaults for a new miner")
        floor = next(row for row in defaults["rows"] if row[0] == "Input voltage floor")
        self.assertEqual(floor[1], "11.76 V")
        self.assertIn("12 V input", floor[2])


def _decision(**overrides):
    values = {
        "current_frequency": 500,
        "current_voltage": 1150,
        "min_freq": 400,
        "max_freq": 800,
        "min_volt": 1000,
        "max_volt": 1300,
        "max_temp": 65,
        "max_watts": 30,
        "max_vr_temp": 85,
        "temp": 50,
        "vr_temp": 60,
        "power": 15,
        "hash_rate": 1000,
        "expected_hashrate": 1020,
        "shares_rejected_delta": 0,
        "overheat_mode": 0,
        "frequency_step": 5,
        "voltage_step": 10,
        "temp_tolerance": 3,
        "error_percentage": 0.5,
    }
    values.update(overrides)
    return values


class CapabilityTests(unittest.TestCase):
    def test_missing_regulator_reading_holds_only_where_the_board_has_one(self):
        values = _decision(vr_temp=None)
        self.assertEqual(
            autotune.decide_adjustment(**values)[2], "holding for telemetry"
        )
        frequency, _voltage, reason = autotune.decide_adjustment(
            **values, vr_temp_required=False
        )
        self.assertEqual(reason, "increase frequency")
        self.assertGreater(frequency, 500)

    def test_errors_raise_voltage_without_a_regulator_sensor(self):
        values = _decision(vr_temp=None, error_percentage=5)
        self.assertEqual(
            autotune.decide_adjustment(**values, vr_temp_required=False)[2],
            "increase voltage",
        )

    def test_climb_steps_on_the_asic_alone_without_a_regulator_sensor(self):
        args = (500, 40, None, 15, 65, 85, 30, 3, 3, 5, 4)
        self.assertEqual(autotune.climb_frequency_steps(*args), 1)
        self.assertGreater(
            autotune.climb_frequency_steps(*args, vr_temp_required=False), 1
        )

    def test_fast_start_projects_without_a_regulator_sensor(self):
        sample = {
            "frequency": 485,
            "voltage": 1200,
            "power": 12.0,
            "temp": 45.0,
            "vr_temp": None,
            "input_voltage": 5.1,
        }
        limits = config.board_limits(ULTRA_204)
        limits["max_vr_temp"] = 101
        start = ULTRA_204.stock_clocks
        self.assertIsNone(autotune.ramp_target(sample, [], limits, start, 5, 10))
        target = autotune.ramp_target(sample, [], limits, start, 5, 10, board=ULTRA_204)
        self.assertIsNotNone(target)
        self.assertGreater(target[0], 485)

    def test_fast_start_current_counts_every_voltage_domain(self):
        sample = {
            "frequency": 485,
            "voltage": 1200,
            "power": 50.0,
            "temp": 40.0,
            "vr_temp": 50.0,
            "input_voltage": 12.1,
        }
        limits = config.board_limits(HEX_302)
        start = HEX_302.stock_clocks
        # 38 W over 1.2 V would be 32 A on one domain; across three it is 10.6 A.
        one = autotune.ramp_target(
            sample, [], limits, start, 5, 10, 11.76, 29.0, boards.GAMMA_601
        )
        three = autotune.ramp_target(
            sample, [], limits, start, 5, 10, 11.76, 29.0, HEX_302
        )
        self.assertIsNone(one)
        self.assertIsNotNone(three)

    def test_fast_start_headroom_scales_with_the_board(self):
        self.assertEqual(autotune.ramp_headroom(), (2.0, 1.5, 0.05))
        watts, amps, volts = autotune.ramp_headroom(GAMMA_HEX)
        self.assertEqual(watts, 12.0)
        self.assertEqual(amps, 8.0)
        self.assertAlmostEqual(volts, 0.12)

    def test_overheat_clears_on_the_asic_alone_without_a_regulator_sensor(self):
        info = {"overheat_mode": 1, "temp": 50, "vrTemp": 0, "power": 10}
        self.assertFalse(autotune.overheat_ready_to_clear(info, 65, 85))
        self.assertTrue(
            autotune.overheat_ready_to_clear(info, 65, 85, None, vr_temp_required=False)
        )

    def test_overheat_latch_waits_for_a_cool_asic_without_a_regulator_sensor(self):
        hot = {"overheat_mode": 1, "temp": 50, "vrTemp": 0}
        cool = {"overheat_mode": 1, "temp": 40, "vrTemp": 0}
        dark = {"overheat_mode": 1, "vrTemp": 0}
        late = autotune.OVERHEAT_LATCH_SECONDS + 1
        self.assertFalse(autotune.overheat_latched(cool, 0, late, 85))
        self.assertFalse(autotune.overheat_latched(hot, 0, late, 85, False))
        self.assertTrue(autotune.overheat_latched(cool, 0, late, 85, False))
        self.assertTrue(autotune.overheat_latched(dark, 0, late, 85, False))
        self.assertFalse(autotune.overheat_latched(cool, 0, 10, 85, False))

    def test_board_limits_for_an_unverified_board(self):
        supra = config.board_limits(SUPRA_402)
        self.assertEqual(
            (supra["min_freq"], supra["max_freq"], supra["start_freq"]), (400, 860, 490)
        )
        self.assertEqual(
            (supra["min_volt"], supra["max_volt"], supra["start_volt"]),
            (1100, 1300, 1166),
        )
        self.assertEqual((supra["max_temp"], supra["max_vr_temp"]), (65, 85))
        self.assertEqual(
            (supra["max_watts"], supra["min_input_voltage"], supra["max_core_amps"]),
            (40, 4.9, 25.0),
        )
        gamma_hex = config.board_limits(GAMMA_HEX)
        self.assertEqual(
            (gamma_hex["max_watts"], gamma_hex["min_input_voltage"]), (180, 11.76)
        )
        self.assertEqual(gamma_hex["max_core_amps"], 150.0)
        ultra = config.board_limits(ULTRA_204)
        self.assertEqual((ultra["max_vr_temp"], ultra["max_core_amps"]), ("", ""))
        # A lower Default Max Temp wins; a higher one does not.
        self.assertEqual(
            config.board_limits(SUPRA_402, {"default_target_temp": 60})["max_temp"], 60
        )
        self.assertEqual(
            config.board_limits(SUPRA_402, {"default_target_temp": 70})["max_temp"], 65
        )
        self.assertEqual(
            config.board_limits(
                boards.GAMMA_601, config.get_default_config(), "max_hashrate"
            ),
            config.gamma_601_limits(config.get_default_config()),
        )

    def test_ultra_without_regulator_sensor_climbs_in_a_session(self):
        state = {"frequency": 400, "voltage": 1100, "calls": []}
        stop_event = threading.Event()
        runtime = dict(
            FAST_CONFIG, max_climb_steps=4, miners=[{"ip": "miner", "board": "204"}]
        )
        logs = []

        def set_settings(ip, volt, freq):
            state["calls"].append((int(freq), int(volt)))
            state["frequency"] = int(freq)
            state["voltage"] = int(volt)
            if int(freq) > 400:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            return _session_info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=360,
                hashRate_1m=360,
                expectedHashrate=357,
                errorPercentage=0.2,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=0,
                # A Balanced miner runs AxeOS auto fan, 5°C under its 60°C cap.
                autofanspeed=1,
                temptarget=55,
                # INA260 input current, not core current.
                current=3500,
                ASICModel="BM1366",
                boardVersion="204",
            )

        with patched_io(get_info, set_settings, runtime_config=runtime):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda message, *args: logs.append(message),
                start_freq=400,
                max_vr_temp="",
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(state["calls"][-1], (420, 1100), logs)


def _record(ip, name, board):
    return config.new_miner_record(
        f"{board.asic.model} {board.version}",
        ip,
        name,
        config.get_default_config(),
        board=board,
    )


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class DashboardBoardTests(unittest.TestCase):
    def _stop(self, app):
        app._signal_all_stops()
        for thread in app.threads:
            thread.join(timeout=2)

    def test_start_asks_once_for_an_unverified_board(self):
        miners = [
            _record("10.0.0.1", "gamma", boards.GAMMA_601),
            _record("10.0.0.2", "supra", SUPRA_402),
        ]
        started = []

        def fake_monitor(*args, **kwargs):
            started.append(args[0])
            kwargs["stop_event"].wait(2)

        with temp_config(miners):
            app = dashboard.TunerDashboard()
            with mock.patch("dashboard.monitor_and_adjust", fake_monitor):
                asked = app.start_autotuner()
                self.assertFalse(asked["ok"])
                self.assertEqual(asked["confirm"]["title"], "Unverified Boards")
                self.assertIn("Supra 402", asked["confirm"]["message"])
                self.assertIn("supra", asked["confirm"]["message"])
                self.assertEqual(started, [])
                self.assertFalse(app.running)

                result = app.start_autotuner(True)
                self.assertTrue(result["ok"])
                saved = {m["ip"]: m for m in config.get_miners()}
                self.assertTrue(saved["10.0.0.2"]["experimental_ok"])
                self.assertNotIn("experimental_ok", saved["10.0.0.1"])
                self._stop(app)
                app.stop_autotuner()
                for thread in app.threads:
                    thread.join(timeout=2)
                app._finish_stop()

                again = app.start_autotuner()
                self.assertTrue(again["ok"])
                self._stop(app)

    def test_miner_enabled_while_running_waits_for_the_confirmation(self):
        miner = _record("10.0.0.2", "supra", SUPRA_402)
        with temp_config([miner]):
            app = dashboard.TunerDashboard()
            app.running = True
            with mock.patch("dashboard.monitor_and_adjust") as monitor:
                app._start_miners_if_running(["10.0.0.2"])
            monitor.assert_not_called()
            self.assertEqual(app.threads, [])
            self.assertTrue(
                any("not verified yet" in line["text"] for line in app._log)
            )

    def test_settings_leave_a_missing_sensor_blank(self):
        miner = _record("10.0.0.3", "ultra", ULTRA_204)
        with temp_config([miner]):
            app = dashboard.TunerDashboard()
            row = app.get_autotuner_settings()["miners"][0]
            self.assertEqual(row["unused"], ["max_vr_temp", "max_core_amps"])
            self.assertTrue(row["experimental"])
            self.assertEqual(row["amps_note"], "")
            self.assertIn("Ultra 204", row["label"])
            fields = dict(row["fields"], max_vr_temp="90", max_core_amps="20")
            saved = app.save_autotuner_settings(
                [{"ip": "10.0.0.3", "enabled": True, "fields": fields}]
            )
            self.assertTrue(saved["ok"])
            stored = config.get_miners()[0]
            self.assertTrue(stored["enabled"])
            self.assertEqual((stored["max_vr_temp"], stored["max_core_amps"]), ("", ""))
            self.assertEqual(dashboard.missing_start_fields(stored), [])

    def test_scan_saves_any_board_on_the_list_with_its_defaults(self):
        replies = {
            "192.168.0.2": {"ASICModel": "BM1368", "boardVersion": "402"},
            "192.168.0.3": {"ASICModel": "BM1370", "boardVersion": "999"},
        }

        def fake_get(url, timeout=1):
            for ip, payload in replies.items():
                if f"//{ip}/" in url:
                    return _Response(payload)
            raise config.requests.exceptions.RequestException("no miner")

        with temp_config([]):
            with mock.patch("config.requests.get", side_effect=fake_get):
                found = config.detect_miners("192.168.0.2", "192.168.0.3")
            self.assertEqual([miner["ip"] for miner in found], ["192.168.0.2"])
            supra = config.get_miners()[0]
            self.assertEqual((supra["board"], supra["type"]), ("402", "BM1368 402"))
            self.assertEqual((supra["max_volt"], supra["max_temp"]), (1300, 65))

    def test_edit_refuses_an_address_off_the_board_list(self):
        miner = _record("10.0.0.1", "gamma", boards.GAMMA_601)
        with temp_config([miner]):
            app = dashboard.TunerDashboard()
            with mock.patch(
                "dashboard.get_system_info",
                lambda ip: {"ASICModel": "BM1370", "boardVersion": "999"},
            ):
                result = app.edit_miner("10.0.0.1", "gamma", "10.0.0.9")
            self.assertFalse(result["ok"])
            self.assertIn("not in this tuner's board list", result["message"])
            with mock.patch(
                "dashboard.get_system_info",
                lambda ip: {"ASICModel": "BM1368", "boardVersion": "402"},
            ):
                moved = app.edit_miner("10.0.0.1", "gamma", "10.0.0.9")
            self.assertTrue(moved["ok"])
            self.assertEqual(config.get_miners()[0]["board"], "402")

    def test_prompts_and_limits_follow_the_fleet(self):
        gamma = _record("10.0.0.1", "gamma", boards.GAMMA_601)
        with temp_config([gamma]):
            app = dashboard.TunerDashboard()
            app.load_rows()
            snapshot = app.get_snapshot(0)
            self.assertIn(
                "the Gamma 601 stock clocks (525 MHz / 1150 mV)",
                snapshot["prompts"]["baseline"],
            )
            self.assertEqual(snapshot["miners"][0]["stock"], "525 MHz / 1150 mV")
            titles = [s["title"] for s in app.get_hardware_limits()["sections"]]
            self.assertIn("BM1370 on the Gamma 601", titles)
        supra = _record("10.0.0.2", "supra", SUPRA_402)
        with temp_config([gamma, supra]):
            app = dashboard.TunerDashboard()
            app.load_rows()
            snapshot = app.get_snapshot(0)
            self.assertIn("its board's stock clocks", snapshot["prompts"]["baseline"])
            rows = {row["ip"]: row for row in snapshot["miners"]}
            self.assertEqual(rows["10.0.0.2"]["board"], "Supra 402")
            self.assertEqual(rows["10.0.0.2"]["stock"], "490 MHz / 1166 mV")
            titles = [s["title"] for s in app.get_hardware_limits()["sections"]]
            self.assertIn("Gamma 601: BM1370 on the Gamma 601", titles)
            self.assertIn("Supra 402: BM1368 on the Supra 402", titles)

    def test_row_hides_readings_a_board_does_not_have(self):
        miner = _record("10.0.0.3", "ultra", ULTRA_204)
        with temp_config([miner]):
            app = dashboard.TunerDashboard()
            app.load_rows()
            info = {
                "ASICModel": "BM1366",
                "boardVersion": "204",
                "temp": 48.2,
                "vrTemp": 0,
                "current": 3500,
                "power": 11.0,
                "frequency": 485,
                "coreVoltage": 1200,
            }
            app._apply_results([("10.0.0.3", info)])
            row = app.get_snapshot(0)["miners"][0]
            self.assertEqual(row["asic"], "48.2")
            self.assertEqual(row["vr"], "-")
            self.assertEqual(row["amps"], "-")
            self.assertEqual(row["vr_level"], "")


class SavedBoardTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "config.json")
        self._old_path = config.CONFIG_FILE
        self._old_last = config._last_good_config
        config.CONFIG_FILE = self.path
        config._last_good_config = None

    def tearDown(self):
        config.CONFIG_FILE = self._old_path
        config._last_good_config = self._old_last
        self._dir.cleanup()

    def test_miners_saved_before_boards_become_601s_once(self):
        old = {
            "limits_version": config.LIMITS_VERSION,
            "miners": [
                {"ip": "a", "type": "BM1370 601"},
                {"ip": "b", "type": "BM1370 601"},
            ],
        }
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(old, handle)
        miners = config.load_config()["miners"]
        self.assertEqual([miner["board"] for miner in miners], ["601", "601"])
        with open(self.path, encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk["config_version"], config.CONFIG_VERSION)
        self.assertEqual(on_disk["miners"][0]["board"], "601")
        # Later loads leave a recorded board alone.
        config.update_miner("b", {"board": "402"})
        config._last_good_config = None
        self.assertEqual(config.load_config()["miners"][1]["board"], "402")

    def test_new_record_saves_its_board(self):
        supra = boards.BOARDS_BY_VERSION["402"]
        self.assertEqual(config.new_miner_record("x", "1.2.3.4", "n")["board"], "601")
        self.assertEqual(
            config.new_miner_record("x", "1.2.3.4", "n", board=supra)["board"], "402"
        )
        self.assertEqual(
            config.get_default_config()["config_version"], config.CONFIG_VERSION
        )


class SessionBoardTests(unittest.TestCase):
    def test_session_skips_a_miner_whose_board_changed(self):
        ip = "10.0.0.9"
        runtime = {"miners": [{"ip": ip, "board": "402"}], "fast_start": False}
        info = {
            "boardVersion": "601",
            "ASICModel": "BM1370",
            "errorPercentage": 0,
            "frequency": 525,
            "coreVoltage": 1150,
        }
        logs = []
        writes = mock.Mock(return_value="applied")
        self.addCleanup(autotune._clear_miner_status, ip)
        with (
            mock.patch.object(autotune, "load_config", lambda: runtime),
            mock.patch.object(autotune, "get_system_info", lambda _ip: dict(info)),
            mock.patch.object(autotune, "set_system_settings", writes),
            mock.patch.object(autotune, "patch_system", writes),
        ):
            autotune.monitor_and_adjust(
                ip,
                "BM1370 601",
                0.01,
                lambda message, _level: logs.append(message),
                400,
                800,
                1100,
                1300,
                65,
                30,
                525,
                1150,
                85,
                stop_event=threading.Event(),
            )
        self.assertTrue(any("saved as a Supra 402" in line for line in logs), logs)
        writes.assert_not_called()
        self.assertEqual(autotune.get_miner_status(ip)["reason"], "board changed")


if __name__ == "__main__":
    unittest.main()
