"""Outdoor weather for the room the miners breathe from, and where it is read.

Weather comes from Open-Meteo: free for personal use, no API key, data under
CC BY 4.0. Place search uses the Open-Meteo geocoding API. A position found on
this device is named with one Nominatim reverse lookup. The device position
comes from Windows location services through PowerShell, so the window needs
no extra package.
"""

import json
import math
import subprocess
import sys

import requests

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
# Nominatim refuses stock library user agents.
USER_AGENT = "groundhog-gamma-tuner/1.0 (Bitaxe tuner dashboard)"
WEATHER_TIMEOUT = 10
# Open-Meteo current conditions are 15-minute model data.
WEATHER_REFRESH_SECONDS = 15 * 60
# The forecast endpoint returns at most this many past days of hourly data.
MAX_PAST_DAYS = 92
DETECT_TIMEOUT_SECONDS = 40
ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0)"

# Open-Meteo variable name -> the name stored with each history sample.
WEATHER_FIELDS = {
    "temperature_2m": "outdoor_temp",
    "relative_humidity_2m": "humidity",
    "apparent_temperature": "apparent_temp",
    "wind_speed_10m": "wind_speed",
    "cloud_cover": "cloud_cover",
    "precipitation": "precipitation",
    "weather_code": "weather_code",
    "is_day": "is_day",
}

# WMO weather interpretation codes, as Open-Meteo documents them.
_SKY = (
    ((0, 1), "clear", "Clear"),
    ((2,), "cloudy", "Partly cloudy"),
    ((3,), "cloudy", "Overcast"),
    ((45, 48), "fog", "Fog"),
    ((51, 53, 55, 56, 57), "rain", "Drizzle"),
    ((61, 63, 65, 66, 67), "rain", "Rain"),
    ((71, 73, 75, 77), "snow", "Snow"),
    ((80, 81, 82), "rain", "Rain showers"),
    ((85, 86), "snow", "Snow showers"),
    ((95, 96, 99), "storm", "Thunderstorm"),
)
SKY_LABELS = {
    "clear": "Clear",
    "cloudy": "Cloudy",
    "fog": "Fog",
    "rain": "Rain",
    "snow": "Snow",
    "storm": "Storm",
}

# GeoCoordinateWatcher is the .NET Framework location API that Windows
# PowerShell 5.1 ships with. It answers only when Location is on in Windows
# Settings and desktop apps may use it.
_DETECT_SCRIPT = r"""
Add-Type -AssemblyName System.Device
$watcher = New-Object System.Device.Location.GeoCoordinateWatcher
$null = $watcher.TryStart($false, [TimeSpan]::FromSeconds(10))
$deadline = (Get-Date).AddSeconds(25)
while ($watcher.Status -ne 'Ready' -and $watcher.Permission -ne 'Denied' -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 200
}
$location = $watcher.Position.Location
$result = @{
    permission = "$($watcher.Permission)"
    status = "$($watcher.Status)"
    latitude = $null
    longitude = $null
    accuracy = $null
}
if (-not $location.IsUnknown) {
    $result.latitude = $location.Latitude
    $result.longitude = $location.Longitude
    $result.accuracy = $location.HorizontalAccuracy
}
$watcher.Stop()
$result | ConvertTo-Json -Compress
"""
_CREATE_NO_WINDOW = 0x08000000


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def sky_group(code):
    """`clear`, `cloudy`, `fog`, `rain`, `snow`, or `storm`. Empty when unknown."""
    number = _number(code)
    if number is None:
        return ""
    for codes, group, _label in _SKY:
        if int(number) in codes:
            return group
    return ""


def sky_label(code):
    number = _number(code)
    if number is None:
        return ""
    for codes, _group, label in _SKY:
        if int(number) in codes:
            return label
    return ""


def normalize_location(value):
    """A saved place with real coordinates, or None.

    Keeps `name`, `latitude`, `longitude`, `timezone`, and `source`
    (`device` or `search`).
    """
    if not isinstance(value, dict):
        return None
    latitude = _number(value.get("latitude"))
    longitude = _number(value.get("longitude"))
    if latitude is None or longitude is None:
        return None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    name = str(value.get("name") or "").strip()
    if not name:
        name = f"{latitude:.3f}, {longitude:.3f}"
    source = value.get("source") if value.get("source") in ("device", "search") else ""
    return {
        "name": name[:120],
        "latitude": round(latitude, 5),
        "longitude": round(longitude, 5),
        "timezone": str(value.get("timezone") or "")[:64],
        "source": source,
    }


def place_name(result):
    """`Moose Jaw, Saskatchewan, Canada` from an Open-Meteo geocoding result."""
    parts = []
    for key in ("name", "admin1", "country"):
        text = str(result.get(key) or "").strip()
        if text and text not in parts:
            parts.append(text)
    return ", ".join(parts)


def _weather_values(block, index=None):
    values = {}
    for source, target in WEATHER_FIELDS.items():
        raw = block.get(source)
        if index is not None:
            raw = raw[index] if isinstance(raw, list) and index < len(raw) else None
        values[target] = _number(raw)
    return values


def read_current_weather(latitude, longitude, get=None):
    """Current outdoor conditions, or None when Open-Meteo did not answer.

    `time` is Unix seconds for the start of the 15-minute model step.
    """
    get = get or requests.get
    try:
        response = get(
            FORECAST_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": ",".join(WEATHER_FIELDS),
                "timezone": "auto",
                "timeformat": "unixtime",
            },
            timeout=WEATHER_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None
    current = payload.get("current") if isinstance(payload, dict) else None
    if not isinstance(current, dict):
        return None
    values = _weather_values(current)
    if values["outdoor_temp"] is None:
        return None
    values["time"] = _number(current.get("time"))
    values["timezone"] = str(payload.get("timezone") or "")
    return values


def read_hourly_weather(latitude, longitude, past_days, get=None):
    """Hourly outdoor conditions for the last `past_days` days, oldest first.

    Each item is `(unix_seconds, values)`. Empty when Open-Meteo did not answer.
    """
    get = get or requests.get
    days = max(1, min(int(past_days), MAX_PAST_DAYS))
    try:
        response = get(
            FORECAST_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "hourly": ",".join(WEATHER_FIELDS),
                "past_days": days,
                "forecast_days": 1,
                "timezone": "UTC",
                "timeformat": "unixtime",
            },
            timeout=WEATHER_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return []
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    times = hourly.get("time") if isinstance(hourly, dict) else None
    if not isinstance(times, list):
        return []
    hours = []
    for index, stamp in enumerate(times):
        moment = _number(stamp)
        if moment is None:
            continue
        values = _weather_values(hourly, index)
        if values["outdoor_temp"] is None:
            continue
        hours.append((int(moment), values))
    return hours


def search_places(query, get=None, count=8):
    """Places matching a typed name, best match first.

    Accepts `Moose Jaw` or `Moose Jaw, Saskatchewan`. Each place is ready to save.
    Raises ValueError for an empty query and RuntimeError when the lookup fails.
    """
    text = str(query or "").strip()
    if not text:
        raise ValueError("Type a city or town.")
    get = get or requests.get
    try:
        response = get(
            GEOCODING_URL,
            params={"name": text, "count": count, "language": "en", "format": "json"},
            timeout=WEATHER_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError("The place search did not answer. Try again.") from exc
    places = []
    for result in (payload or {}).get("results") or []:
        place = normalize_location(
            {
                "name": place_name(result),
                "latitude": result.get("latitude"),
                "longitude": result.get("longitude"),
                "timezone": result.get("timezone"),
                "source": "search",
            }
        )
        if place is not None:
            places.append(place)
    return places


def reverse_place_name(latitude, longitude, get=None):
    """`Moose Jaw, Saskatchewan, Canada` for a position, or None."""
    get = get or requests.get
    try:
        response = get(
            REVERSE_URL,
            params={
                "lat": latitude,
                "lon": longitude,
                "format": "jsonv2",
                "zoom": 10,
                "accept-language": "en",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=WEATHER_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None
    address = payload.get("address") if isinstance(payload, dict) else None
    if not isinstance(address, dict):
        return None
    parts = []
    for key in (
        "city",
        "town",
        "village",
        "municipality",
        "county",
        "state",
        "country",
    ):
        text = str(address.get(key) or "").strip()
        if text and text not in parts:
            parts.append(text)
        if len(parts) == 3:
            break
    return ", ".join(parts) or None


def parse_detect_output(text):
    """(latitude, longitude, error_text) from the PowerShell script output."""
    try:
        payload = json.loads(str(text or "").strip() or "{}")
    except ValueError:
        return None, None, "Windows did not return a location."
    if not isinstance(payload, dict):
        return None, None, "Windows did not return a location."
    if str(payload.get("permission")) == "Denied":
        return (
            None,
            None,
            "Windows blocked the location. Turn on Settings > Privacy & security > "
            "Location, and Let desktop apps access your location.",
        )
    latitude = _number(payload.get("latitude"))
    longitude = _number(payload.get("longitude"))
    if latitude is None or longitude is None:
        return (
            None,
            None,
            "Windows has no location fix yet. Make sure Location is on, "
            "or search for your city instead.",
        )
    return latitude, longitude, ""


def detect_device_location(run=None, get=None, platform=None):
    """(place, error_text) for where this computer is, from Windows location services."""
    platform = platform or sys.platform
    if platform != "win32":
        return None, "Device location needs Windows. Search for your city instead."
    run = run or subprocess.run
    try:
        completed = run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _DETECT_SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=DETECT_TIMEOUT_SECONDS,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None, "Windows location did not answer. Search for your city instead."
    latitude, longitude, error = parse_detect_output(completed.stdout)
    if error:
        return None, error
    name = reverse_place_name(latitude, longitude, get=get)
    return (
        normalize_location(
            {
                "name": name or "",
                "latitude": latitude,
                "longitude": longitude,
                "source": "device",
            }
        ),
        "",
    )
