import json
import os
import tempfile
import threading
import time
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
        "autofanspeed": 0,
        "fanspeed": 100,
        "temptarget": 55,
    }
    info.update(overrides)
    return info


@contextmanager
def patched_io(get_info, set_settings, restart=None, runtime_config=None):
    runtime_config = runtime_config or FAST_CONFIG

    def load_config():
        return dict(runtime_config)

    with (
        mock.patch.object(autotune, "load_config", load_config),
        mock.patch.object(autotune, "get_system_info", get_info),
        mock.patch.object(autotune, "set_system_settings", set_settings),
        mock.patch.object(autotune, "patch_system", lambda ip, settings: (True, "")),
        mock.patch.object(
            autotune,
            "restart_bitaxe",
            restart or (lambda ip: f"{ip} -> Restart initiated."),
        ),
    ):
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
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                current_frequency=700,
                current_voltage=1100,
                temp=75,
                tier_list=tiers,
                error_percentage=8,
            )
        )
        self.assertEqual(frequency, 660)
        self.assertEqual(voltage, 1100)
        self.assertIn("frequency", reason)

    def test_small_overshoot_takes_one_frequency_step(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                current_frequency=700,
                temp=61,
                temp_tolerance=2,
            )
        )
        self.assertEqual(frequency, 695)
        self.assertEqual(voltage, 1100)
        self.assertIn("frequency", reason)

    def test_regulator_under_its_cap_still_climbs(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                temp=45,
                vr_temp=86,
                max_temp=68,
                max_vr_temp=88,
                temp_tolerance=3,
                vr_temp_tolerance=3,
                error_percentage=0.2,
                hash_rate=1400,
            )
        )
        self.assertEqual((frequency, voltage), (505, 1100))
        self.assertIn("frequency", reason)

    def test_regulator_far_over_its_cap_takes_one_step_per_band(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                vr_temp=100,
                max_vr_temp=88,
                vr_temp_tolerance=3,
                temp=45,
            )
        )
        self.assertEqual(frequency, 480)
        self.assertEqual(voltage, 1100)
        self.assertIn("frequency", reason)

    def test_power_limit_still_takes_one_frequency_step(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(power=40, temp=45)
        )
        self.assertEqual(frequency, 495)
        self.assertEqual(voltage, 1100)
        self.assertIn("power", reason)

    def test_overheat_steps_voltage_only_at_minimum_frequency(self):
        frequency, voltage, _reason = autotune.decide_adjustment(
            **_limits(
                current_frequency=400,
                current_voltage=1200,
                temp=75,
            )
        )
        self.assertEqual(frequency, 400)
        self.assertEqual(voltage, 1190)

    def test_overheat_holds_at_floor(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                current_frequency=400,
                current_voltage=1000,
                temp=75,
            )
        )
        self.assertEqual((frequency, voltage), (400, 1000))
        self.assertIn("minimum", reason)

    def test_overheat_at_350_holds_voltage_and_steps_frequency_onto_that_floor(self):
        stepped_frequency, stepped_voltage, stepped_reason = autotune.decide_adjustment(
            **_limits(
                current_frequency=355,
                current_voltage=1100,
                min_freq=350,
                temp=61,
                max_temp=60,
            )
        )
        self.assertEqual((stepped_frequency, stepped_voltage), (350, 1100))
        self.assertIn("frequency", stepped_reason)
        held_frequency, held_voltage, held_reason = autotune.decide_adjustment(
            **_limits(
                current_frequency=350,
                current_voltage=1000,
                min_freq=350,
                temp=75,
            )
        )
        self.assertEqual((held_frequency, held_voltage), (350, 1000))
        self.assertIn("minimum", held_reason)

    def test_high_error_raises_voltage_without_passing_max(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                current_frequency=500,
                current_voltage=1100,
                max_volt=1108,
                voltage_step=10,
                error_percentage=5,
                hash_rate=1400,
                expected_hashrate=1500,
            )
        )
        self.assertEqual(frequency, 500)
        self.assertEqual(voltage, 1108)
        self.assertIn("voltage", reason)

    def test_high_error_at_max_voltage_lowers_frequency(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                current_frequency=500,
                current_voltage=1400,
                error_percentage=5,
                shares_rejected_delta=1,
            )
        )
        self.assertEqual(frequency, 495)
        self.assertEqual(voltage, 1400)
        self.assertIn("silicon", reason)

    def test_frequency_step_does_not_raise_voltage(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                current_frequency=500,
                current_voltage=1079,
                min_freq=400,
                max_freq=800,
                error_percentage=0.4,
                hash_rate=1400,
                expected_hashrate=1500,
            )
        )
        self.assertEqual(frequency, 505)
        self.assertEqual(voltage, 1079)
        self.assertIn("frequency", reason)

    def test_under_the_cap_climbs_and_a_retreat_hold_does_not(self):
        climbed = autotune.decide_adjustment(**_limits(temp=59, hash_rate=100))
        self.assertEqual(climbed[:2], (505, 1100))
        self.assertIn("frequency", climbed[2])

        held = autotune.decide_adjustment(
            **_limits(
                temp=59,
                hash_rate=1400,
                error_percentage=0.2,
                thermal_hold=True,
            )
        )
        self.assertEqual(held[:2], (500, 1100))
        self.assertIn("thermal retreat", held[2])

        self.assertFalse(autotune.cooled_after_retreat(67, 40, 68, 88, 3, 3))
        self.assertTrue(autotune.cooled_after_retreat(65, 40, 68, 88, 3, 3))
        self.assertFalse(autotune.cooled_after_retreat(60, 86, 68, 88, 3, 3))
        self.assertTrue(autotune.cooled_after_retreat(60, 85, 68, 88, 3, 3))

    def test_power_hold_blocks_the_climb_until_the_reading_is_inside_the_cap(self):
        held = autotune.decide_adjustment(
            **_limits(
                power=25,
                max_watts=25,
                hash_rate=1400,
                error_percentage=0.2,
                temp=45,
                safety_hold="power",
            )
        )
        self.assertEqual(held[:2], (500, 1100))
        self.assertEqual(held[2], "holding after power retreat")

        still_over = autotune.decide_adjustment(
            **_limits(power=40, max_watts=25, safety_hold="power", temp=45)
        )
        self.assertLess(still_over[0], 500)
        self.assertIn("power", still_over[2])

        sag = autotune.decide_adjustment(
            **_limits(
                hash_rate=1400,
                error_percentage=0.2,
                temp=45,
                safety_hold="input",
            )
        )
        self.assertEqual(sag[2], "holding after input retreat")

        self.assertFalse(
            autotune.safety_hold_cleared(25, 25, 5.05, 4.9, None, 1100, 40, None)
        )
        self.assertTrue(
            autotune.safety_hold_cleared(24, 25, 5.05, 4.9, None, 1100, 40, None)
        )
        self.assertFalse(
            autotune.safety_hold_cleared(20, 25, 4.8, 4.9, None, 1100, 40, None)
        )
        self.assertTrue(
            autotune.safety_hold_cleared(20, 25, 4.9, 4.9, None, 1100, 40, None)
        )
        self.assertFalse(
            autotune.safety_hold_cleared(20, 25, 5.05, 4.9, 1064, 1100, 40, None)
        )
        self.assertTrue(
            autotune.safety_hold_cleared(20, 25, 5.05, 4.9, 1065, 1100, 40, None)
        )
        self.assertFalse(
            autotune.safety_hold_cleared(20, 25, 5.05, 4.9, None, 1100, 40, "UV")
        )
        self.assertFalse(
            autotune.safety_hold_cleared(None, 25, 5.05, 4.9, None, 1100, 40, None)
        )

    def test_a_dead_board_is_not_a_missing_hashrate_sample(self):
        self.assertTrue(
            autotune.board_hashrate_is_dead({"hashRate": 0, "hashRate_1m": 0})
        )
        self.assertTrue(autotune.board_hashrate_is_dead({"hashRate": 0}))
        self.assertFalse(
            autotune.board_hashrate_is_dead({"hashRate": 900, "hashRate_1m": 0})
        )
        self.assertFalse(autotune.board_hashrate_is_dead({}))
        self.assertTrue(
            autotune.minute_rate_failed({"hashRate": 900, "hashRate_1m": 0})
        )
        self.assertFalse(autotune.minute_rate_failed({"hashRate": 0, "hashRate_1m": 0}))
        self.assertIsNone(
            autotune.measured_hashrate({"hashRate_1m": 0, "hashRate": 900})
        )

    def test_healthy_chip_near_expected_hashrate_steps_frequency_up(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                current_frequency=420,
                temp=45,
                hash_rate=1480,
                expected_hashrate=1500,
                error_percentage=0.4,
            )
        )
        self.assertEqual(frequency, 425)
        self.assertEqual(voltage, 1100)
        self.assertIn("frequency", reason)

    def test_reject_share_steps_down_only_above_one_percent(self):
        kept = autotune.decide_adjustment(
            **_limits(
                current_frequency=500,
                temp=45,
                reject_share=0.01,
                error_percentage=0.2,
                hash_rate=1400,
            )
        )
        self.assertEqual(kept[:2], (505, 1100))
        self.assertIn("frequency", kept[2])

        dropped = autotune.decide_adjustment(
            **_limits(
                current_frequency=500,
                temp=45,
                reject_share=0.02,
                error_percentage=0.2,
                hash_rate=1400,
            )
        )
        self.assertEqual(dropped[:2], (495, 1100))
        self.assertIn("rejected shares", dropped[2])

        self.assertEqual(autotune.reject_share(99, 1, 0), 0.01)
        self.assertAlmostEqual(autotune.reject_share(90, 12, 10), 2 / 92)
        self.assertEqual(autotune.reject_share(90, 10, 10), 0)
        self.assertIsNone(autotune.reject_share(0, 0, 0))
        self.assertEqual(
            autotune.stale_reject_delta(
                [{"message": "Job not found", "count": 4}],
                [
                    {"message": "Job not found", "count": 7},
                    {"message": "Above target", "count": 2},
                    {"message": "Invalid", "count": 1},
                ],
            ),
            3,
        )
        classes = autotune.reject_class_deltas(
            [{"message": "Job not found", "count": 4}],
            [
                {"message": "Job not found", "count": 7},
                {"message": "Above target", "count": 2},
                {"message": "Invalid", "count": 1},
                {"message": "Duplicate share", "count": 4},
                {"message": "Ntime out of range", "count": 1},
            ],
        )
        self.assertEqual(classes["stale"], 3)
        self.assertEqual(classes["above"], 2)
        self.assertEqual(classes["hardware"], 1)
        self.assertEqual(classes["ignore"], 5)
        self.assertEqual(autotune.reject_reason_class("Worker mismatch"), "ignore")
        self.assertEqual(autotune.reject_reason_class("low difficulty share"), "above")
        self.assertEqual(autotune.reject_reason_class("Difficulty too low"), "above")
        self.assertEqual(autotune.reject_reason_class("difficulty-too-low"), "above")
        self.assertEqual(autotune.reject_reason_class("Invalid JobID"), "stale")
        self.assertEqual(autotune.reject_reason_class("invalid-job-id"), "stale")
        self.assertEqual(
            autotune.reject_reason_class("Invalid nonce2 length"), "ignore"
        )
        self.assertEqual(
            autotune.reject_reason_class("Invalid Bitcoin address"), "ignore"
        )
        self.assertEqual(autotune.reject_reason_class("invalid-channel-id"), "ignore")
        self.assertEqual(autotune.reject_reason_class("Invalid"), "hardware")

        sample = autotune.RejectSample()
        sample.add(10, 5, 0)
        self.assertEqual(sample.judge(autotune.MIN_JUDGED_SHARES, 0.01), (None, False))
        sample.add(80, 5, 0)
        hardware_share, above_high = sample.judge(autotune.MIN_JUDGED_SHARES, 0.01)
        self.assertAlmostEqual(hardware_share, 0.1)
        self.assertFalse(above_high)

        mixed = autotune.RejectSample()
        mixed.add(0, 1, 99)
        mixed_share, mixed_above = mixed.judge(autotune.MIN_JUDGED_SHARES, 0.01)
        self.assertAlmostEqual(mixed_share, 0.01)
        self.assertFalse(mixed_above)
        held = autotune.decide_adjustment(**_limits(reject_share=mixed_share, temp=45))
        self.assertEqual(held[:2], (505, 1100))

        burst = autotune.RejectSample()
        burst.add(80, 0, 20)
        _, first_burst = burst.judge(autotune.MIN_JUDGED_SHARES, 0.01)
        self.assertFalse(first_burst)
        burst.add(100, 0, 0)
        _, after_clean = burst.judge(autotune.MIN_JUDGED_SHARES, 0.01)
        self.assertFalse(after_clean)

        sustained = autotune.RejectSample()
        sustained.add(80, 0, 20)
        sustained.judge(autotune.MIN_JUDGED_SHARES, 0.01)
        sustained.add(80, 0, 20)
        _, second_window = sustained.judge(autotune.MIN_JUDGED_SHARES, 0.01)
        self.assertTrue(second_window)

    def test_input_sag_steps_frequency_down_without_raising_voltage(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                error_percentage=8,
                input_voltage=4.5,
                min_input_voltage=4.9,
            )
        )
        self.assertEqual(frequency, 495)
        self.assertEqual(voltage, 1100)
        self.assertIn("input", reason)

    def test_core_droop_steps_frequency_down_without_raising_voltage(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                current_voltage=1100,
                error_percentage=8,
                core_voltage_actual=1050,
                max_droop_mv=40,
            )
        )
        self.assertEqual(frequency, 495)
        self.assertEqual(voltage, 1100)
        self.assertIn("droop", reason)

    def test_droop_after_a_voltage_raise_uses_the_previous_setpoint(self):
        tripped = autotune.decide_adjustment(
            **_limits(
                current_voltage=1110,
                core_voltage_actual=1069,
                max_droop_mv=40,
                error_percentage=0.2,
                hash_rate=1400,
            )
        )
        self.assertEqual(tripped[:2], (495, 1110))
        self.assertIn("droop", tripped[2])
        spared = autotune.decide_adjustment(
            **_limits(
                current_voltage=1110,
                droop_voltage=1100,
                core_voltage_actual=1069,
                max_droop_mv=40,
                error_percentage=0.2,
                hash_rate=1400,
            )
        )
        self.assertEqual(spared[:2], (505, 1110))
        self.assertNotIn("droop", spared[2])

    def test_trim_lowers_voltage_and_restores_it_when_errors_return(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                phase="trim",
                error_percentage=0.2,
                hash_rate=1400,
            )
        )
        self.assertEqual(frequency, 500)
        self.assertEqual(voltage, 1090)
        self.assertEqual(reason, "trim voltage")

        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                phase="trim",
                current_voltage=1090,
                trim_good_voltage=1100,
                error_percentage=5,
            )
        )
        self.assertEqual(frequency, 500)
        self.assertEqual(voltage, 1100)
        self.assertEqual(reason, "restore voltage")

    def test_hold_does_not_climb(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                phase="hold",
                error_percentage=0.2,
                hash_rate=1500,
                expected_hashrate=1500,
            )
        )
        self.assertEqual((frequency, voltage), (500, 1100))
        self.assertEqual(reason, "holding")

    def test_overheat_mode_or_idle_power_holds(self):
        idle = autotune.decide_adjustment(**_limits(temp=45, power=0))
        self.assertEqual(idle[:2], (500, 1100))
        hot = autotune.decide_adjustment(**_limits(temp=80, power=0))
        self.assertEqual(hot[0], 450)
        protected = _limits(temp=45, overheat_mode=1)
        self.assertEqual(autotune.decide_adjustment(**protected)[:2], (500, 1100))

    def test_missing_telemetry_holds(self):
        frequency, voltage, reason = autotune.decide_adjustment(**_limits(temp=None))
        self.assertEqual((frequency, voltage), (500, 1100))
        self.assertIn("telemetry", reason)

    def test_missing_asic_temp_still_steps_down_when_the_regulator_is_hot(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(temp=None, vr_temp=95, max_vr_temp=88, vr_temp_tolerance=3)
        )
        self.assertLess(frequency, 500)
        self.assertEqual(voltage, 1100)
        self.assertIn("frequency", reason)

    def test_zero_temp_does_not_climb_and_zero_core_voltage_is_not_droop(self):
        cold = autotune.decide_adjustment(**_limits(temp=0, hash_rate=1400))
        self.assertEqual(cold[:2], (500, 1100))
        self.assertIn("telemetry", cold[2])
        climbing = autotune.decide_adjustment(
            **_limits(
                temp=45,
                core_voltage_actual=0,
                hash_rate=1400,
                error_percentage=0.2,
            )
        )
        self.assertEqual(climbing[:2], (505, 1100))

    def test_opening_setpoint_keeps_a_cool_chip_and_lowers_a_hot_one(self):
        limits = {
            "min_freq": 350,
            "max_freq": 1100,
            "min_volt": 1000,
            "max_volt": 1300,
            "max_temp": 68,
            "max_watts": 50,
            "max_vr_temp": 88,
        }
        cool = _info(temp=60, vrTemp=70, power=20, errorPercentage=0.2, hashRate=1400)
        kept = autotune.opening_setpoint(
            900, 1250, cool, limits, 5, 10, 3, 3, 4.9, 2.0, 40
        )
        self.assertEqual(kept, (900, 1250, ""))
        hot = _info(temp=80, vrTemp=70, power=20, errorPercentage=0.2, hashRate=1400)
        lowered = autotune.opening_setpoint(
            900, 1250, hot, limits, 5, 10, 3, 3, 4.9, 2.0, 40
        )
        self.assertEqual(lowered[0], 880)
        self.assertEqual(lowered[1], 1250)
        self.assertIn("frequency", lowered[2])
        errors = _info(temp=60, vrTemp=70, power=20, errorPercentage=8, hashRate=1400)
        held = autotune.opening_setpoint(
            900, 1250, errors, limits, 5, 10, 3, 3, 4.9, 2.0, 40
        )
        self.assertEqual(held, (900, 1250, ""))

    def test_expected_hashrate_prefers_device_value(self):
        self.assertEqual(
            autotune.expected_hashrate_from_info({"expectedHashrate": 1234}, 500), 1234
        )
        self.assertEqual(
            autotune.expected_hashrate_from_info(
                {"smallCoreCount": 2, "asicCount": 1000}, 500
            ),
            1000,
        )
        self.assertEqual(autotune.expected_hashrate_from_info({}, 500), 0)

    def test_thermal_step_count_follows_the_hotter_sensor(self):
        self.assertEqual(autotune._thermal_frequency_steps(4, 3), 2)
        self.assertEqual(autotune._thermal_frequency_steps(7, 3), 3)
        self.assertEqual(autotune._thermal_frequency_steps(20, 3), 7)
        self.assertEqual(autotune._thermal_frequency_steps(20, 0), 1)

        two_band = autotune.decide_adjustment(
            **_limits(
                temp=64,
                max_temp=60,
                temp_tolerance=3,
            )
        )
        self.assertEqual(two_band[:2], (490, 1100))

        three_band = autotune.decide_adjustment(
            **_limits(
                temp=67,
                max_temp=60,
                temp_tolerance=3,
            )
        )
        self.assertEqual(three_band[:2], (485, 1100))

        hotter_regulator = autotune.decide_adjustment(
            **_limits(
                temp=61,
                max_temp=60,
                temp_tolerance=3,
                vr_temp=100,
                max_vr_temp=85,
                vr_temp_tolerance=3,
            )
        )
        self.assertEqual(hotter_regulator[:2], (475, 1100))
        self.assertIn("frequency", hotter_regulator[2])

        zero_tolerance = autotune.decide_adjustment(
            **_limits(
                temp=80,
                max_temp=60,
                temp_tolerance=0,
            )
        )
        self.assertEqual(zero_tolerance[:2], (495, 1100))

        near_floor = autotune.decide_adjustment(
            **_limits(
                current_frequency=410,
                min_freq=400,
                temp=80,
                max_temp=60,
                temp_tolerance=2,
            )
        )
        self.assertEqual(near_floor[:2], (400, 1100))

    def test_equal_to_a_cap_does_not_retreat_and_one_past_does(self):
        at_temp = autotune.decide_adjustment(
            **_limits(temp=60, max_temp=60, temp_tolerance=2)
        )
        self.assertEqual(at_temp[:2], (505, 1100))
        past_temp = autotune.decide_adjustment(
            **_limits(temp=61, max_temp=60, temp_tolerance=2)
        )
        self.assertEqual(past_temp[:2], (495, 1100))
        under_decimal = autotune.decide_adjustment(
            **_limits(temp=68.4, max_temp=68.5, temp_tolerance=3, max_watts=50)
        )
        self.assertEqual(under_decimal[:2], (505, 1100))
        over_decimal = autotune.decide_adjustment(
            **_limits(temp=68.6, max_temp=68.5, temp_tolerance=3, max_watts=50)
        )
        self.assertLess(over_decimal[0], 500)

        at_vr = autotune.decide_adjustment(
            **_limits(
                temp=45,
                vr_temp=85,
                max_vr_temp=85,
                vr_temp_tolerance=3,
                max_temp=68,
            )
        )
        self.assertEqual(at_vr[:2], (505, 1100))
        self.assertIn("frequency", at_vr[2])
        past_vr = autotune.decide_adjustment(
            **_limits(
                temp=45,
                vr_temp=86,
                max_vr_temp=85,
                vr_temp_tolerance=3,
                max_temp=68,
            )
        )
        self.assertEqual(past_vr[:2], (495, 1100))

        at_power = autotune.decide_adjustment(
            **_limits(power=25, max_watts=25, temp=45)
        )
        self.assertEqual(at_power[:2], (505, 1100))
        past_power = autotune.decide_adjustment(
            **_limits(power=26, max_watts=25, temp=45)
        )
        self.assertEqual(past_power[:2], (495, 1100))
        self.assertIn("power", past_power[2])

        at_input = autotune.decide_adjustment(
            **_limits(
                input_voltage=4.9,
                min_input_voltage=4.9,
                temp=45,
            )
        )
        self.assertEqual(at_input[:2], (505, 1100))
        past_input = autotune.decide_adjustment(
            **_limits(
                input_voltage=4.89,
                min_input_voltage=4.9,
                temp=45,
            )
        )
        self.assertEqual(past_input[:2], (495, 1100))
        self.assertIn("input", past_input[2])

        at_droop = autotune.decide_adjustment(
            **_limits(
                current_voltage=1100,
                core_voltage_actual=1060,
                max_droop_mv=40,
                temp=45,
            )
        )
        self.assertEqual(at_droop[:2], (505, 1100))
        past_droop = autotune.decide_adjustment(
            **_limits(
                current_voltage=1100,
                core_voltage_actual=1059,
                max_droop_mv=40,
                temp=45,
            )
        )
        self.assertEqual(past_droop[:2], (495, 1100))
        self.assertIn("droop", past_droop[2])

        at_error = autotune.decide_adjustment(
            **_limits(
                error_percentage=2.0,
                max_error_percentage=2.0,
                temp=45,
                hash_rate=1400,
            )
        )
        self.assertEqual(at_error[:2], (505, 1100))
        past_error = autotune.decide_adjustment(
            **_limits(
                error_percentage=2.01,
                max_error_percentage=2.0,
                temp=45,
                hash_rate=1400,
            )
        )
        self.assertEqual(past_error[:2], (500, 1110))
        self.assertIn("voltage", past_error[2])

    def test_high_error_near_the_cap_raises_voltage_until_a_retreat_hold(self):
        near_cap = autotune.decide_adjustment(
            **_limits(
                temp=59,
                max_temp=60,
                temp_tolerance=2,
                error_percentage=5,
            )
        )
        self.assertEqual(near_cap[:2], (500, 1110))
        self.assertIn("voltage", near_cap[2])

        after_retreat = autotune.decide_adjustment(
            **_limits(
                temp=59,
                max_temp=60,
                temp_tolerance=2,
                error_percentage=5,
                thermal_hold=True,
            )
        )
        self.assertEqual(after_retreat[:2], (495, 1100))
        self.assertIn("silicon", after_retreat[2])

        holding = autotune.decide_adjustment(
            **_limits(
                phase="hold",
                temp=45,
                error_percentage=5,
            )
        )
        self.assertEqual(holding[:2], (495, 1100))
        self.assertIn("silicon", holding[2])

        missing_vr = autotune.decide_adjustment(
            **_limits(
                vr_temp=None,
                temp=45,
                error_percentage=5,
                hash_rate=1400,
            )
        )
        self.assertEqual(missing_vr[:2], (500, 1100))
        self.assertIn("telemetry", missing_vr[2])

        masked = autotune.decide_adjustment(
            **_limits(
                temp=45,
                error_percentage=5,
                shares_rejected_delta=1,
                hash_rate=1400,
            )
        )
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

    def test_overheat_flag_stays_until_the_chip_is_cool_and_drawing_power(self):
        cool = {"overheat_mode": 1, "temp": 45, "vrTemp": 40, "power": 12}
        self.assertTrue(autotune.overheat_ready_to_clear(cool, 60, 85))
        self.assertTrue(
            autotune.overheat_ready_to_clear({**cool, "temp": 60, "vrTemp": 85}, 60, 85)
        )
        self.assertFalse(autotune.overheat_ready_to_clear({**cool, "temp": 61}, 60, 85))
        self.assertFalse(
            autotune.overheat_ready_to_clear({**cool, "vrTemp": 86}, 60, 85)
        )
        for temp in (None, 0, -1):
            self.assertFalse(
                autotune.overheat_ready_to_clear({**cool, "temp": temp}, 60, 85)
            )
        self.assertFalse(
            autotune.overheat_ready_to_clear({**cool, "vrTemp": None}, 60, 85)
        )
        self.assertFalse(
            autotune.overheat_ready_to_clear({**cool, "power": 0.5}, 60, 85)
        )
        self.assertFalse(autotune.overheat_ready_to_clear({**cool, "power": 0}, 60, 85))
        self.assertFalse(
            autotune.overheat_ready_to_clear({**cool, "overheat_mode": 0}, 60, 85)
        )
        self.assertFalse(autotune.overheat_ready_to_clear("offline", 60, 85))

    def test_trim_complete_zero_hash_and_wall_labels(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                phase="trim",
                current_voltage=1000,
                min_volt=1000,
                error_percentage=0.2,
                hash_rate=1400,
                temp=45,
            )
        )
        self.assertEqual((frequency, voltage, reason), (500, 1000, "trim complete"))

        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                hash_rate=0,
                temp=45,
                error_percentage=0.2,
            )
        )
        self.assertEqual((frequency, voltage), (500, 1100))
        self.assertEqual(reason, "holding at zero hashrate")

        short = autotune.decide_adjustment(
            **_limits(
                hashrate_short=True,
                error_percentage=0.2,
                hash_rate=1400,
                temp=45,
            )
        )
        self.assertEqual(short[:2], (500, 1110))
        self.assertIn("voltage", short[2])
        short_max = autotune.decide_adjustment(
            **_limits(
                hashrate_short=True,
                error_percentage=0.2,
                hash_rate=1400,
                current_voltage=1400,
            )
        )
        self.assertEqual(short_max[:2], (495, 1400))
        self.assertIn("low hashrate", short_max[2])
        self.assertTrue(autotune.hashrate_well_below_expected(800, 1000))
        self.assertFalse(autotune.hashrate_well_below_expected(900, 1000))
        self.assertEqual(autotune.average_error([1, 3, None]), 2)
        self.assertIsNone(autotune.average_error([]))
        self.assertEqual(autotune.rounded_pll_frequency(527.6), 528)
        self.assertIsNone(autotune.rounded_pll_frequency(0))

        self.assertEqual(
            autotune.expected_hashrate_from_info(
                {"expectedHashrate": 0, "smallCoreCount": 2, "asicCount": 1000},
                500,
            ),
            1000,
        )

        sag = autotune.decide_adjustment(
            **_limits(
                input_voltage=4.5,
                min_input_voltage=4.9,
                temp=45,
            )
        )
        silicon = autotune.decide_adjustment(
            **_limits(
                phase="hold",
                error_percentage=5,
                temp=45,
            )
        )
        power = autotune.decide_adjustment(**_limits(power=40, temp=45))
        droop = autotune.decide_adjustment(
            **_limits(
                current_voltage=1100,
                core_voltage_actual=1050,
                max_droop_mv=40,
                temp=45,
            )
        )
        thermal = autotune.decide_adjustment(**_limits(temp=75))
        self.assertEqual(autotune.wall_type_from_reason(sag[2]), "input")
        self.assertEqual(autotune.wall_type_from_reason(silicon[2]), "silicon")
        self.assertEqual(autotune.wall_type_from_reason(power[2]), "power")
        self.assertEqual(autotune.wall_type_from_reason(droop[2]), "power")
        self.assertEqual(autotune.wall_type_from_reason(thermal[2]), "thermal")
        self.assertEqual(autotune.wall_type_from_reason("increase frequency"), "")
        self.assertEqual(
            autotune.wall_type_from_reason("step frequency down after good hashrate"),
            "hash",
        )
        self.assertEqual(
            autotune.wall_type_from_reason("step frequency down"), "thermal"
        )

    def test_good_hashrate_discounts_invalid_jobs(self):
        self.assertAlmostEqual(autotune.good_hashrate(1638, 0.4), 1638 * 0.996)
        self.assertAlmostEqual(autotune.good_hashrate(1649, 1.9), 1649 * 0.981)
        self.assertIsNone(autotune.good_hashrate(None, 0.4))
        self.assertIsNone(autotune.good_hashrate(1638, None))
        self.assertIsNone(autotune.good_hashrate(0, 0.4))

    def test_frequency_step_pays_unless_good_hashrate_falls_past_the_step(self):
        before = autotune.good_hashrate(1638, 0.4)
        worse = autotune.good_hashrate(1649, 1.9)
        better = autotune.good_hashrate(1700, 0.4)
        self.assertGreater(before - worse, autotune.expected_step_gain(5))
        self.assertFalse(autotune.frequency_step_paid(before, worse, 800, 805))
        self.assertTrue(autotune.frequency_step_paid(before, better, 800, 805))
        self.assertTrue(autotune.frequency_step_paid(before, before, 800, 805))
        self.assertTrue(autotune.frequency_step_paid(before, before - 5, 800, 805))
        self.assertFalse(autotune.frequency_step_paid(before, before - 20, 800, 805))
        self.assertFalse(autotune.frequency_step_paid(before, better, 800, 800))
        self.assertIsNone(autotune.frequency_step_paid(before, None, 800, 805))
        self.assertTrue(autotune.frequency_step_paid(before, better, None, None))

    def test_blocked_frequency_does_not_climb_back(self):
        held = autotune.decide_adjustment(
            **_limits(
                current_frequency=495,
                blocked_frequency=500,
                error_percentage=0.2,
                hash_rate=1400,
            )
        )
        self.assertEqual(held[:2], (495, 1100))
        self.assertIn("frequency retreat", held[2])

        below = autotune.decide_adjustment(
            **_limits(
                current_frequency=490,
                blocked_frequency=500,
                error_percentage=0.2,
                hash_rate=1400,
            )
        )
        self.assertEqual(below[:2], (495, 1100))

    def test_sustained_above_target_raises_voltage_first(self):
        raised = autotune.decide_adjustment(
            **_limits(
                above_target_high=True,
                error_percentage=0.2,
                hash_rate=1400,
                reject_share=0.2,
            )
        )
        self.assertEqual(raised[:2], (500, 1110))
        self.assertIn("voltage", raised[2])

        at_max = autotune.decide_adjustment(
            **_limits(
                above_target_high=True,
                current_voltage=1400,
                error_percentage=0.2,
                hash_rate=1400,
            )
        )
        self.assertEqual(at_max[:2], (495, 1400))
        self.assertIn("above target", at_max[2])

    def test_measured_hashrate_prefers_the_one_minute_rate(self):
        self.assertEqual(
            autotune.measured_hashrate({"hashRate_1m": 1100, "hashRate": 900}),
            1100,
        )
        self.assertEqual(autotune.measured_hashrate({"hashRate": 900}), 900)
        self.assertIsNone(
            autotune.measured_hashrate({"hashRate_1m": 0, "hashRate": 900})
        )
        self.assertIsNone(autotune.measured_hashrate({}))

    def test_immediate_retreat_includes_watts_but_not_idle_or_overheat(self):
        self.assertTrue(_retreat(power=26))
        self.assertFalse(_retreat(power=25))
        self.assertTrue(_retreat(power=40, overheat_mode=1))
        self.assertTrue(_retreat(power=0.4, temp=80))
        self.assertFalse(_retreat(power=0.4, temp=45))
        self.assertFalse(_retreat(temp=None, power=None))
        self.assertTrue(_retreat(temp=None, vr_temp=95, power=12))
        self.assertTrue(_retreat(temp=74, power=None))
        self.assertFalse(_retreat(temp=0, power=12))
        self.assertFalse(
            _retreat(temp=45, power=12, core_voltage_actual=0, current_voltage=1100)
        )
        self.assertTrue(_retreat(temp=61, power=12))

    def test_proposal_one_step_above_the_report_is_allowed(self):
        self.assertFalse(
            autotune._proposal_jumps_above_report(405, 1100, 400, 1100, 5, 10)
        )
        self.assertFalse(
            autotune._proposal_jumps_above_report(385, 1100, 400, 1100, 5, 10)
        )
        self.assertTrue(
            autotune._proposal_jumps_above_report(485, 1100, 400, 1100, 5, 10)
        )
        self.assertFalse(
            autotune._proposal_jumps_above_report(500, 1110, 500, 1100, 5, 10)
        )
        self.assertTrue(
            autotune._proposal_jumps_above_report(500, 1220, 500, 1100, 5, 10)
        )

    def test_reject_block_clears_on_a_later_clean_sample_only(self):
        self.assertFalse(
            autotune.frequency_block_cleared(
                500,
                1100,
                1100,
                False,
                True,
                True,
                None,
            )
        )
        self.assertFalse(
            autotune.frequency_block_cleared(
                500,
                1100,
                1100,
                False,
                True,
                True,
                0.02,
            )
        )
        self.assertTrue(
            autotune.frequency_block_cleared(
                500,
                1100,
                1100,
                False,
                True,
                True,
                0.0,
            )
        )
        self.assertTrue(
            autotune.frequency_block_cleared(
                500,
                1110,
                1100,
                False,
                False,
                False,
                None,
            )
        )
        self.assertFalse(
            autotune.frequency_block_cleared(
                500,
                1100,
                1100,
                False,
                True,
                False,
                0.0,
            )
        )

    def test_stop_remembers_the_probe_it_left_not_the_unconfirmed_climb(self):
        self.assertEqual(
            autotune.setpoint_to_remember(
                (405, 1100), {"from_freq": 400, "from_volt": 1100}, (405, 1100)
            ),
            (400, 1100),
        )
        self.assertEqual(
            autotune.setpoint_to_remember((500, 1100), None, (495, 1100)),
            (495, 1100),
        )
        self.assertEqual(
            autotune.setpoint_to_remember((400, 1100), None, None), (400, 1100)
        )
        self.assertEqual(
            autotune.setpoint_to_remember(
                (400, 1090),
                {
                    "from_freq": 400,
                    "from_volt": 1100,
                    "to_freq": 400,
                    "to_volt": 1090,
                    "restore": True,
                },
                (400, 1100),
            ),
            (400, 1100),
        )

    def test_running_session_picks_up_a_lower_temperature_cap(self):
        limits = {
            "min_freq": 400,
            "max_freq": 800,
            "min_volt": 1000,
            "max_volt": 1400,
            "max_temp": 68,
            "max_watts": 50,
            "max_vr_temp": 88,
        }
        refreshed = autotune.refresh_running_limits(
            limits, {"max_temp": 40, "max_watts": 20}
        )
        self.assertEqual(refreshed["max_temp"], 40)
        self.assertEqual(refreshed["max_watts"], 20)
        self.assertEqual(refreshed["max_freq"], 800)
        decimal = autotune.refresh_running_limits(
            limits, {"max_temp": 68.5, "max_watts": 50.25, "max_vr_temp": 88.5}
        )
        self.assertEqual(decimal["max_temp"], 68.5)
        self.assertEqual(decimal["max_watts"], 50.25)
        self.assertEqual(decimal["max_vr_temp"], 88.5)
        reversed_limits = autotune.refresh_running_limits(
            limits, {"min_freq": 700, "max_freq": 400}
        )
        self.assertEqual(reversed_limits["max_freq"], 800)
        self.assertEqual(reversed_limits["min_freq"], 400)

    def test_hold_above_the_frequency_cap_steps_down_without_raising_voltage(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                phase="hold",
                current_frequency=800,
                max_freq=600,
                error_percentage=0.2,
                hash_rate=1400,
            )
        )
        self.assertEqual((frequency, voltage), (600, 1100))
        self.assertIn("frequency", reason)

    def test_hot_retreat_does_not_raise_frequency_to_a_higher_min(self):
        frequency, voltage, reason = autotune.decide_adjustment(
            **_limits(
                current_frequency=500,
                min_freq=600,
                temp=80,
                max_temp=60,
            )
        )
        self.assertLessEqual(frequency, 500)
        self.assertLess(voltage, 1100)
        self.assertIn("voltage", reason)

    def test_zero_temperatures_do_not_clear_a_retreat_or_allow_a_climb(self):
        self.assertFalse(autotune.cooled_after_retreat(0, 40, 68, 88, 3, 3))
        self.assertTrue(autotune.cooled_after_retreat(65, 0, 68, 88, 3, 3))
        held = autotune.decide_adjustment(
            **_limits(temp=45, vr_temp=0, hash_rate=1400, error_percentage=0.2)
        )
        self.assertEqual(held[:2], (500, 1100))
        self.assertIn("telemetry", held[2])


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
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

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
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

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
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        with patched_io(lambda ip: _info(), set_settings):
            thread = _start_miner(
                "miner", stop_event, lambda *args: None, startup_delay=5
            )
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
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

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

        with (
            patched_io(
                lambda ip: _info(
                    hashRate=100,
                    hashRate_1m=100,
                    frequency=400,
                    voltage=1100,
                    temp=40,
                    vrTemp=30,
                ),
                lambda ip, volt, freq: (
                    f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
                ),
                restart,
                runtime,
            ),
            mock.patch.object(autotune, "FLATLINE_STILL_SECONDS", 0),
        ):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                max_freq=400,
                min_volt=1100,
                start_freq=400,
                start_volt=1100,
            )
            thread.join(2)
            stop_event.set()
            thread.join(2)
        self.assertEqual(restarts, ["miner"])

        restarts.clear()
        logs = []
        idle = threading.Event()

        def no_restart(ip):
            restarts.append(ip)
            return f"{ip} -> Restart initiated."

        def log(message, level="info"):
            logs.append(message)
            if "still 0" in message:
                idle.set()

        with patched_io(
            lambda ip: _info(hashRate=0, temp=40, vrTemp=30),
            lambda ip, volt, freq: (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            ),
            no_restart,
            runtime,
        ):
            thread = _start_miner("miner", idle, log)
            self.assertTrue(idle.wait(2))
            idle.set()
            thread.join(2)
        self.assertEqual(restarts, ["miner"])
        self.assertTrue(any("0 GH/s after settle" in message for message in logs))
        self.assertTrue(
            any("still 0 GH/s after restart" in message for message in logs)
        )

    def test_flatline_ignores_a_steady_live_rate(self):
        restarts = []
        stop_event = threading.Event()
        polls = {"n": 0}
        runtime = dict(FAST_CONFIG)
        runtime["flatline_detection_enabled"] = True
        runtime["flatline_hashrate_repeat_count"] = 3

        def get_info(ip):
            polls["n"] += 1
            return _info(
                hashRate=100,
                hashRate_1m=100 + polls["n"],
                frequency=400,
                voltage=1100,
                temp=40,
                vrTemp=30,
            )

        def restart(ip):
            restarts.append(ip)
            return f"{ip} -> Restart initiated."

        with patched_io(get_info, lambda ip, volt, freq: "", restart, runtime):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                max_freq=400,
                min_volt=1100,
                start_freq=400,
                start_volt=1100,
            )
            self.assertFalse(stop_event.wait(0.6))
            stop_event.set()
            thread.join(2)
        self.assertEqual(restarts, [])

    def test_flatline_ignores_a_sticky_minute_rate_inside_one_minute(self):
        restarts = []
        stop_event = threading.Event()
        runtime = dict(FAST_CONFIG)
        runtime["flatline_detection_enabled"] = True
        runtime["flatline_hashrate_repeat_count"] = 3

        def restart(ip):
            restarts.append(ip)
            return f"{ip} -> Restart initiated."

        with patched_io(
            lambda ip: _info(
                hashRate=100,
                hashRate_1m=100,
                frequency=400,
                voltage=1100,
                temp=40,
                vrTemp=30,
            ),
            lambda ip, volt, freq: "",
            restart,
            runtime,
        ):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                max_freq=400,
                min_volt=1100,
                start_freq=400,
                start_volt=1100,
            )
            self.assertFalse(stop_event.wait(0.6))
            stop_event.set()
            thread.join(2)
        self.assertEqual(restarts, [])

    def test_flatline_ignores_a_minute_rate_while_the_live_rate_moves(self):
        restarts = []
        stop_event = threading.Event()
        polls = {"n": 0}
        runtime = dict(FAST_CONFIG)
        runtime["flatline_detection_enabled"] = True
        runtime["flatline_hashrate_repeat_count"] = 3

        def get_info(ip):
            polls["n"] += 1
            return _info(
                hashRate=100 + polls["n"],
                hashRate_1m=100,
                frequency=400,
                voltage=1100,
                temp=40,
                vrTemp=30,
            )

        def restart(ip):
            restarts.append(ip)
            return f"{ip} -> Restart initiated."

        with (
            patched_io(get_info, lambda ip, volt, freq: "", restart, runtime),
            mock.patch.object(autotune, "FLATLINE_STILL_SECONDS", 0),
        ):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                max_freq=400,
                min_volt=1100,
                start_freq=400,
                start_volt=1100,
            )
            self.assertFalse(stop_event.wait(0.6))
            stop_event.set()
            thread.join(2)
        self.assertEqual(restarts, [])

    def test_flatline_stays_off_when_the_setting_is_missing(self):
        restarts = []
        stop_event = threading.Event()
        runtime = dict(FAST_CONFIG)
        del runtime["flatline_detection_enabled"]
        self.assertFalse(config.get_default_config()["flatline_detection_enabled"])

        def restart(ip):
            restarts.append(ip)
            return f"{ip} -> Restart initiated."

        with patched_io(
            lambda ip: _info(
                hashRate=100,
                hashRate_1m=100,
                frequency=400,
                voltage=1100,
                temp=40,
                vrTemp=30,
            ),
            lambda ip, volt, freq: "",
            restart,
            runtime,
        ):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                max_freq=400,
                min_volt=1100,
                start_freq=400,
                start_volt=1100,
            )
            self.assertFalse(stop_event.wait(0.6))
            stop_event.set()
            thread.join(2)
        self.assertEqual(restarts, [])

    def test_flatline_restarts_once_then_holds(self):
        restarts = []
        stop_event = threading.Event()
        runtime = dict(FAST_CONFIG)
        runtime["flatline_detection_enabled"] = True
        runtime["flatline_hashrate_repeat_count"] = 3
        runtime["refresh_interval"] = 0.05
        runtime["monitor_interval"] = 0.02

        def restart(ip):
            restarts.append(ip)
            return f"{ip} -> Restart initiated."

        def log(message, level="info"):
            if "still flat after restart" in message:
                stop_event.set()

        with (
            patched_io(
                lambda ip: _info(
                    hashRate=100,
                    hashRate_1m=100,
                    frequency=400,
                    voltage=1100,
                    temp=40,
                    vrTemp=30,
                ),
                lambda ip, volt, freq: (
                    f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
                ),
                restart,
                runtime,
            ),
            mock.patch.object(autotune, "FLATLINE_STILL_SECONDS", 0),
        ):
            thread = _start_miner(
                "miner",
                stop_event,
                log,
                max_freq=400,
                min_volt=1100,
                start_freq=400,
                start_volt=1100,
            )
            self.assertTrue(stop_event.wait(2))
            stop_event.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(restarts, ["miner"])

    def test_non_gamma_and_missing_error_percentage_are_skipped(self):
        calls = []
        logs = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((freq, volt))
            stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        with patched_io(lambda ip: _info(boardVersion="602"), set_settings):
            thread = _start_miner(
                "miner", stop_event, lambda message, level="info": logs.append(message)
            )
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
            thread = _start_miner(
                "miner", idle, lambda message, level="info": logs.append(message)
            )
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [])
        self.assertTrue(any("errorPercentage" in message for message in logs))

    def test_stopped_tuner_saves_the_confirmed_clocks(self):
        runtime = dict(FAST_CONFIG)
        runtime["miners"] = [{"ip": "miner", "wall_type": "thermal"}]
        runtime["refresh_interval"] = 30
        updates = []
        stop_event = threading.Event()

        def log(message, level="info"):
            if "Confirmed" in message:
                stop_event.set()

        def update(ip, settings):
            updates.append((ip, dict(settings)))

        with (
            patched_io(
                lambda ip: _info(frequency=400, voltage=1100, temp=40, vrTemp=30),
                lambda ip, volt, freq: (
                    f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
                ),
                runtime_config=runtime,
            ),
            mock.patch.object(autotune, "update_miner", update),
        ):
            thread = _start_miner(
                "miner",
                stop_event,
                log,
                max_freq=400,
                start_freq=400,
                start_volt=1100,
            )
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertTrue(updates)
        ip, saved = updates[-1]
        self.assertEqual(ip, "miner")
        self.assertEqual(saved["last_good_freq"], 400)
        self.assertEqual(saved["last_good_volt"], 1100)
        self.assertEqual(saved["wall_type"], "thermal")
        self.assertTrue(saved["wall_timestamp"])

    def test_stop_before_confirm_does_not_save_clocks(self):
        runtime = dict(FAST_CONFIG)
        runtime["miners"] = [{"ip": "miner", "wall_type": "thermal"}]
        updates = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def update(ip, settings):
            updates.append((ip, dict(settings)))

        with (
            patched_io(lambda ip: _info(), set_settings, runtime_config=runtime),
            mock.patch.object(autotune, "update_miner", update),
        ):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(updates, [])

    def test_resume_uses_saved_setpoint(self):
        calls = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        runtime = dict(FAST_CONFIG)
        runtime["miners"] = [
            {
                "ip": "miner",
                "last_good_freq": 640,
                "last_good_volt": 1200,
                "wall_type": "silicon",
            }
        ]
        with patched_io(lambda ip: _info(), set_settings, runtime_config=runtime):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            thread.join(2)
        self.assertEqual(calls[0], (640, 1200))

    def test_hot_chip_drops_once_then_waits_for_the_heatsink(self):
        state = {"frequency": 500, "voltage": 1100}
        calls = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            calls.append((freq, volt))
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

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
            thread = _start_miner(
                "miner", stop_event, lambda *args: None, start_freq=500, start_volt=1100
            )
            self.assertFalse(stop_event.wait(0.4))
            stop_event.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [(450, 1100)])

    def test_hot_chip_drops_again_after_the_safety_settle(self):
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
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                temp=80,
                vrTemp=40,
            )

        runtime = dict(FAST_CONFIG)
        runtime["refresh_interval"] = 0.05
        with patched_io(get_info, set_settings, runtime_config=runtime):
            thread = _start_miner(
                "miner", stop_event, lambda *args: None, start_freq=500, start_volt=1100
            )
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls[0], (450, 1100))
        self.assertEqual(calls[1], (400, 1100))

    def test_opening_heat_drop_holds_until_the_chip_cools_a_full_band(self):
        state = {"frequency": 500, "voltage": 1100, "temp": 80, "calls": []}
        stop_event = threading.Event()
        logs = []

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            state["frequency"] = freq
            state["voltage"] = volt
            state["temp"] = 59
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def log(message, level="info"):
            logs.append(message)
            if "increase frequency" in message and not state.get("held"):
                state["climbed_early"] = True
            if "holding after thermal retreat" in message and state["temp"] == 59:
                state["held"] = True
                state["temp"] = 57
            if "increase frequency" in message and state["temp"] == 57:
                stop_event.set()

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                temp=state["temp"],
                vrTemp=40,
                hashRate=1000,
                hashRate_1m=1000,
                errorPercentage=0.2,
                actualFrequency=state["frequency"],
            )

        runtime = dict(FAST_CONFIG)
        runtime["temp_tolerance"] = 3
        runtime["refresh_interval"] = 0.05
        with patched_io(get_info, set_settings, runtime_config=runtime):
            thread = _start_miner(
                "miner",
                stop_event,
                log,
                max_temp=60,
                start_freq=500,
                start_volt=1100,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(state["calls"])
        opening = state["calls"][0][0]
        self.assertLess(opening, 500)
        self.assertTrue(
            any("holding after thermal retreat" in message for message in logs)
        )
        self.assertTrue(any(freq > opening for freq, _volt in state["calls"]))
        self.assertFalse(state.get("climbed_early"))

    def test_unconfirmed_write_steps_down_while_the_chip_is_hot(self):
        calls = []
        reads = {"n": 0}
        stop_event = threading.Event()

        def get_info(ip):
            reads["n"] += 1
            temp = 40 if reads["n"] == 1 else 80
            return _info(frequency=520, voltage=1100, temp=temp, vrTemp=40, power=12)

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            if len(calls) >= 2:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        runtime = dict(FAST_CONFIG)
        runtime["refresh_interval"] = 30
        runtime["temp_tolerance"] = 2
        with patched_io(get_info, set_settings, runtime_config=runtime):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                start_freq=500,
                start_volt=1100,
                max_temp=60,
            )
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[0], (500, 1100))
        self.assertLess(calls[1][0], 520)
        self.assertEqual(calls[1][1], 1100)

    def test_climb_still_waits_for_the_refresh_interval(self):
        calls = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        runtime = dict(FAST_CONFIG)
        runtime["refresh_interval"] = 30
        with patched_io(
            lambda ip: _info(temp=40, vrTemp=30), set_settings, runtime_config=runtime
        ):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            self.assertFalse(stop_event.wait(0.4))
            stop_event.set()
            thread.join(2)
        self.assertEqual(calls, [(400, 1100)])

    def test_frequency_step_stays_when_good_hashrate_rises(self):
        state = {"frequency": 400, "voltage": 1100, "hash": 1000.0, "calls": []}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if freq >= 410:
                stop_event.set()
                return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            state["frequency"] = freq
            state["voltage"] = volt
            if freq == 405:
                state["hash"] = 1040.0
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=state["hash"],
                hashRate_1m=state["hash"],
                errorPercentage=0.2,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=30,
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(state["calls"][0], (400, 1100))
        self.assertIn((405, 1100), state["calls"])
        self.assertIn((410, 1100), state["calls"])

    def test_small_hashrate_dip_keeps_the_frequency_step(self):
        state = {"frequency": 400, "voltage": 1100, "calls": []}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if freq >= 410:
                stop_event.set()
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            rate = 1000 if state["frequency"] <= 400 else 995
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=rate,
                hashRate_1m=rate,
                errorPercentage=0.4,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=30,
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn((405, 1100), state["calls"])
        self.assertIn((410, 1100), state["calls"])

    def test_frequency_miss_raises_voltage_before_locking_the_ceiling(self):
        state = {"frequency": 400, "voltage": 1100, "calls": []}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if freq == 405 and volt > 1100:
                stop_event.set()
            if freq == 400 and volt < 1100:
                stop_event.set()
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            rate = 1000 if state["frequency"] <= 400 else 800
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=rate,
                hashRate_1m=rate,
                errorPercentage=0.4,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=30,
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn((405, 1100), state["calls"])
        self.assertIn((405, 1110), state["calls"])
        self.assertFalse(
            any(freq == 400 and volt != 1100 for freq, volt in state["calls"])
        )

    def test_frequency_miss_at_max_voltage_steps_back(self):
        state = {"frequency": 400, "voltage": 1400, "calls": []}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if freq == 400 and volt < 1400:
                stop_event.set()
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            rate = 1000 if state["frequency"] <= 400 else 800
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=rate,
                hashRate_1m=rate,
                errorPercentage=0.4,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=30,
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                start_volt=1400,
                max_volt=1400,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn((405, 1400), state["calls"])
        self.assertIn((400, 1400), state["calls"])
        self.assertTrue(
            any(freq == 400 and volt < 1400 for freq, volt in state["calls"])
        )
        self.assertFalse(any(freq > 405 for freq, _volt in state["calls"]))

    def test_frequency_step_reverts_when_the_pll_clock_does_not_rise(self):
        state = {"frequency": 400, "voltage": 1100, "hash": 1000.0, "calls": []}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if freq == 405:
                state["hash"] = 1200.0
            if len(state["calls"]) >= 3:
                stop_event.set()
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=state["hash"],
                hashRate_1m=state["hash"],
                errorPercentage=0.2,
                actualFrequency=400,
                temp=40,
                vrTemp=30,
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(state["calls"][:3], [(400, 1100), (405, 1100), (400, 1100)])

    def test_climb_steps_from_the_confirmed_setpoint_when_the_pll_is_low(self):
        state = {"frequency": 400, "voltage": 1100, "hash": 1000.0, "calls": []}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if freq >= 410:
                stop_event.set()
            state["frequency"] = freq
            state["voltage"] = volt
            if freq >= 405:
                state["hash"] = 1100.0
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            requested = state["frequency"]
            actual = requested if requested <= 400 else requested - 2
            return _info(
                frequency=requested,
                voltage=state["voltage"],
                hashRate=state["hash"],
                hashRate_1m=state["hash"],
                errorPercentage=0.2,
                actualFrequency=actual,
                temp=40,
                vrTemp=30,
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn((405, 1100), state["calls"])
        self.assertIn((410, 1100), state["calls"])
        self.assertNotIn((408, 1100), state["calls"])
        self.assertFalse(any(freq < 400 for freq, _volt in state["calls"]))

    def test_a_high_pll_does_not_jump_the_confirmed_setpoint(self):
        state = {"frequency": 400, "voltage": 1100, "calls": []}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            state["frequency"] = freq
            state["voltage"] = volt
            if freq != 400:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=1200,
                hashRate_1m=1200,
                errorPercentage=0.2,
                actualFrequency=820,
                temp=40,
                vrTemp=30,
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(state["calls"][0], (400, 1100))
        self.assertEqual(state["calls"][1], (405, 1100))

    def test_voltage_raise_keeps_the_requested_frequency_when_the_pll_is_off(self):
        cases = (
            (501, 500),
            (502, 502.5),
        )
        for requested, actual in cases:
            with self.subTest(requested=requested, actual=actual):
                state = {"frequency": requested, "voltage": 1100, "calls": []}
                stop_event = threading.Event()

                def set_settings(ip, volt, freq, state=state, stop_event=stop_event):
                    freq = int(freq)
                    volt = int(volt)
                    state["calls"].append((freq, volt))
                    state["frequency"] = freq
                    state["voltage"] = volt
                    if volt > 1100:
                        stop_event.set()
                    return (
                        f"{ip} -> Applied settings: Voltage = {volt}mV, "
                        f"Frequency = {freq}MHz"
                    )

                def get_info(ip, state=state, actual=actual):
                    return _info(
                        frequency=state["frequency"],
                        voltage=state["voltage"],
                        hashRate=1000,
                        hashRate_1m=1000,
                        errorPercentage=8,
                        actualFrequency=actual,
                        temp=40,
                        vrTemp=30,
                    )

                with patched_io(get_info, set_settings):
                    thread = _start_miner(
                        "miner",
                        stop_event,
                        lambda *args: None,
                        min_freq=350,
                        max_freq=800,
                        start_freq=requested,
                        start_volt=1100,
                    )
                    thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertGreater(state["calls"][-1][1], 1100)
                self.assertTrue(
                    all(freq == requested for freq, _volt in state["calls"]),
                    state["calls"],
                )

    def test_voltage_trim_keeps_the_requested_frequency_when_the_pll_is_low(self):
        state = {"frequency": 501, "voltage": 1100, "calls": []}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            state["frequency"] = freq
            state["voltage"] = volt
            if volt < 1100:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=1000,
                hashRate_1m=1000,
                errorPercentage=0.2,
                actualFrequency=500,
                temp=40,
                vrTemp=30,
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                min_freq=350,
                max_freq=501,
                start_freq=501,
                start_volt=1100,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn((501, 1090), state["calls"])
        self.assertNotIn((500, 1090), state["calls"])

    def test_stuck_pll_requests_a_higher_clock_instead_of_trimming(self):
        state = {"frequency": 450, "voltage": 1150, "calls": []}
        stop_event = threading.Event()
        runtime = dict(FAST_CONFIG)
        runtime["frequency_step"] = 1

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            state["frequency"] = freq
            state["voltage"] = volt
            if freq >= 454:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            requested = state["frequency"]
            actual = requested if requested >= 453 else 450
            return _info(
                frequency=requested,
                voltage=state["voltage"],
                hashRate=1200,
                hashRate_1m=1200,
                errorPercentage=0.2,
                actualFrequency=actual,
                temp=40,
                vrTemp=30,
            )

        with patched_io(get_info, set_settings, runtime_config=runtime):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                min_freq=400,
                max_freq=800,
                start_freq=450,
                start_volt=1150,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn((453, 1150), state["calls"])
        self.assertTrue(all(volt == 1150 for _freq, volt in state["calls"]))

    def test_one_minute_shortfall_rejects_a_step_before_ten_minutes(self):
        state = {"frequency": 400, "voltage": 1100, "calls": []}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            state["frequency"] = freq
            state["voltage"] = volt
            if volt > 1100 or freq > 405:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            # 855 to 848 is inside one 5 MHz hashrate band, and under 85% of 1000.
            rate = 855 if state["frequency"] <= 400 else 848
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=rate,
                hashRate_1m=rate,
                hashRate_10m=1000,
                expectedHashrate=1000,
                errorPercentage=0.2,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=30,
            )

        with (
            patched_io(get_info, set_settings),
            mock.patch.object(autotune, "HASHRATE_1M_SETTLE_SECONDS", 0),
        ):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                start_freq=400,
                start_volt=1100,
                max_freq=800,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn((405, 1100), state["calls"])
        self.assertTrue(
            any(freq == 405 and volt > 1100 for freq, volt in state["calls"])
        )
        self.assertFalse(any(freq > 405 for freq, _volt in state["calls"]))

    def test_under_cap_climbs_then_holds_until_the_retreat_band_clears(self):
        state = {"temp": 67, "frequency": 500, "voltage": 1100, "calls": []}
        stop_event = threading.Event()
        logs = []

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            state["frequency"] = freq
            state["voltage"] = volt
            if freq == 505:
                state["temp"] = 70
            elif freq < 505 and state["temp"] == 70:
                state["temp"] = 67
            if sum(1 for seen, _volt in state["calls"] if seen > 500) >= 2:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def log(message, level="info"):
            logs.append(message)
            if "thermal retreat" in message and state["temp"] == 67:
                state["temp"] = 65
            if "increase frequency" in message and state["temp"] == 65:
                stop_event.set()

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                temp=state["temp"],
                vrTemp=40,
                hashRate=1000,
                hashRate_1m=1000,
                errorPercentage=0.2,
            )

        runtime = dict(FAST_CONFIG)
        runtime["temp_tolerance"] = 3
        runtime["vr_temp_tolerance"] = 3
        runtime["refresh_interval"] = 0.05
        with patched_io(get_info, set_settings, runtime_config=runtime):
            thread = _start_miner(
                "miner",
                stop_event,
                log,
                max_temp=68,
                max_vr_temp=88,
                start_freq=500,
                start_volt=1100,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn((505, 1100), state["calls"])
        self.assertTrue(any(freq < 505 for freq, _volt in state["calls"]))
        self.assertTrue(any("thermal retreat" in message for message in logs))
        climbed_after_hold = [freq for freq, _volt in state["calls"] if freq > 500]
        self.assertGreaterEqual(len(climbed_after_hold), 2)

    def test_error_window_uses_the_settle_samples(self):
        errors = []
        calls = []
        stop_event = threading.Event()

        def get_info(ip):
            error = 0 if len(errors) >= 8 else 9
            errors.append(error)
            return _info(
                frequency=400,
                voltage=1100,
                temp=40,
                vrTemp=30,
                hashRate=1000,
                hashRate_1m=1000,
                errorPercentage=error,
            )

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            if len(calls) >= 2:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        runtime = dict(FAST_CONFIG)
        runtime["refresh_interval"] = 0.25
        runtime["monitor_interval"] = 0.02
        with patched_io(get_info, set_settings, runtime_config=runtime):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(len(errors), 8)
        self.assertEqual(errors[-1], 0)
        self.assertEqual(calls[0], (400, 1100))
        self.assertEqual(calls[1][0], 400)
        self.assertGreater(calls[1][1], 1100)

    def test_stale_pool_rejects_do_not_step_down(self):
        state = {"accepted": 0, "rejected": 0, "stale": 0, "calls": []}
        stop_event = threading.Event()

        def get_info(ip):
            state["accepted"] += 10
            state["rejected"] += 4
            state["stale"] += 4
            return _info(
                frequency=400,
                voltage=1100,
                temp=40,
                vrTemp=30,
                hashRate=1000,
                hashRate_1m=1000,
                errorPercentage=0.2,
                sharesAccepted=state["accepted"],
                sharesRejected=state["rejected"],
                sharesRejectedReasons=[
                    {"message": "Job not found", "count": state["stale"]}
                ],
            )

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if freq >= 405:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn((405, 1100), state["calls"])
        self.assertFalse(any(freq < 400 for freq, _volt in state["calls"]))

    def test_voltage_trim_restores_when_good_hashrate_falls(self):
        state = {"frequency": 400, "voltage": 1100, "calls": [], "ceiling": False}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if (
                any(seen_volt < 1100 for _seen_freq, seen_volt in state["calls"][:-1])
                and volt == 1100
            ):
                stop_event.set()
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            if state["frequency"] >= 800:
                state["ceiling"] = True
            rate = 1000 if state["voltage"] >= 1100 else 900
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=rate,
                hashRate_1m=rate,
                errorPercentage=0.2,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=30,
                expectedHashrate=1000,
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner("miner", stop_event, lambda *args: None, max_freq=400)
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn((400, 1090), state["calls"])
        self.assertIn((400, 1100), state["calls"])
        self.assertFalse(any(volt < 1090 for _freq, volt in state["calls"]))

    def test_stop_during_voltage_restore_keeps_the_restored_voltage(self):
        state = {"frequency": 400, "voltage": 1100, "calls": []}
        updates = []
        stop_event = threading.Event()
        runtime = dict(FAST_CONFIG)
        runtime["miners"] = [{"ip": "miner", "wall_type": ""}]

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            restoring = (
                any(seen_volt < 1100 for _seen_freq, seen_volt in state["calls"][:-1])
                and volt == 1100
            )
            if not restoring:
                state["frequency"] = freq
                state["voltage"] = volt
            else:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            rate = 1000 if state["voltage"] >= 1100 else 900
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=rate,
                hashRate_1m=rate,
                errorPercentage=0.2,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=30,
                expectedHashrate=1000,
            )

        def update(ip, settings):
            updates.append(dict(settings))

        with (
            patched_io(get_info, set_settings, runtime_config=runtime),
            mock.patch.object(autotune, "update_miner", update),
        ):
            thread = _start_miner("miner", stop_event, lambda *args: None, max_freq=400)
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn((400, 1090), state["calls"])
        self.assertTrue(updates)
        self.assertEqual(updates[-1]["last_good_freq"], 400)
        self.assertEqual(updates[-1]["last_good_volt"], 1100)

    def test_error_retreat_does_not_climb_straight_back(self):
        state = {"frequency": 500, "voltage": 1400, "calls": [], "polls_after_drop": 0}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if any(seen < 500 for seen, _volt in state["calls"][:-1]) and freq >= 500:
                stop_event.set()
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            if state["frequency"] < 500:
                state["polls_after_drop"] += 1
                if state["polls_after_drop"] > 40:
                    stop_event.set()
            error = 8 if state["frequency"] >= 500 else 0.2
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=1000,
                hashRate_1m=1000,
                errorPercentage=error,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=30,
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                start_freq=500,
                start_volt=1400,
                max_volt=1400,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(any(freq < 500 for freq, _volt in state["calls"]))
        self.assertFalse(
            any(freq >= 500 and volt == 1400 for freq, volt in state["calls"][2:])
        )

    def test_silicon_retreat_trims_voltage_and_saves_the_lower_clock(self):
        state = {"frequency": 500, "voltage": 1400, "calls": []}
        updates = []
        logs = []
        stop_event = threading.Event()
        runtime = dict(FAST_CONFIG)
        runtime["miners"] = [{"ip": "miner", "wall_type": ""}]

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            state["frequency"] = freq
            state["voltage"] = volt
            if freq < 500 and volt < 1400:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            error = 8 if state["frequency"] >= 500 else 0.2
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=1000,
                hashRate_1m=1000,
                errorPercentage=error,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=30,
            )

        def update(ip, settings):
            updates.append((ip, dict(settings)))

        def log(message, level="info"):
            logs.append(message)

        with (
            patched_io(get_info, set_settings, runtime_config=runtime),
            mock.patch.object(autotune, "update_miner", update),
        ):
            thread = _start_miner(
                "miner",
                stop_event,
                log,
                start_freq=500,
                start_volt=1400,
                max_volt=1400,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn((495, 1400), state["calls"])
        self.assertTrue(
            any(freq < 500 and volt < 1400 for freq, volt in state["calls"])
        )
        self.assertTrue(
            any("Frequency retreat. Trimming voltage." in message for message in logs)
        )
        self.assertTrue(
            any(
                item.get("last_good_freq") == 495 and item.get("last_good_volt") == 1400
                for _ip, item in updates
            )
        )

    def test_hold_retreat_below_max_voltage_does_not_raise_voltage(self):
        state = {"frequency": 500, "voltage": 1100, "error": 0.2, "calls": []}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            state["frequency"] = freq
            state["voltage"] = volt
            if freq <= 490 or volt > 1100:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=1000,
                hashRate_1m=1000,
                errorPercentage=state["error"],
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=30,
            )

        def log(message, level="info"):
            if "Holding " in message and "MHz" in message:
                state["error"] = 8

        with patched_io(get_info, set_settings):
            thread = _start_miner(
                "miner",
                stop_event,
                log,
                start_freq=500,
                start_volt=1100,
                max_freq=500,
                min_volt=1100,
                max_volt=1400,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn((495, 1100), state["calls"])
        self.assertIn((490, 1100), state["calls"])
        self.assertTrue(all(volt == 1100 for _freq, volt in state["calls"]))

    def test_reject_block_stays_when_the_chip_cools(self):
        state = {
            "accepted": 0,
            "rejected": 0,
            "frequency": 500,
            "voltage": 1100,
            "temp": 59,
            "stepped_from": None,
            "polls_after_drop": 0,
            "calls": [],
        }
        stop_event = threading.Event()

        def get_info(ip):
            # One bad window steps frequency down. Later polls add no shares,
            # so a clean sample cannot clear the block. Cooling is the only
            # path that could allow the old clock back.
            if state["stepped_from"] is None:
                state["accepted"] += 80
                state["rejected"] += 16
            else:
                state["polls_after_drop"] += 1
                if state["polls_after_drop"] > 40:
                    stop_event.set()
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                temp=state["temp"],
                vrTemp=40,
                power=12,
                hashRate=1000,
                hashRate_1m=1000,
                errorPercentage=0.2,
                actualFrequency=state["frequency"],
                sharesAccepted=state["accepted"],
                sharesRejected=state["rejected"],
                sharesRejectedReasons=[
                    {"message": "Invalid", "count": state["rejected"]}
                ],
            )

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            if state["stepped_from"] is None and freq < state["frequency"]:
                state["stepped_from"] = state["frequency"]
                state["drop_at"] = len(state["calls"])
                state["temp"] = 50
            if state["stepped_from"] is not None and freq >= state["stepped_from"]:
                stop_event.set()
            state["calls"].append((freq, volt))
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                start_freq=500,
                start_volt=1100,
                max_freq=520,
                max_temp=60,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(state["stepped_from"])
        after_drop = [freq for freq, _volt in state["calls"][state["drop_at"] + 1 :]]
        self.assertFalse(
            any(freq >= state["stepped_from"] for freq in after_drop),
            (state["stepped_from"], state["calls"]),
        )

    def test_sustained_reject_share_steps_frequency_down(self):
        state = {
            "accepted": 0,
            "rejected": 0,
            "frequency": 500,
            "voltage": 1100,
            "hash": 1000.0,
            "calls": [],
        }
        stop_event = threading.Event()

        def get_info(ip):
            state["accepted"] += 8
            state["rejected"] += 2
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                temp=40,
                vrTemp=30,
                hashRate=state["hash"],
                hashRate_1m=state["hash"],
                errorPercentage=0.2,
                sharesAccepted=state["accepted"],
                sharesRejected=state["rejected"],
                sharesRejectedReasons=[
                    {"message": "Invalid", "count": state["rejected"]}
                ],
            )

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if freq > state["frequency"]:
                state["hash"] += 200
            if freq < state["frequency"]:
                stop_event.set()
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                start_freq=500,
                start_volt=1100,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        peak = max(freq for freq, _volt in state["calls"])
        peak_at = max(
            index for index, (freq, _volt) in enumerate(state["calls"]) if freq == peak
        )
        self.assertGreater(peak, 500)
        self.assertTrue(
            any(freq < peak for freq, _volt in state["calls"][peak_at + 1 :])
        )

    def test_thermal_step_down_is_saved_before_stop(self):
        saves = []
        stop_event = threading.Event()
        runtime = dict(FAST_CONFIG)
        runtime["miners"] = [{"ip": "miner", "wall_type": ""}]
        runtime["refresh_interval"] = 30

        def update(ip, settings):
            saves.append((dict(settings), stop_event.is_set()))
            freq = settings.get("last_good_freq")
            if freq not in ("", None) and int(freq) < 500:
                stop_event.set()

        with (
            patched_io(
                lambda ip: _info(
                    frequency=500, voltage=1100, temp=80, vrTemp=40, power=12
                ),
                lambda ip, volt, freq: (
                    f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
                ),
                runtime_config=runtime,
            ),
            mock.patch.object(autotune, "update_miner", update),
        ):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                start_freq=500,
                start_volt=1100,
                max_temp=60,
            )
            thread.join(3)
            if not stop_event.is_set():
                stop_event.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(
            any(
                int(item["last_good_freq"]) < 500
                and item.get("wall_type") == "thermal"
                and not stopped
                for item, stopped in saves
            )
        )

    def test_reject_step_down_is_saved_before_stop(self):
        state = {
            "accepted": 0,
            "rejected": 0,
            "frequency": 500,
            "voltage": 1100,
            "drop": None,
        }
        saved_before_stop = []
        stop_event = threading.Event()
        runtime = dict(FAST_CONFIG)
        runtime["miners"] = [{"ip": "miner", "wall_type": ""}]

        def get_info(ip):
            state["accepted"] += 8
            state["rejected"] += 2
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                temp=40,
                vrTemp=30,
                hashRate=1000,
                hashRate_1m=1000,
                errorPercentage=0.2,
                sharesAccepted=state["accepted"],
                sharesRejected=state["rejected"],
                sharesRejectedReasons=[
                    {"message": "Invalid", "count": state["rejected"]}
                ],
            )

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            if freq < state["frequency"] and state["drop"] is None:
                state["stepped_from"] = state["frequency"]
                state["drop"] = freq
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def update(ip, settings):
            freq = settings.get("last_good_freq")
            if (
                state["drop"] is not None
                and freq == state["drop"]
                and not stop_event.is_set()
            ):
                saved_before_stop.append(dict(settings))
                stop_event.set()

        with (
            patched_io(get_info, set_settings, runtime_config=runtime),
            mock.patch.object(autotune, "update_miner", update),
        ):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                start_freq=500,
                start_volt=1100,
            )
            thread.join(3)
            if not stop_event.is_set():
                stop_event.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(saved_before_stop)
        self.assertEqual(saved_before_stop[0]["wall_type"], "reject")
        self.assertEqual(saved_before_stop[0]["last_good_freq"], state["drop"])
        self.assertLess(state["drop"], state["stepped_from"])

    def test_low_ten_minute_hashrate_is_a_silicon_wall(self):
        calls = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            if len(calls) >= 2:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        runtime = dict(FAST_CONFIG)
        with (
            patched_io(
                lambda ip: _info(
                    frequency=400,
                    voltage=1100,
                    temp=40,
                    vrTemp=30,
                    hashRate=1000,
                    hashRate_1m=1000,
                    hashRate_10m=1000,
                    expectedHashrate=5000,
                    errorPercentage=0.2,
                ),
                set_settings,
                runtime_config=runtime,
            ),
            mock.patch.object(autotune, "HASHRATE_10M_SETTLE_SECONDS", 0),
        ):
            thread = _start_miner("miner", stop_event, lambda *args: None)
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls[0], (400, 1100))
        self.assertEqual(calls[1][0], 400)
        self.assertGreater(calls[1][1], 1100)

    def test_missing_good_hashrate_does_not_climb(self):
        calls = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            info = _info(
                frequency=400, voltage=1100, temp=40, vrTemp=30, errorPercentage=0.2
            )
            info["hashRate"] = 0
            info.pop("hashRate_1m", None)
            return info

        with patched_io(get_info, set_settings):
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
            lambda ip, volt, freq: (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            ),
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
        self.assertTrue(
            all(
                call.get("autofanspeed") == 0 and call.get("fanspeed") == 100
                for call in fan_calls
            )
        )
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
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def update(ip, settings):
            updates.append((ip, dict(settings)))
            miner.update(settings)

        def log(message, level="info"):
            logs.append((level, message))

        autotune._publish_status(
            "miner",
            wall_type="silicon",
            last_good_freq=640,
            last_good_volt=1200,
            phase="hold",
        )
        try:
            with (
                mock.patch.object(autotune, "set_system_settings", set_settings),
                mock.patch.object(autotune, "update_miner", update),
            ):
                autotune.reset_miners_to_baseline([miner], log, stagger_seconds=0)
            self.assertEqual(autotune.get_miner_status("miner"), {})
        finally:
            autotune._clear_miner_status("miner")

        self.assertEqual((config.STOCK_FREQ, config.STOCK_VOLT), (525, 1150))
        self.assertEqual(calls, [("miner", 525, 1150)])
        self.assertEqual(
            logs,
            [
                (
                    "success",
                    "miner -> Applied settings: Voltage = 1150mV, Frequency = 525MHz",
                )
            ],
        )
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
        runtime["miners"] = [
            {
                "ip": "miner",
                "last_good_freq": 640,
                "last_good_volt": 1200,
                "wall_type": "silicon",
                "start_freq": 700,
                "start_volt": 1250,
            }
        ]
        calls = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            if len(calls) > 1:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def update(ip, settings):
            for miner in runtime["miners"]:
                if miner.get("ip") == ip:
                    miner.update(settings)

        with (
            patched_io(lambda ip: _info(), set_settings, runtime_config=runtime),
            mock.patch.object(autotune, "update_miner", update),
        ):
            autotune.reset_miners_to_baseline(
                runtime["miners"], lambda *args: None, stagger_seconds=0
            )
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
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def update(ip, settings):
            cleared.append((ip, dict(settings)))

        def log(message, level="info"):
            logs.append((level, message))

        with (
            mock.patch.object(autotune, "set_system_settings", set_settings),
            mock.patch.object(autotune, "update_miner", update),
        ):
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

    def test_parallel_baseline_reset_writes_every_miner_at_once(self):
        miners = [
            {
                "ip": "10.0.0.1",
                "last_good_freq": 640,
                "start_freq": 700,
                "start_volt": 1250,
            },
            {
                "ip": "10.0.0.2",
                "last_good_freq": 800,
                "start_freq": 600,
                "start_volt": 1200,
            },
        ]
        calls = []
        cleared = []
        started = threading.Barrier(2)

        def set_settings(ip, volt, freq):
            calls.append(ip)
            started.wait(timeout=2)
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def update(ip, settings):
            cleared.append(ip)

        with (
            mock.patch.object(autotune, "set_system_settings", set_settings),
            mock.patch.object(autotune, "update_miner", update),
        ):
            autotune.reset_miners_to_baseline(miners, lambda *args: None, parallel=True)

        self.assertEqual(set(calls), {"10.0.0.1", "10.0.0.2"})
        self.assertEqual(set(cleared), {"10.0.0.1", "10.0.0.2"})
        self.assertEqual(len(calls), 2)

    def test_restart_miners_keeps_going_after_a_failure(self):
        calls = []
        logs = []

        def restart(ip):
            calls.append(ip)
            if ip == "bad":
                return f"{ip} -> Error restarting system: down"
            return f"{ip} -> Restart initiated."

        def log(message, level="info"):
            logs.append((level, message))

        with mock.patch.object(autotune, "restart_bitaxe", restart):
            autotune.restart_miners(
                [{"ip": "bad"}, {"ip": ""}, {"ip": "good"}],
                log,
                stagger_seconds=0,
            )

        self.assertEqual(calls, ["bad", "good"])
        self.assertEqual(
            logs,
            [
                ("warning", "Restarting miner at bad..."),
                ("warning", "bad -> Error restarting system: down"),
                ("warning", "Restarting miner at good..."),
                ("warning", "good -> Restart initiated."),
            ],
        )

    def test_restart_miners_staggers_after_the_first(self):
        slept = []
        with (
            mock.patch.object(autotune, "restart_bitaxe", lambda ip: f"{ip} ok"),
            mock.patch.object(autotune.time, "sleep", slept.append),
        ):
            autotune.restart_miners(
                [{"ip": "a"}, {"ip": "b"}, {"ip": "c"}],
                lambda *args: None,
            )
        self.assertEqual(
            slept,
            [autotune.STARTUP_STAGGER_SECONDS, autotune.STARTUP_STAGGER_SECONDS],
        )

    def test_overclock_flag_is_sent_with_the_setpoint(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

        with mock.patch(
            "autotune.requests.patch", return_value=FakeResponse()
        ) as patched:
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
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

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
            thread = _start_miner(
                "miner", stop_event, lambda *args: None, start_freq=500, start_volt=1100
            )
            self.assertFalse(stop_event.wait(0.4))
            stop_event.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [(495, 1100)])

    def test_power_retreat_does_not_climb_until_power_is_back_inside_the_cap(self):
        state = {
            "frequency": 500,
            "voltage": 1100,
            "power": 20,
            "calls": [],
            "released": False,
        }
        stop_event = threading.Event()
        logs = []

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if freq > 500:
                state["power"] = 30
            elif state["power"] >= 30:
                state["power"] = 25
            state["frequency"] = freq
            state["voltage"] = volt
            if state["released"] and freq > 500:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                power=state["power"],
                hashRate=1000,
                hashRate_1m=1000,
                errorPercentage=0.2,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=30,
            )

        def log(message, level="info"):
            logs.append(message)
            if "holding after power retreat" in message:
                state["power"] = 20
                state["released"] = True

        with patched_io(get_info, set_settings):
            thread = _start_miner(
                "miner",
                stop_event,
                log,
                start_freq=500,
                start_volt=1100,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(state["calls"][1], (505, 1100))
        self.assertEqual(state["calls"][2], (500, 1100))
        self.assertEqual(state["calls"][3], (505, 1100))
        self.assertTrue(
            any("holding after power retreat" in message for message in logs)
        )

    def test_dead_hashrate_on_an_open_probe_restarts(self):
        state = {"frequency": 400, "voltage": 1100, "hash": 1000.0, "calls": []}
        restarts = []
        logs = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if freq > state["frequency"]:
                state["hash"] = 0.0
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=state["hash"],
                hashRate_1m=state["hash"],
                errorPercentage=0.2,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=30,
            )

        def restart(ip):
            restarts.append(ip)
            stop_event.set()
            return f"{ip} -> Restart initiated."

        def log(message, level="info"):
            logs.append(message)

        with patched_io(get_info, set_settings, restart):
            thread = _start_miner(
                "miner",
                stop_event,
                log,
                start_freq=400,
                start_volt=1100,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(state["calls"][:3], [(400, 1100), (405, 1100), (400, 1100)])
        self.assertEqual(restarts, ["miner"])
        self.assertFalse(any(volt > 1100 for _freq, volt in state["calls"]))
        self.assertFalse(
            any("holding for good hashrate" in message for message in logs)
        )

    def test_pool_down_holds_a_dead_board_without_restarting(self):
        state = {"frequency": 400, "voltage": 1100, "hash": 1000.0, "calls": []}
        restarts = []
        logs = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if freq > state["frequency"]:
                state["hash"] = 0.0
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=state["hash"],
                hashRate_1m=state["hash"],
                errorPercentage=0.2,
                actualFrequency=state["frequency"],
                poolDifficulty=0,
                temp=40,
                vrTemp=30,
            )

        def restart(ip):
            restarts.append(ip)
            return f"{ip} -> Restart initiated."

        def log(message, level="info"):
            logs.append(message)
            if "pool is down" in message:
                stop_event.set()

        with patched_io(get_info, set_settings, restart):
            thread = _start_miner(
                "miner",
                stop_event,
                log,
                start_freq=400,
                start_volt=1100,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(state["calls"], [(400, 1100), (405, 1100)])
        self.assertEqual(restarts, [])
        self.assertTrue(any("pool is down" in message for message in logs))

    def test_pool_down_does_not_treat_a_short_hashrate_as_silicon(self):
        state = {"frequency": 400, "voltage": 1100, "calls": []}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            state["frequency"] = freq
            state["voltage"] = volt
            if freq >= 410:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            rate = 855 if state["frequency"] <= 400 else 848
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=rate,
                hashRate_1m=rate,
                hashRate_10m=1000,
                expectedHashrate=1000,
                errorPercentage=0.2,
                actualFrequency=state["frequency"],
                poolDifficulty=0,
                temp=40,
                vrTemp=30,
            )

        with (
            patched_io(get_info, set_settings),
            mock.patch.object(autotune, "HASHRATE_1M_SETTLE_SECONDS", 0),
        ):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                start_freq=400,
                start_volt=1100,
                max_freq=800,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(any(freq >= 410 for freq, _volt in state["calls"]))
        self.assertTrue(all(volt == 1100 for _freq, volt in state["calls"]))

    def test_zero_minute_rate_fails_an_open_probe(self):
        state = {"frequency": 400, "voltage": 1100, "minute": 1000.0, "calls": []}
        logs = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if freq > state["frequency"] and volt == 1100:
                state["minute"] = 0.0
            if volt > 1100:
                stop_event.set()
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=1000,
                hashRate_1m=state["minute"],
                errorPercentage=0.2,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=30,
            )

        def log(message, level="info"):
            logs.append(message)

        with patched_io(get_info, set_settings):
            thread = _start_miner(
                "miner",
                stop_event,
                log,
                start_freq=400,
                start_volt=1100,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(any(volt > 1100 for _freq, volt in state["calls"]))
        self.assertFalse(
            any("holding for good hashrate" in message for message in logs)
        )

    def test_one_zero_minute_rate_does_not_lock_the_ceiling(self):
        state = {"frequency": 400, "voltage": 1400, "ups": 0, "calls": []}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if freq > state["frequency"]:
                state["ups"] += 1
            if freq >= 410:
                stop_event.set()
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            minute = 1000.0
            if state["frequency"] > 400 and state["ups"] == 1:
                minute = 0.0
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=1000,
                hashRate_1m=minute,
                errorPercentage=0.2,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=30,
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                start_volt=1400,
                max_volt=1400,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(state["calls"].count((405, 1400)), 2)
        self.assertIn((410, 1400), state["calls"])
        self.assertTrue(all(volt == 1400 for _freq, volt in state["calls"]))

    def test_a_second_zero_minute_rate_locks_the_ceiling(self):
        state = {"frequency": 400, "voltage": 1400, "calls": []}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            if freq > 405 or volt < 1400:
                stop_event.set()
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            minute = 0.0 if state["frequency"] > 400 else 1000.0
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=1000,
                hashRate_1m=minute,
                errorPercentage=0.2,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=30,
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                start_volt=1400,
                max_volt=1400,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(state["calls"].count((405, 1400)), 2)
        self.assertFalse(any(freq > 405 for freq, _volt in state["calls"]))
        self.assertTrue(any(volt < 1400 for _freq, volt in state["calls"]))

    def test_droop_after_a_voltage_raise_waits_out_the_settle(self):
        state = {
            "frequency": 500,
            "voltage": 1100,
            "calls": [],
            "raised_at": None,
            "dropped_at": None,
        }
        stop_event = threading.Event()
        runtime = dict(FAST_CONFIG)
        runtime["refresh_interval"] = 0.35
        runtime["monitor_interval"] = 0.02

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            now = time.time()
            state["calls"].append((freq, volt))
            if volt > 1100 and state["raised_at"] is None:
                state["raised_at"] = now
            if freq < 500 and state["dropped_at"] is None:
                state["dropped_at"] = now
                stop_event.set()
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                coreVoltageActual=1069,
                errorPercentage=8,
                hashRate=1000,
                hashRate_1m=1000,
                actualFrequency=state["frequency"],
                temp=40,
                vrTemp=30,
                power=12,
            )

        with patched_io(get_info, set_settings, runtime_config=runtime):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                start_freq=500,
                start_volt=1100,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(state["raised_at"])
        self.assertIsNotNone(state["dropped_at"])
        self.assertGreaterEqual(state["dropped_at"] - state["raised_at"], 0.2)
        self.assertIn((500, 1110), state["calls"])

    def test_a_window_that_ends_at_zero_does_not_raise_voltage(self):
        state = {"polls": 0, "hash": 1000.0, "calls": []}
        restarts = []
        stop_event = threading.Event()
        runtime = dict(FAST_CONFIG)
        runtime["monitor_interval"] = 0.01
        runtime["refresh_interval"] = 0.08

        def get_info(ip):
            state["polls"] += 1
            if state["polls"] >= 3:
                state["hash"] = 0.0
            return _info(
                frequency=500,
                voltage=1100,
                hashRate=state["hash"],
                hashRate_1m=state["hash"],
                errorPercentage=0.2,
                actualFrequency=500,
                temp=40,
                vrTemp=30,
                power=10,
            )

        def set_settings(ip, volt, freq):
            state["calls"].append((int(freq), int(volt)))
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def restart(ip):
            restarts.append(ip)
            stop_event.set()
            return f"{ip} -> Restart initiated."

        with (
            patched_io(get_info, set_settings, restart, runtime),
            mock.patch.object(autotune, "HASHRATE_1M_SETTLE_SECONDS", 0),
        ):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                start_freq=500,
                start_volt=1100,
                max_freq=500,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(restarts, ["miner"])
        self.assertTrue(state["calls"])
        self.assertTrue(all(volt == 1100 for _freq, volt in state["calls"]))

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
                thread = _start_miner(
                    "miner",
                    stop_event,
                    lambda *args: None,
                    start_freq=500,
                    start_volt=1100,
                )
                self.assertFalse(stop_event.wait(0.4))
                stop_event.set()
                thread.join(2)
            return calls

        at_cap = run_case(
            _info(frequency=500, voltage=1100, temp=45, vrTemp=40, power=25)
        )
        self.assertEqual(at_cap, [(500, 1100)])
        high_error = run_case(
            _info(
                frequency=500,
                voltage=1100,
                temp=45,
                vrTemp=40,
                power=12,
                errorPercentage=8,
            )
        )
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
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        runtime = dict(FAST_CONFIG)
        runtime["refresh_interval"] = 30
        with patched_io(get_info, set_settings, runtime_config=runtime):
            thread = _start_miner(
                "miner", stop_event, lambda *args: None, start_freq=500, start_volt=1100
            )
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [(500, 1100), (400, 1100)])

    def test_reversed_limits_skip_and_a_low_floor_still_clamps(self):
        clamped = autotune.clamp_limits(
            {
                "min_freq": 100,
                "max_freq": 500,
                "min_volt": 900,
                "max_volt": 1200,
            }
        )
        self.assertEqual(clamped["min_freq"], 350)
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
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

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
        self.assertEqual(calls[0], (350, 1100))
        self.assertFalse(any("reversed" in message for message in logs))

    def test_saved_setpoint_above_the_cap_is_clamped(self):
        calls = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        runtime = dict(FAST_CONFIG)
        runtime["miners"] = [
            {"ip": "miner", "last_good_freq": 1000, "last_good_volt": 1400}
        ]
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
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            info = _info(
                frequency=400, voltage=1100, temp=40, power=12, errorPercentage=0.2
            )
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
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

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
            thread = _start_miner(
                "miner", stop_event, lambda *args: None, start_freq=500, start_volt=1100
            )
            self.assertFalse(stop_event.wait(0.4))
            stop_event.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [(450, 1100)])

    def test_unconfirmed_apply_adopts_the_reported_clocks(self):
        calls = []
        logs = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def log(message, level="info"):
            logs.append(message)
            if "not confirmed" in message:
                stop_event.set()

        runtime = dict(FAST_CONFIG)
        runtime["refresh_interval"] = 0.05
        with patched_io(
            lambda ip: _info(frequency=390, voltage=1100, temp=45),
            set_settings,
            runtime_config=runtime,
        ):
            thread = _start_miner(
                "miner", stop_event, log, start_freq=400, start_volt=1100
            )
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls.count((400, 1100)), 1)
        self.assertEqual([freq for freq, _volt in calls if freq >= 400], [400])
        self.assertTrue(any("not confirmed" in message for message in logs))

    def test_fan_update_failure_does_not_raise_clocks(self):
        calls = []
        logs = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            info = _info()
            info["autofanspeed"] = 1
            info["fanspeed"] = 40
            return info

        def log(message, level="info"):
            logs.append(message)
            if "Fan update failed" in message or "manual 100%" in message:
                stop_event.set()

        def patch(ip, settings):
            return False, "offline"

        with patched_io(get_info, set_settings):
            with mock.patch.object(autotune, "patch_system", patch):
                thread = _start_miner("miner", stop_event, log)
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [])
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

        with patched_io(
            get_info,
            lambda ip, volt, freq: (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            ),
        ):
            thread = _start_miner("miner", stop_event, log)
            self.assertTrue(continued.wait(2))
            stop_event.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(
            any(
                "UNCAUGHT ERROR" in message and "sensor bus down" in message
                for message in logs
            )
        )

    def test_pll_under_the_cap_still_trims_voltage(self):
        state = {"frequency": 400, "voltage": 1100, "calls": []}
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            state["calls"].append((freq, volt))
            state["frequency"] = freq
            state["voltage"] = volt
            if volt < 1100:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                hashRate=1000,
                hashRate_1m=1000,
                errorPercentage=0.2,
                actualFrequency=398
                if state["frequency"] >= 400
                else state["frequency"],
                temp=40,
                vrTemp=30,
                power=12,
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                max_freq=400,
                start_freq=400,
                start_volt=1100,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn((400, 1090), state["calls"])
        self.assertFalse(any(freq > 400 for freq, _volt in state["calls"]))

    def test_hold_does_not_step_down_on_one_error_sample(self):
        held = threading.Event()
        calls = []
        stop_event = threading.Event()

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def log(message, level="info"):
            if "Holding 400 MHz" in message:
                held.set()

        def get_info(ip):
            return _info(
                frequency=400,
                voltage=1100,
                temp=40,
                vrTemp=30,
                power=12,
                hashRate=1000,
                hashRate_1m=1000,
                errorPercentage=9 if held.is_set() else 0.2,
            )

        runtime = dict(FAST_CONFIG)
        runtime["refresh_interval"] = 0.4
        runtime["monitor_interval"] = 0.02
        with patched_io(get_info, set_settings, runtime_config=runtime):
            thread = _start_miner(
                "miner",
                stop_event,
                log,
                max_freq=400,
                min_volt=1100,
                max_volt=1100,
                start_freq=400,
                start_volt=1100,
            )
            self.assertTrue(held.wait(2))
            time.sleep(0.15)
            stop_event.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [(400, 1100)])

    def test_clean_reject_sample_allows_the_frequency_back(self):
        state = {
            "accepted": 0,
            "rejected": 0,
            "frequency": 500,
            "voltage": 1100,
            "clean": False,
            "stepped_from": None,
            "calls": [],
        }
        stop_event = threading.Event()

        def get_info(ip):
            if state["clean"]:
                state["accepted"] += 80
            else:
                state["accepted"] += 80
                state["rejected"] += 16
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                temp=40,
                vrTemp=30,
                power=12,
                hashRate=1000,
                hashRate_1m=1000,
                errorPercentage=0.2,
                actualFrequency=state["frequency"],
                sharesAccepted=state["accepted"],
                sharesRejected=state["rejected"],
                sharesRejectedReasons=[
                    {"message": "Invalid", "count": state["rejected"]}
                ],
            )

        def set_settings(ip, volt, freq):
            freq = int(freq)
            volt = int(volt)
            if freq < state["frequency"]:
                state["stepped_from"] = state["frequency"]
                state["clean"] = True
            if (
                state["stepped_from"] is not None
                and freq >= state["stepped_from"]
                and state["clean"]
            ):
                stop_event.set()
            state["calls"].append((freq, volt))
            state["frequency"] = freq
            state["voltage"] = volt
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        with patched_io(get_info, set_settings):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                start_freq=500,
                start_volt=1100,
                max_freq=520,
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(state["stepped_from"])
        self.assertTrue(
            any(freq >= state["stepped_from"] for freq, _volt in state["calls"][1:])
        )

    def test_stop_during_a_probe_saves_the_previous_clocks(self):
        runtime = dict(FAST_CONFIG)
        runtime["miners"] = [{"ip": "miner", "wall_type": ""}]
        runtime["refresh_interval"] = 0.05
        state = {"frequency": 400, "voltage": 1100}
        updates = []
        stop_event = threading.Event()

        def log(message, level="info"):
            if "Confirmed 405" in message:
                stop_event.set()

        def update(ip, settings):
            updates.append((ip, dict(settings)))

        def set_settings(ip, volt, freq):
            state["frequency"] = int(freq)
            state["voltage"] = int(volt)
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            return _info(
                frequency=state["frequency"],
                voltage=state["voltage"],
                temp=40,
                vrTemp=30,
                power=12,
                hashRate=1000,
                hashRate_1m=1000,
                errorPercentage=0.2,
                actualFrequency=state["frequency"],
            )

        with (
            patched_io(get_info, set_settings, runtime_config=runtime),
            mock.patch.object(autotune, "update_miner", update),
        ):
            thread = _start_miner(
                "miner", stop_event, log, start_freq=400, start_volt=1100
            )
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(updates)
        self.assertEqual(updates[-1][1]["last_good_freq"], 400)
        self.assertEqual(updates[-1][1]["last_good_volt"], 1100)
        self.assertEqual(state["frequency"], 405)

    def test_lowered_temperature_cap_steps_down_without_a_restart(self):
        calls = []
        stop_event = threading.Event()
        runtime = dict(FAST_CONFIG)
        runtime["refresh_interval"] = 30
        runtime["miners"] = [
            {
                "ip": "miner",
                "min_freq": 400,
                "max_freq": 800,
                "min_volt": 1000,
                "max_volt": 1400,
                "max_temp": 40,
                "max_watts": 25,
                "max_vr_temp": 85,
            }
        ]

        def set_settings(ip, volt, freq):
            calls.append((int(freq), int(volt)))
            if len(calls) >= 2:
                stop_event.set()
            return (
                f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        def get_info(ip):
            return _info(frequency=500, voltage=1100, temp=50, vrTemp=40, power=12)

        with (
            patched_io(get_info, set_settings, runtime_config=runtime),
            mock.patch.object(autotune, "update_miner", lambda *args, **kwargs: None),
        ):
            thread = _start_miner(
                "miner",
                stop_event,
                lambda *args: None,
                start_freq=500,
                start_volt=1100,
                max_temp=80,
            )
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[0], (500, 1100))
        self.assertLess(calls[1][0], 500)


class InstallAndConfigTests(unittest.TestCase):
    def test_requirements_do_not_need_windows_blockers(self):
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "requirements.txt"
        )
        with open(path, encoding="utf-8") as handle:
            requirements = handle.read().lower()
        for blocked in ("tkinter", "gunicorn", "pandas", "flask"):
            self.assertNotIn(blocked, requirements)

    def test_scaling_table_is_gone(self):
        self.assertFalse(hasattr(autotune, "load_scaling_table"))
        csv_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "cpu_voltage_scaling_safeguards.csv",
        )
        self.assertFalse(os.path.exists(csv_path))

    def test_corrupt_config_keeps_last_good_and_does_not_wipe_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            old_path = config.CONFIG_FILE
            old_last = config._last_good_config
            old_corrupt = config._config_corrupt
            try:
                config.CONFIG_FILE = path
                config._last_good_config = None
                config._config_corrupt = False
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
                self.assertIn("damaged", config.config_problem())
                self.assertFalse(config.save_config(config.get_default_config()))
                with open(path, encoding="utf-8") as handle:
                    self.assertTrue(handle.read().startswith("{broken"))
            finally:
                config.CONFIG_FILE = old_path
                config._last_good_config = old_last
                config._config_corrupt = old_corrupt

    def test_new_miner_gets_default_max_temp_and_scan_type(self):
        self.assertEqual(
            config.miner_type_from_info({"ASICModel": "BM1370", "boardVersion": "601"}),
            "BM1370 601",
        )
        self.assertEqual(config.miner_type_from_info({}), "Unknown")
        self.assertTrue(
            config.is_gamma_601({"ASICModel": "bm1370", "boardVersion": 601})
        )
        self.assertFalse(
            config.is_gamma_601({"ASICModel": "BM1370", "boardVersion": "602"})
        )
        self.assertFalse(
            config.is_gamma_601({"ASICModel": "BM1366", "boardVersion": "601"})
        )
        self.assertEqual(autotune.normalize_input_voltage(4900), 4.9)
        self.assertEqual(autotune.normalize_input_voltage(5.01), 5.01)
        self.assertIsNone(autotune.normalize_input_voltage(None))
        clamped = autotune.clamp_limits(
            {
                "min_freq": 100,
                "max_freq": 2000,
                "min_volt": 900,
                "max_volt": 1600,
            }
        )
        self.assertEqual(clamped["min_freq"], 350)
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
                self.assertTrue(miners[0]["enabled"])
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
                return FakeResponse(
                    {
                        "ASICModel": "BM1370",
                        "boardVersion": "601",
                        "hostname": "goose",
                    }
                )
            raise config.requests.exceptions.RequestException("no miner")

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            old_path = config.CONFIG_FILE
            old_last = config._last_good_config
            try:
                config.CONFIG_FILE = path
                config._last_good_config = None
                config.save_config(config.get_default_config())
                with mock.patch("config.requests.get", side_effect=fake_get) as probed:
                    found = config.detect_miners("192.168.0.2", "192.168.0.3")
                self.assertEqual([miner["ip"] for miner in found], ["192.168.0.3"])
                self.assertEqual(
                    [call.kwargs["timeout"] for call in probed.call_args_list],
                    [config.SYSTEM_INFO_TIMEOUT, config.SYSTEM_INFO_TIMEOUT],
                )
                self.assertEqual(found[0]["type"], "BM1370 601")
                self.assertEqual(found[0]["nickname"], "goose")
                self.assertTrue(found[0]["enabled"])
                self.assertEqual(found[0]["max_freq"], 1100)
                self.assertEqual(found[0]["start_volt"], 1150)
                stored = config.get_miners()
                self.assertEqual([miner["ip"] for miner in stored], ["192.168.0.3"])
            finally:
                config.CONFIG_FILE = old_path
                config._last_good_config = old_last

    def test_scan_skips_a_non_json_page_and_keeps_going(self):
        class FakeResponse:
            def __init__(self, payload, broken=False):
                self.status_code = 200
                self._payload = payload
                self._broken = broken

            def json(self):
                if self._broken:
                    raise json.JSONDecodeError("Expecting value", "<html>", 0)
                return self._payload

        def fake_get(url, timeout=1):
            if url.endswith("192.168.0.2/api/system/info"):
                return FakeResponse(None, broken=True)
            if url.endswith("192.168.0.3/api/system/info"):
                return FakeResponse(
                    {
                        "ASICModel": "BM1370",
                        "boardVersion": "601",
                        "hostname": "goose",
                    }
                )
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
            finally:
                config.CONFIG_FILE = old_path
                config._last_good_config = old_last

    def test_system_info_returns_an_error_string_for_non_json(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                raise json.JSONDecodeError("Expecting value", "<html>", 0)

        with mock.patch("autotune.requests.get", return_value=FakeResponse()):
            result = autotune.get_system_info("10.0.0.8")
        self.assertIsInstance(result, str)
        self.assertIn("Error fetching system info", result)

    def test_name_prefers_typed_nickname_then_hostname(self):
        info = {"hostname": "goose", "ASICModel": "BM1370", "boardVersion": "601"}
        self.assertEqual(config.miner_name_from_info(info, "10.0.0.4"), "goose")
        self.assertEqual(
            config.miner_name_from_info(info, "10.0.0.4", "  custom "), "custom"
        )
        self.assertEqual(config.miner_name_from_info({}, "10.0.0.4"), "Miner-10.0.0.4")
        self.assertEqual(
            config.adopted_hostname("Miner-10.0.0.4", "10.0.0.4", info), "goose"
        )
        self.assertEqual(config.adopted_hostname("", "10.0.0.4", info), "goose")
        self.assertIsNone(config.adopted_hostname("custom", "10.0.0.4", info))
        self.assertIsNone(config.adopted_hostname("goose", "10.0.0.4", info))
        self.assertIsNone(config.adopted_hostname("Miner-10.0.0.4", "10.0.0.4", {}))
        self.assertTrue(
            config.new_miner_record("BM1370 601", "10.0.0.4", "goose")["enabled"]
        )

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

    def test_scan_keeps_edits_made_while_it_is_running(self):
        class FakeResponse:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload

            def json(self):
                return self._payload

        def fake_get(url, timeout=1):
            if url.endswith("192.168.0.2/api/system/info"):
                return FakeResponse(
                    {
                        "ASICModel": "BM1370",
                        "boardVersion": "601",
                        "hostname": "new-goose",
                    }
                )
            raise config.requests.exceptions.RequestException("no miner")

        def on_progress(index, _total, _ip):
            if index == 1:
                config.remove_miner("192.168.0.1")

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            old_path = config.CONFIG_FILE
            old_last = config._last_good_config
            try:
                config.CONFIG_FILE = path
                config._last_good_config = None
                saved = config.get_default_config()
                saved["miners"] = [
                    config.new_miner_record(
                        "BM1370 601", "192.168.0.1", "old-goose", saved
                    ),
                ]
                config.save_config(saved)
                with mock.patch("config.requests.get", side_effect=fake_get):
                    found = config.detect_miners(
                        "192.168.0.1",
                        "192.168.0.2",
                        on_progress=on_progress,
                    )
                stored = config.get_miners()
                self.assertEqual([miner["ip"] for miner in found], ["192.168.0.2"])
                self.assertEqual([miner["ip"] for miner in stored], ["192.168.0.2"])
                self.assertEqual(stored[0]["nickname"], "new-goose")
            finally:
                config.CONFIG_FILE = old_path
                config._last_good_config = old_last

    def test_row_color_follows_phase_and_limits(self):
        from dashboard import blank_miner_row, row_state_tag

        self.assertEqual(
            blank_miner_row("gamma", "10.0.0.8"),
            {
                "name": "gamma",
                "ip": "10.0.0.8",
                "freq": "-",
                "mv": "-",
                "vin": "-",
                "asic": "-",
                "vr": "-",
                "hash": "-",
                "watts": "-",
                "jth": "-",
                "best": "-",
                "session": "-",
                "shares": "-",
                "up": "-",
                "phase": "-",
                "error": "-",
                "setpoint_freq": "-",
                "setpoint_volt": "-",
                "setpoint_limit": "",
                "tag": "idle",
                "up_seconds": None,
                "mv_alert": False,
                "asic_level": "",
                "vr_level": "",
                "error_alert": False,
                "watts_alert": False,
                "vin_alert": False,
                "name_title": "",
                "firmware_update": "",
                "mv_title": "",
                "hash_title": "",
                "shares_title": "",
                "reason": "",
                "pool": "",
                "fallback": False,
                "wifi": "",
                "wifi_weak": False,
                "power_fault": False,
                "overheat": False,
                "best_exact": None,
            },
        )
        self.assertEqual(row_state_tag("hold", "60", "1.00%", 66, 2), "hold")
        self.assertEqual(row_state_tag("climb", "60", "1", 66, 2), "climb")
        self.assertEqual(row_state_tag("trim", "60", "1", 66, 2), "trim")
        self.assertEqual(row_state_tag("climb", "70", "1", 66, 2), "alert")
        self.assertEqual(row_state_tag("hold", "60", "3%", 66, 2), "alert")
        self.assertEqual(row_state_tag("offline", "40", "0", 66, 2), "alert")
        self.assertEqual(row_state_tag("stopped", "60", "1", 66, 2), "idle")
        self.assertEqual(row_state_tag("stopped", "70", "1", 66, 2), "alert")
        self.assertEqual(row_state_tag("-", "-", "-", None, None), "idle")

    def test_log_replaces_longer_ip_before_shorter_prefix(self):
        from dashboard import replace_ips_with_names

        names = {
            "192.168.8.10": "short",
            "192.168.8.100": "long",
            "192.168.8.101": "Miner-192.168.8.101",
        }
        text = replace_ips_with_names(
            "192.168.8.100 -> holding\n192.168.8.10 -> holding\n192.168.8.101 -> holding",
            names,
        )
        self.assertEqual(
            text,
            "long -> holding\nshort -> holding\n192.168.8.101 -> holding",
        )

    def test_dashboard_page_keeps_table_and_log(self):
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "web", "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        with open(os.path.join(root, "web", "app.js"), encoding="utf-8") as handle:
            script = handle.read()
        with open(os.path.join(root, "web", "app.css"), encoding="utf-8") as handle:
            styles = handle.read()
        self.assertIn('id="miner-body"', html)
        self.assertIn('id="log"', html)
        self.assertIn("log.children.length > 500", script)
        self.assertIn("Reset All to Baseline", html)
        self.assertIn('class="toolbar"', html)
        self.assertNotIn('class="danger"', html)
        menu = html.split('id="settings-menu"', 1)[1].split('id="row-menu"', 1)[0]
        self.assertLess(menu.find("AutoTuner Settings"), menu.find("Global Settings"))
        self.assertLess(menu.find("Global Settings"), menu.find("Restart All Miners"))
        self.assertLess(
            menu.find("Restart All Miners"), menu.find("Reset All to Baseline")
        )
        self.assertIn('id="restart-all"', menu)
        self.assertIn("restart_all_miners", script)
        self.assertIn('id="reset"', menu)
        for label in (
            "Edit Miner Settings",
            "Restart Miner",
            "Remove Miner",
        ):
            self.assertIn(label, html)
        self.assertNotIn("Open Miner Web UI", html)
        self.assertNotIn(">Refresh<", html)
        self.assertNotIn("open_miner_page", script)
        self.assertNotIn("refresh_miner", script)
        self.assertIn('data-sort="best"', html)
        self.assertEqual(html.count("data-sort="), 10)
        for label in (
            "Name",
            "Clock",
            "Temp",
            "Hash",
            "Power",
            "Best",
            "Shares",
            "Up",
            "Phase",
            "Setpoint",
        ):
            self.assertIn(f">{label}</button>", html)
        self.assertIn(
            'title="Last saved frequency and voltage, and the limit that stopped the climb"',
            html,
        )
        for column in (
            "name",
            "freq",
            "asic",
            "hash",
            "watts",
            "best",
            "shares",
            "up",
            "phase",
            "setpoint",
        ):
            self.assertIn(f'data-sort="{column}"', html)
        for column in ("ip", "mv", "vin", "vr", "jth", "session", "error"):
            self.assertNotIn(f'data-sort="{column}"', html)
        self.assertIn("°C", script)
        self.assertIn("MHz", script)
        self.assertNotIn("min-width: 1480px", styles)
        self.assertNotIn("daily_reset", html)
        self.assertNotIn("daily_reset", script)
        self.assertIn("flatline_detection_enabled", html)
        self.assertIn("Groundhog Gamma Tuner", html)
        self.assertIn('id="settings-menu"', html)
        self.assertIn('id="scan-open"', html)
        brand = html.split('class="brand"', 1)[1].split('id="status-pill"', 1)[0]
        self.assertNotIn('id="run"', brand)
        identity = html.split('class="identity"', 1)[1].split('class="commands"', 1)[0]
        self.assertIn('id="status-pill"', identity)
        self.assertIn('aria-live="polite"', identity)
        self.assertNotIn('id="run"', identity)
        commands = html.split('class="commands"', 1)[1].split("</header>", 1)[0]
        self.assertNotIn('id="status-pill"', commands)
        self.assertLess(commands.find('id="run"'), commands.find('id="scan-open"'))
        self.assertNotIn('id="updated"', commands)
        toolbar = commands.split('class="toolbar"', 1)[1].split(
            'class="header-tools"', 1
        )[0]
        self.assertNotIn('id="run"', toolbar)
        statusbar = html.split('class="statusbar"', 1)[1].split("</footer>", 1)[0]
        self.assertLess(
            statusbar.find('id="updated"'), statusbar.find('id="network-diff"')
        )
        self.assertIn("stratum.ckpool.org", statusbar)
        self.assertIn("public-pool.io", statusbar)
        self.assertNotIn('class="group"', html)
        self.assertNotIn('id="start"', html)
        self.assertNotIn('id="stop"', html)
        self.assertNotIn("start_label", script)
        self.assertIn("Stopping…", script)
        self.assertIn('id="network-diff"', html)
        self.assertIn("stratum.ckpool.org", html)
        self.assertIn("public-pool.io", html)
        self.assertNotIn('id="add-menu"', html)
        self.assertNotIn('id="add-open"', html)
        self.assertNotIn("Add Miner", html)
        self.assertNotIn("Add by IP", html)
        self.assertNotIn("add_miner_address", script)
        self.assertNotIn('id="detail"', html)
        self.assertNotIn('id="detail-restart"', html)
        self.assertNotIn('id="detail-chart"', html)
        self.assertNotIn('id="detail-csv"', html)
        self.assertNotIn("save_history_csv", script)
        self.assertNotIn("renderDetail", script)
        self.assertIn('id="fleet"', html)
        self.assertNotIn('id="odds"', html)
        self.assertNotIn("Climbing", script)
        self.assertNotIn("1 in ", script)
        self.assertIn("max_droop_mv", html)
        self.assertIn("vr_temp_tolerance", html)
        self.assertIn("ceiling_soak_seconds", html)
        self.assertNotIn('id="row-actions"', html)
        self.assertIn('classList.toggle("fullscreen"', script)
        self.assertIn("body.fullscreen .toolbar", styles)
        self.assertNotIn("miner-body", styles.split("body.fullscreen")[1])

    def test_saved_config_drops_daily_reset_and_keeps_flatline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            old_path = config.CONFIG_FILE
            old_last = config._last_good_config
            try:
                config.CONFIG_FILE = path
                config._last_good_config = None
                saved = config.get_default_config()
                saved["daily_reset_enabled"] = True
                saved["daily_reset_time"] = "03:00"
                saved["flatline_detection_enabled"] = True
                config.save_config(saved)
                loaded = config.load_config()
                self.assertNotIn("daily_reset_enabled", loaded)
                self.assertNotIn("daily_reset_time", loaded)
                self.assertTrue(loaded["flatline_detection_enabled"])
            finally:
                config.CONFIG_FILE = old_path
                config._last_good_config = old_last

    def test_parse_autotuner_value_clamps_frequency_and_voltage(self):
        from dashboard import parse_autotuner_value

        self.assertEqual(parse_autotuner_value("min_freq", "50"), 350)
        self.assertEqual(parse_autotuner_value("max_freq", "2000"), 1100)
        self.assertEqual(parse_autotuner_value("start_freq", "700"), 700)
        self.assertEqual(parse_autotuner_value("min_volt", "900"), 1000)
        self.assertEqual(parse_autotuner_value("max_volt", "1600"), 1400)
        self.assertEqual(parse_autotuner_value("start_volt", "  "), "")
        self.assertEqual(parse_autotuner_value("max_temp", "68"), 68)
        self.assertEqual(parse_autotuner_value("max_temp", "68.5"), 68.5)
        self.assertEqual(parse_autotuner_value("max_watts", "50.25"), 50.25)
        self.assertEqual(parse_autotuner_value("max_vr_temp", "88.5"), 88.5)
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
                stored = {
                    miner["ip"]: miner["nickname"] for miner in config.get_miners()
                }
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
