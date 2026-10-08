"""Edge cases of the tuner's helpers and its fast-start ramp."""

import threading
import unittest
from unittest import mock

import autotune
from boards import GAMMA_601
from test_autotune import _info, _limits, _retreat

RAMP_LIMITS = {
    "min_freq": 400,
    "max_freq": 1100,
    "min_volt": 1000,
    "max_volt": 1300,
    "max_temp": 70,
    "max_vr_temp": 95,
    "max_watts": 50,
}


class SessionEventTests(unittest.TestCase):
    def test_a_new_session_gets_a_fresh_shared_stop(self):
        old = autotune._default_stop_event
        self.addCleanup(setattr, autotune, "_default_stop_event", old)
        autotune.begin_autotune_session()
        fresh = autotune._default_stop_event
        self.assertIsNot(fresh, old)
        autotune.stop_autotuning()
        self.assertTrue(fresh.is_set())


class BaselineTests(unittest.TestCase):
    def test_a_miner_without_an_address_is_not_a_failure(self):
        self.assertTrue(autotune._reset_one_miner_to_baseline({}, print))

    def test_one_at_a_time_waits_between_miners(self):
        with (
            mock.patch.object(
                autotune, "_reset_one_miner_to_baseline", return_value=False
            ),
            mock.patch.object(autotune.time, "sleep") as sleep,
        ):
            failed = autotune.reset_miners_to_baseline(
                [{"ip": "a"}, {"ip": "b"}], print, stagger_seconds=2
            )
        self.assertEqual(failed, 2)
        sleep.assert_called_once_with(2)


class RecordTests(unittest.TestCase):
    def test_running_limits_ignore_anything_but_a_record(self):
        limits = {"min_freq": 400}
        self.assertIs(autotune.refresh_running_limits(limits, None), limits)

    def test_unreadable_or_zero_caps_use_the_board_cap(self):
        self.assertEqual(autotune._record_float({"x": "hot"}, "x", 7), 7)
        hard = GAMMA_601.limits.max_core_amps
        self.assertEqual(autotune.core_amps_cap({"max_core_amps": 0}), hard)

    def test_a_record_is_found_past_other_miners(self):
        saved = {"miners": [{"ip": "a"}, {"ip": "b", "nickname": "two"}]}
        with mock.patch.object(autotune, "load_config", return_value=saved):
            self.assertEqual(autotune._miner_record("b")["nickname"], "two")


class ReasonTests(unittest.TestCase):
    def test_a_frequency_cap_is_not_a_wall(self):
        self.assertEqual(autotune.wall_type_from_reason("frequency cap"), "")

    def test_no_overshoot_sheds_one_step(self):
        self.assertEqual(autotune._thermal_frequency_steps(0, 3), 1)

    def test_voltage_lands_on_its_floor(self):
        self.assertEqual(
            autotune._step_down(400, 1005, 400, 1000, 5, 10),
            (400, 1000, "step voltage down"),
        )

    def test_odd_flag_values(self):
        self.assertTrue(autotune._overheat_mode_set(object()))
        self.assertTrue(autotune._power_fault_set(1))

    def test_a_low_input_or_power_fault_is_an_immediate_retreat(self):
        self.assertTrue(_retreat(input_voltage=4.5))
        self.assertTrue(_retreat(power_fault=1))

    def test_quality_retreats_need_a_frequency_step(self):
        self.assertFalse(autotune._quality_frequency_retreat("holding"))


class HashrateTests(unittest.TestCase):
    def test_more_than_all_errors_is_no_good_hashrate(self):
        self.assertEqual(autotune.good_hashrate(1000, 150), 0)

    def test_missing_readings(self):
        self.assertIsNone(autotune.measured_hashrate(None))
        self.assertFalse(autotune.hashrate_well_below_expected(None, 1000))
        self.assertIsNone(autotune.good_hashrate_held(None, 1000, 10))

    def test_a_negative_band_counts_as_none(self):
        self.assertTrue(autotune.good_hashrate_held(1000, 1000, -5))
        self.assertFalse(autotune.good_hashrate_held(1000, 999, -5))

    def test_a_step_of_nothing_counts_as_one_mhz(self):
        self.assertEqual(autotune.expected_step_gain(0, 2.0), 2.0)

    def test_stale_rejects_and_reason_counts(self):
        self.assertTrue(autotune._stale_reject_reason("Stale job"))
        reasons = [1, {"message": "x", "count": None}, {"message": "Stale", "count": 2}]
        self.assertEqual(autotune._reject_reason_counts(reasons), {"Stale": 2})


class ShareAndCapTests(unittest.TestCase):
    def test_a_cooled_thermal_block_clears(self):
        self.assertTrue(
            autotune.frequency_block_cleared(800, 1200, 1200, True, True, False, None)
        )

    def test_reject_share_handles_odd_counts(self):
        self.assertIsNone(autotune.reject_share(None, 1))
        self.assertEqual(autotune.reject_share(3, 1, stale_delta=None), 0.25)
        self.assertIsNone(autotune.reject_share(0, 1, stale_delta=5))

    def test_odd_margins_count_as_none(self):
        self.assertTrue(autotune._at_or_under(10, 10, -1))
        self.assertTrue(autotune._at_or_under(10, 10, 50))

    def test_an_efficiency_step_without_noise_allowance(self):
        self.assertFalse(autotune.efficiency_step_paid(10, 1000, 11, 1000, None))

    def test_a_clock_on_its_cap_at_minimum_voltage_holds(self):
        self.assertEqual(
            autotune._apply_frequency_cap(800, 1000, 400, 800, 1000, 1300, 5, 10)[2],
            "frequency ceiling",
        )


class DecisionEdgeTests(unittest.TestCase):
    def test_a_trim_lands_on_the_voltage_floor(self):
        self.assertEqual(
            autotune.decide_adjustment(
                **_limits(phase="trim", current_voltage=1005, hash_rate=1000)
            ),
            (500, 1000, "trim voltage"),
        )


class MinerIoTests(unittest.TestCase):
    def test_a_failed_patch_reports_why(self):
        with mock.patch.object(
            autotune.requests,
            "patch",
            side_effect=autotune.requests.exceptions.ConnectionError("down"),
        ):
            self.assertEqual(autotune.patch_system("a", {}), (False, "down"))
            self.assertIn(
                "Error setting system settings",
                autotune.set_system_settings("a", 1100, 500),
            )

    def test_restart_reports_success_and_failure(self):
        with mock.patch.object(autotune.requests, "post") as post:
            self.assertEqual(autotune.restart_bitaxe("a"), "a -> Restart initiated.")
        post.assert_called_once_with("http://a/api/system/restart", timeout=10)
        with mock.patch.object(
            autotune.requests,
            "post",
            side_effect=autotune.requests.exceptions.Timeout("slow"),
        ):
            self.assertEqual(
                autotune.restart_bitaxe("a"), "a -> Error restarting system: slow"
            )

    def test_reported_numbers_and_restores(self):
        self.assertFalse(autotune._numbers_match(None, 500))
        self.assertFalse(autotune._restore_is_confirmed({"restore": True}, (500, 1100)))

    def test_fan_report_must_be_manual_and_full(self):
        self.assertFalse(autotune._fan_is_manual_full(None))
        self.assertFalse(autotune._fan_is_manual_full({"fanspeed": 100}))
        self.assertFalse(
            autotune._fan_is_manual_full({"autofanspeed": 1, "fanspeed": 100})
        )
        self.assertTrue(
            autotune._fan_is_manual_full({"autofanspeed": 0, "fanspeed": 100})
        )

    def test_switches_saved_as_text(self):
        self.assertFalse(autotune.fast_start_enabled({"fast_start": "off"}))
        self.assertTrue(autotune.fast_start_enabled({"fast_start": "yes"}))
        self.assertFalse(
            autotune.continue_from_live_enabled({"continue_from_live": "0"})
        )

    def test_no_reply_means_no_live_clocks_or_opening_change(self):
        self.assertIsNone(autotune.live_start_clocks(None, RAMP_LIMITS))
        self.assertEqual(
            autotune.opening_setpoint(
                500, 1100, None, RAMP_LIMITS, 5, 10, 3, 3, 4.9, 2.0, 40
            ),
            (500, 1100, ""),
        )


class RampHelperTests(unittest.TestCase):
    def test_no_target_without_readings_or_a_positive_power_scale(self):
        sample = {
            "frequency": 500,
            "voltage": 1100,
            "power": 15,
            "temp": 50,
            "vr_temp": 50,
        }
        missing = dict(sample, temp=None)
        self.assertIsNone(
            autotune.ramp_target(missing, [], RAMP_LIMITS, (500, 1100), 5, 10)
        )
        low = dict(sample, power=GAMMA_601.board_power_w)
        self.assertIsNone(
            autotune.ramp_target(low, [], RAMP_LIMITS, (500, 1100), 5, 10)
        )

    def test_each_hard_breach_has_its_own_name(self):
        clocks = (600, 1150)
        cases = {
            "overheat mode": {"overheat_mode": 1},
            "power fault": {"power_fault": 1},
            "the ASIC is off": {"power": 3},
            "the core current limit": {"current": 40000},
        }
        for expected, changes in cases.items():
            with self.subTest(expected):
                info = _info(600, 1150, **changes)
                self.assertEqual(
                    autotune._ramp_breach(info, clocks, RAMP_LIMITS, 4.9, 29.0),
                    expected,
                )

    def test_a_dead_board_or_a_short_minute_rate(self):
        dead = _info(600, 1150, hashRate=0, hashRate_1m=0, sharesAccepted=5)
        with mock.patch.object(autotune, "pool_is_down", return_value=False):
            self.assertEqual(
                autotune._ramp_quality({"frequency": 600}, dead, 2.0, 0), "dead"
            )
        short = _info(600, 1150, hashRate_1m=100)
        self.assertEqual(
            autotune._ramp_quality({"frequency": 600, "error": 0}, short, 2.0, 120),
            "low hashrate",
        )


class RampSettleTests(unittest.TestCase):
    def settle(self, replies, clock_values=None, stop=False):
        replies = iter(replies)
        times = iter(clock_values or [0] * 50)
        with (
            mock.patch.object(autotune, "_wait", return_value=stop),
            mock.patch.object(
                autotune, "get_system_info", side_effect=lambda ip: next(replies)
            ),
            mock.patch.object(autotune.time, "time", side_effect=lambda: next(times)),
        ):
            return autotune._ramp_settle(
                "a", (600, 1150), RAMP_LIMITS, 0, 10, 4.9, threading.Event()
            )

    def test_a_stop_ends_the_settle(self):
        self.assertIsNone(self.settle([], stop=True))

    def test_a_miner_that_stops_answering(self):
        result = self.settle(["timeout", "timeout"], [0, 1, 100])
        self.assertEqual(result, (None, None, "the miner stopped answering"))

    def test_a_miner_that_never_takes_the_clocks(self):
        wrong = _info(500, 1100)
        result = self.settle([wrong, wrong], [0, 1, 100])
        self.assertEqual(result[2], "the miner did not take the new clocks")


class RampSessionTests(unittest.TestCase):
    def ramp(self, settles, qualities=(), targets=(), applied="ok", moves=None):
        settles, qualities, targets = iter(settles), iter(qualities), iter(targets)
        patches = [
            mock.patch.object(autotune, "set_system_settings", return_value=applied),
            mock.patch.object(
                autotune, "settings_were_applied", side_effect=lambda m: m == "ok"
            ),
            mock.patch.object(
                autotune, "_ramp_settle", side_effect=lambda *a, **k: next(settles)
            ),
            mock.patch.object(
                autotune,
                "_ramp_quality",
                side_effect=lambda *a, **k: next(qualities, ""),
            ),
            mock.patch.object(
                autotune, "ramp_target", side_effect=lambda *a, **k: next(targets, None)
            ),
            mock.patch.object(autotune, "_publish_status"),
        ]
        if moves is not None:
            patches.append(mock.patch.object(autotune, "RAMP_MAX_MOVES", moves))
        log = []
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            if moves is not None:
                with patches[6]:
                    result = self._run(log)
            else:
                result = self._run(log)
        return result, log

    def _run(self, log):
        return autotune.ramp_session(
            "a",
            500,
            1100,
            {"x": 1},
            RAMP_LIMITS,
            0,
            10,
            5,
            10,
            4.9,
            2.0,
            threading.Event(),
            lambda message, level="info": log.append(message),
        )

    def sample(self):
        return {"frequency": 500, "voltage": 1100, "temp": 50, "power": 15}

    def test_a_refused_write_ends_on_the_start(self):
        result, log = self.ramp([], applied="refused")
        self.assertEqual(result[:2], (500, 1100))
        self.assertTrue(any("refused a change" in line for line in log))

    def test_a_stop_while_settling_ends_the_ramp(self):
        result, _log = self.ramp([None])
        self.assertIsNone(result)

    def test_a_silent_miner_steps_back(self):
        result, log = self.ramp([(None, None, "the miner stopped answering")])
        self.assertEqual(result, (500, 1100, {"x": 1}))
        self.assertTrue(any("stopped answering" in line for line in log))

    def test_a_dead_board_steps_back(self):
        result, log = self.ramp([(self.sample(), {"y": 2}, "")], qualities=["dead"])
        self.assertEqual(result[:2], (500, 1100))
        self.assertTrue(any("stopped hashing" in line for line in log))

    def test_jumps_stay_under_a_clock_that_failed(self):
        good = (self.sample(), {"y": 2}, "")
        settles = [good, good, good, good]
        # 500 good, jump to 700 fails at max voltage, split to 600 good, then a
        # target above the failed 700 MHz is held under it.
        qualities = ["", "errors", "", ""]
        targets = [(700, 1300, 900, 64.0), (900, 1300, 950, 66.0)]
        with mock.patch.dict(RAMP_LIMITS, {"max_volt": 1300}):
            result, log = self.ramp(settles, qualities, targets)
        self.assertTrue(any("Trying 600 MHz" in line for line in log))
        self.assertTrue(any("Next 645 MHz" in line for line in log))
        self.assertEqual(result[:2], (645, 1300))

    def test_the_ramp_ends_after_its_last_move(self):
        good = (self.sample(), {"y": 2}, "")
        targets = [(505, 1100, 900, 60.0)]
        result, log = self.ramp([good], targets=targets, moves=1)
        self.assertEqual(result[:2], (500, 1100))
        self.assertTrue(any("Fast start done" in line for line in log))


if __name__ == "__main__":
    unittest.main()
