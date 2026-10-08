import subprocess
import unittest

import requests

import weather


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._payload


def _get_returning(payload, seen=None):
    def get(url, params=None, headers=None, timeout=None):
        if seen is not None:
            seen.append({"url": url, "params": params, "headers": headers})
        return _Response(payload)

    return get


def _get_raising(url, params=None, headers=None, timeout=None):
    raise requests.ConnectionError("offline")


class SkyTests(unittest.TestCase):
    def test_wmo_codes_map_to_sky_groups(self):
        self.assertEqual(weather.sky_group(0), "clear")
        self.assertEqual(weather.sky_group(3), "cloudy")
        self.assertEqual(weather.sky_group(63), "rain")
        self.assertEqual(weather.sky_group(81), "rain")
        self.assertEqual(weather.sky_group(75), "snow")
        self.assertEqual(weather.sky_group(95), "storm")
        self.assertEqual(weather.sky_group(None), "")
        self.assertEqual(weather.sky_label(2), "Partly cloudy")


class LocationTests(unittest.TestCase):
    def test_normalize_keeps_real_coordinates_only(self):
        place = weather.normalize_location(
            {"name": " Moose Jaw ", "latitude": "50.40005", "longitude": -105.53445}
        )
        self.assertEqual(place["name"], "Moose Jaw")
        self.assertEqual(place["latitude"], 50.40005)
        self.assertEqual(place["source"], "")
        self.assertIsNone(weather.normalize_location({"latitude": 95, "longitude": 0}))
        self.assertIsNone(weather.normalize_location({"latitude": "x", "longitude": 1}))
        self.assertIsNone(weather.normalize_location(None))
        unnamed = weather.normalize_location({"latitude": 1, "longitude": 2})
        self.assertEqual(unnamed["name"], "1.000, 2.000")

    def test_search_names_each_place_with_region_and_country(self):
        seen = []
        payload = {
            "results": [
                {
                    "name": "Moose Jaw",
                    "admin1": "Saskatchewan",
                    "country": "Canada",
                    "latitude": 50.40005,
                    "longitude": -105.53445,
                    "timezone": "America/Regina",
                },
                {"name": "Nowhere", "latitude": None, "longitude": 1},
            ]
        }
        places = weather.search_places(
            "Moose Jaw, Saskatchewan", get=_get_returning(payload, seen)
        )
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["name"], "Moose Jaw, Saskatchewan, Canada")
        self.assertEqual(places[0]["timezone"], "America/Regina")
        self.assertEqual(places[0]["source"], "search")
        self.assertEqual(seen[0]["url"], weather.GEOCODING_URL)
        self.assertEqual(seen[0]["params"]["name"], "Moose Jaw, Saskatchewan")

    def test_search_rejects_an_empty_query_and_reports_a_failed_lookup(self):
        with self.assertRaises(ValueError):
            weather.search_places("  ", get=_get_raising)
        with self.assertRaises(RuntimeError):
            weather.search_places("Moose Jaw", get=_get_raising)

    def test_reverse_lookup_identifies_the_app(self):
        seen = []
        payload = {
            "address": {
                "city": "Moose Jaw",
                "county": "Division No. 7",
                "state": "Saskatchewan",
                "country": "Canada",
            }
        }
        name = weather.reverse_place_name(
            50.41, -105.53, get=_get_returning(payload, seen)
        )
        self.assertEqual(name, "Moose Jaw, Division No. 7, Saskatchewan")
        self.assertEqual(seen[0]["headers"]["User-Agent"], weather.USER_AGENT)
        self.assertIsNone(weather.reverse_place_name(1, 2, get=_get_raising))


class DetectTests(unittest.TestCase):
    def test_detect_output_explains_a_block_or_a_missing_fix(self):
        latitude, longitude, error = weather.parse_detect_output(
            '{"permission":"Granted","status":"Ready","latitude":50.39,"longitude":-105.53}'
        )
        self.assertEqual((latitude, longitude, error), (50.39, -105.53, ""))
        _lat, _lon, denied = weather.parse_detect_output('{"permission":"Denied"}')
        self.assertIn("Let desktop apps access your location", denied)
        _lat, _lon, unknown = weather.parse_detect_output(
            '{"permission":"Granted","status":"NoData","latitude":null}'
        )
        self.assertIn("no location fix", unknown)
        _lat, _lon, garbage = weather.parse_detect_output("not json")
        self.assertTrue(garbage)

    def test_detect_needs_windows(self):
        place, error = weather.detect_device_location(platform="linux")
        self.assertIsNone(place)
        self.assertIn("Windows", error)

    def test_detect_runs_powershell_and_names_the_place(self):
        seen = {}

        def run(command, **kwargs):
            seen["command"] = command
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='{"permission":"Granted","status":"Ready",'
                '"latitude":50.40005,"longitude":-105.53445,"accuracy":40}',
                stderr="",
            )

        looked_up = []
        get = _get_returning(
            {"address": {"city": "Moose Jaw", "state": "Saskatchewan"}}, looked_up
        )
        place, error = weather.detect_device_location(
            run=run, get=get, platform="win32"
        )
        self.assertEqual(error, "")
        self.assertEqual(place["name"], "Moose Jaw, Saskatchewan")
        self.assertEqual(place["source"], "device")
        # The exact fix never leaves the machine or reaches the settings file.
        self.assertEqual((place["latitude"], place["longitude"]), (50.4, -105.53))
        self.assertEqual(
            (looked_up[0]["params"]["lat"], looked_up[0]["params"]["lon"]),
            (50.4, -105.53),
        )
        self.assertEqual(seen["command"][0], "powershell.exe")
        self.assertIn("GeoCoordinateWatcher", seen["command"][-1])
        self.assertEqual(seen["kwargs"]["timeout"], weather.DETECT_TIMEOUT_SECONDS)

    def test_detect_survives_a_powershell_timeout(self):
        def run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 1)

        place, error = weather.detect_device_location(run=run, platform="win32")
        self.assertIsNone(place)
        self.assertTrue(error)


class ForecastTests(unittest.TestCase):
    def test_current_weather_is_read_as_unix_time(self):
        seen = []
        payload = {
            "timezone": "America/Regina",
            "current": {
                "time": 1791140400,
                "temperature_2m": 19.3,
                "relative_humidity_2m": 59,
                "apparent_temperature": 17.1,
                "is_day": 1,
                "precipitation": 0.0,
                "weather_code": 1,
                "cloud_cover": 22,
                "wind_speed_10m": 18.3,
            },
        }
        current = weather.read_current_weather(
            50.39, -105.53, get=_get_returning(payload, seen)
        )
        self.assertEqual(current["outdoor_temp"], 19.3)
        self.assertEqual(current["humidity"], 59.0)
        self.assertEqual(current["weather_code"], 1.0)
        self.assertEqual(current["time"], 1791140400.0)
        self.assertEqual(current["timezone"], "America/Regina")
        params = seen[0]["params"]
        self.assertEqual(params["timeformat"], "unixtime")
        self.assertIn("temperature_2m", params["current"])
        self.assertIsNone(weather.read_current_weather(1, 2, get=_get_raising))
        self.assertIsNone(weather.read_current_weather(1, 2, get=_get_returning({})))

    def test_hourly_weather_skips_hours_with_no_temperature(self):
        seen = []
        payload = {
            "hourly": {
                "time": [3600, 7200, 10800],
                "temperature_2m": [1.5, None, 2.5],
                "weather_code": [0, 3, 61],
            }
        }
        hours = weather.read_hourly_weather(
            1, 2, 200, get=_get_returning(payload, seen)
        )
        self.assertEqual([moment for moment, _values in hours], [3600, 10800])
        self.assertEqual(hours[1][1]["weather_code"], 61.0)
        self.assertIsNone(hours[0][1]["humidity"])
        self.assertEqual(seen[0]["params"]["past_days"], weather.MAX_PAST_DAYS)
        self.assertEqual(weather.read_hourly_weather(1, 2, 3, get=_get_raising), [])


if __name__ == "__main__":
    unittest.main()
