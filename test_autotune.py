import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from unittest import mock

import autotune
import config


FAST_CONFIG = {
    "voltage_step": 10,
    "frequency_step": 5,
    "monitor_interval": 0.02,
    "temp_tolerance": 2,
    "refresh_interval": 0.05,
    "enforce_safe_pairing": False,
    "flatline_detection_enabled": False,
    "flatline_hashrate_repeat_count": 5,
}


def _limits(**overrides):
    values = {
        "current_frequency": 500,
        "current_voltage": 1100,
        "min_freq": 400,
        "max_freq": 800,
        "min_volt": 1000,
        "max_volt": 1400,
        "max_temp": 60,
        "max_watts": 25,
        "max_vr_temp": 85,
        "temp": 45,
        "vr_temp": 40,
        "power": 12,
        "hash_rate": 100,
        "expected_hashrate": 1500,
        "shares_rejected_delta": 0,
        "overheat_mode": 0,
        "frequency_step": 5,
        "voltage_step": 10,
        "temp_tolerance": 2,
        "tier_list": [],
        "error_percentage": 0,
        "input_voltage": None,
        "min_input_voltage": None,
        "core_voltage_actual": None,
        "max_droop_mv": 40,
        "power_fault": None,
        "phase": "climb",
        "trim_good_voltage": None,
        "max_error_percentage": 2.0,
    }
    values.update(overrides)
    return values


def _retreat(**overrides):
    values = {
        "temp": 45,
        "vr_temp": 40,
        "power": 12,
        "max_temp": 60,
        "max_vr_temp": 85,
        "max_watts": 25,
        "input_voltage": 5.0,
        "min_input_voltage": 4.9,
        "core_voltage_actual": 1100,
        "current_voltage": 1100,
        "max_droop_mv": 40,
        "power_fault": None,
        "overheat_mode": 0,
    }
    values.update(overrides)
    return autotune._needs_immediate_retreat(**values)


def _info(frequency=400, voltage=1100, **overrides):
    info = {
        "temp": 40,
        "vrTemp": 30,
        "power": 10,
        "hashRate": 100,
        "frequency": frequency,
        "coreVoltage": voltage,
        "sharesRejected": 0,
        "expectedHashrate": 5000,
        "overheat_mode": 0,
        "smallCoreCount": 128,
        "asicCount": 1,
        "ASICModel": "BM1370",
        "boardVersion": "601",
        "errorPercentage": 0,
        "voltage": 5.05,
        "autofanspeed": 1,
        "fanspeed": 40,
        "temptarget": 55,
    }
    info.update(overrides)
    return info


@contextmanager
def patched_io(get_info, set_settings, restart=None, runtime_config=None):
    runtime_config = runtime_config or FAST_CONFIG

    def load_config():
        return dict(runtime_config)

    with mock.patch.object(autotune, "load_config", load_config), \
            mock.patch.object(autotune, "get_system_info", get_info), \
            mock.patch.object(autotune, "set_system_settings", set_settings), \
            mock.patch.object(autotune, "patch_system", lambda ip, settings: (True, "")), \
            mock.patch.object(autotune, "restart_bitaxe", restart or (lambda ip: f"{ip} -> Restart initiated.")):
        yield


def _start_miner(ip, stop_event, log, startup_delay=0, **limit_overrides):
    limits = {
        "min_freq": 400,
        "max_freq": 800,
        "min_volt": 1000,
        "max_volt": 1400,
        "max_temp": 60,
        "max_watts": 25,
        "max_vr_temp": 85,
        "start_freq": 400,
        "start_volt": 1100,
    }
    limits.update(limit_overrides)
    thread = threading.Thread(
        target=autotune.monitor_and_adjust,
        args=(
            ip,
            "Gamma",
            0.02,
            log,
            limits["min_freq"],
            limits["max_freq"],
            limits["min_volt"],
            limits["max_volt"],
            limits["max_temp"],
            limits["max_watts"],
            limits["start_freq"],
            limits["start_volt"],
            limits["max_vr_temp"],
        ),
        kwargs={"stop_event": stop_event, "startup_delay": startup_delay},
        daemon=True,
    )
    thread.start()
    return thread


class DecisionTests(unittest.TestCase):
    def test_overheat_steps_frequency_down_without_raising_voltage(self):
        tiers = [
            {"frequency_(mhz)": 695, "voltage": 1158, "target_hashrate": 1400},
            {"frequency_(mhz)": 700, "voltage": 1160, "target_hashrate": 1410},
        ]
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            current_frequency=700,
            current_voltage=1100,
            temp=75,
            tier_list=tiers,
            error_percentage=8,
        ))
        self.assertEqual(frequency, 685)
        self.assertEqual(voltage, 1100)
        self.assertIn("frequency", reason)

    def test_small_overshoot_takes_one_frequency_step(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            current_frequency=700,
            temp=61,
            temp_tolerance=2,
        ))
        self.assertEqual(frequency, 695)
        self.assertEqual(voltage, 1100)
        self.assertIn("frequency", reason)

    def test_regulator_band_holds_while_the_chip_is_still_cool(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            temp=45,
            vr_temp=86,
            max_temp=68,
            max_vr_temp=88,
            temp_tolerance=3,
            vr_temp_tolerance=3,
            error_percentage=0.2,
            hash_rate=1400,
        ))
        self.assertEqual((frequency, voltage), (500, 1100))
        self.assertIn("holding", reason)

    def test_regulator_far_over_its_cap_takes_three_frequency_steps(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            vr_temp=100,
            max_vr_temp=88,
            vr_temp_tolerance=3,
            temp=45,
        ))
        self.assertEqual(frequency, 485)
        self.assertEqual(voltage, 1100)
        self.assertIn("frequency", reason)

    def test_power_limit_still_takes_one_frequency_step(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(power=40, temp=45))
        self.assertEqual(frequency, 495)
        self.assertEqual(voltage, 1100)
        self.assertIn("power", reason)

    def test_overheat_steps_voltage_only_at_minimum_frequency(self):
        frequency, voltage, _reason = autotune.decide_adjustment(**_limits(
            current_frequency=400,
            current_voltage=1200,
            temp=75,
        ))
        self.assertEqual(frequency, 400)
        self.assertEqual(voltage, 1190)

    def test_overheat_holds_at_floor(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            current_frequency=400,
            current_voltage=1000,
            temp=75,
        ))
        self.assertEqual((frequency, voltage), (400, 1000))
        self.assertIn("minimum", reason)

    def test_high_error_raises_voltage_without_passing_max(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            current_frequency=500,
            current_voltage=1100,
            max_volt=1108,
            voltage_step=10,
            error_percentage=5,
            hash_rate=1400,
            expected_hashrate=1500,
        ))
        self.assertEqual(frequency, 500)
        self.assertEqual(voltage, 1108)
        self.assertIn("voltage", reason)

    def test_high_error_at_max_voltage_lowers_frequency(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            current_frequency=500,
            current_voltage=1400,
            error_percentage=5,
            shares_rejected_delta=1,
        ))
        self.assertEqual(frequency, 495)
        self.assertEqual(voltage, 1400)
        self.assertIn("silicon", reason)

    def test_frequency_step_does_not_raise_voltage(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            current_frequency=500,
            current_voltage=1079,
            min_freq=400,
            max_freq=800,
            error_percentage=0.4,
            hash_rate=1400,
            expected_hashrate=1500,
        ))
        self.assertEqual(frequency, 505)
        self.assertEqual(voltage, 1079)
        self.assertIn("frequency", reason)

    def test_inside_temperature_band_holds(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(temp=59, hash_rate=100))
        self.assertEqual((frequency, voltage), (500, 1100))
        self.assertIn("holding", reason)

    def test_healthy_chip_near_expected_hashrate_steps_frequency_up(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            current_frequency=420,
            temp=45,
            hash_rate=1480,
            expected_hashrate=1500,
            error_percentage=0.4,
        ))
        self.assertEqual(frequency, 425)
        self.assertEqual(voltage, 1100)
        self.assertIn("frequency", reason)

    def test_pool_reject_without_asic_errors_holds(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            current_frequency=500,
            temp=45,
            shares_rejected_delta=1,
            error_percentage=0.2,
        ))
        self.assertEqual((frequency, voltage), (500, 1100))
        self.assertIn("holding", reason)

    def test_input_sag_steps_frequency_down_without_raising_voltage(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            error_percentage=8,
            input_voltage=4.5,
            min_input_voltage=4.9,
        ))
        self.assertEqual(frequency, 495)
        self.assertEqual(voltage, 1100)
        self.assertIn("input", reason)

    def test_core_droop_steps_frequency_down_without_raising_voltage(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            current_voltage=1100,
            error_percentage=8,
            core_voltage_actual=1050,
            max_droop_mv=40,
        ))
        self.assertEqual(frequency, 495)
        self.assertEqual(voltage, 1100)
        self.assertIn("droop", reason)

    def test_trim_lowers_voltage_and_restores_it_when_errors_return(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            phase="trim",
            error_percentage=0.2,
            hash_rate=1400,
        ))
        self.assertEqual(frequency, 500)
        self.assertEqual(voltage, 1090)
        self.assertEqual(reason, "trim voltage")

        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            phase="trim",
            current_voltage=1090,
            trim_good_voltage=1100,
            error_percentage=5,
        ))
        self.assertEqual(frequency, 500)
        self.assertEqual(voltage, 1100)
        self.assertEqual(reason, "restore voltage")

    def test_hold_does_not_climb(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            phase="hold",
            error_percentage=0.2,
            hash_rate=1500,
            expected_hashrate=1500,
        ))
        self.assertEqual((frequency, voltage), (500, 1100))
        self.assertEqual(reason, "holding")

    def test_overheat_mode_or_idle_power_holds(self):
        hot = _limits(temp=80, power=0)
        self.assertEqual(autotune.decide_adjustment(**hot)[:2], (500, 1100))
        protected = _limits(temp=45, overheat_mode=1)
        self.assertEqual(autotune.decide_adjustment(**protected)[:2], (500, 1100))

    def test_missing_telemetry_holds(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(temp=None))
        self.assertEqual((frequency, voltage), (500, 1100))
        self.assertIn("telemetry", reason)

    def test_expected_hashrate_prefers_device_value(self):
        self.assertEqual(autotune.expected_hashrate_from_info({"expectedHashrate": 1234}, 500), 1234)
        self.assertEqual(
            autotune.expected_hashrate_from_info({"smallCoreCount": 2, "asicCount": 1000}, 500),
            1000,
        )
        self.assertEqual(autotune.expected_hashrate_from_info({}, 500), 0)

    def test_thermal_step_count_follows_the_hotter_sensor(self):
        self.assertEqual(autotune._thermal_frequency_steps(4, 3), 2)
        self.assertEqual(autotune._thermal_frequency_steps(7, 3), 3)
        self.assertEqual(autotune._thermal_frequency_steps(20, 0), 1)

        two_band = autotune.decide_adjustment(**_limits(
            temp=64,
            max_temp=60,
            temp_tolerance=3,
        ))
        self.assertEqual(two_band[:2], (490, 1100))

        three_band = autotune.decide_adjustment(**_limits(
            temp=67,
            max_temp=60,
            temp_tolerance=3,
        ))
        self.assertEqual(three_band[:2], (485, 1100))

        hotter_regulator = autotune.decide_adjustment(**_limits(
            temp=61,
            max_temp=60,
            temp_tolerance=3,
            vr_temp=100,
            max_vr_temp=85,
            vr_temp_tolerance=3,
        ))
        self.assertEqual(hotter_regulator[:2], (485, 1100))
        self.assertIn("frequency", hotter_regulator[2])

        zero_tolerance = autotune.decide_adjustment(**_limits(
            temp=80,
            max_temp=60,
            temp_tolerance=0,
        ))
        self.assertEqual(zero_tolerance[:2], (495, 1100))

        near_floor = autotune.decide_adjustment(**_limits(
            current_frequency=410,
            min_freq=400,
            temp=80,
            max_temp=60,
            temp_tolerance=2,
        ))
        self.assertEqual(near_floor[:2], (400, 1100))

    def test_equal_to_a_cap_does_not_retreat_and_one_past_does(self):
        at_temp = autotune.decide_adjustment(**_limits(temp=60, max_temp=60, temp_tolerance=2))
        self.assertEqual(at_temp[:2], (500, 1100))
        past_temp = autotune.decide_adjustment(**_limits(temp=61, max_temp=60, temp_tolerance=2))
        self.assertEqual(past_temp[:2], (495, 1100))

        at_vr = autotune.decide_adjustment(**_limits(
            temp=45, vr_temp=85, max_vr_temp=85, vr_temp_tolerance=3, max_temp=68,
        ))
        self.assertEqual(at_vr[:2], (500, 1100))
        self.assertIn("holding", at_vr[2])
        past_vr = autotune.decide_adjustment(**_limits(
            temp=45, vr_temp=86, max_vr_temp=85, vr_temp_tolerance=3, max_temp=68,
        ))
        self.assertEqual(past_vr[:2], (495, 1100))

        at_power = autotune.decide_adjustment(**_limits(power=25, max_watts=25, temp=45))
        self.assertEqual(at_power[:2], (505, 1100))
        past_power = autotune.decide_adjustment(**_limits(power=26, max_watts=25, temp=45))
        self.assertEqual(past_power[:2], (495, 1100))
        self.assertIn("power", past_power[2])

        at_input = autotune.decide_adjustment(**_limits(
            input_voltage=4.9, min_input_voltage=4.9, temp=45,
        ))
        self.assertEqual(at_input[:2], (505, 1100))
        past_input = autotune.decide_adjustment(**_limits(
            input_voltage=4.89, min_input_voltage=4.9, temp=45,
        ))
        self.assertEqual(past_input[:2], (495, 1100))
        self.assertIn("input", past_input[2])

        at_droop = autotune.decide_adjustment(**_limits(
            current_voltage=1100, core_voltage_actual=1060, max_droop_mv=40, temp=45,
        ))
        self.assertEqual(at_droop[:2], (505, 1100))
        past_droop = autotune.decide_adjustment(**_limits(
            current_voltage=1100, core_voltage_actual=1059, max_droop_mv=40, temp=45,
        ))
        self.assertEqual(past_droop[:2], (495, 1100))
        self.assertIn("droop", past_droop[2])

        at_error = autotune.decide_adjustment(**_limits(
            error_percentage=2.0, max_error_percentage=2.0, temp=45, hash_rate=1400,
        ))
        self.assertEqual(at_error[:2], (505, 1100))
        past_error = autotune.decide_adjustment(**_limits(
            error_percentage=2.01, max_error_percentage=2.0, temp=45, hash_rate=1400,
        ))
        self.assertEqual(past_error[:2], (500, 1110))
        self.assertIn("voltage", past_error[2])

    def test_high_error_in_band_or_hold_steps_down_without_raising_voltage(self):
        in_band = autotune.decide_adjustment(**_limits(
            temp=59, max_temp=60, temp_tolerance=2, error_percentage=5,
        ))
        self.assertEqual(in_band[:2], (495, 1100))
        self.assertIn("silicon", in_band[2])

        holding = autotune.decide_adjustment(**_limits(
            phase="hold", temp=45, error_percentage=5,
        ))
        self.assertEqual(holding[:2], (495, 1100))
        self.assertIn("silicon", holding[2])

        missing_vr = autotune.decide_adjustment(**_limits(
            vr_temp=None, temp=45, error_percentage=5, hash_rate=1400,
        ))
        self.assertEqual(missing_vr[:2], (500, 1100))
        self.assertIn("telemetry", missing_vr[2])

        masked = autotune.decide_adjustment(**_limits(
            temp=45, error_percentage=5, shares_rejected_delta=1, hash_rate=1400,
        ))
        self.assertEqual(masked[:2], (500, 1110))
        self.assertNotIn("pool", masked[2])

    def test_power_fault_and_overheat_strings(self):
        fault = autotune.decide_adjustment(**_limits(power_fault="UV", temp=45))
        self.assertEqual(fault[:2], (495, 1100))
        self.assertIn("power fault", fault[2])
        self.assertEqual(
            autotune.decide_adjustment(**_limits(power_fault="none", temp=45))[:2],
            (505, 1100),
        )
        self.assertEqual(
            autotune.decide_adjustment(**_limits(power_fault="0", temp=45))[:2],
            (505, 1100),
        )
        self.assertEqual(
            autotune.decide_adjustment(**_limits(overheat_mode="0", temp=45))[:2],
            (505, 1100),
        )
        self.assertEqual(
            autotune.decide_adjustment(**_limits(overheat_mode="false", temp=45))[:2],
            (505, 1100),
        )
        frozen = autotune.decide_adjustment(**_limits(overheat_mode="true", temp=45))
        self.assertEqual(frozen[:2], (500, 1100))
        self.assertIn("overheat", frozen[2])

    def test_trim_complete_zero_hash_and_wall_labels(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            phase="trim",
            current_voltage=1000,
            min_volt=1000,
            error_percentage=0.2,
            hash_rate=1400,
            temp=45,
        ))
        self.assertEqual((frequency, voltage, reason), (500, 1000, "trim complete"))

        frequency, voltage, reason = autotune.decide_adjustment(**_limits(
            hash_rate=0, temp=45, error_percentage=0.2,
        ))
        self.assertEqual((frequency, voltage), (500, 1100))
        self.assertEqual(reason, "holding")

        self.assertEqual(
            autotune.expected_hashrate_from_info(
                {"expectedHashrate": 0, "smallCoreCount": 2, "asicCount": 1000},
                500,
            ),
            1000,
        )

        sag = autotune.decide_adjustment(**_limits(
            input_voltage=4.5, min_input_voltage=4.9, temp=45,
        ))
        silicon = autotune.decide_adjustment(**_limits(
            phase="hold", error_percentage=5, temp=45,
        ))
        power = autotune.decide_adjustment(**_limits(power=40, temp=45))
        droop = autotune.decide_adjustment(**_limits(
            current_voltage=1100, core_voltage_actual=1050, max_droop_mv=40, temp=45,
        ))
        thermal = autotune.decide_adjustment(**_limits(temp=75))
        self.assertEqual(autotune.wall_type_from_reason(sag[2]), "input")
        self.assertEqual(autotune.wall_type_from_reason(silicon[2]), "silicon")
        self.assertEqual(autotune.wall_type_from_reason(power[2]), "power")
        self.assertEqual(autotune.wall_type_from_reason(droop[2]), "power")
        self.assertEqual(autotune.wall_type_from_reason(thermal[2]), "thermal")
        self.assertEqual(autotune.wall_type_from_reason("increase frequency"), "")

    def test_immediate_retreat_includes_watts_but_not_idle_or_overheat(self):
        self.assertTrue(_retreat(power=26))
        self.assertFalse(_retreat(power=25))
        self.assertFalse(_retreat(power=40, overheat_mode=1))
        self.assertFalse(_retreat(power=0.4, temp=80))
        self.assertTrue(_retreat(temp=61, power=12))

    def test_proposal_one_step_above_the_report_is_allowed(self):
        self.assertFalse(autotune._proposal_jumps_above_report(405, 1100, 400, 1100, 5, 10))
        self.assertFalse(autotune._proposal_jumps_above_report(385, 1100, 400, 1100, 5, 10))
        self.assertTrue(autotune._proposal_jumps_above_report(485, 1100, 400, 1100, 5, 10))
        self.assertFalse(autotune._proposal_jumps_above_report(500, 1110, 500, 1100, 5, 10))
        self.assertTrue(autotune._proposal_jumps_above_report(500, 1220, 500, 1100, 5, 10))


class SessionTests(unittest.TestCase):
    def test_missing_settings_do_not_stop_another_miner(self):
        stop_event = threading.Event()
        good_entered = threading.Event()
        logs = []

        def log(message, level="info"):
            logs.append(message)

        def get_info(ip):
            if ip == "good":
                good_entered.set()
                stop_event.wait(2)
            return _info()

        def set_settings(ip, volt, freq):
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        with patched_io(get_info, set_settings):
            bad = _start_miner("bad", stop_event, log, max_vr_temp=None)
            good = _start_miner("good", stop_event, log)
            self.assertTrue(good_entered.wait(2))
            bad.join(1)
            self.assertFalse(bad.is_alive())
            self.assertTrue(good.is_alive())
            self.assertFalse(stop_event.is_set())
            stop_event.set()
            good.join(2)
            self.assertFalse(good.is_alive())
        self.assertTrue(any("Skipping tuning" in message for message in logs))

    def test_stop_event_joins_and_a_new_session_is_independent(self):
        phase = {"event": None, "entered": None}
        logs = []

        def log(message, level="info"):
            logs.append(message)

        def get_info(ip):
            phase["entered"].set()
            phase["event"].wait(2)
            return _info()

        def set_settings(ip, volt, freq):
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        old_event = threading.Event()
        new_event = threading.Event()
        with patched_io(get_info, set_settings):
            phase["event"] = old_event
            phase["entered"] = threading.Event()
            first = _start_miner("miner", old_event, log)
            self.assertTrue(phase["entered"].wait(2))
            old_event.set()
            first.join(2)
            self.assertFalse(first.is_alive())

            phase["event"] = new_event
            phase["entered"] = threading.Event()
            second = _start_miner("miner", new_event, log)
            self.assertTrue(phase["entered"].wait(2))
            self.assertTrue(second.is_alive())
            self.assertTrue(old_event.is_set())
            new_event.set()
            second.join(2)
            self.assertFalse(second.is_alive())

    def test_stopped_during_stagger_does_not_write_settings(self):
        calls = []
        stop_event = threading.Event()
        stop_event.set()

        def set_settings(ip, volt, freq):
            calls.append((freq, volt))
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        with patched_io(lambda ip: _info(), set_settings):
            thread = _start_miner("miner", stop_event, lambda *args: None, startup_delay=5)
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [])

    def test_rejected_write_does_not_advance_the_setpoint(self):
        state = {"freq": 400, "volt": 1100, "calls": []}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if freq == 405 or freq >= 410:
                if state["calls"].count((405, 1100)) >= 2 or freq >= 410:
                    stop_event.set()
                return f"{ip} -> Error setting system settings: rejected"
            state["freq"] = freq
            state["volt"] = volt
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        def get_info(ip):
            return _info(frequency=state["freq"], voltage=state["volt"])

        with patched_io(get_info, set_settings):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn((405, 1100), state["calls"])
        self.assertGreaterEqual(state["calls"].count((405, 1100)), 2)
        self.assertFalse(any(freq >= 410 for freq, _volt in state["calls"]))

    def test_flatline_restarts_a_frozen_rate_but_not_zero(self):
        restarts = []
        stop_event = threading.Event()

        def restart(ip):
            restarts.append(ip)
            stop_event.set()
            return f"{ip} -> Restart initiated."

        runtime = dict(FAST_CONFIG)
        runtime["flatline_detection_enabled"] = True
        runtime["flatline_hashrate_repeat_count"] = 3

        with patched_io(lambda ip: _info(hashRate=100, temp=59), lambda ip, volt, freq: f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz", restart, runtime):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            thread.join(2)
        self.assertEqual(restarts, ["miner"])

        restarts.clear()
        idle = threading.Event()

        def no_restart(ip):
            restarts.append(ip)
            return f"{ip} -> Restart initiated."

        with patched_io(lambda ip: _info(hashRate=0, temp=59), lambda ip, volt, freq: f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz", no_restart, runtime):
            thread = _start_miner("miner", idle, lambda *args: None)
            self.assertFalse(idle.wait(0.35))
            idle.set()
            thread.join(2)
        self.assertEqual(restarts, [])

    def test_non_gamma_and_missing_error_percentage_are_skipped(self):
        calls = []
        logs = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((freq, volt))
            stop_event.set()
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        with patched_io(lambda ip: _info(boardVersion="602"), set_settings):
            thread = _start_miner("miner", stop_event, lambda message, level="info": logs.append(message))
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [])
        self.assertTrue(any("Gamma 601" in message for message in logs))

        calls.clear()
        logs.clear()
        idle = threading.Event()
        info = _info()
        info.pop("errorPercentage")
        with patched_io(lambda ip: info, set_settings):
            thread = _start_miner("miner", idle, lambda message, level="info": logs.append(message))
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [])
        self.assertTrue(any("errorPercentage" in message for message in logs))

    def test_resume_uses_saved_setpoint(self):
        calls = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            stop_event.set()
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        runtime = dict(FAST_CONFIG)
        runtime["miners"] = [{"ip": "miner", "last_good_freq": 640, "last_good_volt": 1200, "wall_type": "silicon"}]
        with patched_io(lambda ip: _info(), set_settings, runtime_config=runtime):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            thread.join(2)
        self.assertEqual(calls[0], (640, 1200))

    def test_hot_chip_steps_down_without_waiting_out_the_climb_interval(self):
        state = {"frequency": 500, "voltage": 1100}
        calls = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            calls.append((freq, volt))
            state["frequency"] = freq
            state["voltage"] = volt
            if len(calls) >= 2:
                stop_event.set()
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                temp=80,
                vrTemp=40,
            )

        runtime = dict(FAST_CONFIG)
        runtime["refresh_interval"] = 30
        with patched_io(get_info, set_settings, runtime_config=runtime):
            thread = _start_miner("miner", stop_event, lambda *args: None, start_freq=500, start_volt=1100)
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[0], (500, 1100))
        self.assertEqual(calls[1], (485, 1100))

    def test_climb_still_waits_for_the_refresh_interval(self):
        calls = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        runtime = dict(FAST_CONFIG)
        runtime["refresh_interval"] = 30
        with patched_io(lambda ip: _info(temp=40, vrTemp=30), set_settings, runtime_config=runtime):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            self.assertFalse(stop_event.wait(0.4))
            stop_event.set()
            thread.join(2)
        self.assertEqual(calls, [(400, 1100)])

    def test_hold_leaves_the_fan_at_full_speed(self):
        fan_calls = []
        stop_event = threading.Event()

        def patch(ip, settings):
            fan_calls.append(dict(settings))
            return True, ""

        def log(message, level="info"):
            if "Holding" in message:
                stop_event.set()

        runtime = dict(FAST_CONFIG)
        with patched_io(
            lambda ip: _info(frequency=400, voltage=1100, temp=40, vrTemp=30),
            lambda ip, volt, freq: f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz",
            runtime_config=runtime,
        ):
            with mock.patch.object(autotune, "patch_system", patch):
                thread = _start_miner(
                    "miner",
                    stop_event,
                    log,
                    max_freq=400,
                    min_volt=1100,
                    start_freq=400,
                    start_volt=1100,
                )
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(fan_calls)
        self.assertTrue(all(
            call.get("autofanspeed") == 0 and call.get("fanspeed") == 100
            for call in fan_calls
        ))
        self.assertFalse(any(call.get("autofanspeed") == 1 for call in fan_calls))

    def test_reset_writes_stock_clocks_and_clears_learned_setpoint(self):
        miner = {
            "ip": "miner",
            "last_good_freq": 640,
            "last_good_volt": 1200,
            "wall_type": "silicon",
            "wall_timestamp": "2026-01-01T00:00:00Z",
            "target_hashrate": 1500,
            "start_freq": 700,
            "start_volt": 1250,
            "min_freq": 400,
            "max_temp": 66,
        }
        calls = []
        updates = []
        logs = []

        def set_settings(ip, volt, freq):
            calls.append((ip, int(freq), int(volt)))
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        def update(ip, settings):
            updates.append((ip, dict(settings)))
            miner.update(settings)

        def log(message, level="info"):
            logs.append((level, message))

        autotune._publish_status(
            "miner", wall_type="silicon", last_good_freq=640, last_good_volt=1200, phase="hold"
        )
        try:
            with mock.patch.object(autotune, "set_system_settings", set_settings), \
                    mock.patch.object(autotune, "update_miner", update):
                autotune.reset_miners_to_baseline([miner], log, stagger_seconds=0)
            self.assertEqual(autotune.get_miner_status("miner"), {})
        finally:
            autotune._clear_miner_status("miner")

        self.assertEqual((config.STOCK_FREQ, config.STOCK_VOLT), (525, 1150))
        self.assertEqual(calls, [("miner", 525, 1150)])
        self.assertEqual(logs, [("success", "miner -> Applied settings: Voltage = 1150mV, Frequency = 525MHz")])
        ip, cleared = updates[0]
        self.assertEqual(ip, "miner")
        self.assertEqual(cleared["last_good_freq"], "")
        self.assertEqual(cleared["last_good_volt"], "")
        self.assertEqual(cleared["wall_type"], "")
        self.assertEqual(cleared["wall_timestamp"], "")
        self.assertEqual(cleared["target_hashrate"], "")
        self.assertEqual(cleared["start_freq"], 525)
        self.assertEqual(cleared["start_volt"], 1150)
        self.assertNotIn("min_freq", cleared)
        self.assertNotIn("max_temp", cleared)
        self.assertEqual(miner["min_freq"], 400)
        self.assertEqual(miner["max_temp"], 66)

    def test_start_after_reset_uses_stock_instead_of_saved_setpoint(self):
        runtime = dict(FAST_CONFIG)
        runtime["miners"] = [{
            "ip": "miner",
            "last_good_freq": 640,
            "last_good_volt": 1200,
            "wall_type": "silicon",
            "start_freq": 700,
            "start_volt": 1250,
        }]
        calls = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            if len(calls) > 1:
                stop_event.set()
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        def update(ip, settings):
            for miner in runtime["miners"]:
                if miner.get("ip") == ip:
                    miner.update(settings)

        with patched_io(lambda ip: _info(), set_settings, runtime_config=runtime), \
                mock.patch.object(autotune, "update_miner", update):
            autotune.reset_miners_to_baseline(runtime["miners"], lambda *args: None, stagger_seconds=0)
            miner = runtime["miners"][0]
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                start_freq=miner["start_freq"],
                start_volt=miner["start_volt"],
            )
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [(525, 1150), (525, 1150)])
        self.assertEqual(runtime["miners"][0]["last_good_freq"], "")
        self.assertEqual(runtime["miners"][0]["last_good_volt"], "")
        self.assertEqual(runtime["miners"][0]["start_freq"], 525)
        self.assertEqual(runtime["miners"][0]["start_volt"], 1150)

    def test_failed_baseline_write_still_clears_every_miner(self):
        miners = [
            {
                "ip": "bad",
                "last_good_freq": 640,
                "last_good_volt": 1200,
                "wall_type": "silicon",
                "start_freq": 700,
                "start_volt": 1250,
                "min_freq": 450,
            },
            {
                "ip": "good",
                "last_good_freq": 800,
                "last_good_volt": 1300,
                "wall_type": "thermal",
                "start_freq": 600,
                "start_volt": 1200,
                "max_watts": 36,
            },
        ]
        calls = []
        cleared = []
        logs = []

        def set_settings(ip, volt, freq):
            calls.append((ip, int(freq), int(volt)))
            if ip == "bad":
                return f"{ip} -> Error setting system settings: offline"
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        def update(ip, settings):
            cleared.append((ip, dict(settings)))

        def log(message, level="info"):
            logs.append((level, message))

        with mock.patch.object(autotune, "set_system_settings", set_settings), \
                mock.patch.object(autotune, "update_miner", update):
            autotune.reset_miners_to_baseline(miners, log, stagger_seconds=0)

        self.assertEqual(calls, [("bad", 525, 1150), ("good", 525, 1150)])
        self.assertEqual([ip for ip, _settings in cleared], ["bad", "good"])
        self.assertEqual([level for level, _message in logs], ["error", "success"])
        for _ip, settings in cleared:
            self.assertEqual(settings["last_good_freq"], "")
            self.assertEqual(settings["last_good_volt"], "")
            self.assertEqual(settings["wall_type"], "")
            self.assertEqual(settings["wall_timestamp"], "")
            self.assertEqual(settings["target_hashrate"], "")
            self.assertEqual(settings["start_freq"], 525)
            self.assertEqual(settings["start_volt"], 1150)
            self.assertNotIn("min_freq", settings)
            self.assertNotIn("max_watts", settings)

    def test_overclock_flag_is_sent_with_the_setpoint(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

        with mock.patch("autotune.requests.patch", return_value=FakeResponse()) as patched:
            message = autotune.set_system_settings("10.0.0.5", 1150, 525)
        self.assertIn("Applied", message)
        body = patched.call_args.kwargs["json"]
        self.assertEqual(body["overclockEnabled"], 1)
        self.assertEqual(body["frequency"], 525)
        self.assertEqual(body["coreVoltage"], 1150)

    def test_power_over_the_cap_steps_down_without_waiting(self):
        state = {"frequency": 500, "voltage": 1100}
        calls = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            calls.append((freq, volt))
            state["frequency"] = freq
            state["voltage"] = volt
            if len(calls) >= 2:
                stop_event.set()
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                temp=45,
                vrTemp=40,
                power=40,
            )

        runtime = dict(FAST_CONFIG)
        runtime["refresh_interval"] = 30
        with patched_io(get_info, set_settings, runtime_config=runtime):
            thread = _start_miner("miner", stop_event, lambda *args: None, start_freq=500, start_volt=1100)
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls[0], (500, 1100))
        self.assertEqual(calls[1], (495, 1100))

    def test_power_at_the_cap_and_high_error_wait_for_the_tune_interval(self):
        def run_case(info):
            calls = []
            stop_event = threading.Event()

            def set_settings(ip, volt, freq):
                calls.append((int(freq), int(volt)))
                return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

            runtime = dict(FAST_CONFIG)
            runtime["refresh_interval"] = 30
            with patched_io(lambda ip: info, set_settings, runtime_config=runtime):
                thread = _start_miner("miner", stop_event, lambda *args: None, start_freq=500, start_volt=1100)
                self.assertFalse(stop_event.wait(0.4))
                stop_event.set()
                thread.join(2)
            return calls

        at_cap = run_case(_info(frequency=500, voltage=1100, temp=45, vrTemp=40, power=25))
        self.assertEqual(at_cap, [(500, 1100)])
        high_error = run_case(_info(
            frequency=500, voltage=1100, temp=45, vrTemp=40, power=12, errorPercentage=8,
        ))
        self.assertEqual(high_error, [(500, 1100)])

    def test_stale_confirmed_clocks_step_down_from_the_live_report(self):
        polls = {"n": 0}
        calls = []
        stop_event = threading.Event()

        def get_info(ip):
            polls["n"] += 1
            if polls["n"] <= 2:
                return _info(frequency=500, voltage=1100, temp=40, vrTemp=40, power=12)
            return _info(frequency=450, voltage=1100, temp=80, vrTemp=40, power=12)

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            calls.append((freq, volt))
            if freq != 500:
                stop_event.set()
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        runtime = dict(FAST_CONFIG)
        runtime["refresh_interval"] = 30
        with patched_io(get_info, set_settings, runtime_config=runtime):
            thread = _start_miner("miner", stop_event, lambda *args: None, start_freq=500, start_volt=1100)
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [(500, 1100), (435, 1100)])

    def test_reversed_limits_skip_and_a_low_floor_still_clamps(self):
        clamped = autotune.clamp_limits({
            "min_freq": 100,
            "max_freq": 500,
            "min_volt": 900,
            "max_volt": 1200,
        })
        self.assertEqual(clamped["min_freq"], 400)
        self.assertEqual(clamped["max_freq"], 500)
        self.assertEqual(clamped["min_volt"], 1000)
        self.assertEqual(clamped["max_volt"], 1200)

        def run_skip(**limits):
            calls = []
            logs = []
            stop_event = threading.Event()

            def set_settings(ip, volt, freq):
                calls.append((int(freq), int(volt)))
                stop_event.set()
                return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

            with patched_io(lambda ip: _info(), set_settings):
                thread = _start_miner(
                    "miner",
                    stop_event,
                    lambda message, level="info": logs.append(message),
                    **limits,
                )
                thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(calls, [])
            self.assertTrue(any("reversed" in message for message in logs))

        run_skip(min_freq=900, max_freq=600)
        run_skip(min_volt=1300, max_volt=1100)

        calls = []
        logs = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            stop_event.set()
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        with patched_io(lambda ip: _info(), set_settings):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda message, level="info": logs.append(message),
                min_freq=100,
                max_freq=500,
                start_freq=100,
            )
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls[0], (400, 1100))
        self.assertFalse(any("reversed" in message for message in logs))

    def test_saved_setpoint_above_the_cap_is_clamped(self):
        calls = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            stop_event.set()
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        runtime = dict(FAST_CONFIG)
        runtime["miners"] = [{"ip": "miner", "last_good_freq": 1000, "last_good_volt": 1400}]
        with patched_io(lambda ip: _info(), set_settings, runtime_config=runtime):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                max_freq=800,
                max_volt=1200,
            )
            thread.join(2)
        self.assertEqual(calls[0], (800, 1200))

    def test_missing_regulator_temperature_does_not_climb(self):
        calls = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        def get_info(ip):
            info = _info(frequency=400, voltage=1100, temp=40, power=12, errorPercentage=0.2)
            info.pop("vrTemp")
            return info

        runtime = dict(FAST_CONFIG)
        runtime["refresh_interval"] = 0.05
        with patched_io(get_info, set_settings, runtime_config=runtime):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            self.assertFalse(stop_event.wait(0.4))
            stop_event.set()
            thread.join(2)
        self.assertEqual(calls, [(400, 1100)])

    def test_missing_regulator_temperature_still_steps_down_when_the_chip_is_hot(self):
        state = {"frequency": 500, "voltage": 1100}
        calls = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            calls.append((freq, volt))
            state["frequency"] = freq
            state["voltage"] = volt
            if len(calls) >= 2:
                stop_event.set()
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        def get_info(ip):
            info = _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                temp=80,
                power=12,
            )
            info.pop("vrTemp")
            return info

        runtime = dict(FAST_CONFIG)
        runtime["refresh_interval"] = 30
        with patched_io(get_info, set_settings, runtime_config=runtime):
            thread = _start_miner("miner", stop_event, lambda *args: None, start_freq=500, start_volt=1100)
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls[0], (500, 1100))
        self.assertEqual(calls[1], (485, 1100))

    def test_unconfirmed_apply_adopts_the_reported_clocks(self):
        calls = []
        logs = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        def log(message, level="info"):
            logs.append(message)
            if "not confirmed" in message:
                stop_event.set()

        runtime = dict(FAST_CONFIG)
        runtime["refresh_interval"] = 0.05
        with patched_io(lambda ip: _info(frequency=390, voltage=1100, temp=45), set_settings, runtime_config=runtime):
            thread = _start_miner("miner", stop_event, log, start_freq=400, start_volt=1100)
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls.count((400, 1100)), 1)
        self.assertEqual([freq for freq, _volt in calls if freq >= 400], [400])
        self.assertTrue(any("not confirmed" in message for message in logs))

    def test_fan_update_failure_does_not_stop_the_clock_loop(self):
        calls = []
        logs = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            stop_event.set()
            return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

        def patch(ip, settings):
            return False, "offline"

        with patched_io(lambda ip: _info(), set_settings):
            with mock.patch.object(autotune, "patch_system", patch):
                thread = _start_miner(
                    "miner",
                    stop_event,
                    lambda message, level="info": logs.append(message),
                )
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [(400, 1100)])
        self.assertTrue(any("Fan update failed" in message for message in logs))

    def test_uncaught_poll_error_does_not_kill_the_tuner(self):
        polls = {"n": 0}
        logs = []
        continued = threading.Event()
        stop_event = threading.Event()

        def get_info(ip):
            polls["n"] += 1
            if polls["n"] == 2:
                raise RuntimeError("sensor bus down")
            if polls["n"] > 2:
                continued.set()
            return _info(temp=59)

        def log(message, level="info"):
            logs.append(message)

        with patched_io(get_info, lambda ip, volt, freq: f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"):
            thread = _start_miner("miner", stop_event, log)
            self.assertTrue(continued.wait(2))
            stop_event.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(any("UNCAUGHT ERROR" in message and "sensor bus down" in message for message in logs))


class InstallAndConfigTests(unittest.TestCase):
    def test_requirements_do_not_need_windows_blockers(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
        with open(path, encoding="utf-8") as handle:
            requirements = handle.read().lower()
        for blocked in ("tkinter", "gunicorn", "pandas", "flask"):
            self.assertNotIn(blocked, requirements)

    def test_scaling_table_is_gone(self):
        self.assertFalse(hasattr(autotune, "load_scaling_table"))
        csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cpu_voltage_scaling_safeguards.csv")
        self.assertFalse(os.path.exists(csv_path))

    def test_corrupt_config_keeps_last_good_and_does_not_wipe_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            old_path = config.CONFIG_FILE
            old_last = config._last_good_config
            try:
                config.CONFIG_FILE = path
                config._last_good_config = None
                saved = config.get_default_config()
                saved["miners"] = [{"ip": "10.0.0.8", "nickname": "gamma"}]
                config.save_config(saved)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("{broken")
                loaded = config.load_config()
                self.assertEqual(loaded["miners"][0]["ip"], "10.0.0.8")
                with open(path, encoding="utf-8") as handle:
                    self.assertTrue(handle.read().startswith("{broken"))

                config._last_good_config = None
                loaded = config.load_config()
                self.assertEqual(loaded["miners"], [])
                with open(path, encoding="utf-8") as handle:
                    self.assertTrue(handle.read().startswith("{broken"))
            finally:
                config.CONFIG_FILE = old_path
                config._last_good_config = old_last

    def test_new_miner_gets_default_max_temp_and_scan_type(self):
        self.assertEqual(
            config.miner_type_from_info({"ASICModel": "BM1370", "boardVersion": "601"}),
            "BM1370 601",
        )
        self.assertEqual(config.miner_type_from_info({}), "Unknown")
        self.assertTrue(config.is_gamma_601({"ASICModel": "bm1370", "boardVersion": 601}))
        self.assertFalse(config.is_gamma_601({"ASICModel": "BM1370", "boardVersion": "602"}))
        self.assertFalse(config.is_gamma_601({"ASICModel": "BM1366", "boardVersion": "601"}))
        self.assertEqual(autotune.normalize_input_voltage(4900), 4.9)
        self.assertEqual(autotune.normalize_input_voltage(5.01), 5.01)
        self.assertIsNone(autotune.normalize_input_voltage(None))
        clamped = autotune.clamp_limits({
            "min_freq": 100,
            "max_freq": 2000,
            "min_volt": 900,
            "max_volt": 1600,
        })
        self.assertEqual(clamped["min_freq"], 400)
        self.assertEqual(clamped["max_freq"], 1100)
        self.assertEqual(clamped["min_volt"], 1000)
        self.assertEqual(clamped["max_volt"], 1400)

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            old_path = config.CONFIG_FILE
            old_last = config._last_good_config
            try:
                config.CONFIG_FILE = path
                config._last_good_config = None
                config.save_config(config.get_default_config())
                config.add_miner("Gamma", "10.0.0.6", "gamma-1")
                miners = config.get_miners()
                self.assertEqual(miners[0]["max_temp"], 68)
                self.assertEqual(miners[0]["start_freq"], 525)
                self.assertEqual(miners[0]["start_volt"], 1150)
                self.assertEqual(miners[0]["max_freq"], 1100)
                self.assertEqual(miners[0]["max_volt"], 1300)
                self.assertEqual(miners[0]["max_watts"], 50)
                self.assertEqual(miners[0]["max_vr_temp"], 88)
                self.assertEqual(miners[0]["min_input_voltage"], 4.9)
                self.assertEqual(miners[0]["max_error_percentage"], 2.0)
                self.assertEqual(miners[0]["nickname"], "gamma-1")
                autotune.remember_setpoint("10.0.0.6", 800, 1250, "silicon")
                stored = config.get_miners()[0]
                self.assertEqual(stored["last_good_freq"], 800)
                self.assertEqual(stored["last_good_volt"], 1250)
                self.assertEqual(stored["wall_type"], "silicon")
                self.assertTrue(stored["wall_timestamp"])
            finally:
                config.CONFIG_FILE = old_path
                config._last_good_config = old_last

    def test_scan_keeps_only_gamma_601(self):
        class FakeResponse:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload

            def json(self):
                return self._payload

        def fake_get(url, timeout=1):
            if url.endswith("192.168.0.2/api/system/info"):
                return FakeResponse({"ASICModel": "BM1366", "boardVersion": "601"})
            if url.endswith("192.168.0.3/api/system/info"):
                return FakeResponse({"ASICModel": "BM1370", "boardVersion": "601"})
            raise config.requests.exceptions.RequestException("no miner")

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            old_path = config.CONFIG_FILE
            old_last = config._last_good_config
            try:
                config.CONFIG_FILE = path
                config._last_good_config = None
                config.save_config(config.get_default_config())
                with mock.patch("config.requests.get", side_effect=fake_get):
                    found = config.detect_miners("192.168.0.2", "192.168.0.3")
                self.assertEqual([miner["ip"] for miner in found], ["192.168.0.3"])
                self.assertEqual(found[0]["type"], "BM1370 601")
                self.assertEqual(found[0]["max_freq"], 1100)
                self.assertEqual(found[0]["start_volt"], 1150)
                stored = config.get_miners()
                self.assertEqual([miner["ip"] for miner in stored], ["192.168.0.3"])
            finally:
                config.CONFIG_FILE = old_path
                config._last_good_config = old_last

    def test_scan_reports_progress_and_stops_when_cancelled(self):
        calls = []

        def fake_get(url, timeout=1):
            calls.append(url)
            raise config.requests.exceptions.RequestException("no miner")

        progress = []

        def on_progress(index, total, ip):
            progress.append((index, total, ip))

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            old_path = config.CONFIG_FILE
            old_last = config._last_good_config
            try:
                config.CONFIG_FILE = path
                config._last_good_config = None
                config.save_config(config.get_default_config())
                with mock.patch("config.requests.get", side_effect=fake_get):
                    found = config.detect_miners(
                        "192.168.0.2",
                        "192.168.0.5",
                        on_progress=on_progress,
                        should_cancel=lambda: len(progress) >= 1,
                    )
                self.assertEqual(found, [])
                self.assertEqual(progress, [(1, 4, "192.168.0.2")])
                self.assertEqual(len(calls), 1)
            finally:
                config.CONFIG_FILE = old_path
                config._last_good_config = old_last

    def test_row_color_follows_phase_and_limits(self):
        try:
            from gui import blank_miner_row, row_state_tag
        except ModuleNotFoundError as error:
            if error.name != "tkinter":
                raise
            self.skipTest("tkinter is not installed")

        self.assertEqual(blank_miner_row("gamma", "10.0.0.8"), (
            "gamma", "10.0.0.8", "-", "-", "-", "-", "-", "-", "-", "-",
        ))
        self.assertEqual(row_state_tag("hold", "60", "1.00%", 66, 2), "hold")
        self.assertEqual(row_state_tag("climb", "60", "1", 66, 2), "climb")
        self.assertEqual(row_state_tag("trim", "60", "1", 66, 2), "trim")
        self.assertEqual(row_state_tag("climb", "70", "1", 66, 2), "alert")
        self.assertEqual(row_state_tag("hold", "60", "3%", 66, 2), "alert")
        self.assertEqual(row_state_tag("offline", "40", "0", 66, 2), "alert")
        self.assertEqual(row_state_tag("-", "-", "-", None, None), "idle")

    def test_parse_autotuner_value_clamps_frequency_and_voltage(self):
        try:
            from gui import parse_autotuner_value
        except ModuleNotFoundError as error:
            if error.name != "tkinter":
                raise
            self.skipTest("tkinter is not installed")

        self.assertEqual(parse_autotuner_value("min_freq", "50"), 400)
        self.assertEqual(parse_autotuner_value("max_freq", "2000"), 1100)
        self.assertEqual(parse_autotuner_value("start_freq", "700"), 700)
        self.assertEqual(parse_autotuner_value("min_volt", "900"), 1000)
        self.assertEqual(parse_autotuner_value("max_volt", "1600"), 1400)
        self.assertEqual(parse_autotuner_value("start_volt", "  "), "")
        self.assertEqual(parse_autotuner_value("max_temp", "68"), 68)
        self.assertEqual(parse_autotuner_value("min_input_voltage", "4.9"), 4.9)

    def test_overlapping_miner_updates_keep_both_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            old_path = config.CONFIG_FILE
            old_last = config._last_good_config
            try:
                config.CONFIG_FILE = path
                config._last_good_config = None
                saved = config.get_default_config()
                saved["miners"] = [
                    {"ip": "10.0.0.1", "nickname": "one"},
                    {"ip": "10.0.0.2", "nickname": "two"},
                ]
                config.save_config(saved)
                barrier = threading.Barrier(2)

                def hammer(ip, nickname):
                    barrier.wait()
                    for index in range(25):
                        config.update_miner(ip, {"nickname": f"{nickname}-{index}"})

                threads = [
                    threading.Thread(target=hammer, args=("10.0.0.1", "one")),
                    threading.Thread(target=hammer, args=("10.0.0.2", "two")),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)
                self.assertFalse(any(thread.is_alive() for thread in threads))
                stored = {miner["ip"]: miner["nickname"] for miner in config.get_miners()}
                self.assertEqual(stored, {"10.0.0.1": "one-24", "10.0.0.2": "two-24"})
            finally:
                config.CONFIG_FILE = old_path
                config._last_good_config = old_last

    def test_failed_config_replace_leaves_the_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            old_path = config.CONFIG_FILE
            old_last = config._last_good_config
            try:
                config.CONFIG_FILE = path
                config._last_good_config = None
                saved = config.get_default_config()
                saved["miners"] = [{"ip": "10.0.0.8", "nickname": "kept"}]
                config.save_config(saved)
                changed = config.get_default_config()
                changed["miners"] = [{"ip": "10.0.0.9", "nickname": "lost"}]
                with mock.patch("config.os.replace", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        config.save_config(changed)
                with open(path, encoding="utf-8") as handle:
                    self.assertEqual(json.load(handle)["miners"][0]["nickname"], "kept")
                self.assertEqual(config.load_config()["miners"][0]["nickname"], "kept")
            finally:
                config.CONFIG_FILE = old_path
                config._last_good_config = old_last


if __name__ == "__main__":
    unittest.main()
