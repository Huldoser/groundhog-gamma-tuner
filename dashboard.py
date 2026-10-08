"""Local dashboard window for the Bitaxe tuner.

The page is a file inside a pywebview window. Python keeps the tuner threads
and hands the page a snapshot to poll. Nothing listens on the network.
"""

import ipaddress
import math
import os
import platform
import re
import socket
import sqlite3
import sys
import threading
import time
from collections import deque
from datetime import datetime

import requests

import history
import modes
import weather
from autotune import (
    HASHRATE_1M_SETTLE_SECONDS,
    RAMP_HEADROOM_C,
    STARTUP_STAGGER_SECONDS,
    TRIP_GUARD_FREQUENCY_STEPS,
    TRIP_SAFE_MAX_TEMP,
    TRIP_SAFE_MAX_VR_TEMP,
    _overheat_mode_set,
    _power_fault_set,
    _publish_status,
    coerce_limit,
    coerce_real_limit,
    continue_from_live_enabled,
    core_amps_cap,
    core_current_amps,
    fast_start_enabled,
    get_miner_status,
    get_system_info,
    monitor_and_adjust,
    normalize_input_voltage,
    overheat_ready_to_clear,
    patch_system,
    ramp_headroom,
    reset_miners_to_baseline,
    restart_bitaxe,
    restart_miners,
    restart_was_accepted,
)
from boards import (
    GAMMA_601,
    TPS546,
    asic_temp,
    board_for_info,
    board_for_record,
    board_list_note,
)
from config import (
    CONFIG_CORRUPT_MESSAGE,
    DEFAULT_MAX_DROOP_MV,
    FIRMWARE_ASIC_TRIP_C,
    FIRMWARE_TRIP_MARGIN_C,
    FIRMWARE_VR_TRIP_C,
    INTERNET_SWITCHES,
    adopted_hostname,
    board_limits,
    config_problem,
    detect_miners,
    get_default_config,
    get_miner_defaults,
    get_miners,
    internet_switch_on,
    load_config,
    miner_type_from_info,
    modify_config,
    new_miner_mode,
    remove_miner,
    update_miner,
)
from desktop import format_local_time, notify, snap_fullscreen_window

STATUS_REFRESH_SECONDS = 5
# Reads a miner may miss in a row before it shows as offline. A reboot or a
# Wi-Fi blip misses one or two.
OFFLINE_AFTER_MISSES = 3
# Core current this close to a miner's cap shows amber in the table.
CORE_AMPS_WARN_BAND = 1.0
# Reads in a row with core voltage this far under its setting before the table
# marks droop. The rail can lag a moment after a voltage change.
DROOP_ALERT_READS = 3
LOG_LIMIT = 500
WEAK_WIFI_DBM = -70
NETWORK_REFRESH_SECONDS = 60
DIFFICULTY_URL = "https://mempool.space/api/v1/mining/hashrate/3d"
FIRMWARE_RELEASES_URL = (
    "https://api.github.com/repos/bitaxeorg/ESP-Miner/releases?per_page=100"
)
_STABLE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_INSTALLED_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")

FREQ_FIELDS = (("min_freq", "Min"), ("start_freq", "Start"), ("max_freq", "Max"))
VOLT_FIELDS = (("min_volt", "Min"), ("start_volt", "Start"), ("max_volt", "Max"))
LIMIT_FIELDS = (
    ("max_temp", "ASIC temp (°C)"),
    ("max_watts", "Watts"),
    ("max_vr_temp", "VR temp (°C)"),
    ("min_input_voltage", "Input voltage (V)"),
    ("max_error_percentage", "Error %"),
    ("max_droop_mv", "Droop (mV)"),
    ("max_core_amps", "Core current (A)"),
)
# Limits added after miners were already saved. A miner without one shows its
# board's default instead of a blank, which Save would read as "turn this miner off".
NEW_LIMIT_FIELDS = ("max_core_amps",)
ALL_AUTOTUNE_FIELDS = tuple(
    field for field, _label in (*FREQ_FIELDS, *VOLT_FIELDS, *LIMIT_FIELDS)
)
GLOBAL_INT_FIELDS = (
    "voltage_step",
    "frequency_step",
    "monitor_interval",
    "refresh_interval",
    "default_target_temp",
    "temp_tolerance",
    "vr_temp_tolerance",
)
# A /22. A home network is a /24 (254 addresses).
MAX_SCAN_ADDRESSES = 1024
START_REQUIRED_FIELDS = (
    "min_freq",
    "max_freq",
    "min_volt",
    "max_volt",
    "max_temp",
    "max_watts",
    "max_vr_temp",
)


def _cells(limits):
    """Limits as the strings the AutoTuner form shows."""
    return {
        field: "" if limits.get(field) in (None, "") else str(limits.get(field))
        for field in ALL_AUTOTUNE_FIELDS
    }


def _same_cells(fields, preset, board):
    """True when every limit the board uses matches the preset."""
    for field in required_fields({"board": board.version}):
        if parse_display_number(fields.get(field)) != parse_display_number(
            preset.get(field)
        ):
            return False
    return True


def required_fields(miner, fields=ALL_AUTOTUNE_FIELDS):
    """The limit fields this miner needs. Its board's unused ones are left out."""
    unused = board_for_record(miner).unused_limit_fields
    return tuple(field for field in fields if field not in unused)


def missing_start_fields(miner):
    """Limits a miner still needs before it can start."""
    return [
        field
        for field in required_fields(miner, START_REQUIRED_FIELDS)
        if field not in miner or miner[field] == "" or miner[field] is None
    ]


def needs_experimental_ok(miner):
    """True for a miner on an unverified board that has not been confirmed yet."""
    return not board_for_record(miner).verified and not miner.get("experimental_ok")


SETUP_COOLING = ("stock", "upgraded", "custom")
SETUP_GOALS = ("hashrate", "balance", "efficiency")


def setup_mode(cooling, goal, acknowledged=False):
    """The new-miner mode for the first-run answers.

    Max hashrate needs custom cooling and an explicit OK; otherwise the most
    hashrate a stock or upgraded cooler should give is Balanced.
    """
    if goal == "efficiency":
        return modes.MODES[modes.EFFICIENCY_MODE]
    if goal == "hashrate" and cooling == "custom" and acknowledged:
        return modes.MODES[modes.MAX_HASHRATE]
    return modes.MODES[modes.BALANCED]


EXPERIMENTAL_PROMPT = (
    "Nobody has verified {boards} with this tuner yet.\n\n"
    "{names} will tune inside their boards' AxeOS presets, with no voltage above "
    "the top preset. Check their limits in AutoTuner Settings and watch the "
    "first session.\n\n"
    "Start anyway? You are asked once per miner."
)


def format_log_line(message, moment=None):
    """One activity line: [YYYY-MM-DD HH:MM:SS] [hostname] message.

    Tuner lines arrive as ``hostname -> message``. Other lines have no host.
    """
    moment = moment or datetime.now()
    stamp = moment.strftime("%Y-%m-%d %H:%M:%S")
    text = "" if message is None else str(message)
    host, separator, body = text.partition(" -> ")
    if separator and host.strip() and "\n" not in host:
        return f"[{stamp}] [{host.strip()}] {body}"
    return f"[{stamp}] {text}"


def replace_ips_with_names(message, names):
    """Swap known miner IPs for nicknames. Longer addresses are replaced first.

    A nickname that already contains its IP is left alone so Miner-192.168.1.10
    does not become Miner-Miner-192.168.1.10.
    """
    text = "" if message is None else str(message)
    if not text or not names:
        return text
    for ip in sorted(names, key=len, reverse=True):
        name = str(names.get(ip) or "").strip()
        if not ip or not name or name == ip or ip in name:
            continue
        text = re.sub(
            rf"\b{re.escape(str(ip))}\b",
            lambda _match, replacement=name: replacement,
            text,
        )
    return text


def parse_autotuner_value(field, raw, board=GAMMA_601):
    """Parse one AutoTuner cell. Frequency and voltage are clamped to the board's range."""
    text = str(raw).strip()
    if text == "":
        return ""
    value = float(text)
    # float() accepts "nan" and "inf". A NaN cap makes every "over the cap"
    # test false, so it would switch that limit off.
    if not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    if field in (
        "min_input_voltage",
        "max_error_percentage",
        "max_temp",
        "max_watts",
        "max_vr_temp",
    ):
        return value
    hard = board.limits
    if field == "max_core_amps":
        # The regulator shuts down with no retry 1 A over the hard cap.
        if hard.max_core_amps is None:
            return value
        return max(1.0, min(hard.max_core_amps, value))
    number = int(value)
    if field in ("min_freq", "max_freq", "start_freq"):
        return max(hard.min_freq, min(hard.max_freq, number))
    if field in ("min_volt", "max_volt", "start_volt"):
        return max(hard.min_volt, min(hard.max_volt, number))
    return number


def limit_order_error(fields, label):
    """Error text when min, start, and max are out of order. Empty when they are fine.

    A blank cell turns that miner off, so a missing number is not an order error.
    """

    def number(key):
        value = fields.get(key)
        if value == "" or value is None:
            return None
        return value

    def check(low_key, mid_key, high_key, name):
        low, mid, high = number(low_key), number(mid_key), number(high_key)
        if low is not None and high is not None and low > high:
            return f"{label}: {name} min must be at or below max."
        if low is not None and mid is not None and mid < low:
            return f"{label}: {name} start must be at or above min."
        if high is not None and mid is not None and mid > high:
            return f"{label}: {name} start must be at or below max."
        return ""

    return check("min_freq", "start_freq", "max_freq", "Frequency") or check(
        "min_volt", "start_volt", "max_volt", "Voltage"
    )


_LIMIT_LABELS = {
    "silicon": "chip errors",
    "hash": "low hashrate",
    "thermal": "temperature",
    "power": "power",
    "reject": "rejected shares",
    "input": "input sag",
    "current": "core current",
}


# AxeOS shows "Danger: Low Voltage" under this share of the nominal input.
AXEOS_LOW_INPUT_SHARE = 0.949


def _mode_row(mode, board):
    """One Modes row: name, key numbers on this board, and what it is for."""
    preset = board_limits(board, mode=mode.key)
    caps = f"{preset['max_temp']:g} °C ASIC"
    if board.has_vr_temp:
        caps += f" / {preset['max_vr_temp']:g} °C VR"
    fan = "auto fan" if mode.auto_fan else "fan 100%"
    return [mode.name, f"up to {preset['max_volt']} mV · {caps} · {fan}", mode.summary]


def hardware_limits(board=GAMMA_601, mode=None):
    """Sections for the Limits screen: what the chip, regulator, firmware,
    and tuner allow. Tuner numbers come from the code, so the page cannot drift.

    Each section is `{"title", "intro", "rows"}`; each row is `[name, value, note]`.
    """
    new_mode = new_miner_mode(board, mode=mode)
    defaults = board_limits(board, mode=new_mode.key)
    hard = board.limits
    family = board.family
    regulator = family.regulator
    hashrate_per_mhz = board.hashrate_per_mhz
    cores = board.asic.small_cores * family.asic_count
    low_input = round(AXEOS_LOW_INPUT_SHARE * family.nominal_voltage, 3)
    full_push_watts = board.board_power_w + (hard.max_core_amps or 0) * (
        hard.max_volt * family.voltage_domains / 1000
    )
    sections = [
        {
            "title": "AxeOS firmware",
            "intro": "Built into AxeOS v2.15.3. The tuner cannot change these; it stays under them.",
            "rows": [
                [
                    "ASIC overheat cutoff",
                    f"{FIRMWARE_ASIC_TRIP_C:g} °C",
                    "Stops mining, cools, then restarts 100 MHz and 100 mV lower.",
                ],
                [
                    "Regulator overheat cutoff",
                    f"{FIRMWARE_VR_TRIP_C:g} °C",
                    "Same as the ASIC cutoff, on the regulator sensor.",
                ],
                [
                    "Low input warning",
                    f"under {low_input:g} V",
                    "Shown as Danger: Low Voltage on the AxeOS page.",
                ],
                [
                    "Board share of power",
                    f"{board.board_power_w:g} W",
                    "AxeOS adds this to the regulator's output in the power reading.",
                ]
                if board.vr_chip == TPS546
                else [
                    "Power reading",
                    "at the input",
                    "The INA260 measures the whole board. `current` is input current.",
                ],
            ],
        },
    ]
    if board.vr_chip == TPS546:
        sections.append(
            {
                "title": "Voltage regulator (TPS546)",
                "intro": f"How AxeOS programs the {family.name}'s core regulator.",
                "rows": [
                    [
                        "Input on / off",
                        f"{regulator.vin_on:g} V / {regulator.vin_off:g} V",
                        "Under the off voltage the core loses power and the ASIC stops.",
                    ],
                    [
                        "Input over-voltage fault",
                        f"{regulator.vin_ov_fault:g} V",
                        "Highest supply voltage the board accepts.",
                    ],
                    [
                        "Core voltage range",
                        f"{regulator.vout_min:.1f}–{regulator.vout_max:.1f} V",
                        "Anything outside is refused.",
                    ],
                    [
                        "Core current warning",
                        f"{regulator.iout_warn:g} A",
                        "Flag only. Mining continues.",
                    ],
                    [
                        "Core current shutdown",
                        f"{regulator.iout_fault:g} A",
                        "Shuts down with no retry. Needs a restart.",
                    ],
                    [
                        "Temperature warning / shutdown",
                        f"{regulator.ot_warn} °C / {regulator.ot_fault} °C",
                        "Restarts by itself once it cools under the warning.",
                    ],
                ],
            }
        )
    sections.append(
        {
            "title": f"{board.asic.model} on the {board.name}",
            "intro": "",
            "rows": [
                [
                    "Stock clocks",
                    f"{board.asic.stock_freq} MHz / {board.asic.stock_volt} mV",
                    f"How the {board.name} ships.",
                ],
                [
                    "Lowest presets",
                    f"{hard.min_freq} MHz / {hard.min_volt} mV",
                    f"Lowest {board.asic.model} clock and voltage in the AxeOS preset lists.",
                ],
                [
                    "Hashrate per MHz",
                    f"{hashrate_per_mhz:g} GH/s",
                    f"{cores:,} small cores. 1,000 MHz is about "
                    f"{round(hashrate_per_mhz, 1):g} TH/s.",
                ],
            ],
        }
    )
    tuner_rows = [
        ["Frequency", f"{hard.min_freq}–{hard.max_freq} MHz", ""],
        ["Core voltage", f"{hard.min_volt}–{hard.max_volt} mV", ""],
        [
            "ASIC cap",
            f"up to {TRIP_SAFE_MAX_TEMP:g} °C",
            f"{FIRMWARE_TRIP_MARGIN_C:g} °C under the AxeOS cutoff. At it, the "
            f"tuner sheds {TRIP_GUARD_FREQUENCY_STEPS * 5} MHz and 10 mV at once.",
        ],
        [
            "Regulator cap",
            f"up to {TRIP_SAFE_MAX_VR_TEMP:g} °C",
            f"{FIRMWARE_TRIP_MARGIN_C:g} °C under the AxeOS cutoff.",
        ],
    ]
    if hard.max_core_amps is not None:
        tuner_rows.append(
            [
                "Core current cap",
                f"up to {hard.max_core_amps:g} A",
                f"Under the regulator's {regulator.iout_fault:g} A shutdown.",
            ]
        )
    sections.append(
        {
            "title": "Tuner hard limits",
            "intro": "No setting goes past these.",
            "rows": tuner_rows,
        }
    )
    default_rows = [
        [
            "Start clocks",
            f"{defaults['start_freq']} MHz / {defaults['start_volt']} mV",
            "Used when a miner is not already hashing inside its limits.",
        ],
        ["Max voltage", f"{defaults['max_volt']} mV", ""],
        ["ASIC temperature", f"{defaults['max_temp']} °C", ""],
    ]
    if board.has_vr_temp:
        default_rows.append(
            ["Regulator temperature", f"{defaults['max_vr_temp']} °C", ""]
        )
    if board.has_core_current:
        default_rows.append(
            [
                "Core current",
                f"{defaults['max_core_amps']:g} A",
                f"Fast start aims {ramp_headroom(board)[1]:g} A under it.",
            ]
        )
    default_rows += [
        ["Power", f"{defaults['max_watts']} W", "A runaway guard."],
        [
            "Input voltage floor",
            f"{defaults['min_input_voltage']:g} V",
            f"Steps down when the {family.nominal_voltage} V input sags under it.",
        ],
        ["Error budget", f"{defaults['max_error_percentage']:g}%", ""],
        [
            "Fast start headroom",
            f"{RAMP_HEADROOM_C:g} °C",
            "Jumps aim this far under both temperature caps.",
        ],
    ]
    intro = f"{new_mode.name} mode. Per miner in AutoTuner Settings."
    if new_mode.key == modes.MAX_HASHRATE and board is GAMMA_601:
        intro = "Per miner in AutoTuner Settings."
    if not board.verified:
        intro += (
            " Nobody has verified this board with the tuner yet, so it stays "
            "inside its AxeOS presets."
        )
    sections.append(
        {"title": "Defaults for a new miner", "intro": intro, "rows": default_rows}
    )
    sections.append(
        {
            "title": "Modes",
            "intro": "Pick one per miner in AutoTuner Settings. It fills in the "
            "limits; you can still change them.",
            "rows": [_mode_row(modes.MODES[key], board) for key in modes.MODE_ORDER],
        }
    )
    if family.name != "Gamma" or hard.max_core_amps is None:
        # The barrel-plug notes below are the Gamma's.
        return sections
    sections.extend(
        [
            {
                "title": "5 V supply and wiring",
                "intro": "Where heat collects outside the board.",
                "rows": [
                    [
                        "Board at full push",
                        f"about {full_push_watts:.0f} W",
                        f"{hard.max_core_amps:g} A at {hard.max_volt} mV, plus the board. "
                        f"About {full_push_watts / family.nominal_voltage:.0f} A through "
                        f"the {family.nominal_voltage} V plug.",
                    ],
                    [
                        "5.5 × 2.1 mm barrel plug",
                        "often rated 5 A",
                        "The hottest point at full push. Too hot to hold means lower that miner's Watts.",
                    ],
                    [
                        "Wire",
                        "18 AWG or thicker",
                        "Short runs, one cable per miner, tight terminals.",
                    ],
                ],
            },
        ]
    )
    return sections


def live_limit(status):
    """The plain limit that stopped this session's climb, or ""."""
    wall = str((status or {}).get("wall_type") or "").strip().lower()
    return _LIMIT_LABELS.get(wall, "")


def parse_repaste_date(value, today=None):
    """(`YYYY-MM-DD` or "", error text) for the day a miner got new thermal paste.

    Blank clears the date. A day after today is refused.
    """
    text = str(value or "").strip()
    if not text:
        return "", ""
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return "", "Repaste date must look like 2026-10-04."
    if day > (today or datetime.now().date()):
        return "", "Repaste date cannot be in the future."
    return day.isoformat(), ""


def repaste_starts(miners):
    """{ip: unix seconds at local midnight of its repaste day} for miners with one."""
    starts = {}
    for miner in miners:
        start = history.day_start(miner.get("repasted_on"))
        if miner.get("ip") and start is not None:
            starts[miner["ip"]] = start
    return starts


def parse_display_number(value):
    if value in (None, "", "-"):
        return None
    cleaned = (
        str(value)
        .replace("°C", "")
        .replace("°", "")
        .replace("%", "")
        .replace(",", "")
        .strip()
    )
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def format_number(value, digits):
    number = parse_display_number(value)
    if number is None:
        return "-"
    if digits == 0:
        return str(int(round(number)))
    return f"{number:.{digits}f}"


_DIFF_SUFFIXES = ("", "k", "M", "G", "T", "P")


def _plain_number(value):
    """A JSON number or a plain numeric string. SI text such as 270M is not a number."""
    if isinstance(value, bool) or value in (None, "", "-"):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def format_difficulty(value):
    """Format an AxeOS difficulty the way the miner UI does: 49224525 -> 49.22M."""
    number = _plain_number(value)
    if number is None:
        return "-"
    scaled = float(number)
    index = 0
    while scaled >= 1000 and index < len(_DIFF_SUFFIXES) - 1:
        scaled /= 1000.0
        index += 1
    if index == 0:
        return str(int(round(scaled)))
    return f"{scaled:.2f}{_DIFF_SUFFIXES[index]}"


def difficulty_title(value):
    """Full difficulty for the cell tooltip. Empty when the value is not numeric."""
    number = _plain_number(value)
    if number is None:
        return ""
    if isinstance(number, int) or float(number).is_integer():
        return str(int(number))
    return str(number)


def format_shares(accepted, rejected):
    """Accepted/rejected share counts from the current miner session."""
    accepted_number = _plain_number(accepted)
    rejected_number = _plain_number(rejected)
    if accepted_number is None or rejected_number is None:
        return "-"

    def count_text(number):
        if isinstance(number, int) or float(number).is_integer():
            return str(int(number))
        return str(int(round(float(number))))

    return f"{count_text(accepted_number)}/{count_text(rejected_number)}"


def _count_text(number):
    if isinstance(number, int) or float(number).is_integer():
        return str(int(number))
    return str(number)


def format_input_voltage(value):
    """Board input voltage in volts. AxeOS reports millivolts above 20."""
    return format_number(normalize_input_voltage(value), 2)


def format_minute_hashrate(info):
    """The 1-minute rate the tuner trusts. Live rate stays in the tooltip."""
    if (
        not isinstance(info, dict)
        or "hashRate_1m" not in info
        or info.get("hashRate_1m") is None
    ):
        return "-"
    return format_number(info.get("hashRate_1m"), 2)


def format_efficiency(power, hashrate, power_fault):
    """Joules per terahash from the 1-minute rate. Blank when the rate is not real."""
    if _power_fault_set(power_fault):
        return "-"
    rate = _plain_number(hashrate)
    watts = _plain_number(power)
    if rate is None or rate <= 0 or watts is None:
        return "-"
    return format_number(watts / (rate / 1000.0), 2)


def format_uptime(seconds):
    """Short age and the whole seconds used to sort it. Two largest units."""
    number = _plain_number(seconds)
    if number is None or number < 0:
        return "-", None
    total = int(number)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts), total


def format_hashrate(value):
    """AxeOS reports GH/s. Below 1 TH/s stays in GH/s; 1 TH/s and above is TH/s."""
    number = _plain_number(value)
    if number is None:
        return "-"
    if number >= 1000:
        return f"{format_number(number / 1000.0, 2)} TH/s"
    return f"{format_number(number, 2)} GH/s"


def format_hash_title(info):
    """Live, 10-minute, and expected hashrate for the tooltip."""
    if not isinstance(info, dict):
        return ""
    lines = []
    live = format_hashrate(info.get("hashRate"))
    ten = format_hashrate(info.get("hashRate_10m"))
    expected = format_hashrate(info.get("expectedHashrate"))
    if live != "-":
        lines.append(f"Live {live}")
    if ten != "-":
        lines.append(f"10m {ten}")
    if expected != "-":
        lines.append(f"Expected {expected}")
    return "\n".join(lines)


def core_droop_mv(setpoint, actual):
    """Setpoint minus measured core voltage, in millivolts."""
    set_mv = _plain_number(setpoint)
    actual_mv = _plain_number(actual)
    if set_mv is None or actual_mv is None:
        return None
    return set_mv - actual_mv


def format_core_voltage_title(setpoint, actual):
    """Measured core voltage and droop. Empty when the regulator reading is missing."""
    droop = core_droop_mv(setpoint, actual)
    actual_mv = _plain_number(actual)
    if droop is None or actual_mv is None:
        return ""
    return (
        f"Measured {format_number(actual_mv, 0)} mV, droop {format_number(droop, 0)} mV"
    )


def droop_limit_mv(stored):
    number = _plain_number((stored or {}).get("max_droop_mv"))
    if number is None:
        return DEFAULT_MAX_DROOP_MV
    return number


def droop_alert(setpoint, actual, stored):
    """True when measured core voltage sags past this miner's droop cap."""
    droop = core_droop_mv(setpoint, actual)
    if droop is None:
        return False
    return droop > droop_limit_mv(stored)


def _flag_set(value):
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "none", "null")
    try:
        return float(value) != 0
    except (TypeError, ValueError):
        return bool(value)


def format_share_title(info):
    """Reject reasons, pool difficulty, and a fallback mark."""
    if not isinstance(info, dict):
        return ""
    lines = []
    reasons = info.get("sharesRejectedReasons")
    if isinstance(reasons, list):
        for reason in reasons:
            if not isinstance(reason, dict):
                continue
            message = str(reason.get("message") or "").strip()
            count = _plain_number(reason.get("count"))
            if not message or count is None:
                continue
            lines.append(f"{message} {_count_text(count)}")
    difficulty = _plain_number(info.get("poolDifficulty"))
    if difficulty is not None and difficulty > 0:
        lines.append(f"Pool difficulty {_count_text(difficulty)}")
    if _flag_set(info.get("isUsingFallbackStratum")):
        lines.append("Fallback pool")
    return "\n".join(lines)


def format_version_title(info):
    if not isinstance(info, dict):
        return ""
    return str(info.get("version") or "").strip()


def pool_host(info):
    """Stratum host the miner is using, and whether that host is the fallback."""
    if not isinstance(info, dict):
        return "", False
    fallback = _flag_set(info.get("isUsingFallbackStratum"))
    raw = info.get("fallbackStratumURL") if fallback else info.get("stratumURL")
    if not str(raw or "").strip():
        raw = info.get("stratumURL") or info.get("fallbackStratumURL") or ""
    text = str(raw or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/")[0].split(":")[0]
    return text, fallback


def wifi_reading(info):
    """Wi-Fi RSSI in dBm. AxeOS uses wifiRSSI on system info."""
    if not isinstance(info, dict):
        return None
    number = _plain_number(info.get("wifiRSSI"))
    if number is None:
        number = _plain_number(info.get("wifiRssi"))
    return number


def wifi_is_weak(rssi):
    """True when the radio is at or below the weak-signal mark."""
    number = _plain_number(rssi)
    return number is not None and number <= WEAK_WIFI_DBM


def subnet_range_for(ip):
    """First and last host of an IPv4 address's /24. Blank when the address is not usable."""
    text = str(ip or "").strip()
    if not text:
        return "", ""
    try:
        network = ipaddress.ip_network(f"{text}/24", strict=False)
    except ValueError:
        return "", ""
    if network.version != 4:
        return "", ""
    # A /24 always has 254 hosts.
    hosts = list(network.hosts())
    return str(hosts[0]), str(hosts[-1])


def local_ipv4():
    """This machine's IPv4, or blank when it cannot be learned without sending a packet."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except Exception:
        return ""
    try:
        sock.connect(("192.0.2.1", 80))
        address = sock.getsockname()[0]
    except Exception:
        return ""
    finally:
        try:
            sock.close()
        except Exception:
            pass
    if not address or address.startswith("127."):
        return ""
    return address


def fleet_summary(rows):
    """Online count and summed hash and watts across the table."""
    online = offline = hold = trim = 0
    hash_sum = 0.0
    watt_sum = 0.0
    paired_watts = 0.0
    counted_hash = False
    counted_watts = False
    live_phases = {"climb", "hold", "trim", "skipped", "stopped"}
    for row in rows or []:
        phase = str(row.get("phase") or "").strip().lower()
        if phase == "offline":
            offline += 1
            continue
        has_reading = (
            _plain_number(row.get("hash")) is not None
            or _plain_number(row.get("freq")) is not None
        )
        if phase in live_phases or has_reading:
            online += 1
        if phase == "hold":
            hold += 1
        elif phase == "trim":
            trim += 1
        hashrate = _plain_number(row.get("hash"))
        watts = _plain_number(row.get("watts"))
        if hashrate is not None and hashrate > 0:
            hash_sum += hashrate
            counted_hash = True
            if watts is not None and watts > 0:
                paired_watts += watts
        if watts is not None and watts > 0:
            watt_sum += watts
            counted_watts = True
    joules = None
    if counted_hash and hash_sum > 0 and paired_watts > 0:
        joules = paired_watts / (hash_sum / 1000.0)
    return {
        "online": online,
        "offline": offline,
        "hold": hold,
        "trim": trim,
        "hash": format_number(hash_sum, 2) if counted_hash else "-",
        "watts": format_number(watt_sum, 2) if counted_watts else "-",
        "jth": format_number(joules, 2) if joules is not None else "-",
    }


SECONDS_PER_DAY = 86400
# Each hash beats difficulty D with chance 1 / (D * 2^32).
HASHES_PER_DIFFICULTY = 2**32
ODDS_WINDOWS = (("day", 1), ("month", 30), ("year", 365))


def block_odds(hash_ghs, difficulty):
    """Mean seconds to a solo block, and the chance of one per day, month, and year.

    None when the hashrate (GH/s) or the network difficulty is missing or not positive.
    """
    rate = _plain_number(hash_ghs)
    diff = _plain_number(difficulty)
    if rate is None or diff is None or rate <= 0 or diff <= 0:
        return None
    mean_seconds = diff * HASHES_PER_DIFFICULTY / (rate * 1e9)
    odds = {"mean_seconds": mean_seconds}
    for name, days in ODDS_WINDOWS:
        odds[name] = -math.expm1(-days * SECONDS_PER_DAY / mean_seconds)
    return odds


def _one_in(chance):
    if chance >= 0.5:
        return f"{chance * 100:.0f}%"
    return f"1 in {format_difficulty(1.0 / chance)}"


def _wait_text(seconds):
    days = seconds / SECONDS_PER_DAY
    if days >= 365:
        return f"{days / 365:,.0f} years"
    if days >= 1:
        return f"{days:,.0f} days"
    return f"{seconds / 3600:,.1f} hours"


def format_block_odds(odds):
    """Stat text for the yearly chance, and a tooltip with day, month, year, and mean wait."""
    if not odds:
        return "-", ""
    lines = ["Chance to find a block solo"]
    for name, _days in ODDS_WINDOWS:
        lines.append(f"{name.capitalize()} {_one_in(odds[name])}")
    lines[-1] += f" ({odds['year'] * 100:.3f}%)"
    lines.append(f"Average wait about {_wait_text(odds['mean_seconds'])}")
    return _one_in(odds["year"]), "\n".join(lines)


def alert_kind(row):
    """The background notice for one row. Offline wins over a fault still on the last sample."""
    phase = str((row or {}).get("phase") or "").strip().lower()
    if phase == "offline":
        return "offline"
    if (row or {}).get("overheat"):
        return "overheat"
    if (row or {}).get("power_fault"):
        return "power_fault"
    if (row or {}).get("floor_alert"):
        return "below_floor"
    return ""


def alert_message(name, kind):
    label = str(name or "Miner").strip() or "Miner"
    if kind == "offline":
        return f"{label} is offline."
    if kind == "overheat":
        return f"{label} is in overheat mode."
    if kind == "power_fault":
        return f"{label} reported a power fault."
    if kind == "below_floor":
        return f"{label} is running under its minimum clocks."
    return ""


def stratum_port_open(host, port, timeout=8, connect=None):
    """True when a Stratum port accepts a TCP connection."""
    opener = socket.create_connection if connect is None else connect
    try:
        sock = opener((host, port), timeout)
    except Exception:
        return False
    close = getattr(sock, "close", None)
    if close is not None:
        try:
            close()
        except Exception:
            pass
    return True


def pool_targets(info):
    """(host, port) for the primary and fallback pools a miner reports."""
    if not isinstance(info, dict):
        return []
    targets = []
    for url_key, port_key in (
        ("stratumURL", "stratumPort"),
        ("fallbackStratumURL", "fallbackStratumPort"),
    ):
        text = str(info.get(url_key) or "").strip()
        if "://" in text:
            text = text.split("://", 1)[1]
        host = text.split("/")[0].split(":")[0].strip()
        port = coerce_limit(info.get(port_key))
        if host and port and (host, port) not in targets:
            targets.append((host, port))
    return targets


def read_network_status(get=None, connect=None, pools=(), difficulty=True):
    """Current block difficulty, and whether each pool's Stratum port answers.

    `pools` is the (host, port) list the miners report. With `difficulty` off,
    mempool.space is not asked.
    """
    getter = requests.get if get is None else get
    wanted = difficulty
    difficulty = None
    try:
        if wanted:
            response = getter(DIFFICULTY_URL, timeout=8)
            response.raise_for_status()
            difficulty = response.json().get("currentDifficulty")
    except Exception:
        difficulty = None
    results = []
    for host, port in pools:
        results.append(
            {
                "name": host,
                "online": stratum_port_open(host, port, connect=connect),
            }
        )
    return {"difficulty": difficulty, "pools": results}


def parse_firmware_version(text):
    """(major, minor, patch) from a miner version or a stable tag."""
    match = _INSTALLED_VERSION.match(str(text or "").strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def latest_stable_firmware(releases):
    """Highest vMAJOR.MINOR.PATCH among releases that are not drafts or prereleases."""
    best = None
    best_tag = ""
    if not isinstance(releases, list):
        return ""
    for release in releases:
        if not isinstance(release, dict):
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = str(release.get("tag_name") or "").strip()
        match = _STABLE_TAG.fullmatch(tag)
        if match is None:
            continue
        version = tuple(int(part) for part in match.groups())
        if best is None or version > best:
            best = version
            best_tag = tag
    return best_tag


def firmware_update_version(installed, stable):
    """Stable tag when its triple is newer than the installed version, else ''."""
    current = parse_firmware_version(installed)
    available = parse_firmware_version(stable)
    if current is None or available is None or available <= current:
        return ""
    return str(stable or "").strip()


def firmware_check_due(last_checked, now):
    """True before the first check, then once after local noon if that noon is still unchecked.

    A noon missed while the tablet slept is caught the next time this runs, still once that day.
    """
    if last_checked is None:
        return True
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    return now >= noon and last_checked < noon


def read_latest_stable_firmware(get=None):
    """Newest stable ESP-Miner tag. '' when none qualify. None when the request fails."""
    getter = requests.get if get is None else get
    try:
        response = getter(FIRMWARE_RELEASES_URL, timeout=8)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None
    return latest_stable_firmware(payload)


def over_limit(value, limit):
    """True when a reading is above its cap. A text reading is parsed first."""
    number = parse_display_number(value)
    limit_number = parse_display_number(limit)
    if number is None or limit_number is None:
        return False
    return number > limit_number


def under_limit(value, limit):
    """True when a reading falls under its floor."""
    number = parse_display_number(value)
    floor = parse_display_number(limit)
    if number is None or floor is None:
        return False
    return number < floor


def below_floor(frequency, core_voltage, stored):
    """True when the miner reports clocks under its saved min frequency or voltage.

    AxeOS can save clocks under the floor after an overheat trip.
    """
    return under_limit(frequency, _cap_or_default(stored, "min_freq")) or under_limit(
        core_voltage, _cap_or_default(stored, "min_volt")
    )


def limit_level(value, limit, tolerance=0):
    """'bad' above the cap, 'warn' inside the tolerance band under it, else ''."""
    number = parse_display_number(value)
    cap = parse_display_number(limit)
    if number is None or cap is None:
        return ""
    if number > cap:
        return "bad"
    band = parse_display_number(tolerance)
    if band is None or band <= 0:
        return ""
    if number > cap - band:
        return "warn"
    return ""


def _cap_or_default(stored, key):
    """A saved miner cap, or its board's default when the cell is empty.

    None when the board has no sensor for that cap.
    """
    raw = (stored or {}).get(key)
    if key in ("max_temp", "max_watts", "max_vr_temp"):
        value = coerce_real_limit(raw)
    else:
        value = coerce_limit(raw)
    if value is None:
        board = board_for_record(stored)
        mode = modes.mode_for_record(stored, board)
        default = board_limits(board, mode=mode.key).get(key)
        return None if default == "" else default
    return value


def _configured_tolerance(settings, key):
    """A global tolerance. Missing or negative values use the tuner default of 3."""
    if not isinstance(settings, dict):
        return 3
    number = parse_display_number(settings.get(key))
    if number is None or number < 0:
        return 3
    return number


def row_state_tag(phase, asic_text, error_text, max_temp, max_error):
    """Color a miner row from its phase, or red when it is offline or past a limit."""
    phase_name = str(phase or "").strip().lower()
    if phase_name == "offline":
        return "alert"
    if over_limit(parse_display_number(asic_text), max_temp):
        return "alert"
    if over_limit(parse_display_number(error_text), max_error):
        return "alert"
    if phase_name == "hold":
        return "hold"
    if phase_name in ("climb", "ramp"):
        return "climb"
    if phase_name == "trim":
        return "trim"
    if phase_name == "stopped":
        return "idle"
    return "idle"


def blank_miner_row(nickname, ip):
    """One table row before the first live reading arrives."""
    return {
        "name": nickname,
        "ip": ip,
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
        "limit": "",
        "tag": "idle",
        "up_seconds": None,
        "mv_alert": False,
        "asic_level": "",
        "vr_level": "",
        "error_alert": False,
        "watts_alert": False,
        "watts_title": "",
        "amps": "-",
        "amps_level": "",
        "vin_alert": False,
        "floor_alert": False,
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
        "repasted_on": "",
    }


def stock_text(board):
    """A board's factory clocks, as the page and the log show them."""
    freq, volt = board.stock_clocks
    return f"{freq} MHz / {volt} mV"


def fleet_stock(miners):
    """(board name, stock text) shared by every saved miner, or None when they differ."""
    found = {board_for_record(miner) for miner in miners or []} or {GAMMA_601}
    if len(found) != 1:
        return None
    board = found.pop()
    return board.name, stock_text(board)


def baseline_prompt(miners):
    """The Reset All question for the saved miners."""
    shared = fleet_stock(miners)
    target = (
        "its board's stock clocks"
        if shared is None
        else f"the {shared[0]} stock clocks ({shared[1]})"
    )
    return (
        f"Set every miner to {target} and make those its start clocks?\n\n"
        "The next Start Autotuner tunes every miner up from there."
    )


# The page replaces {miner}, {board}, and {stock} from the selected row.
MINER_BASELINE_PROMPT = (
    "Set {miner} to the {board} stock clocks ({stock}) "
    "and make those its start clocks?\n\n"
    "History marks the reset, so Since reset can start there."
)


def _as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _notice(level, title, message):
    return {"level": level, "title": title, "message": message}


def _fail(message, title="Error", level="error"):
    return {"ok": False, "message": message, "notice": _notice(level, title, message)}


class TunerDashboard:
    """Tuner state the page reads and the actions it calls."""

    def __init__(self):
        self._lock = threading.RLock()
        self.running = False
        self.threads = []
        self.stop_event = None
        self._miner_stops = {}
        self._status_refresh_running = False
        self._stop_in_progress = False
        self._baseline_reset_running = False
        self._restart_all_running = False
        self._start_pending = False
        self._rows = []
        self._log = deque(maxlen=LOG_LIMIT)
        self._log_seq = 0
        self._updated = "--:--:--"
        self._network = {
            "difficulty": "-",
            "difficulty_title": "",
            "difficulty_value": None,
            "difficulty_enabled": True,
            "pools": [],
        }
        self._latest_firmware = ""
        self._firmware_checked = None
        self._alerts = {}
        self._misses = {}
        self._droop_reads = {}
        self._overheat_clear_failed = set()
        self._focused = True
        start_ip, end_ip = subnet_range_for(local_ipv4())
        self._scan_range = {"start": start_ip, "end": end_ip}
        self._scan = None
        self._scan_cancel = threading.Event()
        self._scan_running = False
        self._closed = threading.Event()
        self._wake = threading.Event()
        self._window = None
        self._fullscreen = False
        self._background_started = False
        self._history = history.HistoryRecorder()
        self._weather_now = None
        self._weather_checked = 0.0
        # (host, port) pools each miner reported last, for the network panel.
        self._pool_targets = {}

    def run(self):
        """Open the local dashboard window and block until it closes."""
        try:
            import webview
        except ImportError as exc:
            message = "The dashboard window needs pywebview. Install it with: pip install -r requirements.txt"
            print(message)
            raise SystemExit(message) from exc

        self.start_background()
        page = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "web", "index.html"
        )
        # An absolute file path loads in the window directly. A relative path would
        # make pywebview start its own local server, which this app does not use.
        window = webview.create_window(
            "Groundhog Gamma Tuner",
            url=page,
            js_api=DashboardApi(self),
            width=1280,
            height=800,
            min_size=(640, 480),
            background_color="#141414",
            text_select=True,
        )
        self._window = window
        window.events.closing += self._on_closing
        webview.start(self._on_ready)

    def _on_ready(self):
        """Open maximized. Every pywebview backend (WebView2, Cocoa, GTK, Qt) can."""
        if self._window is None:
            return
        try:
            self._window.maximize()
        except Exception:
            pass

    def _on_closing(self):
        self._closed.set()
        self._scan_cancel.set()
        self._wake.set()
        self._signal_all_stops()
        # Tuner threads are daemons. Join them so a confirmed setpoint is written
        # before the process exits. Each join uses the same budget as Stop.
        for thread in list(self.threads):
            thread.join(timeout=12)

    def start_background(self):
        """Load saved miners and poll them until the window closes."""
        if self._background_started:
            return
        self._background_started = True
        self.load_rows()
        threading.Thread(target=self._display_loop, daemon=True).start()
        threading.Thread(target=self._network_loop, daemon=True).start()

    def load_rows(self):
        """Replace the table with the miners saved in config.json."""
        saved = [
            (
                miner["ip"],
                miner.get("nickname") or f"Miner-{miner['ip']}",
                str(miner.get("repasted_on") or ""),
                board_for_record(miner),
                modes.mode_for_record(miner, board_for_record(miner)),
            )
            for miner in get_miners()
        ]
        with self._lock:
            # Keep the last reading for a miner that is still saved, so a scan
            # or an edit does not blank the table until the next refresh.
            current = {row["ip"]: row for row in self._rows}
            rows = []
            for ip, nickname, repasted_on, board, mode in saved:
                row = current.get(ip)
                if row is None:
                    row = blank_miner_row(nickname, ip)
                else:
                    row["name"] = nickname
                row["repasted_on"] = repasted_on
                row["board"] = board.name
                row["board_version"] = board.version
                row["mode"] = mode.name
                row["stock"] = stock_text(board)
                rows.append(row)
            self._rows = rows
            kept = {row["ip"] for row in rows}
            for ip in list(self._alerts):
                if ip not in kept:
                    self._drop_miner_runtime_locked(ip)
        problem = config_problem()
        if problem:
            self.log_message(problem, "error")
        self.log_message(
            f"Loaded {len(rows)} miners.",
            "error" if problem else "success",
        )
        self._wake.set()

    def log_message(self, message, level="info"):
        """Append one activity line. Tuner threads call this directly."""
        message = replace_ips_with_names(message, self._miner_names())
        if level not in ("success", "warning", "error", "info"):
            level = "info"
        with self._lock:
            self._log_seq += 1
            self._log.append(
                {
                    "id": self._log_seq,
                    "text": format_log_line(message),
                    "level": level,
                }
            )

    def get_snapshot(self, since_log_id=0, focused=None):
        """Table, button state, scan progress, and log lines after since_log_id.

        `focused` is whether the window is in front. A background fault can toast.
        """
        since = _coerce_log_id(since_log_id)
        with self._lock:
            if focused is not None:
                self._focused = _as_bool(focused)
            self._reap_threads_locked()
            summary = fleet_summary(self._rows)
            odds, odds_title = format_block_odds(
                block_odds(summary["hash"], self._network["difficulty_value"])
            )
            latest = self._latest_firmware
            miners = []
            for row in self._rows:
                item = dict(row)
                item["hash_label"] = format_hashrate(item.get("hash"))
                item["firmware_update"] = firmware_update_version(
                    item.get("name_title"), latest
                )
                miners.append(item)
            return {
                "updated": self._updated,
                "miners": miners,
                "log": [dict(line) for line in self._log if line["id"] > since],
                "scan": None if self._scan is None else dict(self._scan),
                "controls": self._controls_locked(),
                "prompts": {
                    "baseline": baseline_prompt(
                        [{"board": row.get("board_version")} for row in self._rows]
                    ),
                    "miner_baseline": MINER_BASELINE_PROMPT,
                },
                "network": self._network_locked(),
                "weather": self._weather_payload_locked(),
                "fleet": {
                    "online": summary["online"],
                    "offline": summary["offline"],
                    "hold": summary["hold"],
                    "trim": summary["trim"],
                    "hash": format_hashrate(summary["hash"]),
                    "watts": summary["watts"],
                    "jth": summary["jth"],
                    "odds": odds,
                    "odds_title": odds_title,
                },
                "scan_range": dict(self._scan_range),
                "config_error": config_problem(),
            }

    def _network_locked(self):
        return {
            "difficulty": self._network["difficulty"],
            "difficulty_title": self._network["difficulty_title"],
            "difficulty_enabled": self._network["difficulty_enabled"],
            "pools": [dict(pool) for pool in self._network["pools"]],
        }

    def _refresh_network(self, difficulty=True, pools=True):
        """Read difficulty and pool status. A switched-off read is skipped and cleared."""
        with self._lock:
            targets = []
            if pools:
                for found in self._pool_targets.values():
                    targets.extend(item for item in found if item not in targets)
            self._network["difficulty_enabled"] = difficulty
            if not difficulty:
                self._network["difficulty"] = "-"
                self._network["difficulty_title"] = ""
                self._network["difficulty_value"] = None
        if not difficulty and not targets:
            with self._lock:
                self._network["pools"] = []
            return
        status = read_network_status(pools=targets, difficulty=difficulty)
        number = _plain_number(status.get("difficulty"))
        with self._lock:
            if difficulty and number is not None:
                self._network["difficulty"] = format_difficulty(number)
                self._network["difficulty_title"] = difficulty_title(number)
                self._network["difficulty_value"] = number
            self._network["pools"] = status["pools"]

    def _refresh_firmware(self, now=None):
        """Read the newest stable tag. A failed request keeps the previous tag."""
        tag = read_latest_stable_firmware()
        with self._lock:
            self._firmware_checked = now or datetime.now()
            if tag is not None:
                self._latest_firmware = tag

    def _refresh_firmware_if_due(self):
        now = datetime.now()
        with self._lock:
            checked = self._firmware_checked
        if not firmware_check_due(checked, now):
            return
        self._refresh_firmware(now)

    def _network_loop(self):
        while not self._closed.is_set():
            try:
                settings = load_config()
            except Exception:
                settings = {}
            try:
                self._refresh_network(
                    difficulty=internet_switch_on(settings, "network_stats_enabled"),
                    pools=internet_switch_on(settings, "pool_check_enabled"),
                )
            except Exception:
                pass
            try:
                if internet_switch_on(settings, "firmware_check_enabled"):
                    self._refresh_firmware_if_due()
                else:
                    with self._lock:
                        self._latest_firmware = ""
                        self._firmware_checked = None
            except Exception:
                pass
            try:
                self._refresh_weather_if_due()
            except Exception:
                pass
            if self._closed.wait(NETWORK_REFRESH_SECONDS):
                return

    def _weather_payload_locked(self):
        """Outdoor conditions for the status bar and the History screen."""
        current = self._weather_now
        if not current:
            return None
        return {
            "temp": current.get("outdoor_temp"),
            "apparent": current.get("apparent_temp"),
            "humidity": current.get("humidity"),
            "wind": current.get("wind_speed"),
            "sky": weather.sky_group(current.get("weather_code")),
            "label": weather.sky_label(current.get("weather_code")),
            "is_day": current.get("is_day"),
            "place": current.get("place") or "",
            "fetched_at": current.get("fetched_at"),
        }

    def _store_location(self, place):
        def mutate(config):
            config["location"] = place

        return modify_config(mutate) not in (None, False)

    def _refresh_weather_if_due(self, now=None, force=False):
        """Read outdoor weather every 15 minutes and fill gaps in the history."""
        now = time.time() if now is None else now
        with self._lock:
            if (
                not force
                and now - self._weather_checked < weather.WEATHER_REFRESH_SECONDS
            ):
                return
            self._weather_checked = now
        settings = load_config()
        if not settings.get("weather_enabled", True):
            return
        # The place is only ever one the user picked or asked this device for.
        place = weather.normalize_location(settings.get("location"))
        if place is None:
            return
        current = weather.read_current_weather(place["latitude"], place["longitude"])
        if current is None:
            # Try again in two minutes instead of a full refresh.
            with self._lock:
                self._weather_checked = now - weather.WEATHER_REFRESH_SECONDS + 120
            return
        current["fetched_at"] = now
        current["place"] = place["name"]
        with self._lock:
            self._weather_now = current
        self._backfill_weather(place, now)

    def _backfill_weather(self, place, now):
        """Give saved samples with no outdoor reading the hourly weather for their time."""
        since = now - weather.MAX_PAST_DAYS * 86400
        try:
            oldest = history.oldest_missing_weather(since)
        except sqlite3.Error:
            return
        if oldest is None:
            return
        days = int((now - oldest) // 86400) + 1
        hours = weather.read_hourly_weather(place["latitude"], place["longitude"], days)
        try:
            filled = history.fill_missing_weather(hours)
        except sqlite3.Error:
            return
        if filled:
            self.log_message(
                f"Added outdoor weather to {filled} history sample(s).", "info"
            )

    def _record_history(self, results):
        """Hand this round of readings to the ten-minute history."""
        with self._lock:
            names = {row["ip"]: row.get("name") or row["ip"] for row in self._rows}
            current = dict(self._weather_now) if self._weather_now else None
        readings = [
            (ip, names.get(ip, ip), info, get_miner_status(ip).get("phase") or "")
            for ip, info in results
            if ip in names
        ]
        try:
            self._history.observe(readings, current)
        except sqlite3.Error as exc:
            self.log_message(f"History sample was not saved: {exc}", "warning")

    def _note_fresh_start(self, ips):
        """Mark a baseline reset in the history so the screen can start there."""
        for ip in ips:
            self._history.forget(ip)
            try:
                history.record_event(ip, "reset")
            except sqlite3.Error as exc:
                self.log_message(
                    f"The reset of {ip} was not marked in the history: {exc}",
                    "warning",
                )

    def start_autotuner(self, confirmed=False):
        """Start one tuner thread per enabled miner that has the required limits.

        A miner on an unverified board needs one confirmation. Without
        `confirmed`, the reply asks the page for it and nothing starts.
        """
        with self._lock:
            self._reap_threads_locked()
            if self._baseline_reset_running:
                blocked = "baseline"
            elif self._restart_all_running:
                blocked = "restart"
            elif (
                self._start_pending
                or self.running
                or self._stop_in_progress
                or any(thread.is_alive() for thread in self.threads)
            ):
                blocked = "running"
            else:
                blocked = None
                self._start_pending = True
        if blocked == "baseline":
            self.log_message(
                "Wait for the baseline reset to finish before starting.", "warning"
            )
            return _fail(
                "Wait for the baseline reset to finish before starting.",
                "Reset in Progress",
                "warning",
            )
        if blocked == "restart":
            self.log_message(
                "Wait for the miner restart to finish before starting.", "warning"
            )
            return _fail(
                "Wait for the miner restart to finish before starting.",
                "Restart in Progress",
                "warning",
            )
        if blocked == "running":
            self.log_message("Autotuner is already running.", "warning")
            return _fail(
                "Autotuner is already running.", "Autotuner Running", "warning"
            )

        notice = None
        try:
            config = load_config()
            interval = config.get("monitor_interval", 5)
            self.log_message("Checking AutoTuner settings before starting...", "info")

            enabled_miners = [
                miner
                for miner in config.get("miners", [])
                if miner.get("enabled", False)
            ]
            ready_miners = []
            missing_settings = []
            for miner in enabled_miners:
                missing = missing_start_fields(miner)
                if missing:
                    for field in missing:
                        missing_settings.append((miner["ip"], field))
                else:
                    ready_miners.append(miner)

            if not enabled_miners:
                message = "No miners are enabled for AutoTuning. Please enable at least one miner in settings."
                self.log_message(message, "error")
                return _fail(message, "No Miners Enabled", "warning")

            unconfirmed = [m for m in ready_miners if needs_experimental_ok(m)]
            names = ", ".join(m.get("nickname") or m["ip"] for m in unconfirmed)
            if unconfirmed and not confirmed:
                boards = sorted({board_for_record(m).name for m in unconfirmed})
                return {
                    "ok": False,
                    "confirm": {
                        "title": "Unverified Boards",
                        "message": EXPERIMENTAL_PROMPT.format(
                            boards=" and ".join(boards), names=names
                        ),
                        "confirmLabel": "Start",
                    },
                }
            if unconfirmed:
                confirmed_ips = {m["ip"] for m in unconfirmed}

                def mark_confirmed(config):
                    for saved in config.get("miners", []):
                        if saved.get("ip") in confirmed_ips:
                            saved["experimental_ok"] = True

                if modify_config(mark_confirmed) is False:
                    return _fail(CONFIG_CORRUPT_MESSAGE, "Config file damaged", "error")
                self.log_message(
                    f"Confirmed tuning on unverified boards: {names}.", "warning"
                )

            if missing_settings:
                error_message = "These miners are missing AutoTuner settings and will be skipped:\n\n"
                for ip, field in missing_settings:
                    error_message += f"- Miner {ip}: Missing {field}\n"
                self.log_message(error_message, "error")
                if not ready_miners:
                    return _fail(error_message.strip(), "Incomplete Settings", "error")
                notice = _notice(
                    "warning", "Some miners skipped", error_message.strip()
                )

            stop_event = threading.Event()
            miner_stops = {}
            threads = []
            self.log_message("Starting autotuning for selected miners...", "success")
            for index, miner in enumerate(ready_miners):
                miner_event, thread = self._miner_thread(
                    miner, interval, index * STARTUP_STAGGER_SECONDS
                )
                miner_stops[miner["ip"]] = miner_event
                threads.append(thread)

            with self._lock:
                self.stop_event = stop_event
                self._miner_stops = miner_stops
            for thread in threads:
                thread.start()
            with self._lock:
                self.threads = threads
                # Stop can land after the events are published and before the
                # threads are stored. Leave Running clear when that stop won.
                stopped = self._stop_in_progress or stop_event.is_set()
                self.running = not stopped
                if stopped:
                    for event in miner_stops.values():
                        event.set()
            self._wake.set()
            result = {"ok": True}
            if notice is not None:
                result["notice"] = notice
            return result
        finally:
            with self._lock:
                self._start_pending = False

    def stop_autotuner(self):
        """Ask every tuner thread to leave, then log when they have."""
        with self._lock:
            if self._stop_in_progress:
                return {"ok": True}
            if self.stop_event is None and not self.threads:
                self.running = False
                return {"ok": True}
            self._stop_in_progress = True
            self.running = False
            threads = list(self.threads)
        self._signal_all_stops()
        self.log_message("Stopping autotuning...", "warning")

        def join_threads():
            for thread in threads:
                thread.join(timeout=12)
            self._finish_stop()

        threading.Thread(target=join_threads, daemon=True).start()
        return {"ok": True}

    def reset_baseline(self):
        """Write factory clocks to every saved miner and forget the learned setpoint."""
        with self._lock:
            if self._baseline_reset_running:
                blocked = "reset"
            elif self._restart_all_running:
                blocked = "restart"
            elif (
                self.running
                or self._stop_in_progress
                or self._start_pending
                or any(thread.is_alive() for thread in self.threads)
            ):
                blocked = "busy"
            else:
                blocked = None
                self._baseline_reset_running = True
        if blocked == "reset":
            self.log_message("A baseline reset is already in progress.", "warning")
            return _fail(
                "A baseline reset is already in progress.",
                "Reset in Progress",
                "warning",
            )
        if blocked == "restart":
            self.log_message(
                "Wait for the miner restart to finish before resetting to baseline.",
                "warning",
            )
            return _fail(
                "Wait for the miner restart to finish before resetting to baseline.",
                "Restart in Progress",
                "warning",
            )
        if blocked == "busy":
            self.log_message(
                "Stop the autotuner before resetting to baseline.", "warning"
            )
            return _fail(
                "Stop the autotuner before resetting miners to baseline.",
                "Autotuner Running",
                "warning",
            )

        miners = list(get_miners())
        if not miners:
            with self._lock:
                self._baseline_reset_running = False
            return _fail(
                "Please add a miner first before resetting to baseline.",
                "No Miners Found",
                "warning",
            )

        shared = fleet_stock(miners)
        target = "their stock clocks" if shared is None else shared[1]
        self.log_message(f"Resetting miners to {target}.", "info")

        def work():
            failed = 0
            try:
                failed = reset_miners_to_baseline(miners, self.log_message)
            finally:
                self._note_fresh_start(
                    [miner.get("ip") for miner in miners if miner.get("ip")]
                )
                with self._lock:
                    self._baseline_reset_running = False
                if failed:
                    self.log_message(
                        "Baseline reset finished with "
                        f"{failed} miner(s) that did not accept "
                        f"{target}. "
                        "Learned setpoints were cleared.",
                        "error",
                    )
                else:
                    self.log_message(
                        f"Baseline reset finished. Start Autotuner to tune from {target}.",
                        "success",
                    )

        threading.Thread(target=work, daemon=True).start()
        return {
            "ok": True,
            "notice": _notice(
                "info",
                "Baseline Reset Started",
                f"Setting {len(miners)} miner(s) to {target}. "
                "The log shows when it finishes.",
            ),
        }

    def reset_miner_baseline(self, ip):
        """Write factory clocks to one miner and forget its learned setpoint.

        The page confirms before it calls this.
        """
        ip = str(ip or "").strip()
        if not ip:
            return _fail("Please select a miner first.", "No Selection", "warning")
        miner = next((row for row in get_miners() if row.get("ip") == ip), None)
        if miner is None:
            return _fail(f"{ip} is not a saved miner.", "Miner Not Found", "warning")
        with self._lock:
            self._reap_threads_locked()
            if self._baseline_reset_running:
                blocked = "baseline"
            elif self._restart_all_running:
                blocked = "restart"
            elif self._start_pending or any(
                getattr(thread, "miner_ip", None) == ip and thread.is_alive()
                for thread in self.threads
            ):
                blocked = "tuning"
            else:
                blocked = None
                self._baseline_reset_running = True
        if blocked == "baseline":
            self.log_message(
                "A baseline reset is already in progress.",
                "warning",
            )
            return _fail(
                "A baseline reset is already in progress.",
                "Reset in Progress",
                "warning",
            )
        if blocked == "restart":
            self.log_message(
                "Wait for the miner restart to finish before resetting this miner.",
                "warning",
            )
            return _fail(
                "Wait for the miner restart to finish before resetting this miner.",
                "Restart in Progress",
                "warning",
            )
        if blocked == "tuning":
            self.log_message(
                f"Stop the autotuner before resetting {ip} to baseline.", "warning"
            )
            return _fail(
                "Stop the autotuner before resetting this miner to baseline.",
                "Autotuner Running",
                "warning",
            )
        stock = stock_text(board_for_record(miner))
        self.log_message(f"Resetting {ip} to {stock}.", "info")
        try:
            failed = reset_miners_to_baseline([miner], self.log_message)
        finally:
            self._note_fresh_start([ip])
            with self._lock:
                self._baseline_reset_running = False
        name = miner.get("nickname") or ip
        if failed:
            message = (
                f"{name} did not accept {stock}. "
                "Its start clocks were still set to them."
            )
            self.log_message(message, "error")
            return _fail(message, "Reset Failed")
        message = f"{name} is at {stock}, and Start Autotuner tunes it from there."
        self.log_message(message, "success")
        return {"ok": True, "message": message}

    def get_setup_state(self):
        """Whether the first-run setup still has to run, and what it offers."""
        config = load_config()
        return {
            "ok": True,
            "setup_done": bool(config.get("setup_done", True)),
            "modes": [
                {"key": mode.key, "name": mode.name, "summary": mode.summary}
                for mode in (modes.MODES[key] for key in modes.MODE_ORDER)
            ],
            "scan_range": dict(self._scan_range),
        }

    def complete_setup(self, answers):
        """Save the first-run answers: cooling and goal pick the new-miner mode."""
        if not isinstance(answers, dict):
            return _fail("Pick your cooling and what you want most.")
        cooling = str(answers.get("cooling") or "").strip()
        goal = str(answers.get("goal") or "").strip()
        if cooling not in SETUP_COOLING or goal not in SETUP_GOALS:
            return _fail("Pick your cooling and what you want most.")
        raw_supply = str(answers.get("supply_watts") or "").strip()
        supply = ""
        if raw_supply:
            try:
                supply = float(raw_supply)
            except ValueError:
                supply = None
            if supply is None or not math.isfinite(supply) or supply <= 0:
                return _fail(
                    "Enter the watts per miner as a number, or leave it blank."
                )
        mode = setup_mode(cooling, goal, _as_bool(answers.get("acknowledged")))
        weather_on = _as_bool(answers.get("weather"))
        # Each internet read stays off unless its box was ticked.
        switches = {
            key: _as_bool(answers.get(key.removesuffix("_enabled")))
            for key in INTERNET_SWITCHES
        }

        def mutate(config):
            config["default_mode"] = mode.key
            config["setup_done"] = True
            config["weather_enabled"] = weather_on
            config.update(switches)
            config["supply_watts"] = supply

        if modify_config(mutate) is False:
            return _fail(CONFIG_CORRUPT_MESSAGE, "Config file damaged", "error")
        self.log_message(
            f"Setup saved. New miners start in {mode.name} mode.", "success"
        )
        if weather_on:
            with self._lock:
                self._weather_checked = 0.0
            self._wake.set()
        return {"ok": True, "mode": mode.key, "mode_name": mode.name}

    def get_location(self):
        """The saved weather location and the latest outdoor reading."""
        with self._lock:
            current = self._weather_payload_locked()
        return {
            "ok": True,
            "location": weather.normalize_location(load_config().get("location")),
            "weather": current,
            "device_supported": sys.platform == "win32",
        }

    def search_location(self, query):
        """Places matching a typed city name."""
        text = str(query or "").strip()
        try:
            places = weather.search_places(text)
        except (ValueError, RuntimeError) as exc:
            return _fail(str(exc), "Weather Location", "warning")
        if not places:
            return _fail(
                f"No place called {text} was found. Try the nearest city.",
                "Weather Location",
                "warning",
            )
        return {"ok": True, "places": places}

    def detect_location(self):
        """Save this computer's position from Windows location services."""
        place, error = weather.detect_device_location()
        if place is None:
            return _fail(error, "Device Location", "warning")
        return self.save_location(place)

    def save_location(self, place):
        """Save the place the outdoor weather is read for, then read it now."""
        place = weather.normalize_location(place)
        if place is None:
            return _fail("Pick a place from the list.", "Weather Location", "warning")
        if not self._store_location(place):
            return _fail(CONFIG_CORRUPT_MESSAGE, "Config file damaged", "error")
        self.log_message(f"Weather location set to {place['name']}.", "success")
        threading.Thread(
            target=self._refresh_weather_if_due, kwargs={"force": True}, daemon=True
        ).start()
        return {"ok": True, "location": place}

    def clear_location(self):
        """Forget the saved place. Weather is not read again until a new one is set."""

        def mutate(config):
            config.pop("location", None)

        if modify_config(mutate) is False:
            return _fail(CONFIG_CORRUPT_MESSAGE, "Config file damaged", "error")
        with self._lock:
            self._weather_now = None
        self.log_message("Weather location removed.", "info")
        return {"ok": True, "location": None}

    def get_hardware_limits(self):
        """Chip, regulator, firmware, and tuner limits for the Limits screen.

        One set per board in the saved fleet. With more than one board, each
        section title names its board.
        """
        config = load_config()
        mode = config.get("default_mode")
        boards = []
        for miner in config.get("miners", []):
            board = board_for_record(miner)
            if board not in boards:
                boards.append(board)
        if len(boards) <= 1:
            board = boards[0] if boards else GAMMA_601
            return {"ok": True, "sections": hardware_limits(board, mode)}
        sections = []
        for board in boards:
            for section in hardware_limits(board, mode):
                sections.append(
                    dict(section, title=f"{board.name}: {section['title']}")
                )
        return {"ok": True, "sections": sections}

    def get_history(self, filters=None):
        """Saved samples, summarized for the History screen."""
        filters = filters if isinstance(filters, dict) else {}
        metric = filters.get("metric")
        if metric not in history.METRICS:
            metric = "good_hashrate"
        period = filters.get("period")
        if period not in history.PERIODS:
            period = "7d"
        saved = get_miners()
        miners = [
            {
                "ip": miner["ip"],
                "name": miner.get("nickname") or miner["ip"],
                "slot": index % 8,
            }
            for index, miner in enumerate(saved)
            if miner.get("ip")
        ]
        ip = str(filters.get("ip") or "").strip()
        if ip not in {miner["ip"] for miner in miners}:
            ip = ""
        try:
            repastes = history.repaste_moments(repaste_starts(saved))
            samples = history.period_samples(
                period, time.time(), ip or None, repastes=repastes
            )
            summary = history.summarize(samples, metric, ip or None)
            resets = history.last_events("reset")
        except sqlite3.Error as exc:
            return _fail(f"The history could not be read: {exc}", "History")
        with self._lock:
            current = self._weather_payload_locked()
        return {
            "ok": True,
            "filters": {"ip": ip, "metric": metric, "period": period},
            "miners": miners,
            "location": weather.normalize_location(load_config().get("location")),
            "weather": current,
            "attribution": weather.ATTRIBUTION,
            "resets": resets,
            "repastes": repastes,
            "sample_minutes": history.SAMPLE_SECONDS // 60,
            "band_c": history.TEMP_BAND_C,
            **summary,
        }

    def restart_all_miners(self):
        """Restart every saved miner. The page confirms before it calls this."""
        with self._lock:
            self._reap_threads_locked()
            if self._restart_all_running:
                blocked = "restart"
            elif self._baseline_reset_running:
                blocked = "baseline"
            elif self._autotuner_active_locked():
                blocked = "tuning"
            else:
                blocked = None
                self._restart_all_running = True
        if blocked == "restart":
            self.log_message(
                "A restart of all miners is already in progress.", "warning"
            )
            return _fail(
                "A restart of all miners is already in progress.",
                "Restart in Progress",
                "warning",
            )
        if blocked == "baseline":
            self.log_message(
                "Wait for the baseline reset to finish before restarting miners.",
                "warning",
            )
            return _fail(
                "Wait for the baseline reset to finish before restarting miners.",
                "Reset in Progress",
                "warning",
            )
        if blocked == "tuning":
            self.log_message("Stop the autotuner before restarting miners.", "warning")
            return _fail(
                "Stop the autotuner before restarting miners.",
                "Autotuner Running",
                "warning",
            )

        miners = list(get_miners())
        if not miners:
            with self._lock:
                self._restart_all_running = False
            return _fail(
                "Please add a miner first before restarting all miners.",
                "No Miners Found",
                "warning",
            )

        self.log_message("Restarting all miners.", "warning")

        def work():
            failed = 0
            try:
                failed = restart_miners(miners, self.log_message)
            finally:
                with self._lock:
                    self._restart_all_running = False
                if failed:
                    self.log_message(
                        f"Restart of all miners finished with {failed} failure(s).",
                        "error",
                    )
                else:
                    self.log_message("Restart of all miners finished.", "success")

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def start_scan(self, start_ip, end_ip):
        """Scan an inclusive IPv4 range. Only boards on the AxeOS board list are saved."""
        start_ip = str(start_ip or "").strip()
        end_ip = str(end_ip or "").strip()
        try:
            start = ipaddress.IPv4Address(start_ip)
            end = ipaddress.IPv4Address(end_ip)
        except ipaddress.AddressValueError:
            return _fail("Enter a valid starting IP and ending IP.")
        if int(end) < int(start):
            return _fail("Ending IP must be the same as or after the starting IP.")

        total = int(end) - int(start) + 1
        if total > MAX_SCAN_ADDRESSES:
            return _fail(
                f"Scan at most {MAX_SCAN_ADDRESSES} addresses at a time. "
                f"This range has {total}."
            )
        with self._lock:
            if self._scan_running:
                return _fail("A scan is already running.", "Scan Network", "warning")
            self._scan_cancel.clear()
            self._scan_running = True
            self._scan = {
                "running": True,
                "index": 0,
                "total": total,
                "message": "Starting scan...",
            }
        self.log_message(f"Scanning network from {start_ip} to {end_ip}...", "info")

        def on_progress(index, total_count, _ip):
            with self._lock:
                if self._scan is None:
                    return
                self._scan["index"] = index
                self._scan["total"] = total_count
                self._scan["message"] = f"Checking {index} of {total_count}"

        def scan_task():
            found = []
            cancelled = False
            failed = None
            try:
                found = detect_miners(
                    start_ip,
                    end_ip,
                    on_progress=on_progress,
                    should_cancel=self._scan_cancel.is_set,
                )
                cancelled = self._scan_cancel.is_set()
            except Exception as exc:
                failed = exc
            finally:
                with self._lock:
                    self._scan_running = False
                    self._scan = None
            self.load_rows()
            if failed is not None:
                self.log_message(f"Scan failed: {failed}", "error")
            elif cancelled:
                self.log_message("Scan cancelled.", "warning")
            else:
                self.log_message(
                    f"Scan finished. Added {len(found)} miner(s).", "success"
                )

        threading.Thread(target=scan_task, daemon=True).start()
        return {"ok": True}

    def cancel_scan(self):
        """Stop the scan after the address it is on. Miners already found stay saved."""
        if not self._scan_running:
            return {"ok": True, "running": False}
        self._scan_cancel.set()
        with self._lock:
            if self._scan is not None:
                self._scan["message"] = "Stopping scan..."
        return {"ok": True, "running": True}

    def remove_miner_address(self, ip):
        """Drop one miner from the table and from config.json."""
        ip = str(ip or "").strip()
        if not ip:
            return _fail("Please select a miner to delete.", "No Selection", "warning")
        if not any(miner["ip"] == ip for miner in get_miners()):
            return _fail(f"Miner with IP {ip} was not found.")
        self._signal_miner_stop(ip)
        remove_miner(ip)
        with self._lock:
            self._rows = [row for row in self._rows if row["ip"] != ip]
            self._drop_miner_runtime_locked(ip)
        self.log_message("Miner(s) removed successfully.", "success")
        return {"ok": True}

    def edit_miner(self, current_ip, nickname, new_ip, repasted_on=None):
        """Save a nickname and repaste date. A new address must be a board on the list.

        `repasted_on` is `YYYY-MM-DD`, blank to clear it, or None to keep it.
        """
        current_ip = str(current_ip or "").strip()
        new_ip = str(new_ip or "").strip()
        nickname = str(nickname or "").strip()
        if not new_ip:
            return _fail("IP Address is required.")
        if repasted_on is not None:
            repasted_on, error = parse_repaste_date(repasted_on)
            if error:
                return _fail(error)
        miners = get_miners()
        previous = next((miner for miner in miners if miner["ip"] == current_ip), None)
        if previous is None:
            return _fail(f"Miner with IP {current_ip} was not found.")
        if new_ip != current_ip and any(miner["ip"] == new_ip for miner in miners):
            return _fail(f"Miner with IP {new_ip} already exists.")

        new_board = None
        if new_ip == current_ip:
            miner_type = self._miner_type(current_ip)
        else:
            info = get_system_info(new_ip)
            if isinstance(info, str):
                return _fail(info)
            new_board = board_for_info(info)
            if new_board is None:
                return _fail(f"{new_ip}: {board_list_note(info)}", "Unsupported Board")
            miner_type = miner_type_from_info(info)
            self._signal_miner_stop(current_ip)

        if (
            self._apply_miner_edit(
                current_ip, nickname, new_ip, miner_type, repasted_on, new_board
            )
            is False
        ):
            return _fail(CONFIG_CORRUPT_MESSAGE, "Config file damaged", "error")
        self.log_message(
            f"Updated miner settings: {nickname} ({miner_type}) at {new_ip}", "success"
        )
        if repasted_on is not None and repasted_on != str(
            previous.get("repasted_on") or ""
        ):
            label = nickname or new_ip
            self.log_message(
                f"Saved {label}'s repaste date: {repasted_on}."
                if repasted_on
                else f"Cleared {label}'s repaste date.",
                "success",
            )
        if new_ip != current_ip:
            self._start_miners_if_running([new_ip])
        return {
            "ok": True,
            "message": f"Updated miner settings: {nickname} ({miner_type}) at {new_ip}",
        }

    def restart_miner(self, ip):
        """Restart one miner. The page confirms before it calls this."""
        ip = str(ip or "").strip()
        if not ip:
            return _fail("Please select a miner first.", "No Selection", "warning")
        with self._lock:
            self._reap_threads_locked()
            if self._baseline_reset_running:
                blocked = "baseline"
            elif self._restart_all_running:
                blocked = "restart"
            elif any(
                getattr(thread, "miner_ip", None) == ip and thread.is_alive()
                for thread in self.threads
            ):
                blocked = "tuning"
            else:
                blocked = None
        if blocked == "baseline":
            self.log_message(
                "Wait for the baseline reset to finish before restarting this miner.",
                "warning",
            )
            return _fail(
                "Wait for the baseline reset to finish before restarting this miner.",
                "Reset in Progress",
                "warning",
            )
        if blocked == "restart":
            self.log_message(
                "Wait for the miner restart to finish before restarting this miner.",
                "warning",
            )
            return _fail(
                "Wait for the miner restart to finish before restarting this miner.",
                "Restart in Progress",
                "warning",
            )
        if blocked == "tuning":
            self.log_message(f"Stop the autotuner before restarting {ip}.", "warning")
            return _fail(
                "Stop the autotuner before restarting this miner.",
                "Autotuner Running",
                "warning",
            )
        self.log_message(f"Restarting miner at {ip}...", "warning")
        message = restart_bitaxe(ip)
        if not restart_was_accepted(message):
            self.log_message(message, "error")
            return _fail(message, "Restart Failed")
        self.log_message(message, "warning")
        return {
            "ok": True,
            "message": message,
            "notice": _notice("info", "Restart Triggered", message),
        }

    def get_global_settings(self):
        """Global steps, temperatures, fast start, and flatline detection."""
        config = load_config()
        # A setting an older version did not save shows its default.
        defaults = get_default_config()
        settings = {key: config.get(key, defaults[key]) for key in GLOBAL_INT_FIELDS}
        settings["fast_start"] = fast_start_enabled(config)
        settings["continue_from_live"] = continue_from_live_enabled(config)
        settings["default_mode"] = new_miner_mode(None, config).key
        settings["weather_enabled"] = bool(config.get("weather_enabled", True))
        for key in INTERNET_SWITCHES:
            settings[key] = internet_switch_on(config, key)
        settings["modes"] = [
            {"key": key, "name": modes.MODES[key].name} for key in modes.MODE_ORDER
        ]
        settings["flatline_detection_enabled"] = bool(
            config.get("flatline_detection_enabled", False)
        )
        settings["flatline_hashrate_repeat_count"] = config.get(
            "flatline_hashrate_repeat_count", 5
        )
        return {"ok": True, "settings": settings}

    def save_global_settings(self, settings):
        """Write global settings. Integer fields must be whole numbers."""
        if not isinstance(settings, dict):
            return _fail("Please enter valid integer values.")
        try:
            new_settings = {key: int(settings[key]) for key in GLOBAL_INT_FIELDS}
            # A page that does not know the switch leaves it on.
            new_settings["fast_start"] = _as_bool(settings.get("fast_start", True))
            new_settings["continue_from_live"] = _as_bool(
                settings.get("continue_from_live", True)
            )
            # A page that does not know the field leaves the saved mode alone.
            chosen = modes.mode_named(settings.get("default_mode"))
            if chosen is not None:
                new_settings["default_mode"] = chosen.key
            for key in ("weather_enabled", *INTERNET_SWITCHES):
                if key in settings:
                    new_settings[key] = _as_bool(settings[key])
            new_settings["flatline_detection_enabled"] = _as_bool(
                settings.get("flatline_detection_enabled")
            )
            new_settings["flatline_hashrate_repeat_count"] = int(
                settings["flatline_hashrate_repeat_count"]
            )
        except (KeyError, TypeError, ValueError):
            return _fail("Please enter valid integer values.")
        if new_settings["monitor_interval"] < 1:
            return _fail("Monitor interval must be at least 1 second.")
        if new_settings["refresh_interval"] < HASHRATE_1M_SETTLE_SECONDS:
            return _fail(
                f"Tune interval must be at least {HASHRATE_1M_SETTLE_SECONDS} seconds."
            )

        def mutate(config):
            config.update(new_settings)

        if modify_config(mutate) is False:
            return _fail(CONFIG_CORRUPT_MESSAGE, "Config file damaged", "error")
        self.log_message("Global settings updated.", "success")
        return {"ok": True}

    def get_autotuner_settings(self):
        """Per-miner limits. A blank cell stays blank so Save can turn that miner off."""
        miners = get_miners()
        if not miners:
            return _fail(
                "Please add a miner first before modifying AutoTuner settings.",
                "No Miners Found",
                "warning",
            )
        config = load_config()
        rows = []
        for miner in miners:
            board = board_for_record(miner)
            mode = modes.mode_for_record(miner, board)
            presets = {
                key: _cells(board_limits(board, config, key))
                for key in modes.MODE_ORDER
            }
            defaults = presets[mode.key]
            fields = {}
            for field in ALL_AUTOTUNE_FIELDS:
                fallback = defaults.get(field, "") if field in NEW_LIMIT_FIELDS else ""
                display = miner.get(field, fallback)
                fields[field] = "" if display is None else str(display)
            nickname = miner.get("nickname") or miner["ip"]
            hard = board.limits
            amps = hard.max_core_amps
            model = board.asic.model
            rows.append(
                {
                    "ip": miner["ip"],
                    "name": nickname,
                    "label": f"{nickname} ({miner['ip']}) · {board.name}",
                    "enabled": bool(miner.get("enabled", False)),
                    "fields": fields,
                    "board": board.name,
                    "mode": mode.key,
                    "presets": presets,
                    "custom": not _same_cells(fields, defaults, board),
                    "experimental": not board.verified,
                    "unused": list(board.unused_limit_fields),
                    "freq_hint": (
                        f"Frequency stays {hard.min_freq}–{hard.max_freq} MHz on this "
                        f"board. {hard.min_freq} is the lowest {model} preset in AxeOS."
                    ),
                    "volt_hint": (
                        f"Voltage stays {hard.min_volt}–{hard.max_volt} mV. "
                        f"{hard.min_volt} is the lowest {model} voltage preset."
                    ),
                    "amps_note": (
                        ""
                        if amps is None
                        else f"At most {amps:g} A. The regulator shuts down with no "
                        f"retry at {board.family.regulator.iout_fault:g} A."
                    ),
                }
            )
        return {
            "ok": True,
            "miners": rows,
            "modes": [
                {"key": mode.key, "name": mode.name, "summary": mode.summary}
                for mode in (modes.MODES[key] for key in modes.MODE_ORDER)
            ],
        }

    def save_autotuner_settings(self, rows):
        """Save every miner's limits. A blank cell turns that miner off."""
        if not isinstance(rows, list):
            return _fail("Enter a number for each limit.")
        boards = {miner.get("ip"): board_for_record(miner) for miner in get_miners()}
        parsed = []
        for row in rows:
            if not isinstance(row, dict):
                return _fail("Enter a number for each limit.")
            incoming = row.get("fields") if isinstance(row.get("fields"), dict) else row
            board = boards.get(row.get("ip"), GAMMA_601)
            fields = {}
            for field in ALL_AUTOTUNE_FIELDS:
                if field in board.unused_limit_fields:
                    fields[field] = ""
                    continue
                try:
                    fields[field] = parse_autotuner_value(
                        field, incoming.get(field, ""), board
                    )
                except (TypeError, ValueError):
                    return _fail(
                        f"Enter a number for {field.replace('_', ' ')} on {row.get('ip')}."
                    )
            parsed.append((row, fields))

        for row, fields in parsed:
            problem = limit_order_error(fields, row.get("ip") or "miner")
            if problem:
                return _fail(problem)

        stopped = []
        enabled = []
        mode_changed = set()

        def mutate(config):
            by_ip = {miner["ip"]: miner for miner in config.get("miners", [])}
            for row, fields in parsed:
                miner = by_ip.get(row.get("ip"))
                if miner is None:
                    continue
                miner.update(fields)
                chosen = modes.mode_named(row.get("mode"))
                if chosen is not None:
                    if (
                        modes.mode_for_record(miner, board_for_record(miner))
                        is not chosen
                    ):
                        mode_changed.add(miner["ip"])
                    miner["mode"] = chosen.key
                if any(miner[field] == "" for field in required_fields(miner)):
                    miner["enabled"] = False
                else:
                    miner["enabled"] = _as_bool(row.get("enabled"))
                if not miner["enabled"]:
                    stopped.append(miner["ip"])
                else:
                    enabled.append(miner["ip"])

        if modify_config(mutate) is False:
            return _fail(CONFIG_CORRUPT_MESSAGE, "Config file damaged", "error")
        # A running tuner reads its mode once, so a miner whose mode changed
        # starts over in the new one.
        for ip in [*stopped, *(ip for ip in enabled if ip in mode_changed)]:
            self._signal_miner_stop(ip)
        self._start_miners_if_running(enabled)
        self.log_message("Updated AutoTuner settings for all miners.", "success")
        return {"ok": True}

    def set_fullscreen(self, enabled):
        """Fill the work area. The header, table, and log stay; the toolbar does not.

        On Windows the window then covers the monitor, so the desktop does not
        show through the resize border or the rounded corners.
        """
        enabled = bool(enabled)
        window = self._window
        if window is not None and enabled != self._fullscreen:
            try:
                window.toggle_fullscreen()
            except Exception as exc:
                return _fail(str(exc))
            if enabled and platform.system() == "Windows":
                snap_fullscreen_window(window)
        self._fullscreen = enabled
        return {"ok": True, "fullscreen": enabled}

    def refresh_once(self):
        """Read each saved miner once. Tests call this directly."""
        with self._lock:
            if self._status_refresh_running:
                return
            ips = [row["ip"] for row in self._rows]
            if not ips:
                return
            self._status_refresh_running = True
        try:
            # Read the miners in parallel. One at a time, each offline miner
            # held every other row back by its full request timeout.
            results = [None] * len(ips)

            def read(index, ip):
                results[index] = (ip, get_system_info(ip))

            readers = [
                threading.Thread(target=read, args=(index, ip), daemon=True)
                for index, ip in enumerate(ips)
            ]
            for reader in readers:
                reader.start()
            for reader in readers:
                reader.join()
            self._apply_results([result for result in results if result])
        finally:
            with self._lock:
                self._status_refresh_running = False

    def _display_loop(self):
        while not self._closed.is_set():
            try:
                self.refresh_once()
            except Exception:
                pass
            self._wake.wait(self._poll_interval())
            self._wake.clear()

    def _poll_interval(self):
        """How often the table reads every miner. The tuner keeps its own interval."""
        return STATUS_REFRESH_SECONDS

    def _apply_results(self, results):
        messages = []
        with self._lock:
            by_ip = {row["ip"]: row for row in self._rows}
            for ip, miner_data in results:
                row = by_ip.get(ip)
                if row is None:
                    continue
                if isinstance(miner_data, dict):
                    self._pool_targets[ip] = pool_targets(miner_data)
                self._apply_one_locked(row, ip, miner_data)
                message = self._note_alert_locked(row)
                if message:
                    messages.append(message)
            self._updated = format_local_time()
        self._deliver_alerts(messages)
        self._clear_cooled_overheat(results)
        self._record_history(results)

    def _apply_one_locked(self, row, ip, miner_data):
        if isinstance(miner_data, str) or not isinstance(miner_data, dict):
            misses = self._misses.get(ip, 0) + 1
            self._misses[ip] = misses
            if misses < OFFLINE_AFTER_MISSES:
                # Keep the last reading. One missed read is not an outage.
                row["reason"] = "no reply"
                return
            row["phase"] = "offline"
            row["tag"] = "alert"
            row["reason"] = ""
            row["power_fault"] = False
            row["overheat"] = False
            row["floor_alert"] = False
            return
        self._misses.pop(ip, None)
        self._maybe_adopt_hostname(ip, miner_data, row)
        stored = get_miner_defaults(ip)
        minute_hash = (
            miner_data.get("hashRate_1m") if "hashRate_1m" in miner_data else None
        )
        row["freq"] = format_number(miner_data.get("frequency"), 0)
        row["mv"] = format_number(miner_data.get("coreVoltage"), 0)
        row["mv_title"] = format_core_voltage_title(
            miner_data.get("coreVoltage"), miner_data.get("coreVoltageActual")
        )
        drooping = droop_alert(
            miner_data.get("coreVoltage"), miner_data.get("coreVoltageActual"), stored
        )
        reads = self._droop_reads.get(ip, 0) + 1 if drooping else 0
        self._droop_reads[ip] = reads
        row["mv_alert"] = reads >= DROOP_ALERT_READS
        row["floor_alert"] = below_floor(
            miner_data.get("frequency"), miner_data.get("coreVoltage"), stored
        )
        row["vin"] = format_input_voltage(miner_data.get("voltage"))
        board = board_for_record(stored)
        row["asic"] = format_number(asic_temp(miner_data), 1)
        row["vr"] = (
            format_number(miner_data.get("vrTemp"), 1) if board.has_vr_temp else "-"
        )
        row["hash"] = format_minute_hashrate(miner_data)
        row["hash_title"] = format_hash_title(miner_data)
        row["watts"] = format_number(miner_data.get("power"), 2)
        row["jth"] = format_efficiency(
            miner_data.get("power"), minute_hash, miner_data.get("power_fault")
        )
        row["best"] = format_difficulty(miner_data.get("bestDiff"))
        row["best_title"] = difficulty_title(miner_data.get("bestDiff"))
        row["session"] = format_difficulty(miner_data.get("bestSessionDiff"))
        row["session_title"] = difficulty_title(miner_data.get("bestSessionDiff"))
        row["shares"] = format_shares(
            miner_data.get("sharesAccepted"), miner_data.get("sharesRejected")
        )
        row["shares_title"] = format_share_title(miner_data)
        row["up"], row["up_seconds"] = format_uptime(miner_data.get("uptimeSeconds"))
        row["name_title"] = format_version_title(miner_data)
        status = get_miner_status(ip)
        row["phase"] = status.get("phase") or "-"
        error = miner_data.get("errorPercentage", status.get("error_percentage"))
        row["error"] = "-" if error in (None, "") else f"{format_number(error, 2)}%"
        row["limit"] = live_limit(status)
        row["reason"] = str(status.get("reason") or "").strip()
        row["power_fault"] = _power_fault_set(miner_data.get("power_fault"))
        row["overheat"] = _overheat_mode_set(miner_data.get("overheat_mode"))
        if not row["reason"] and row["overheat"]:
            row["reason"] = "overheat mode"
        elif not row["reason"] and row["power_fault"]:
            row["reason"] = "power fault"
        elif not row["reason"] and row["floor_alert"]:
            row["reason"] = "under minimum clocks"
        host, fallback = pool_host(miner_data)
        row["pool"] = host
        row["fallback"] = fallback
        rssi = wifi_reading(miner_data)
        row["wifi"] = "" if rssi is None else format_number(rssi, 0)
        row["wifi_weak"] = wifi_is_weak(rssi)
        row["best_exact"] = _plain_number(miner_data.get("bestDiff"))
        settings = load_config()
        row["asic_level"] = limit_level(
            asic_temp(miner_data),
            stored.get("max_temp"),
            _configured_tolerance(settings, "temp_tolerance"),
        )
        row["vr_level"] = (
            limit_level(
                miner_data.get("vrTemp"),
                stored.get("max_vr_temp"),
                _configured_tolerance(settings, "vr_temp_tolerance"),
            )
            if board.has_vr_temp
            else ""
        )
        row["error_alert"] = over_limit(error, stored.get("max_error_percentage"))
        amps = core_current_amps(miner_data, board)
        amps_cap = core_amps_cap(stored, board)
        row["watts_alert"] = over_limit(
            miner_data.get("power"), stored.get("max_watts")
        )
        row["amps"] = "-" if amps is None else format_number(amps, 1)
        row["amps_level"] = limit_level(amps, amps_cap, CORE_AMPS_WARN_BAND)
        row["watts_title"] = (
            "" if amps is None else f"Core current {amps:.1f} A of {amps_cap:g} A"
        )
        row["vin_alert"] = under_limit(
            normalize_input_voltage(miner_data.get("voltage")),
            stored.get("min_input_voltage"),
        )
        row["tag"] = row_state_tag(
            row["phase"],
            row["asic"],
            row["error"],
            stored.get("max_temp"),
            stored.get("max_error_percentage"),
        )
        if row["power_fault"] or row["overheat"] or row["floor_alert"]:
            row["tag"] = "alert"

    def _drop_miner_runtime_locked(self, ip):
        self._pool_targets.pop(ip, None)
        self._alerts.pop(ip, None)
        self._misses.pop(ip, None)
        self._droop_reads.pop(ip, None)
        self._overheat_clear_failed.discard(ip)

    def _clear_cooled_overheat(self, results):
        """PATCH overheat_mode 0 when a cool sample still carries the flag.

        A failed write is logged once. Later polls retry quietly until the
        miner reports the flag clear.
        """
        for ip, miner_data in results:
            if not isinstance(miner_data, dict):
                continue
            with self._lock:
                if not any(row["ip"] == ip for row in self._rows):
                    continue
            if not _overheat_mode_set(miner_data.get("overheat_mode")):
                with self._lock:
                    self._overheat_clear_failed.discard(ip)
                continue
            stored = get_miner_defaults(ip)
            if not overheat_ready_to_clear(
                miner_data,
                _cap_or_default(stored, "max_temp"),
                _cap_or_default(stored, "max_vr_temp"),
                board_for_record(stored).limits.asic_off_watts,
                board_for_record(stored).has_vr_temp,
            ):
                continue
            with self._lock:
                if not any(row["ip"] == ip for row in self._rows):
                    continue
                quiet = ip in self._overheat_clear_failed
            ok, error = patch_system(ip, {"overheat_mode": 0})
            with self._lock:
                if ok:
                    self._overheat_clear_failed.discard(ip)
                else:
                    self._overheat_clear_failed.add(ip)
            if quiet:
                continue
            if ok:
                self.log_message(f"{ip} -> Cleared overheat mode.")
            else:
                self.log_message(
                    f"{ip} -> Could not clear overheat mode: {error}", "warning"
                )

    def _note_alert_locked(self, row):
        kind = alert_kind(row)
        previous = self._alerts.get(row["ip"], "")
        self._alerts[row["ip"]] = kind
        if not kind or kind == previous or self._focused:
            return ""
        return alert_message(row.get("name") or row["ip"], kind)

    def _deliver_alerts(self, messages):
        for message in messages:
            try:
                notify("Groundhog Gamma Tuner", message)
            except Exception:
                pass

    def _maybe_adopt_hostname(self, ip, miner_data, row):
        stored_name = str(
            get_miner_defaults(ip).get("nickname") or row.get("name") or ""
        )
        hostname = adopted_hostname(stored_name, ip, miner_data)
        if not hostname:
            return
        update_miner(ip, {"nickname": hostname})
        row["name"] = hostname

    def _apply_miner_edit(
        self, current_ip, nickname, new_ip, miner_type, repasted_on=None, board=None
    ):
        def mutate(config):
            for miner in config.get("miners", []):
                if miner["ip"] == current_ip:
                    miner["nickname"] = nickname
                    miner["type"] = miner_type
                    miner["ip"] = new_ip
                    if board is not None:
                        miner["board"] = board.version
                    if repasted_on is not None:
                        miner["repasted_on"] = repasted_on
                    break

        if modify_config(mutate) is False:
            return False
        self.load_rows()
        return True

    def _miner_type(self, ip, fallback="Unknown"):
        stored = get_miner_defaults(ip)
        return stored.get("type") or fallback

    def _miner_names(self):
        names = {}
        for miner in get_miners():
            ip = str(miner.get("ip") or "").strip()
            name = str(miner.get("nickname") or "").strip()
            if ip and name:
                names[ip] = name
        return names

    def _autotuner_active_locked(self):
        """True while a tuner thread is starting, running, or still leaving."""
        return (
            self.running
            or self._start_pending
            or self._stop_in_progress
            or any(thread.is_alive() for thread in self.threads)
        )

    def _miner_thread(self, miner, interval, startup_delay, after=None):
        """One tuner thread and the event that asks it to leave.

        `after` is a tuner thread for the same miner that was asked to stop and
        has not exited yet. The new one waits for it, so two never tune at once.
        """
        miner_event = threading.Event()
        thread = threading.Thread(
            target=_run_after,
            args=(
                after,
                monitor_and_adjust,
                miner["ip"],
                miner.get("type", "Unknown"),
                interval,
                self.log_message,
                miner.get("min_freq"),
                miner.get("max_freq"),
                miner.get("min_volt"),
                miner.get("max_volt"),
                miner.get("max_temp"),
                miner.get("max_watts"),
                miner.get("start_freq", ""),
                miner.get("start_volt", ""),
                miner.get("max_vr_temp"),
            ),
            kwargs={
                "stop_event": miner_event,
                "startup_delay": startup_delay,
            },
            daemon=True,
        )
        thread.miner_ip = miner["ip"]
        return miner_event, thread

    def _start_miners_if_running(self, ips):
        """Start threads for miners enabled while a session is already running."""
        ips = {ip for ip in ips if ip}
        if not ips:
            return
        with self._lock:
            if (
                not self.running
                or self._stop_in_progress
                or self._baseline_reset_running
                or self._restart_all_running
            ):
                return
            self._reap_threads_locked()
            # A thread already asked to stop is leaving. Turning the miner off
            # and on again before it exits must still start a new tuner.
            alive = set()
            stopping = {}
            for thread in self.threads:
                ip = getattr(thread, "miner_ip", None)
                event = self._miner_stops.get(ip)
                if event is not None and event.is_set():
                    stopping[ip] = thread
                else:
                    alive.add(ip)
        runtime = load_config()
        interval = runtime.get("monitor_interval", 5)
        ready = []
        for miner in runtime.get("miners", []):
            ip = miner.get("ip")
            if ip not in ips or not miner.get("enabled") or ip in alive:
                continue
            if missing_start_fields(miner):
                continue
            if needs_experimental_ok(miner):
                self.log_message(
                    f"{ip} -> Not started: {board_for_record(miner).name} is not "
                    "verified yet. Stop and Start the autotuner to confirm it.",
                    "warning",
                )
                continue
            ready.append(miner)
        if not ready:
            return
        events = {}
        threads = []
        for miner in ready:
            event, thread = self._miner_thread(
                miner, interval, 0, after=stopping.get(miner["ip"])
            )
            events[miner["ip"]] = event
            threads.append(thread)
        with self._lock:
            if (
                not self.running
                or self._stop_in_progress
                or (self.stop_event is not None and self.stop_event.is_set())
            ):
                return
            for ip, event in events.items():
                self._miner_stops[ip] = event
            self.threads.extend(threads)
        for thread in threads:
            thread.start()
        with self._lock:
            if self._stop_in_progress or (
                self.stop_event is not None and self.stop_event.is_set()
            ):
                for event in events.values():
                    event.set()
        names = ", ".join(miner["ip"] for miner in ready)
        self.log_message(f"Starting autotuning for {names}.", "success")

    def _signal_miner_stop(self, ip):
        """Ask the tuner thread for one address to leave. Other miners keep running."""
        with self._lock:
            event = self._miner_stops.get(ip)
        if event is not None:
            event.set()

    def _signal_all_stops(self):
        """Ask every tuner thread to leave."""
        with self._lock:
            events = list(self._miner_stops.values())
            shared = self.stop_event
        if shared is not None:
            shared.set()
        for event in events:
            event.set()

    def _reap_threads_locked(self):
        """Drop tuner threads that have already exited and mark those rows stopped."""
        finished = [thread for thread in self.threads if not thread.is_alive()]
        self.threads = [thread for thread in self.threads if thread.is_alive()]
        if finished:
            self._publish_stopped_phases(finished)
        if (
            finished
            and not self.threads
            and not self._stop_in_progress
            and not self._start_pending
        ):
            self.running = False

    def _controls_locked(self):
        self._reap_threads_locked()
        alive = bool(self.threads)
        if self._stop_in_progress or (
            alive and not self.running and not self._baseline_reset_running
        ):
            status, label = "stopping", "Stopping"
        elif self._baseline_reset_running:
            status, label = "resetting", "Resetting"
        elif self._restart_all_running and not self.running:
            status, label = "restarting", "Restarting"
        elif self.running:
            status, label = "running", "Running"
        else:
            status, label = "idle", "Idle"
        reset_locked = (
            self._baseline_reset_running
            or self._restart_all_running
            or self.running
            or self._stop_in_progress
            or self._start_pending
            or alive
        )
        return {
            "status": status,
            "status_label": label,
            "reset_enabled": not reset_locked,
            "restart_all_enabled": not (
                self._restart_all_running
                or self._baseline_reset_running
                or self._autotuner_active_locked()
            ),
            "scan_enabled": not self._scan_running,
        }

    def _publish_stopped_phases(self, threads):
        """Leave a finished tuner on Stopped. Keep the limit it stopped at."""
        stopped_ips = []
        with self._lock:
            running_ips = {
                getattr(thread, "miner_ip", "")
                for thread in self.threads
                if thread.is_alive()
            }
        for thread in threads:
            ip = getattr(thread, "miner_ip", "")
            # A newer tuner for this miner owns its status now.
            if not ip or ip in running_ips:
                continue
            phase = str(get_miner_status(ip).get("phase") or "").strip().lower()
            if phase in ("climb", "hold", "trim"):
                _publish_status(ip, phase="stopped")
                stopped_ips.append(ip)
        if not stopped_ips:
            return
        with self._lock:
            for row in self._rows:
                if row.get("ip") not in stopped_ips:
                    continue
                stored = get_miner_defaults(row["ip"])
                row["phase"] = "stopped"
                row["tag"] = row_state_tag(
                    "stopped",
                    row.get("asic"),
                    row.get("error"),
                    stored.get("max_temp"),
                    stored.get("max_error_percentage"),
                )

    def _finish_stop(self):
        with self._lock:
            finished = [thread for thread in self.threads if not thread.is_alive()]
            self.threads = [thread for thread in self.threads if thread.is_alive()]
            self._stop_in_progress = False
            self.running = False
            still_running = bool(self.threads)
        if still_running:
            self.log_message(
                "Some tuner threads are still finishing a request.", "warning"
            )
        else:
            self.log_message("Autotuning stopped.", "warning")
        self._publish_stopped_phases(finished)


def _run_after(previous, target, *args, **kwargs):
    """Wait for `previous` to exit when there is one, then run `target`."""
    if previous is not None:
        previous.join()
    target(*args, **kwargs)


class DashboardApi:
    """Methods pywebview exposes to the page. Names stay public on purpose."""

    def __init__(self, dashboard):
        self._dashboard = dashboard

    def get_snapshot(self, since_log_id=0, focused=None):
        return self._dashboard.get_snapshot(since_log_id, focused)

    def start_autotuner(self, confirmed=False):
        return self._dashboard.start_autotuner(bool(confirmed))

    def stop_autotuner(self):
        return self._dashboard.stop_autotuner()

    def reset_baseline(self):
        return self._dashboard.reset_baseline()

    def reset_miner_baseline(self, ip):
        return self._dashboard.reset_miner_baseline(ip)

    def get_hardware_limits(self):
        return self._dashboard.get_hardware_limits()

    def get_history(self, filters=None):
        return self._dashboard.get_history(filters)

    def get_setup_state(self):
        return self._dashboard.get_setup_state()

    def complete_setup(self, answers):
        return self._dashboard.complete_setup(answers)

    def get_location(self):
        return self._dashboard.get_location()

    def search_location(self, query):
        return self._dashboard.search_location(query)

    def detect_location(self):
        return self._dashboard.detect_location()

    def save_location(self, place):
        return self._dashboard.save_location(place)

    def clear_location(self):
        return self._dashboard.clear_location()

    def start_scan(self, start_ip, end_ip):
        return self._dashboard.start_scan(start_ip, end_ip)

    def cancel_scan(self):
        return self._dashboard.cancel_scan()

    def remove_miner_address(self, ip):
        return self._dashboard.remove_miner_address(ip)

    def edit_miner(self, current_ip, nickname, new_ip, repasted_on=None):
        return self._dashboard.edit_miner(current_ip, nickname, new_ip, repasted_on)

    def restart_miner(self, ip):
        return self._dashboard.restart_miner(ip)

    def restart_all_miners(self):
        return self._dashboard.restart_all_miners()

    def get_global_settings(self):
        return self._dashboard.get_global_settings()

    def save_global_settings(self, settings):
        return self._dashboard.save_global_settings(settings)

    def get_autotuner_settings(self):
        return self._dashboard.get_autotuner_settings()

    def save_autotuner_settings(self, rows):
        return self._dashboard.save_autotuner_settings(rows)

    def set_fullscreen(self, enabled):
        return self._dashboard.set_fullscreen(enabled)


def _coerce_log_id(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
