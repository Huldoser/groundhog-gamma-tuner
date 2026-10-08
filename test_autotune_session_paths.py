"""Rare turns of a tuning session: silent miners, refused writes, and stops mid-wait.

A FakeMiner keeps the clocks it was last given. run_session drives a real
session against it and can stop the session at the wait right after a given
status or log line, so each "stop while waiting here" path is reached the
way a user's Stop would reach it.
"""

import contextlib
import threading
import unittest
from unittest import mock

import autotune
from test_autotune import FAST_CONFIG, _info, _start_miner, patched_io

# A reply field set to MISSING is left out of the reply.
MISSING = object()


class FakeMiner:
    """A miner whose clocks follow every write it accepts."""

    def __init__(self, frequency=400, voltage=1100, reading=None, refuse=None):
        self.frequency = frequency
        self.voltage = voltage
        self.reading = reading or (lambda miner: {})
        self.refuse = refuse or (lambda frequency, voltage: False)
        # False: writes are accepted but the miner keeps reporting old clocks.
        self.echo = True
        self.writes = []
        self.polls = 0

    def get_info(self, ip):
        self.polls += 1
        extra = self.reading(self)
        if isinstance(extra, Exception):
            raise extra
        if not isinstance(extra, dict):
            return extra
        info = _info(frequency=self.frequency, voltage=self.voltage)
        info.update(extra)
        for key in [key for key, value in info.items() if value is MISSING]:
            del info[key]
        return info

    def set_settings(self, ip, volt, freq):
        freq, volt = int(freq), int(volt)
        self.writes.append((freq, volt))
        if self.refuse(freq, volt):
            return f"{ip} -> Error setting system settings: refused"
        if self.echo:
            self.frequency, self.voltage = freq, volt
        return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"


class Run:
    def __init__(self):
        self.logs = []
        self.statuses = []


def run_session(
    test,
    miner,
    runtime=None,
    stop_on_status=None,
    stop_on_log=None,
    seconds=1.5,
    patches=(),
    **limits,
):
    """Run one session. Stop at the first wait after a matching status or log."""
    stop = threading.Event()
    armed = threading.Event()
    run = Run()
    real_wait = autotune._wait
    real_publish = autotune._publish_status

    def wait(event, seconds):
        if armed.is_set():
            event.set()
            return True
        return real_wait(event, seconds)

    def publish(ip, **fields):
        real_publish(ip, **fields)
        run.statuses.append(fields)
        if stop_on_status is not None and fields.get("reason") == stop_on_status:
            armed.set()

    def log(message, level="info"):
        run.logs.append(message)
        if stop_on_log is not None and stop_on_log in str(message):
            armed.set()

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patched_io(miner.get_info, miner.set_settings, runtime_config=runtime)
        )
        stack.enter_context(mock.patch.object(autotune, "_wait", wait))
        stack.enter_context(mock.patch.object(autotune, "_publish_status", publish))
        for patch in patches:
            stack.enter_context(patch)
        thread = _start_miner("miner", stop, log, **limits)
        thread.join(seconds)
        stop.set()
        thread.join(2)
    test.assertFalse(thread.is_alive())
    return run


def _stopped(run):
    return any(line.endswith("Autotuning stopped.") for line in run.logs)


class OpeningTests(unittest.TestCase):
    def test_a_stop_just_after_the_stagger_reads_nothing(self):
        miner = FakeMiner()
        stop = threading.Event()
        logs = []

        def wait(event, seconds):
            event.set()
            return False

        with (
            patched_io(miner.get_info, miner.set_settings),
            mock.patch.object(autotune, "_wait", wait),
        ):
            _start_miner("miner", stop, lambda m, level="info": logs.append(m)).join(2)
        self.assertEqual(miner.polls, 0)
        self.assertTrue(logs[-1].endswith("Autotuning stopped."))

    def test_a_silent_miner_can_be_stopped_before_it_answers(self):
        miner = FakeMiner(reading=lambda m: "timed out")
        run = run_session(self, miner, stop_on_log="No reply")
        self.assertEqual(miner.writes, [])
        self.assertTrue(_stopped(run))

    def test_newer_firmware_tunes_with_a_warning(self):
        miner = FakeMiner(reading=lambda m: {"version": "v2.99.0"})
        run = run_session(self, miner, stop_on_log="Applied settings")
        self.assertTrue(any("v2.99.0" in line for line in run.logs))

    def test_blank_start_clocks_open_at_the_minimum(self):
        miner = FakeMiner()
        run_session(
            self,
            miner,
            stop_on_log="Applied settings",
            start_freq=None,
            start_volt=None,
        )
        self.assertEqual(miner.writes[0], (400, 1000))

    def test_a_stop_while_waiting_for_the_fan(self):
        miner = FakeMiner(reading=lambda m: {"autofanspeed": 1})
        run = run_session(self, miner, stop_on_log="Tuning as a")
        self.assertEqual(miner.writes, [])
        self.assertTrue(_stopped(run))

    def test_the_fan_wait_survives_a_failed_or_odd_reply(self):
        replies = iter(
            [
                {"autofanspeed": 1},
                RuntimeError("socket"),
                "timed out",
                {"autofanspeed": 1},
            ]
        )
        miner = FakeMiner(reading=lambda m: next(replies, {}))
        run = run_session(self, miner, stop_on_log="Applied settings")
        self.assertTrue(any("UNCAUGHT ERROR: socket" in line for line in run.logs))
        self.assertEqual(miner.writes[0], (400, 1100))

    def test_a_stop_during_the_fast_start(self):
        runtime = dict(FAST_CONFIG, fast_start=True)
        miner = FakeMiner()
        run = run_session(self, miner, runtime=runtime, stop_on_log="Fast start from")
        self.assertTrue(_stopped(run))
        self.assertEqual(miner.writes, [(400, 1100)])

    def test_a_refused_first_write_uses_the_reported_setpoint(self):
        miner = FakeMiner(refuse=lambda f, v: True)
        run = run_session(self, miner, stop_on_log="Using reported setpoint")
        self.assertTrue(any("were not applied" in line for line in run.logs))
        self.assertTrue(
            any(
                "Using reported setpoint 400 MHz / 1100 mV" in line for line in run.logs
            )
        )


class LoopTests(unittest.TestCase):
    def test_a_miner_that_goes_silent_mid_session(self):
        def reading(miner):
            return "timed out" if miner.polls > 3 else {}

        run = run_session(self, FakeMiner(reading=reading), stop_on_log="No reply")
        self.assertTrue(_stopped(run))

    def test_a_fan_that_drifts_is_set_again(self):
        fans = []

        def reading(miner):
            return {"autofanspeed": 1} if miner.polls > 3 else {}

        def patch(ip, settings):
            fans.append(dict(settings))
            return True, ""

        run_session(
            self,
            FakeMiner(reading=reading),
            seconds=0.4,
            patches=[mock.patch.object(autotune, "patch_system", patch)],
        )
        self.assertGreaterEqual(len(fans), 2)

    def test_a_reply_without_clocks_is_still_read(self):
        def reading(miner):
            return {"frequency": None} if miner.polls > 3 else {}

        run = run_session(self, FakeMiner(reading=reading), seconds=0.4)
        self.assertFalse(any("UNCAUGHT" in line for line in run.logs))

    def test_a_stop_after_a_firmware_trip(self):
        def reading(miner):
            if miner.polls > 4:
                miner.frequency, miner.voltage = 300, 1000
            return {}

        miner = FakeMiner(frequency=400, voltage=1100, reading=reading)
        run = run_session(
            self, miner, stop_on_status="firmware overheat", min_freq=250, min_volt=900
        )
        self.assertTrue(any("overheat protection" in line for line in run.logs))
        self.assertTrue(_stopped(run))

    def test_a_latched_overheat_with_a_refused_write_and_a_failed_clear(self):
        def reading(miner):
            return {"overheat_mode": 1, "temp": 21, "power": 9.5, "hashRate": 0}

        miner = FakeMiner(frequency=550, voltage=1050, reading=reading)
        miner.refuse = lambda f, v: miner.polls > 1
        run = run_session(
            self,
            miner,
            stop_on_status="overheat restart",
            patches=[
                mock.patch.object(autotune, "OVERHEAT_LATCH_SECONDS", 0.05),
                mock.patch.object(
                    autotune, "patch_system", lambda ip, settings: (False, "busy")
                ),
            ],
        )
        self.assertTrue(
            any("Could not clear overheat mode: busy" in line for line in run.logs)
        )
        self.assertTrue(_stopped(run))

    def test_an_asic_off_restart_puts_the_floor_back_first(self):
        def reading(miner):
            if miner.polls > 2:
                miner.frequency, miner.voltage = 350, 950
            return {"power": 5.0, "hashRate": 0, "temp": 21}

        miner = FakeMiner(reading=reading)
        run = run_session(
            self,
            miner,
            stop_on_status="asic off restart",
            patches=[mock.patch.object(autotune, "ASIC_OFF_RESTART_SECONDS", 0.05)],
        )
        self.assertIn((400, 1000), miner.writes)
        self.assertTrue(_stopped(run))

    def test_an_asic_off_restart_without_any_clocks(self):
        def reading(miner):
            return {
                "power": 5.0,
                "hashRate": 0,
                "temp": 21,
                "frequency": None,
                "coreVoltage": None,
            }

        miner = FakeMiner(reading=reading, refuse=lambda f, v: True)
        run = run_session(
            self,
            miner,
            stop_on_status="asic off restart",
            patches=[mock.patch.object(autotune, "ASIC_OFF_RESTART_SECONDS", 0.05)],
        )
        self.assertEqual(miner.writes, [(400, 1100)])
        self.assertTrue(any("The ASIC has been off" in line for line in run.logs))

    def test_a_hardware_fault_that_clears_resets_its_restart(self):
        restarts = []

        def reading(miner):
            if 3 <= miner.polls <= 4 or 7 <= miner.polls <= 8:
                return {"hardware_fault": "fan", "hashRate": 0}
            return {}

        def restart(ip):
            restarts.append(ip)
            return f"{ip} -> Restart initiated."

        run_session(
            self,
            FakeMiner(reading=reading),
            seconds=0.6,
            patches=[mock.patch.object(autotune, "restart_bitaxe", restart)],
        )
        self.assertEqual(len(restarts), 2)


class RetryTests(unittest.TestCase):
    def test_a_first_poll_that_fails_is_tried_again(self):
        miner = FakeMiner(reading=lambda m: "timed out" if m.polls == 1 else {})
        run_session(self, miner, stop_on_log="Applied settings")
        self.assertEqual(miner.writes, [(400, 1100)])

    def test_one_missed_poll_mid_session_is_tried_again(self):
        miner = FakeMiner(reading=lambda m: "timed out" if m.polls == 4 else {})
        run_session(self, miner, seconds=0.4)
        self.assertGreater(miner.polls, 6)

    def test_an_error_inside_a_round_is_logged_and_a_stop_still_ends_it(self):
        def reading(miner):
            return RuntimeError("socket closed") if miner.polls == 4 else {}

        run = run_session(self, FakeMiner(reading=reading), stop_on_log="UNCAUGHT")
        self.assertTrue(
            any("UNCAUGHT ERROR: socket closed" in line for line in run.logs)
        )
        self.assertTrue(_stopped(run))

    def test_a_stop_at_the_end_of_a_round(self):
        run = run_session(self, FakeMiner(), stop_on_log="Confirmed 400 MHz")
        self.assertTrue(_stopped(run))


class RefusedFloorTests(unittest.TestCase):
    def test_high_errors_restart_even_when_the_floor_is_refused(self):
        restarts = []

        def restart(ip):
            restarts.append(ip)
            return f"{ip} -> Restart initiated."

        miner = FakeMiner(
            frequency=350,
            voltage=1000,
            reading=lambda m: {"errorPercentage": 30},
            refuse=lambda f, v: True,
        )
        run = run_session(
            self,
            miner,
            stop_on_status="high error restart",
            patches=[mock.patch.object(autotune, "restart_bitaxe", restart)],
            min_freq=400,
            min_volt=1000,
        )
        self.assertEqual(restarts, ["miner"])
        self.assertIn((400, 1000), miner.writes)
        self.assertTrue(_stopped(run))

    def test_an_asic_off_floor_that_is_refused_still_restarts(self):
        def reading(miner):
            if miner.polls > 2:
                miner.frequency, miner.voltage = 350, 950
            return {"power": 5.0, "hashRate": 0, "temp": 21}

        miner = FakeMiner(reading=reading)
        miner.refuse = lambda f, v: miner.polls > 1
        run = run_session(
            self,
            miner,
            stop_on_status="asic off restart",
            patches=[mock.patch.object(autotune, "ASIC_OFF_RESTART_SECONDS", 0.05)],
        )
        self.assertIn((400, 1000), miner.writes)
        self.assertTrue(any("The ASIC has been off" in line for line in run.logs))


class PartialReplyTests(unittest.TestCase):
    def test_replies_without_errors_hashrate_or_rejects_are_read(self):
        def reading(miner):
            if miner.polls <= 3:
                return {}
            return {"errorPercentage": None, "hashRate": None, "sharesRejected": None}

        run = run_session(self, FakeMiner(reading=reading), seconds=0.4)
        self.assertFalse(any("UNCAUGHT" in line for line in run.logs))


EFFICIENCY = dict(
    FAST_CONFIG, miners=[{"ip": "miner", "board": "601", "mode": "efficiency"}]
)
AUTO_FAN = {"autofanspeed": 1, "temptarget": None}
MULTI_STEP = dict(FAST_CONFIG, max_climb_steps=4)


def _said(run, text):
    return any(text in line for line in run.logs)


def _status(run, reason):
    return any(status.get("reason") == reason for status in run.statuses)


class ProbeTests(unittest.TestCase):
    def test_a_stop_at_the_frequency_ceiling(self):
        miner = FakeMiner()
        run = run_session(self, miner, stop_on_status="frequency ceiling", max_freq=405)
        self.assertIn((405, 1100), miner.writes)
        self.assertTrue(_stopped(run))

    def test_a_trim_that_stops_hashing_restores_its_voltage(self):
        def reading(miner):
            return {"hashRate": 0} if miner.voltage < 1100 else {}

        miner = FakeMiner(reading=reading)
        run = run_session(self, miner, stop_on_status="restore voltage", max_freq=405)
        self.assertTrue(_said(run, "1090 mV stopped hashing. Restoring 1100 mV."))
        self.assertEqual(miner.writes[-2:], [(405, 1090), (405, 1100)])
        self.assertTrue(_stopped(run))

    def test_a_refused_restore_after_a_dead_trim_is_logged(self):
        def reading(miner):
            return {"hashRate": 0} if miner.voltage < 1100 else {}

        miner = FakeMiner(reading=reading)
        miner.refuse = lambda f, v: (
            (f, v) == (405, 1100) and (405, 1090) in miner.writes
        )
        run = run_session(self, miner, seconds=0.5, max_freq=405)
        self.assertTrue(_said(run, "Miner rejected the change"))

    def test_a_multi_step_climb_that_stops_hashing_retries_single_steps(self):
        def reading(miner):
            return {"hashRate": 0} if miner.frequency > 405 else {}

        miner = FakeMiner(reading=reading)
        run = run_session(
            self, miner, runtime=MULTI_STEP, stop_on_status="retry single steps"
        )
        self.assertTrue(_said(run, "Climbing one step at a time."))
        self.assertTrue(_stopped(run))

    def test_a_step_without_a_good_hashrate_reading_waits(self):
        def reading(miner):
            if miner.frequency == 405 and miner.polls < 30:
                return {"errorPercentage": None}
            return {}

        miner = FakeMiner(reading=reading)
        run = run_session(self, miner, seconds=1.0, max_freq=410)
        self.assertTrue(_said(run, "holding for good hashrate"))
        self.assertIn((410, 1100), miner.writes)

    def test_a_stop_while_waiting_for_good_hashrate(self):
        miner = FakeMiner(
            reading=lambda m: {"errorPercentage": None} if m.frequency == 405 else {}
        )
        run = run_session(self, miner, stop_on_status="holding for good hashrate")
        self.assertTrue(_stopped(run))

    def test_a_refused_voltage_raise_is_logged(self):
        miner = FakeMiner(
            reading=lambda m: {"hashRate": 100 if m.frequency <= 400 else 10}
        )
        miner.refuse = lambda f, v: v > 1100
        run = run_session(self, miner, seconds=0.5)
        self.assertTrue(_said(run, "Raising voltage to 1110 mV"))
        self.assertTrue(_said(run, "Miner rejected the change"))

    def test_a_multi_step_climb_with_no_minute_rate(self):
        miner = FakeMiner(
            reading=lambda m: {"hashRate_1m": 0} if m.frequency > 405 else {}
        )
        run = run_session(self, miner, runtime=MULTI_STEP, seconds=0.5)
        self.assertTrue(_said(run, "had no 1-minute rate. Stepping back to 400 MHz"))

    def test_a_multi_step_climb_under_the_expected_rate(self):
        def reading(miner):
            if miner.frequency > 405:
                return {"hashRate_1m": 100, "expectedHashrate": 5000}
            return {"hashRate_1m": 100, "expectedHashrate": 100}

        run = run_session(
            self,
            FakeMiner(reading=reading),
            runtime=MULTI_STEP,
            seconds=0.5,
            patches=[mock.patch.object(autotune, "HASHRATE_1M_SETTLE_SECONDS", 0)],
        )
        self.assertTrue(_said(run, "under 85% of expected. Stepping back to 400 MHz"))

    def test_a_hashrate_ceiling_stops_the_climb(self):
        miner = FakeMiner(
            reading=lambda m: {"hashRate": 100 if m.frequency <= 400 else 10}
        )
        run = run_session(
            self, miner, stop_on_status="frequency ceiling", max_volt=1100
        )
        self.assertTrue(_status(run, "step frequency down after good hashrate"))
        self.assertTrue(_stopped(run))

    def test_a_zero_minute_rate_holds_the_climb(self):
        miner = FakeMiner(reading=lambda m: {"hashRate_1m": 0})
        run = run_session(self, miner, seconds=0.4)
        self.assertTrue(_status(run, "holding for good hashrate"))
        self.assertEqual(miner.writes, [(400, 1100)])

    def test_a_fan_that_stays_wrong_holds_the_climb(self):
        miner = FakeMiner(reading=lambda m: {"autofanspeed": 1} if m.polls > 2 else {})
        run = run_session(self, miner, seconds=0.5)
        self.assertTrue(_status(run, "holding for fan"))

    def test_a_pool_that_stays_down_keeps_holding(self):
        def reading(miner):
            return {"hashRate": 0, "poolDifficulty": 0} if miner.polls > 2 else {}

        run = run_session(self, FakeMiner(reading=reading), seconds=0.5)
        said = [line for line in run.logs if "the pool is down" in line]
        self.assertEqual(len(said), 1)
        self.assertTrue(sum(_status(run, "holding for pool") for _ in [0]))

    def test_a_zero_followed_by_a_reply_without_rates_counts_as_zero(self):
        def reading(miner):
            if miner.polls % 2:
                return {"hashRate": 0, "poolDifficulty": 0}
            return {"hashRate": MISSING}

        run = run_session(self, FakeMiner(reading=reading), seconds=0.5)
        self.assertFalse(_said(run, "UNCAUGHT"))


class EfficiencyTrimTests(unittest.TestCase):
    def test_a_trim_that_costs_energy_tries_a_lower_clock(self):
        def reading(miner):
            return dict(AUTO_FAN, power=20 if miner.voltage < 1100 else 10)

        miner = FakeMiner(reading=reading)
        miner.refuse = lambda f, v: f == 395
        run = run_session(
            self,
            miner,
            runtime=EFFICIENCY,
            stop_on_log="Trying 395 MHz",
            min_freq=390,
            max_freq=405,
        )
        self.assertTrue(_said(run, "1090 mV cost energy per hash at 405 MHz"))
        self.assertTrue(_said(run, "Miner rejected the change"))
        self.assertTrue(_stopped(run))

    def wall_trim(self, refuse_restore):
        def reading(miner):
            hot = miner.frequency >= 405 and miner.voltage >= 1100
            # Each trim step costs power here, so energy per hash gets worse.
            return dict(
                AUTO_FAN,
                temp=61 if hot else 59,
                power=10 + (1100 - miner.voltage) / 2,
            )

        miner = FakeMiner(reading=reading)
        if refuse_restore:
            miner.refuse = lambda f, v: (
                (f, v) == (400, 1090) and (400, 1080) in miner.writes
            )
        return run_session(
            self,
            miner,
            runtime=EFFICIENCY,
            seconds=0.6,
            start_freq=405,
            min_freq=400,
        )

    def test_a_heat_wall_trim_that_costs_energy_is_put_back(self):
        run = self.wall_trim(refuse_restore=False)
        self.assertTrue(_said(run, "1080 mV cost energy per hash. Restoring 1090 mV."))
        self.assertTrue(_status(run, "restore voltage"))

    def test_a_refused_heat_wall_restore_is_logged(self):
        run = self.wall_trim(refuse_restore=True)
        self.assertTrue(_said(run, "Miner rejected the change"))


class LateProbeTests(unittest.TestCase):
    def test_a_heat_wall_trim_that_stops_hashing_is_put_back(self):
        def reading(miner):
            hot = miner.frequency >= 405 and miner.voltage >= 1100
            dead = miner.voltage < 1090
            return dict(AUTO_FAN, temp=61 if hot else 59, hashRate=0 if dead else 100)

        miner = FakeMiner(reading=reading)
        run = run_session(
            self,
            miner,
            runtime=EFFICIENCY,
            stop_on_status="restore voltage",
            start_freq=405,
            min_freq=400,
        )
        self.assertTrue(_said(run, "1080 mV stopped hashing. Restoring 1090 mV."))
        self.assertTrue(_stopped(run))

    def test_a_miner_reporting_a_lower_clock_is_followed_one_step_at_a_time(self):
        def reading(miner):
            if miner.frequency >= 420:
                miner.seen = getattr(miner, "seen", 0) + 1
                if miner.seen > 2:
                    return {"frequency": miner.frequency - 10}
            return {}

        miner = FakeMiner(reading=reading)
        run = run_session(self, miner, runtime=MULTI_STEP, seconds=0.6)
        self.assertTrue(_said(run, "Following the miner instead of jumping."))
        self.assertIn((415, 1100), miner.writes)

    def test_a_hot_miner_with_its_pool_down_still_steps_down(self):
        def reading(miner):
            if miner.polls <= 3:
                return {}
            return {"hashRate": 0, "poolDifficulty": 0, "temp": 61}

        miner = FakeMiner(frequency=420, reading=reading)
        run_session(self, miner, seconds=0.4, start_freq=420)
        self.assertTrue(any(f < 420 for f, _v in miner.writes))

    def test_a_retreat_the_miner_has_not_echoed_is_not_sent_again(self):
        # Near the AxeOS trip a retreat may repeat after a short settle. One
        # still waiting for its echo is not written a second time.
        def reading(miner):
            if miner.polls > 3:
                miner.echo = False
                return {"temp": 72}
            return {}

        miner = FakeMiner(frequency=420, reading=reading)
        runtime = dict(FAST_CONFIG, refresh_interval=0.5)
        run_session(
            self,
            miner,
            runtime=runtime,
            seconds=0.3,
            start_freq=420,
            patches=[mock.patch.object(autotune, "TRIP_GUARD_SETTLE_SECONDS", 0)],
        )
        retreats = [write for write in miner.writes if write[0] < 420]
        self.assertEqual(len(retreats), 1)

    def test_heat_during_the_trim_climbs_again_after_it_cools(self):
        def reading(miner):
            trimmed = miner.voltage < 1100
            return {"temp": 61 if trimmed and miner.polls < 40 else 45}

        miner = FakeMiner(reading=reading)
        run = run_session(self, miner, seconds=0.8, max_freq=405)
        self.assertTrue(_said(run, "Frequency ceiling. Trimming voltage."))
        self.assertTrue(any(f < 405 for f, _v in miner.writes[2:]))


class TrimEdgeTests(unittest.TestCase):
    def test_a_lower_reported_clock_is_followed_on_single_steps(self):
        def reading(miner):
            if miner.frequency >= 410:
                miner.seen = getattr(miner, "seen", 0) + 1
                if miner.seen > 2:
                    return {"frequency": miner.frequency - 10}
            return {}

        miner = FakeMiner(reading=reading)
        run = run_session(self, miner, seconds=0.6)
        self.assertTrue(_said(run, "Following the miner instead of jumping."))

    def test_errors_after_a_trim_the_miner_undershot_restore_the_voltage(self):
        # The miner applies 10 mV less than each trim asks for, and errors
        # climb there. The trim's last good voltage comes back one step at a time.
        def reading(miner):
            if miner.voltage < 1100:
                return {"coreVoltage": miner.voltage - 10, "errorPercentage": 5}
            return {}

        miner = FakeMiner(reading=reading)
        run = run_session(self, miner, seconds=0.6, max_freq=405)
        self.assertTrue(_said(run, "Following the miner instead of jumping."))
        self.assertTrue(_said(run, "Holding 405 MHz / 1090 mV."))

    def test_errors_at_max_voltage_in_the_trim_step_frequency_down(self):
        def reading(miner):
            return {"errorPercentage": 5} if miner.polls > 12 else {}

        miner = FakeMiner(reading=reading)
        run = run_session(self, miner, seconds=0.6, max_freq=405, max_volt=1100)
        self.assertTrue(_said(run, "Frequency ceiling. Trimming voltage."))
        self.assertTrue(any(f < 405 for f, _v in miner.writes[2:]))


if __name__ == "__main__":
    unittest.main()
