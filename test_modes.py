import json
import os
import random
import tempfile
import threading
import unittest
from unittest import mock

import autotune
import boards
import config
import dashboard
import modes
from test_autotune import FAST_CONFIG, _info, _start_miner, patched_io
from test_dashboard import temp_config

SUPRA_402 = boards.BOARDS_BY_VERSION["402"]
ULTRA_204 = boards.BOARDS_BY_VERSION["204"]


def _decision(**overrides):
    values = {
        "current_frequency": 600,
        "current_voltage": 1100,
        "min_freq": 400,
        "max_freq": 800,
        "min_volt": 1000,
        "max_volt": 1150,
        "max_temp": 65,
        "max_watts": 30,
        "max_vr_temp": 85,
        "temp": 50,
        "vr_temp": 60,
        "power": 15,
        "hash_rate": 1200,
        "expected_hashrate": 1224,
        "shares_rejected_delta": 0,
        "overheat_mode": 0,
        "frequency_step": 5,
        "voltage_step": 10,
        "temp_tolerance": 3,
        "error_percentage": 5,
    }
    values.update(overrides)
    return values


class PresetTests(unittest.TestCase):
    def test_max_on_the_601_is_the_fleet_preset(self):
        fleet = config.gamma_601_limits(config.get_default_config())
        self.assertEqual(
            modes.preset_limits(modes.MAX_HASHRATE, boards.GAMMA_601, fleet), fleet
        )

    def test_balanced_and_efficiency_on_a_gamma(self):
        balanced = modes.preset_limits(modes.BALANCED, boards.GAMMA_601)
        self.assertEqual(
            (balanced["max_volt"], balanced["max_temp"], balanced["max_vr_temp"]),
            (1250, 65, 85),
        )
        self.assertEqual((balanced["max_core_amps"], balanced["max_watts"]), (25.0, 40))
        efficiency = modes.preset_limits(modes.EFFICIENCY_MODE, boards.GAMMA_601)
        self.assertEqual(efficiency["max_volt"], 1150)
        self.assertEqual(
            (efficiency["start_freq"], efficiency["start_volt"]), (525, 1150)
        )

    def test_max_on_an_unverified_board_stays_inside_its_presets(self):
        supra = modes.preset_limits(modes.MAX_HASHRATE, SUPRA_402)
        self.assertEqual((supra["max_freq"], supra["max_volt"]), (860, 1300))
        self.assertEqual((supra["max_temp"], supra["max_vr_temp"]), (70, 95))
        self.assertEqual(supra["max_core_amps"], 29.0)

    def test_a_board_without_sensors_gets_blank_caps_in_every_mode(self):
        for key in modes.MODE_ORDER:
            ultra = modes.preset_limits(key, ULTRA_204)
            self.assertEqual((ultra["max_vr_temp"], ultra["max_core_amps"]), ("", ""))

    def test_lower_default_max_temp_wins(self):
        supra = modes.preset_limits(modes.BALANCED, SUPRA_402, default_target_temp=60)
        self.assertEqual(supra["max_temp"], 60)

    def test_saved_mode_and_default(self):
        self.assertEqual(
            modes.mode_for_record({"mode": "efficiency"}).key, modes.EFFICIENCY_MODE
        )
        self.assertEqual(modes.mode_for_record({}).key, modes.MAX_HASHRATE)
        self.assertEqual(modes.mode_for_record({}, SUPRA_402).key, modes.BALANCED)
        self.assertEqual(
            modes.mode_for_record({"mode": "turbo"}, SUPRA_402).key, modes.BALANCED
        )


def _usable(value):
    return None if value in ("", None) else float(value)


class EveryBoardTests(unittest.TestCase):
    """Every board and mode starts from limits that are safe for that board."""

    def test_presets_sit_inside_the_board_and_firmware_limits(self):
        for board in boards.BOARDS:
            hard = board.limits
            nominal = board.family.nominal_voltage
            for key in modes.MODE_ORDER:
                with self.subTest(board=board.name, mode=key):
                    preset = config.board_limits(board, mode=key)
                    self.assertLessEqual(hard.min_freq, preset["min_freq"])
                    self.assertLessEqual(preset["min_freq"], preset["start_freq"])
                    self.assertLessEqual(preset["start_freq"], preset["max_freq"])
                    self.assertLessEqual(preset["max_freq"], hard.max_freq)
                    self.assertLessEqual(hard.min_volt, preset["min_volt"])
                    self.assertLessEqual(preset["min_volt"], preset["start_volt"])
                    self.assertLessEqual(preset["start_volt"], preset["max_volt"])
                    self.assertLessEqual(preset["max_volt"], hard.max_volt)
                    self.assertLessEqual(
                        preset["max_temp"], autotune.TRIP_SAFE_MAX_TEMP
                    )
                    vr_cap = _usable(preset["max_vr_temp"])
                    if board.has_vr_temp:
                        self.assertLessEqual(vr_cap, autotune.TRIP_SAFE_MAX_VR_TEMP)
                    else:
                        self.assertIsNone(vr_cap)
                    amps = _usable(preset["max_core_amps"])
                    if board.has_core_current:
                        self.assertLess(amps, board.family.regulator.iout_fault)
                    else:
                        self.assertIsNone(amps)
                    self.assertGreater(preset["max_watts"], 0)
                    # Between AxeOS's low-input warning and the nominal input.
                    self.assertGreater(preset["min_input_voltage"], 0.949 * nominal)
                    self.assertLess(preset["min_input_voltage"], nominal)
                    if not board.verified:
                        self.assertLessEqual(
                            preset["max_volt"], max(board.asic.voltage_presets)
                        )
                    if key != modes.MAX_HASHRATE:
                        self.assertLessEqual(preset["max_temp"], 65)

    def test_decisions_stay_inside_the_limits_on_every_board(self):
        rng = random.Random(2026)
        for board in boards.BOARDS:
            for key in modes.MODE_ORDER:
                mode = modes.MODES[key]
                preset = config.board_limits(board, mode=key)
                limits = autotune.clamp_limits(
                    {
                        name: _usable(preset[name])
                        if _usable(preset[name]) is not None
                        else autotune.TRIP_SAFE_MAX_VR_TEMP
                        for name in (
                            "min_freq",
                            "max_freq",
                            "min_volt",
                            "max_volt",
                            "max_temp",
                            "max_watts",
                            "max_vr_temp",
                        )
                    },
                    board,
                )
                for _ in range(200):
                    frequency = rng.randrange(
                        limits["min_freq"], limits["max_freq"] + 1, 5
                    )
                    voltage = rng.randrange(
                        limits["min_volt"], limits["max_volt"] + 1, 10
                    )
                    temp = round(rng.uniform(30, 80), 1)
                    vr_temp = round(rng.uniform(40, 110), 1) if board.has_vr_temp else 0
                    power = round(rng.uniform(4, limits["max_watts"] * 1.3), 2)
                    new_frequency, new_voltage, reason = autotune.decide_adjustment(
                        current_frequency=frequency,
                        current_voltage=voltage,
                        min_freq=limits["min_freq"],
                        max_freq=limits["max_freq"],
                        min_volt=limits["min_volt"],
                        max_volt=limits["max_volt"],
                        max_temp=limits["max_temp"],
                        max_watts=limits["max_watts"],
                        max_vr_temp=limits["max_vr_temp"],
                        temp=temp,
                        vr_temp=vr_temp,
                        power=power,
                        hash_rate=rng.uniform(100, 3000),
                        expected_hashrate=None,
                        shares_rejected_delta=0,
                        overheat_mode=0,
                        frequency_step=5,
                        voltage_step=10,
                        temp_tolerance=3,
                        error_percentage=round(rng.uniform(0, 5), 2),
                        input_voltage=board.family.nominal_voltage * 1.01,
                        min_input_voltage=board.default_min_input_voltage,
                        phase=rng.choice(["climb", "trim", "hold"]),
                        max_core_amps=board.limits.max_core_amps,
                        asic_off_watts=board.limits.asic_off_watts,
                        vr_temp_required=board.has_vr_temp,
                        objective=mode.objective,
                    )
                    case = (board.name, key, frequency, voltage, temp, power, reason)
                    self.assertTrue(
                        limits["min_freq"] <= new_frequency <= limits["max_freq"], case
                    )
                    self.assertTrue(
                        limits["min_volt"] <= new_voltage <= limits["max_volt"], case
                    )
                    hot = temp > limits["max_temp"] or power > limits["max_watts"]
                    if board.has_vr_temp:
                        hot = hot or vr_temp > limits["max_vr_temp"]
                    if hot:
                        self.assertLessEqual(new_frequency, frequency, case)
                        self.assertLessEqual(new_voltage, voltage, case)
                    if reason.startswith("step"):
                        self.assertLessEqual(new_voltage, voltage, case)
                    if mode.objective == autotune.EFFICIENCY:
                        self.assertLessEqual(
                            new_voltage, max(voltage, preset["max_volt"])
                        )


class FanTests(unittest.TestCase):
    def test_payload_per_mode(self):
        self.assertEqual(
            modes.fan_payload(modes.MODES[modes.MAX_HASHRATE], 70),
            {"autofanspeed": 0, "manualFanSpeed": 100},
        )
        self.assertEqual(
            modes.fan_payload(modes.MODES[modes.BALANCED], 65),
            {"autofanspeed": 1, "temptarget": 60},
        )
        self.assertEqual(modes.fan_target(90), 66)
        self.assertEqual(modes.fan_target(30), 35)
        self.assertEqual(modes.fan_target(None), 66)

    def test_auto_fan_reads_back_and_a_trip_does_not_match(self):
        auto = {"autofanspeed": 1, "temptarget": 60}
        self.assertTrue(
            autotune.fan_matches({"autofanspeed": 1, "temptarget": 60}, auto)
        )
        self.assertTrue(autotune.fan_matches({"autofanspeed": 1}, auto))
        self.assertFalse(
            autotune.fan_matches({"autofanspeed": 1, "temptarget": 55}, auto)
        )
        # AxeOS runs manual 100% after an overheat trip.
        tripped = {"autofanspeed": 0, "fanspeed": 100, "temptarget": 60}
        self.assertFalse(autotune.fan_matches(tripped, auto))
        self.assertTrue(autotune.fan_matches(tripped, autotune.MANUAL_FULL_FAN))


class EfficiencyDecisionTests(unittest.TestCase):
    def test_errors_raise_voltage_for_hashrate(self):
        self.assertEqual(
            autotune.decide_adjustment(**_decision())[2], "increase voltage"
        )

    def test_errors_cost_a_clock_for_efficiency(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_decision(), objective=autotune.EFFICIENCY
        )
        self.assertEqual((frequency, voltage), (595, 1100))
        self.assertIn("silicon wall", reason)

    def test_efficiency_raises_voltage_only_at_the_frequency_floor(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_decision(current_frequency=400), objective=autotune.EFFICIENCY
        )
        self.assertEqual((frequency, voltage, reason), (400, 1110, "increase voltage"))

    def test_a_clean_chip_still_climbs_for_efficiency(self):
        reason = autotune.decide_adjustment(
            **_decision(error_percentage=0.2), objective=autotune.EFFICIENCY
        )[2]
        self.assertEqual(reason, "increase frequency")

    def test_step_paid_on_energy_per_hash(self):
        paid = autotune.efficiency_step_paid
        # 20 W for 1000 GH/s, then 20.1 W for 1010 GH/s: less energy per hash.
        self.assertTrue(paid(20.0, 1000, 20.1, 1010, 0.01))
        # 21 W for the same hashrate is 5% worse, past a 1% allowance.
        self.assertFalse(paid(20.0, 1000, 21.0, 1000, 0.01))
        self.assertTrue(paid(20.0, 1000, 20.15, 1000, 0.01))
        self.assertIsNone(paid(None, 1000, 20.0, 1000, 0.01))
        self.assertIsNone(paid(20.0, 1000, 20.0, None, 0.01))
        # A clock whose PLL did not rise did not pay.
        self.assertFalse(paid(20.0, 1000, 19.0, 1100, 0.01, 600, 600))

    def test_a_trim_that_loses_a_little_hashrate_but_more_power_pays(self):
        self.assertTrue(autotune.efficiency_step_paid(20.0, 1000, 19.0, 990, 0.01))
        self.assertFalse(
            autotune.good_hashrate_held(1000, 980, autotune.expected_step_gain(5))
        )
        self.assertTrue(autotune.efficiency_step_paid(20.0, 1000, 18.5, 980, 0.01))


class HeatWallTrimTests(unittest.TestCase):
    def _held(self, **overrides):
        values = _decision(
            error_percentage=0.5, thermal_hold=True, temp=64, max_temp=65
        )
        values.update(overrides)
        return values

    def test_efficiency_trims_voltage_at_a_heat_wall(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **self._held(), objective=autotune.EFFICIENCY
        )
        self.assertEqual((frequency, voltage, reason), (600, 1090, "trim voltage"))

    def test_hashrate_waits_at_a_heat_wall(self):
        self.assertEqual(
            autotune.decide_adjustment(**self._held())[2],
            "holding after thermal retreat",
        )

    def test_a_trim_that_lost_is_not_tried_again(self):
        reason = autotune.decide_adjustment(
            **self._held(), objective=autotune.EFFICIENCY, trim_floor_voltage=1090
        )[2]
        self.assertEqual(reason, "holding after thermal retreat")

    def test_errors_over_budget_do_not_trim(self):
        reason = autotune.decide_adjustment(
            **self._held(error_percentage=3), objective=autotune.EFFICIENCY
        )[2]
        self.assertNotEqual(reason, "trim voltage")


class ModeSessionTests(unittest.TestCase):
    def _session(self, mode, info_overrides, stop_when):
        state = {"frequency": 450, "voltage": 1100, "calls": [], "fans": []}
        stop_event = threading.Event()
        runtime = dict(FAST_CONFIG, miners=[{"ip": "miner", "mode": mode}])

        def set_settings(ip, volt, freq):
            state["calls"].append((int(freq), int(volt)))
            state["frequency"] = int(freq)
            state["voltage"] = int(volt)
            if stop_when(state):
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def fan(ip, payload):
            state["fans"].append(dict(payload))
            return True, ""

        def get_info(ip):
            values = dict(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=900,
                hashRate_1m=900,
                expectedHashrate=918,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=45,
            )
            values.update(info_overrides(state))
            return _info(**values)

        with patched_io(get_info, set_settings, runtime_config=runtime):
            with mock.patch.object(autotune, "patch_system", fan):
                thread = _start_miner(
                    "miner",
                    stop_event,
                    lambda *args: None,
                    start_freq=450,
                    max_volt=1150,
                    max_temp=60,
                )
                thread.join(3)
        self.assertFalse(thread.is_alive())
        return state

    def test_efficiency_session_sheds_a_clock_on_errors(self):
        state = self._session(
            "efficiency",
            lambda state: {"errorPercentage": 5, "autofanspeed": 1, "temptarget": 55},
            lambda state: state["frequency"] < 450,
        )
        self.assertEqual(state["calls"][-1], (445, 1100))
        self.assertEqual(state["fans"][0], {"autofanspeed": 1, "temptarget": 55})

    def test_balanced_session_puts_auto_fan_back_after_a_trip(self):
        # The miner reads manual 100% (as after an overheat trip) until the
        # session has asked for auto twice.
        def reply(state):
            auto = sum(1 for fan in state["fans"] if fan.get("autofanspeed")) >= 2
            return {
                "errorPercentage": 0.2,
                "autofanspeed": 1 if auto else 0,
                "fanspeed": 70 if auto else 100,
                "temptarget": 55,
            }

        state = self._session("balanced", reply, lambda state: state["frequency"] > 450)
        self.assertGreaterEqual(len(state["fans"]), 2)
        self.assertTrue(all(fan == state["fans"][0] for fan in state["fans"]))
        self.assertEqual(state["fans"][0], {"autofanspeed": 1, "temptarget": 55})
        self.assertGreater(state["calls"][-1][0], 450)


class ModeConfigTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "config.json")
        self._old = (config.CONFIG_FILE, config._last_good_config)
        config.CONFIG_FILE = self.path
        config._last_good_config = None

    def tearDown(self):
        config.CONFIG_FILE, config._last_good_config = self._old
        self._dir.cleanup()

    def test_an_old_config_keeps_max_mode_and_skips_setup(self):
        old = {
            "limits_version": config.LIMITS_VERSION,
            "config_version": 1,
            "miners": [
                {"ip": "a", "board": "601"},
                {"ip": "b", "board": "402"},
            ],
        }
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(old, handle)
        loaded = config.load_config()
        self.assertEqual(
            [miner["mode"] for miner in loaded["miners"]], ["max_hashrate", "balanced"]
        )
        self.assertEqual(loaded["default_mode"], "max_hashrate")
        self.assertTrue(loaded["setup_done"])
        self.assertEqual(loaded["config_version"], config.CONFIG_VERSION)

    def test_a_fresh_config_waits_for_setup_in_balanced(self):
        fresh = config.load_config()
        self.assertFalse(fresh["setup_done"])
        self.assertEqual(fresh["default_mode"], "balanced")

    def test_fleet_raises_skip_other_boards_and_modes(self):
        old = {
            "limits_version": 2,
            "config_version": config.CONFIG_VERSION,
            "miners": [
                {
                    "ip": "a",
                    "board": "601",
                    "mode": "max_hashrate",
                    "max_core_amps": 25,
                },
                {"ip": "b", "board": "601", "mode": "balanced", "max_core_amps": 25},
                {
                    "ip": "c",
                    "board": "402",
                    "mode": "max_hashrate",
                    "max_core_amps": 25,
                },
            ],
        }
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(old, handle)
        amps = [miner["max_core_amps"] for miner in config.load_config()["miners"]]
        self.assertEqual(amps, [29.0, 25, 25])


def _record(ip, name, board, mode=None):
    return config.new_miner_record(
        f"{board.asic.model} {board.version}",
        ip,
        name,
        config.get_default_config(),
        board=board,
        mode=mode,
    )


class ModeDashboardTests(unittest.TestCase):
    def test_settings_carry_the_mode_and_its_presets(self):
        miner = _record("10.0.0.1", "gamma", boards.GAMMA_601, "balanced")
        with temp_config([miner]):
            result = dashboard.TunerDashboard().get_autotuner_settings()
            row = result["miners"][0]
            self.assertEqual(row["mode"], "balanced")
            self.assertFalse(row["custom"])
            self.assertEqual(row["presets"]["efficiency"]["max_volt"], "1150")
            self.assertEqual(
                [mode["key"] for mode in result["modes"]],
                ["max_hashrate", "balanced", "efficiency"],
            )

    def test_edited_limits_are_custom(self):
        miner = _record("10.0.0.1", "gamma", boards.GAMMA_601, "balanced")
        miner["max_temp"] = 62
        with temp_config([miner]):
            row = dashboard.TunerDashboard().get_autotuner_settings()["miners"][0]
            self.assertTrue(row["custom"])

    def test_saving_a_new_mode_restarts_that_miner(self):
        miner = _record("10.0.0.1", "gamma", boards.GAMMA_601, "balanced")
        with temp_config([miner]):
            app = dashboard.TunerDashboard()
            row = app.get_autotuner_settings()["miners"][0]
            fields = dict(row["presets"]["efficiency"])
            with (
                mock.patch.object(app, "_signal_miner_stop") as stop,
                mock.patch.object(app, "_start_miners_if_running") as start,
            ):
                saved = app.save_autotuner_settings(
                    [
                        {
                            "ip": "10.0.0.1",
                            "enabled": True,
                            "mode": "efficiency",
                            "fields": fields,
                        }
                    ]
                )
            self.assertTrue(saved["ok"])
            stored = config.get_miners()[0]
            self.assertEqual((stored["mode"], stored["max_volt"]), ("efficiency", 1150))
            stop.assert_called_once_with("10.0.0.1")
            start.assert_called_once_with(["10.0.0.1"])

    def test_global_settings_set_the_mode_for_new_miners(self):
        with temp_config([]):
            app = dashboard.TunerDashboard()
            settings = app.get_global_settings()["settings"]
            self.assertEqual(settings["default_mode"], "balanced")
            settings["default_mode"] = "efficiency"
            self.assertTrue(app.save_global_settings(settings)["ok"])
            self.assertEqual(config.load_config()["default_mode"], "efficiency")
            new = config.new_miner_record("x", "1.2.3.4", "n", config.load_config())
            self.assertEqual((new["mode"], new["max_volt"]), ("efficiency", 1150))

    def test_limits_screen_lists_the_modes(self):
        sections = dashboard.hardware_limits(SUPRA_402, "balanced")
        listed = next(s for s in sections if s["title"] == "Modes")
        self.assertEqual(
            [row[0] for row in listed["rows"]],
            ["Max hashrate", "Balanced", "Efficiency"],
        )
        defaults = next(s for s in sections if s["title"] == "Defaults for a new miner")
        self.assertTrue(defaults["intro"].startswith("Balanced mode."))


if __name__ == "__main__":
    unittest.main()
