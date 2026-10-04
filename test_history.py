import os
import tempfile
import time
import unittest

import history

HOUR = 3600


def _info(**overrides):
    info = {
        "frequency": 550,
        "coreVoltage": 1150,
        "hashRate": 1100,
        "hashRate_1m": 1110,
        "hashRate_10m": 1120,
        "errorPercentage": 1.0,
        "temp": 63.5,
        "vrTemp": 70.0,
        "power": 17.5,
        "uptimeSeconds": 7200,
    }
    info.update(overrides)
    return info


def _weather(now, temp=12.0, code=0):
    return {
        "outdoor_temp": temp,
        "humidity": 60.0,
        "apparent_temp": temp - 1,
        "wind_speed": 10.0,
        "cloud_cover": 5.0,
        "precipitation": 0.0,
        "weather_code": code,
        "is_day": 1,
        "fetched_at": now,
    }


def _sample(ts, ip="a", good=1000.0, power=17.0, settled=1, temp=12.0, code=0, **extra):
    sample = {
        "ts": ts,
        "ip": ip,
        "name": ip,
        "frequency": 550,
        "voltage": 1150,
        "hashrate": good,
        "good_hashrate": good,
        "error_pct": 0.0,
        "asic_temp": 64.0,
        "vr_temp": 70.0,
        "power": power,
        "settled": settled,
        "phase": "hold",
        "outdoor_temp": temp,
        "humidity": 50.0,
        "apparent_temp": temp,
        "wind_speed": 5.0,
        "cloud_cover": 0.0,
        "precipitation": 0.0,
        "weather_code": code,
        "is_day": 1,
    }
    sample.update(extra)
    return sample


class SampleTests(unittest.TestCase):
    def test_sample_prefers_the_ten_minute_rate_and_takes_errors_out(self):
        now = 1_800_000_000
        sample = history.sample_from_info(
            "a", "Alpha", _info(), "hold", True, _weather(now), now
        )
        self.assertEqual(sample["hashrate"], 1120)
        self.assertAlmostEqual(sample["good_hashrate"], 1120 * 0.99)
        self.assertEqual((sample["frequency"], sample["voltage"]), (550, 1150))
        self.assertEqual(sample["settled"], 1)
        self.assertEqual(sample["outdoor_temp"], 12.0)

    def test_sample_falls_back_to_the_one_minute_rate(self):
        sample = history.sample_from_info(
            "a", "Alpha", _info(hashRate_10m=0), "climb", True, None, 1
        )
        self.assertEqual(sample["hashrate"], 1110)

    def test_stale_or_missing_weather_is_not_stored(self):
        now = 1_800_000_000
        stale = _weather(now - history.CURRENT_WEATHER_MAX_AGE - 1)
        sample = history.sample_from_info("a", "A", _info(), "", True, stale, now)
        self.assertIsNone(sample["outdoor_temp"])
        self.assertIsNone(sample["weather_code"])

    def test_a_dead_board_is_never_settled(self):
        sample = history.sample_from_info(
            "a",
            "A",
            _info(hashRate=0, hashRate_1m=0, hashRate_10m=0),
            "",
            True,
            None,
            1,
        )
        self.assertIsNone(sample["good_hashrate"])
        self.assertEqual(sample["settled"], 0)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "history.db")

    def tearDown(self):
        self.directory.cleanup()

    def test_samples_and_events_round_trip(self):
        self.assertEqual(history.load_samples(path=self.path), [])
        history.record_samples([_sample(100), _sample(700, ip="b")], self.path)
        history.record_samples([_sample(100, good=900.0)], self.path)
        loaded = history.load_samples(path=self.path)
        self.assertEqual(
            [(row["ts"], row["ip"]) for row in loaded], [(100, "a"), (700, "b")]
        )
        self.assertEqual(loaded[0]["good_hashrate"], 900.0)
        self.assertEqual(len(history.load_samples(since=500, path=self.path)), 1)
        history.record_event("a", "reset", ts=50, path=self.path)
        history.record_event("a", "reset", ts=400, path=self.path)
        self.assertEqual(history.last_events("reset", self.path), {"a": 400})

    def test_since_reset_starts_each_miner_at_its_own_reset(self):
        history.record_samples(
            [_sample(100), _sample(100, ip="b"), _sample(900), _sample(900, ip="b")],
            self.path,
        )
        history.record_event("a", "reset", ts=500, path=self.path)
        fleet = history.period_samples("reset", 1000, path=self.path)
        self.assertEqual(
            sorted((row["ip"], row["ts"]) for row in fleet),
            [("a", 900), ("b", 100), ("b", 900)],
        )
        one = history.period_samples("reset", 1000, ip="a", path=self.path)
        self.assertEqual([row["ts"] for row in one], [900])

    def test_missing_weather_is_filled_from_the_nearest_hour(self):
        history.record_samples(
            [
                _sample(10 * HOUR + 600, temp=None),
                _sample(11 * HOUR + 2400, temp=None),
                _sample(12 * HOUR, temp=5.0),
            ],
            self.path,
        )
        self.assertEqual(history.oldest_missing_weather(0, self.path), 10 * HOUR + 600)
        hours = [
            (10 * HOUR, {"outdoor_temp": 1.0, "weather_code": 3}),
            (12 * HOUR, {"outdoor_temp": 2.0, "weather_code": 61}),
        ]
        self.assertEqual(history.fill_missing_weather(hours, self.path), 2)
        temps = [row["outdoor_temp"] for row in history.load_samples(path=self.path)]
        self.assertEqual(temps, [1.0, 2.0, 5.0])
        self.assertIsNone(history.oldest_missing_weather(0, self.path))


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "history.db")
        self.now = 1_800_000_000.0
        self.recorder = history.HistoryRecorder(
            path_fn=lambda: self.path, clock=lambda: self.now
        )

    def tearDown(self):
        self.directory.cleanup()

    def _observe(self, **overrides):
        readings = [
            ("a", "Alpha", _info(**overrides), "hold"),
            ("b", "Beta", _info(), "climb"),
            ("c", "Gone", "c -> Error: timeout", ""),
        ]
        return self.recorder.observe(readings, _weather(self.now))

    def test_first_write_waits_a_full_interval_then_saves_every_live_miner(self):
        self.assertEqual(self._observe(), 0)
        self.assertFalse(os.path.exists(self.path))
        self.now += history.SAMPLE_SECONDS - 1
        self.assertEqual(self._observe(), 0)
        self.now += 1
        self.assertEqual(self._observe(), 2)
        rows = history.load_samples(path=self.path)
        self.assertEqual({row["ip"] for row in rows}, {"a", "b"})
        self.assertEqual({row["ts"] for row in rows}, {int(self.now)})
        self.assertTrue(all(row["settled"] for row in rows))

    def test_a_new_setpoint_or_restart_is_not_settled(self):
        self._observe()
        self.now += history.SAMPLE_SECONDS - 60
        self._observe(frequency=560)
        self.now += 60
        self._observe(frequency=560)
        rows = {row["ip"]: row for row in history.load_samples(path=self.path)}
        self.assertEqual(rows["a"]["settled"], 0)
        self.assertEqual(rows["b"]["settled"], 1)

        self.now += history.SAMPLE_SECONDS
        self._observe(frequency=560, uptimeSeconds=120)
        latest = history.load_samples(since=int(self.now), path=self.path)
        self.assertEqual({row["ip"]: row["settled"] for row in latest}["a"], 0)

    def test_forget_restarts_the_settle_clock(self):
        self._observe()
        self.now += history.SAMPLE_SECONDS - 30
        self.recorder.forget("a")
        self._observe()
        self.now += 30
        self._observe()
        rows = {row["ip"]: row for row in history.load_samples(path=self.path)}
        self.assertEqual(rows["a"]["settled"], 0)
        self.assertEqual(rows["b"]["settled"], 1)


class SummaryTests(unittest.TestCase):
    def _hour(self, day, hour):
        return int(time.mktime((2026, 1, day, hour, 0, 0, 0, 0, -1)))

    def test_fleet_totals_add_good_hashrate_and_pool_efficiency(self):
        ts = self._hour(5, 9)
        samples = [
            _sample(ts, ip="a", good=1000.0, power=20.0),
            _sample(ts, ip="b", good=500.0, power=10.0),
        ]
        hash_points = history.fleet_points(samples, "good_hashrate")
        self.assertEqual(hash_points[0]["value"], 1500.0)
        efficiency = history.fleet_points(samples, "efficiency")
        self.assertAlmostEqual(efficiency[0]["value"], 30.0 / 1.5)

    def test_a_round_with_an_unsettled_miner_is_not_a_result(self):
        ts = self._hour(5, 9)
        samples = [_sample(ts, ip="a"), _sample(ts, ip="b", settled=0)]
        summary = history.summarize(samples, "good_hashrate")
        self.assertEqual(summary["settled_count"], 0)
        self.assertIsNone(summary["best"])
        self.assertEqual(len(summary["series"]), 2)

    def test_best_typical_heatmap_and_combinations(self):
        samples = []
        # Cool clear mornings run faster than warm cloudy afternoons.
        for day in range(1, 5):
            for minute in range(0, 60, 10):
                morning = self._hour(day, 8) + minute * 60
                samples.append(_sample(morning, good=1100.0 + day, temp=4.0, code=0))
                afternoon = self._hour(day, 15) + minute * 60
                samples.append(_sample(afternoon, good=1000.0, temp=16.0, code=3))
        samples.append(_sample(self._hour(6, 8), good=5000.0, settled=0, temp=4.0))
        summary = history.summarize(samples, "good_hashrate", ip="a")

        self.assertEqual(summary["best"]["value"], 1104.0)
        self.assertEqual(summary["best"]["daypart"], "morning")
        self.assertEqual(summary["best"]["sky"], "clear")
        self.assertEqual(summary["sample_count"], 49)
        self.assertEqual(summary["settled_count"], 48)
        top = summary["combos"][0]
        self.assertEqual(
            (top["band_label"], top["daypart"], top["sky"]),
            ("3–6 °C", "morning", "clear"),
        )
        self.assertEqual(top["count"], 24)
        self.assertEqual(summary["combos"][-1]["sky"], "cloudy")
        bands = [band["low"] for band in summary["heatmap"]["bands"]]
        self.assertEqual(bands, [15, 3])
        hours = {(cell["band"], cell["hour"]) for cell in summary["heatmap"]["cells"]}
        self.assertEqual(hours, {(3, 8), (15, 15)})
        self.assertLess(summary["trend_per_c"], 0)
        self.assertEqual((summary["outdoor_low"], summary["outdoor_high"]), (4.0, 16.0))

    def test_efficiency_ranks_the_lowest_first(self):
        samples = []
        for minute in range(0, 60, 10):
            samples.append(
                _sample(self._hour(2, 3) + minute * 60, power=16.0, temp=1.0)
            )
            samples.append(
                _sample(self._hour(2, 14) + minute * 60, power=20.0, temp=20.0)
            )
        summary = history.summarize(samples, "efficiency", ip="a")
        self.assertAlmostEqual(summary["best"]["value"], 16.0)
        self.assertEqual(summary["combos"][0]["daypart"], "night")
        self.assertLess(summary["combos"][0]["value"], summary["combos"][1]["value"])

    def test_short_combinations_and_flat_weather_are_not_ranked(self):
        samples = [
            _sample(self._hour(3, 10) + minute * 60, temp=10.0)
            for minute in range(0, 20, 10)
        ]
        summary = history.summarize(samples, "good_hashrate", ip="a")
        self.assertEqual(summary["combos"], [])
        self.assertIsNone(summary["trend_per_c"])

    def test_long_series_are_averaged_down(self):
        start = self._hour(1, 0)
        samples = [_sample(start + index * 600) for index in range(2000)]
        summary = history.summarize(samples, "good_hashrate", ip="a")
        self.assertLessEqual(len(summary["series"]["a"]), history.MAX_SERIES_POINTS + 1)
        self.assertEqual(summary["series"]["a"][0][1], 1000.0)


if __name__ == "__main__":
    unittest.main()
