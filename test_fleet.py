"""The owner's fleet: six custom-cooled Gamma 601s in Max hashrate mode.

Support for other boards, modes, and systems must not change how these
miners are tuned. FLEET_DECISIONS_SHA256 is the fingerprint of the tuner's
answers over a fixed grid of Gamma 601 readings, taken from the code before
any of that existed (commit 986fb10). A change that moves it changes the
fleet's tuning; update the fingerprint only when that is the point.
"""

import hashlib
import json
import os
import random
import tempfile
import threading
import unittest
from unittest import mock

import autotune
import config

FLEET_DECISIONS_SHA256 = (
    "751014f6b65830a94f584944db78809a99aea8704991a0d920c8096a0ab4fafb"
)


def _maybe(rng, value, chance=0.1):
    return None if rng.random() < chance else value


def fleet_decisions():
    """The tuner's answers for a fixed grid of Gamma 601 readings.

    Only arguments the original code already had are used, so the same grid
    runs on the code before boards and modes existed.
    """
    results = {"decide": [], "ramp": [], "climb": [], "clamp": []}
    rng = random.Random(601)
    for _ in range(6000):
        frequency = rng.randrange(250, 1150, 5)
        voltage = rng.randrange(900, 1550, 10)
        answer = autotune.decide_adjustment(
            current_frequency=frequency,
            current_voltage=voltage,
            min_freq=rng.choice([350, 400, 525]),
            max_freq=rng.choice([800, 1000, 1100]),
            min_volt=rng.choice([1000, 1100]),
            max_volt=rng.choice([1250, 1400, 1500]),
            max_temp=rng.choice([65, 70, 71]),
            max_watts=rng.choice([30, 40, 50]),
            max_vr_temp=rng.choice([85, 95, 101]),
            temp=_maybe(rng, round(rng.uniform(30, 80), 1)),
            vr_temp=_maybe(rng, round(rng.uniform(40, 110), 1)),
            power=_maybe(rng, round(rng.uniform(4, 55), 2)),
            hash_rate=_maybe(rng, round(rng.uniform(0, 2300), 1)),
            expected_hashrate=None,
            shares_rejected_delta=0,
            overheat_mode=rng.choice([0, 0, 0, 1]),
            frequency_step=5,
            voltage_step=10,
            temp_tolerance=3,
            error_percentage=_maybe(rng, round(rng.uniform(0, 6), 2)),
            input_voltage=_maybe(rng, round(rng.uniform(4.6, 5.3), 3)),
            min_input_voltage=4.9,
            core_voltage_actual=_maybe(rng, voltage - rng.randrange(0, 80)),
            max_droop_mv=40,
            power_fault=rng.choice([None, None, None, "", "fault"]),
            phase=rng.choice(["climb", "trim", "hold"]),
            trim_good_voltage=_maybe(rng, voltage + rng.choice([0, 10, 20]), 0.5),
            max_error_percentage=2.0,
            vr_temp_tolerance=3,
            thermal_hold=rng.random() < 0.15,
            safety_hold=rng.choice([False, False, False, "power", "input"]),
            reject_share=_maybe(rng, round(rng.uniform(0, 0.03), 4), 0.6),
            hashrate_short=rng.random() < 0.1,
            blocked_frequency=_maybe(rng, frequency + rng.choice([5, 10, 20]), 0.8),
            above_target_high=rng.random() < 0.05,
            droop_voltage=_maybe(rng, voltage - 10, 0.85),
            max_climb_steps=rng.choice([1, 4]),
            core_current=_maybe(rng, round(rng.uniform(5, 32), 2), 0.2),
            max_core_amps=rng.choice([None, 28.0, 29.0]),
        )
        results["decide"].append(list(answer))

    rng = random.Random(602)
    for _ in range(1500):
        sample = {
            "frequency": rng.randrange(525, 1000, 5),
            "voltage": rng.randrange(1150, 1400, 10),
            "power": round(rng.uniform(12, 40), 2),
            "temp": round(rng.uniform(40, 68), 1),
            "vr_temp": round(rng.uniform(50, 90), 1),
            "input_voltage": round(rng.uniform(4.85, 5.2), 3),
        }
        history = [
            {
                "power": round(rng.uniform(10, 30), 2),
                "temp": round(rng.uniform(35, 60), 1),
                "vr_temp": round(rng.uniform(45, 80), 1),
            }
            for _ in range(rng.randrange(0, 3))
        ]
        limits = dict(config.GAMMA601_LIMITS)
        limits["max_temp"] = rng.choice([65, 70])
        limits["max_vr_temp"] = rng.choice([85, 95])
        target = autotune.ramp_target(
            sample,
            history,
            limits,
            (525, 1150),
            5,
            10,
            min_input_voltage=4.9,
            max_core_amps=rng.choice([None, 29.0]),
        )
        results["ramp"].append(list(target) if target else None)

    rng = random.Random(603)
    for _ in range(3000):
        results["climb"].append(
            autotune.climb_frequency_steps(
                rng.randrange(400, 1100, 5),
                _maybe(rng, round(rng.uniform(30, 75), 1)),
                _maybe(rng, round(rng.uniform(40, 100), 1)),
                _maybe(rng, round(rng.uniform(5, 50), 2)),
                70,
                95,
                50,
                3,
                3,
                5,
                4,
                _maybe(rng, round(rng.uniform(5, 30), 2)),
                29.0,
            )
        )

    rng = random.Random(604)
    for _ in range(600):
        limits = {
            "min_freq": rng.randrange(200, 1300, 5),
            "max_freq": rng.randrange(200, 1300, 5),
            "min_volt": rng.randrange(800, 1700, 10),
            "max_volt": rng.randrange(800, 1700, 10),
            "max_temp": rng.choice([None, 60, 71, 75, 80]),
            "max_vr_temp": rng.choice([None, 90, 101, 110]),
        }
        results["clamp"].append(autotune.clamp_limits(limits))

    results["fleet_preset"] = dict(config.GAMMA601_LIMITS)
    results["fan"] = dict(autotune.MANUAL_FULL_FAN)
    results["fast_start"] = [
        autotune.RAMP_HEADROOM_C,
        autotune.RAMP_POWER_HEADROOM_W,
        autotune.RAMP_CURRENT_HEADROOM_A,
        autotune.RAMP_INPUT_HEADROOM_V,
        autotune.RAMP_MV_PER_MHZ,
        autotune.BOARD_POWER_W,
        autotune.ASIC_OFF_POWER_WATTS,
        autotune.BM1370_HASHRATE_PER_MHZ,
        autotune.HASHRATE_SHORTFALL_RATIO,
    ]
    return results


def fingerprint(results):
    text = json.dumps(results, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# The owner's config as it was saved before boards and modes existed.
OLD_FLEET_MINER = {
    "type": "BM1370 601",
    "enabled": True,
    "repasted_on": "2026-10-04",
    "min_freq": 350,
    "max_freq": 1100,
    "start_freq": 525,
    "min_volt": 1000,
    "max_volt": 1500,
    "start_volt": 1150,
    "max_temp": 70,
    "max_watts": 50,
    "max_vr_temp": 95,
    "min_input_voltage": 4.9,
    "max_error_percentage": 2.0,
    "max_droop_mv": 40,
    "max_core_amps": 29.0,
}


class FleetTests(unittest.TestCase):
    def test_gamma_601_decisions_match_the_original_code(self):
        self.assertEqual(fingerprint(fleet_decisions()), FLEET_DECISIONS_SHA256)

    def test_old_fleet_config_keeps_every_limit(self):
        old = {
            "voltage_step": 10,
            "frequency_step": 5,
            "monitor_interval": 5,
            "default_target_temp": 70,
            "temp_tolerance": 3,
            "vr_temp_tolerance": 3,
            "refresh_interval": 180,
            "fast_start": True,
            "continue_from_live": True,
            "limits_version": config.LIMITS_VERSION,
            "flatline_detection_enabled": False,
            "flatline_hashrate_repeat_count": 5,
            "location": {"name": "Moose Jaw", "latitude": 50.4, "longitude": -105.5},
            "miners": [
                dict(OLD_FLEET_MINER, ip=f"192.168.1.{100 + index}", nickname=name)
                for index, name in enumerate(
                    ["Opossumator", "Maximilian", "Naknak", "Evelina", "Moneypit"]
                )
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(old, handle)
            saved = (config.CONFIG_FILE, config._last_good_config)
            config.CONFIG_FILE, config._last_good_config = path, None
            try:
                loaded = config.load_config()
            finally:
                config.CONFIG_FILE, config._last_good_config = saved
        for before, after in zip(old["miners"], loaded["miners"], strict=True):
            self.assertEqual({k: after[k] for k in before}, before)
            self.assertEqual((after["board"], after["mode"]), ("601", "max_hashrate"))
        for key, value in old.items():
            if key != "miners":
                self.assertEqual(loaded[key], value, key)
        # No first-run questions, weather and every other internet read stay
        # on, new 601s join in Max mode.
        self.assertTrue(loaded["setup_done"])
        self.assertTrue(loaded["weather_enabled"])
        for key in config.INTERNET_SWITCHES:
            self.assertTrue(loaded[key], key)
        self.assertEqual(loaded["default_mode"], "max_hashrate")
        new = config.new_miner_record("BM1370 601", "192.168.1.105", "Goose", loaded)
        self.assertEqual(
            {key: new[key] for key in OLD_FLEET_MINER if key in config.GAMMA601_LIMITS},
            {key: OLD_FLEET_MINER[key] for key in config.GAMMA601_LIMITS},
        )

    def test_fleet_miner_tunes_in_max_mode_with_the_fan_at_100(self):
        ip = "192.168.1.100"
        fans = []
        logs = []
        stop = threading.Event()
        runtime = {
            "miners": [dict(OLD_FLEET_MINER, ip=ip, board="601", mode="max_hashrate")],
            "fast_start": False,
            "continue_from_live": False,
            "monitor_interval": 0.01,
            "refresh_interval": 60,
        }
        info = {
            "ASICModel": "BM1370",
            "boardVersion": "601",
            "version": "v2.15.3",
            "errorPercentage": 0.3,
            "frequency": 525,
            "coreVoltage": 1150,
            "temp": 50,
            "vrTemp": 60,
            "power": 18,
            "autofanspeed": 0,
            "fanspeed": 100,
        }

        def fan(_ip, payload):
            fans.append(dict(payload))
            return True, ""

        def write(_ip, volt, freq):
            stop.set()
            return (
                f"{_ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"
            )

        self.addCleanup(autotune._clear_miner_status, ip)
        with (
            mock.patch.object(autotune, "load_config", lambda: runtime),
            mock.patch.object(autotune, "get_system_info", lambda _ip: dict(info)),
            mock.patch.object(autotune, "patch_system", fan),
            mock.patch.object(autotune, "set_system_settings", write),
        ):
            autotune.monitor_and_adjust(
                ip,
                "BM1370 601",
                0.01,
                lambda message, *_level: logs.append(message),
                350,
                1100,
                1000,
                1500,
                70,
                50,
                525,
                1150,
                95,
                stop_event=stop,
            )
        self.assertEqual(fans[0], {"autofanspeed": 0, "manualFanSpeed": 100})
        self.assertTrue(any("Max hashrate mode" in line for line in logs), logs)
        self.assertFalse(any("Skipping" in line for line in logs), logs)
        self.assertFalse(any("newer than" in line for line in logs), logs)


if __name__ == "__main__":
    unittest.main()
