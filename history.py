"""Ten-minute history of every miner, with the weather outside, for the History screen.

Samples live in `history.db` next to `config.json`. A baseline reset is kept as
an event, so the screen can show a miner since its fresh start. A repaste date
is saved on the miner in `config.json`, and the screen can start there too.
"""

import datetime
import itertools
import math
import os
import sqlite3
import statistics
import threading
import time
from contextlib import closing

import config
from weather import SKY_LABELS, WEATHER_FIELDS, sky_group

SAMPLE_SECONDS = 10 * 60
# A sample is settled when its setpoint and the miner's uptime are at least this
# old, so its 10-minute hashrate belongs to that setpoint.
SETTLED_SECONDS = 10 * 60
# Current weather older than this is not stored with a sample. A later hourly
# backfill fills it in.
CURRENT_WEATHER_MAX_AGE = 45 * 60
TEMP_BAND_C = 3
MAX_SERIES_POINTS = 360
# A combination needs this many settled samples (30 minutes) to be ranked.
MIN_COMBO_SAMPLES = 3
COMBO_LIMIT = 12
# The outdoor-temperature trend needs this much data and spread.
MIN_TREND_SAMPLES = 12
MIN_TREND_SPREAD_C = 3.0

PERIODS = {
    "24h": 24 * 3600,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "90d": 90 * 86400,
    "all": None,
    "reset": None,
    "repaste": None,
}
METRICS = {
    "good_hashrate": {
        "label": "Good hashrate",
        "unit": "GH/s",
        "higher_is_better": True,
        "digits": 1,
    },
    "efficiency": {
        "label": "Efficiency",
        "unit": "J/TH",
        "higher_is_better": False,
        "digits": 2,
    },
    "frequency": {
        "label": "Clock",
        "unit": "MHz",
        "higher_is_better": True,
        "digits": 0,
    },
}
DAYPARTS = (
    ("night", "Night", 0, 6),
    ("morning", "Morning", 6, 12),
    ("afternoon", "Afternoon", 12, 18),
    ("evening", "Evening", 18, 24),
)

SAMPLE_COLUMNS = (
    "ts",
    "ip",
    "name",
    "frequency",
    "voltage",
    "hashrate",
    "good_hashrate",
    "error_pct",
    "asic_temp",
    "vr_temp",
    "power",
    "settled",
    "phase",
    *WEATHER_FIELDS.values(),
)
_WEATHER_COLUMNS = tuple(WEATHER_FIELDS.values())
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS samples (
        ts INTEGER NOT NULL,
        ip TEXT NOT NULL,
        name TEXT,
        frequency INTEGER,
        voltage INTEGER,
        hashrate REAL,
        good_hashrate REAL,
        error_pct REAL,
        asic_temp REAL,
        vr_temp REAL,
        power REAL,
        settled INTEGER NOT NULL DEFAULT 0,
        phase TEXT,
        outdoor_temp REAL,
        humidity REAL,
        apparent_temp REAL,
        wind_speed REAL,
        cloud_cover REAL,
        precipitation REAL,
        weather_code INTEGER,
        is_day INTEGER,
        PRIMARY KEY (ip, ts)
    )
    """,
    "CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts)",
    """
    CREATE TABLE IF NOT EXISTS events (
        ts INTEGER NOT NULL,
        ip TEXT NOT NULL,
        kind TEXT NOT NULL
    )
    """,
)

_db_lock = threading.Lock()


def history_path():
    """`history.db` beside `config.json`."""
    return os.path.join(
        os.path.dirname(os.path.abspath(config.CONFIG_FILE)), "history.db"
    )


def _connect(path):
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    for statement in _SCHEMA:
        connection.execute(statement)
    return connection


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _positive(value):
    number = _number(value)
    return number if number is not None and number > 0 else None


def _whole(value):
    number = _number(value)
    return None if number is None else int(round(number))


def sample_hashrate(info):
    """GH/s for a sample: the 10-minute rate, then the 1-minute rate, then live."""
    for key in ("hashRate_10m", "hashRate_1m", "hashRate"):
        rate = _positive(info.get(key))
        if rate is not None:
            return rate
    return None


def weather_is_fresh(weather, now):
    if not isinstance(weather, dict):
        return False
    fetched = _number(weather.get("fetched_at"))
    return fetched is not None and now - fetched <= CURRENT_WEATHER_MAX_AGE


def sample_from_info(ip, name, info, phase, settled, weather, now):
    """One history row from an AxeOS reading and the current weather."""
    hashrate = sample_hashrate(info)
    error = _number(info.get("errorPercentage"))
    good = None
    if hashrate is not None:
        kept = 1 - (min(max(error or 0.0, 0.0), 100.0) / 100.0)
        good = hashrate * kept
    sample = {
        "ts": int(now),
        "ip": ip,
        "name": name,
        "frequency": _whole(info.get("frequency")),
        "voltage": _whole(info.get("coreVoltage")),
        "hashrate": hashrate,
        "good_hashrate": good,
        "error_pct": error,
        "asic_temp": _positive(info.get("temp")),
        "vr_temp": _positive(info.get("vrTemp")),
        "power": _positive(info.get("power")),
        "settled": 1 if settled and hashrate is not None else 0,
        "phase": str(phase or ""),
    }
    fresh = weather_is_fresh(weather, now)
    for column in _WEATHER_COLUMNS:
        sample[column] = _number(weather.get(column)) if fresh else None
    return sample


def record_samples(samples, path=None):
    """Save samples. A second sample for the same miner and second replaces the first."""
    if not samples:
        return
    path = path or history_path()
    placeholders = ", ".join("?" for _ in SAMPLE_COLUMNS)
    statement = (
        f"INSERT OR REPLACE INTO samples ({', '.join(SAMPLE_COLUMNS)}) "
        f"VALUES ({placeholders})"
    )
    rows = [
        tuple(sample.get(column) for column in SAMPLE_COLUMNS) for sample in samples
    ]
    with _db_lock, closing(_connect(path)) as connection, connection:
        connection.executemany(statement, rows)


def record_event(ip, kind, ts=None, path=None):
    """Keep a dated event, such as `reset`, for one miner."""
    path = path or history_path()
    moment = int(time.time() if ts is None else ts)
    with _db_lock, closing(_connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO events (ts, ip, kind) VALUES (?, ?, ?)", (moment, ip, kind)
        )


def last_events(kind, path=None):
    """{ip: unix seconds} of the newest event of this kind for each miner."""
    path = path or history_path()
    if not os.path.exists(path):
        return {}
    with _db_lock, closing(_connect(path)) as connection:
        rows = connection.execute(
            "SELECT ip, MAX(ts) AS ts FROM events WHERE kind = ? GROUP BY ip", (kind,)
        ).fetchall()
    return {row["ip"]: int(row["ts"]) for row in rows}


def load_samples(since=None, path=None, ip=None):
    """Samples at or after `since`, oldest first, for one miner or all.

    Empty before the first one is saved.
    """
    path = path or history_path()
    if not os.path.exists(path):
        return []
    query = f"SELECT {', '.join(SAMPLE_COLUMNS)} FROM samples"
    clauses = []
    params = []
    if since is not None:
        clauses.append("ts >= ?")
        params.append(int(since))
    if ip:
        clauses.append("ip = ?")
        params.append(ip)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY ts, ip"
    with _db_lock, closing(_connect(path)) as connection:
        return [dict(row) for row in connection.execute(query, params).fetchall()]


def oldest_missing_weather(since, path=None):
    """Unix seconds of the oldest sample after `since` with no outdoor reading."""
    path = path or history_path()
    if not os.path.exists(path):
        return None
    with _db_lock, closing(_connect(path)) as connection:
        row = connection.execute(
            "SELECT MIN(ts) AS ts FROM samples WHERE outdoor_temp IS NULL AND ts >= ?",
            (int(since),),
        ).fetchone()
    return None if row is None or row["ts"] is None else int(row["ts"])


def fill_missing_weather(hours, path=None):
    """Give samples with no outdoor reading the hourly weather nearest to them.

    `hours` is `[(unix_seconds, values), ...]` from `read_hourly_weather`.
    Returns how many samples were filled.
    """
    if not hours:
        return 0
    path = path or history_path()
    if not os.path.exists(path):
        return 0
    assignments = ", ".join(f"{column} = ?" for column in _WEATHER_COLUMNS)
    statement = (
        f"UPDATE samples SET {assignments} "
        "WHERE outdoor_temp IS NULL AND ts >= ? AND ts < ?"
    )
    filled = 0
    with _db_lock, closing(_connect(path)) as connection, connection:
        for moment, values in hours:
            cursor = connection.execute(
                statement,
                (
                    *(values.get(column) for column in _WEATHER_COLUMNS),
                    moment - 1800,
                    moment + 1800,
                ),
            )
            filled += cursor.rowcount
    return filled


class HistoryRecorder:
    """Save every miner the window reads, once per SAMPLE_SECONDS.

    The window reads each miner every few seconds. The recorder tracks how long
    each setpoint has held, and writes all miners together so a fleet total
    belongs to one moment. The first write waits a full interval.
    """

    def __init__(self, path_fn=history_path, interval=SAMPLE_SECONDS, clock=None):
        self._path_fn = path_fn
        self._interval = interval
        self._clock = clock or time.time
        self._setpoints = {}
        self._next_write = None

    def forget(self, ip):
        """Start the settle clock again, as after a baseline reset."""
        self._setpoints.pop(ip, None)

    def observe(self, readings, weather):
        """`readings` is `[(ip, name, info, phase), ...]`. Returns how many rows were saved."""
        now = self._clock()
        live = []
        for ip, name, info, phase in readings:
            if not isinstance(info, dict):
                continue
            setpoint = (info.get("frequency"), info.get("coreVoltage"))
            known = self._setpoints.get(ip)
            if known is None or known[0] != setpoint:
                self._setpoints[ip] = (setpoint, now)
            live.append((ip, name, info, phase))
        if self._next_write is None:
            self._next_write = now + self._interval
        if now < self._next_write or not live:
            return 0
        self._next_write = now + self._interval
        samples = []
        for ip, name, info, phase in live:
            held = now - self._setpoints[ip][1]
            uptime = _number(info.get("uptimeSeconds"))
            settled = held >= SETTLED_SECONDS and (
                uptime is None or uptime >= SETTLED_SECONDS
            )
            samples.append(
                sample_from_info(ip, name, info, phase, settled, weather, now)
            )
        record_samples(samples, self._path_fn())
        return len(samples)


def metric_value(sample, metric):
    """The chosen metric for one sample, or None."""
    if metric == "efficiency":
        power = _positive(sample.get("power"))
        rate = _positive(sample.get("hashrate"))
        if power is None or rate is None:
            return None
        return power / (rate / 1000.0)
    if metric == "frequency":
        return _positive(sample.get("frequency"))
    return _positive(sample.get("good_hashrate"))


def _first(values):
    for value in values:
        if value is not None:
            return value
    return None


def _mean(values):
    kept = [value for value in values if value is not None]
    return sum(kept) / len(kept) if kept else None


def fleet_points(samples, metric):
    """One point per sample round for the whole fleet.

    Good hashrate adds up, efficiency is total watts over total hashrate, and
    clock is the average. A round is settled only when every miner in it is.
    """
    rounds = {}
    for sample in samples:
        rounds.setdefault(int(sample["ts"]) // SAMPLE_SECONDS, []).append(sample)
    points = []
    for key in sorted(rounds):
        group = rounds[key]
        if metric == "efficiency":
            power = sum(_positive(item.get("power")) or 0 for item in group)
            rate = sum(_positive(item.get("hashrate")) or 0 for item in group)
            value = power / (rate / 1000.0) if power > 0 and rate > 0 else None
        elif metric == "frequency":
            value = _mean(_positive(item.get("frequency")) for item in group)
        else:
            rates = [_positive(item.get("good_hashrate")) for item in group]
            value = sum(rate for rate in rates if rate) or None
        point = {
            "ts": max(int(item["ts"]) for item in group),
            "value": value,
            "settled": all(item.get("settled") for item in group),
            "asic_temp": _mean(_positive(item.get("asic_temp")) for item in group),
            "frequency": _mean(_positive(item.get("frequency")) for item in group),
            "voltage": _mean(_positive(item.get("voltage")) for item in group),
            "miners": len(group),
        }
        for column in _WEATHER_COLUMNS:
            point[column] = _first(_number(item.get(column)) for item in group)
        points.append(point)
    return points


def miner_points(samples, metric):
    points = []
    for sample in samples:
        point = dict(sample)
        point["value"] = metric_value(sample, metric)
        point["settled"] = bool(sample.get("settled"))
        point["miners"] = 1
        points.append(point)
    return points


def daypart(hour):
    for key, label, start, end in DAYPARTS:
        if start <= hour < end:
            return key, label
    return "night", "Night"


def local_hour(ts):
    return time.localtime(int(ts)).tm_hour


def temp_band(outdoor_temp):
    """Lower edge of the TEMP_BAND_C band holding this temperature."""
    return int(math.floor(outdoor_temp / TEMP_BAND_C) * TEMP_BAND_C)


def band_label(low):
    return f"{low}–{low + TEMP_BAND_C} °C"


def _better(value, other, higher_is_better):
    return value > other if higher_is_better else value < other


def _downsample(rows, span):
    """Average (ts, value) rows into at most MAX_SERIES_POINTS buckets."""
    if not rows:
        return []
    width = max(SAMPLE_SECONDS, math.ceil(span / MAX_SERIES_POINTS))
    buckets = {}
    for ts, value in rows:
        if value is None:
            continue
        buckets.setdefault(int(ts) // width, []).append((int(ts), value))
    points = []
    for key in sorted(buckets):
        group = buckets[key]
        points.append(
            [
                int(sum(ts for ts, _value in group) / len(group)),
                sum(value for _ts, value in group) / len(group),
            ]
        )
    return points


def outdoor_trend(points):
    """Least-squares change in the metric per 1 °C outside, or None."""
    pairs = [
        (point["outdoor_temp"], point["value"])
        for point in points
        if point.get("outdoor_temp") is not None and point.get("value") is not None
    ]
    if len(pairs) < MIN_TREND_SAMPLES:
        return None
    temps = [temp for temp, _value in pairs]
    if max(temps) - min(temps) < MIN_TREND_SPREAD_C:
        return None
    mean_temp = sum(temps) / len(temps)
    mean_value = sum(value for _temp, value in pairs) / len(pairs)
    # Never zero: the temperatures above span at least MIN_TREND_SPREAD_C.
    spread = sum((temp - mean_temp) ** 2 for temp in temps)
    return (
        sum((temp - mean_temp) * (value - mean_value) for temp, value in pairs) / spread
    )


def _conditions(point):
    hour = local_hour(point["ts"])
    part_key, part_label = daypart(hour)
    group = sky_group(point.get("weather_code"))
    return {
        "ts": int(point["ts"]),
        "hour": hour,
        "daypart": part_key,
        "daypart_label": part_label,
        "outdoor_temp": point.get("outdoor_temp"),
        "humidity": point.get("humidity"),
        "sky": group,
        "sky_label": SKY_LABELS.get(group, ""),
        "is_day": point.get("is_day"),
        "asic_temp": point.get("asic_temp"),
        "frequency": point.get("frequency"),
        "voltage": point.get("voltage"),
    }


def summarize(samples, metric, ip=None):
    """Everything the History screen draws for one miner or the whole fleet.

    `samples` are already limited to the chosen period. Results (best, typical,
    heatmap, combinations, trend) use settled samples only. The time series
    shows every sample.
    """
    if metric not in METRICS:
        metric = "good_hashrate"
    meta = METRICS[metric]
    higher = meta["higher_is_better"]
    if ip:
        samples = [sample for sample in samples if sample["ip"] == ip]
        points = miner_points(samples, metric)
    else:
        points = fleet_points(samples, metric)
    settled = [
        point for point in points if point["settled"] and point["value"] is not None
    ]
    first = min((int(sample["ts"]) for sample in samples), default=None)
    last = max((int(sample["ts"]) for sample in samples), default=None)
    span = (last - first) if first is not None else 0

    series = {}
    by_ip = {}
    for sample in samples:
        by_ip.setdefault(sample["ip"], []).append(
            (sample["ts"], metric_value(sample, metric))
        )
    if ip:
        series[ip] = _downsample(by_ip.get(ip, []), span)
    else:
        for miner_ip, rows in by_ip.items():
            series[miner_ip] = _downsample(rows, span)
    outdoor = _downsample(
        [(point["ts"], point.get("outdoor_temp")) for point in points], span
    )

    best = None
    for point in settled:
        if best is None or _better(point["value"], best["value"], higher):
            best = point
    values = [point["value"] for point in settled]
    weathered = [point for point in settled if point.get("outdoor_temp") is not None]

    cells = {}
    combos = {}
    for point in weathered:
        band = temp_band(point["outdoor_temp"])
        hour = local_hour(point["ts"])
        cell = cells.setdefault((band, hour), [])
        cell.append(point["value"])
        part_key, part_label = daypart(hour)
        group = sky_group(point.get("weather_code")) or "unknown"
        combo = combos.setdefault(
            (band, part_key, group),
            {
                "band": band,
                "band_label": band_label(band),
                "daypart": part_key,
                "daypart_label": part_label,
                "sky": group,
                "sky_label": SKY_LABELS.get(group, "Unknown"),
                "values": [],
                "asic": [],
                "frequency": [],
                "voltage": [],
            },
        )
        combo["values"].append(point["value"])
        combo["asic"].append(point.get("asic_temp"))
        combo["frequency"].append(point.get("frequency"))
        combo["voltage"].append(point.get("voltage"))

    bands = sorted({band for band, _hour in cells}, reverse=True)
    heatmap = {
        "bands": [{"low": band, "label": band_label(band)} for band in bands],
        "cells": [
            {
                "band": band,
                "hour": hour,
                "value": sum(cell) / len(cell),
                "count": len(cell),
            }
            for (band, hour), cell in sorted(cells.items())
        ],
    }
    ranked = []
    for combo in combos.values():
        if len(combo["values"]) < MIN_COMBO_SAMPLES:
            continue
        ranked.append(
            {
                "band": combo["band"],
                "band_label": combo["band_label"],
                "daypart": combo["daypart"],
                "daypart_label": combo["daypart_label"],
                "sky": combo["sky"],
                "sky_label": combo["sky_label"],
                "value": sum(combo["values"]) / len(combo["values"]),
                "best": (max if higher else min)(combo["values"]),
                "asic_temp": _mean(combo["asic"]),
                "frequency": _mean(combo["frequency"]),
                "voltage": _mean(combo["voltage"]),
                "count": len(combo["values"]),
                "hours": len(combo["values"]) * SAMPLE_SECONDS / 3600,
            }
        )
    ranked.sort(key=lambda item: item["value"], reverse=higher)

    outdoor_values = [point["outdoor_temp"] for point in weathered]
    return {
        "metric": metric,
        "meta": dict(meta),
        "first": first,
        "last": last,
        "sample_count": len(points),
        "settled_count": len(settled),
        "hours": len(points) * SAMPLE_SECONDS / 3600,
        "best": (
            None
            if best is None
            else {
                "value": best["value"],
                "ip": best.get("ip") if ip else None,
                **_conditions(best),
            }
        ),
        "typical": statistics.median(values) if values else None,
        "outdoor_low": min(outdoor_values) if outdoor_values else None,
        "outdoor_high": max(outdoor_values) if outdoor_values else None,
        "trend_per_c": outdoor_trend(settled),
        "series": series,
        "outdoor": outdoor,
        "heatmap": heatmap,
        "combos": ranked[:COMBO_LIMIT],
        "combo_total": len(ranked),
    }


def day_start(date_text):
    """Unix seconds at local midnight of a `YYYY-MM-DD` date, or None."""
    try:
        day = datetime.date.fromisoformat(str(date_text or "").strip())
    except ValueError:
        return None
    return int(time.mktime(day.timetuple()))


def repaste_moments(days, path=None):
    """{ip: unix seconds} for repaste days, moved to when each miner came back.

    `days` is `{ip: local midnight}`. A repaste powers the miner off, so its
    samples that day have a gap. The first sample after the longest gap of at
    least two sample intervals marks the new paste. Without one, midnight stays.
    """
    moments = dict(days)
    path = path or history_path()
    if not days or not os.path.exists(path):
        return moments
    with _db_lock, closing(_connect(path)) as connection:
        for ip, start in days.items():
            rows = connection.execute(
                "SELECT ts FROM samples WHERE ip = ? AND ts >= ? AND ts < ? "
                "ORDER BY ts",
                (ip, start - 2 * SAMPLE_SECONDS, start + 86400),
            ).fetchall()
            stamps = [int(row["ts"]) for row in rows]
            longest = 2 * SAMPLE_SECONDS - 1
            for before, after in itertools.pairwise(stamps):
                if after >= start and after - before > longest:
                    longest = after - before
                    moments[ip] = after
    return moments


def period_samples(period, now, ip=None, path=None, repastes=None):
    """Samples for a period key.

    `reset` starts each miner at its last baseline reset, and `repaste` at its
    repaste. `repastes` is `{ip: unix seconds}` from `repaste_moments`.
    """
    seconds = PERIODS.get(period, PERIODS["7d"])
    if period in ("reset", "repaste"):
        if period == "reset":
            starts = last_events("reset", path)
        else:
            starts = dict(repastes or {})
        if ip:
            return load_samples(starts.get(ip), path, ip)
        # A miner with no start keeps all of its samples.
        samples = load_samples(None, path)
        return [
            sample for sample in samples if sample["ts"] >= starts.get(sample["ip"], 0)
        ]
    since = None if seconds is None else now - seconds
    return load_samples(since, path, ip)
