"""Local dashboard window for the Gamma 601 tuner.

The page is a file inside a pywebview window. Python keeps the tuner threads
and hands the page a snapshot to poll. Nothing listens on the network.
"""

import ipaddress
import os
import platform
import re
import socket
import subprocess
import threading
from collections import deque
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape

import requests

from autotune import (
    STARTUP_STAGGER_SECONDS,
    STOCK_FREQ,
    STOCK_VOLT,
    _overheat_mode_set,
    _power_fault_set,
    _publish_status,
    get_miner_status,
    get_system_info,
    monitor_and_adjust,
    normalize_input_voltage,
    reset_miners_to_baseline,
    restart_bitaxe,
)
from config import (
    DEFAULT_MAX_DROOP_MV,
    GAMMA601_LIMITS,
    HARD_MAX_FREQ,
    HARD_MAX_VOLT,
    HARD_MIN_FREQ,
    HARD_MIN_VOLT,
    adopted_hostname,
    detect_miners,
    get_miner_defaults,
    get_miners,
    is_gamma_601,
    load_config,
    miner_type_from_info,
    remove_miner,
    save_config,
    update_miner,
)

STATUS_REFRESH_SECONDS = 5
LOG_LIMIT = 500
WEAK_WIFI_DBM = -70
NETWORK_REFRESH_SECONDS = 60
DIFFICULTY_URL = "https://mempool.space/api/v1/mining/hashrate/3d"
FIRMWARE_RELEASES_URL = "https://api.github.com/repos/bitaxeorg/ESP-Miner/releases?per_page=100"
_STABLE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_INSTALLED_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")
# Stratum V2 listeners. CK Pool is stratum.ckpool.org:3336. Public Pool solo is :23330.
POOLS = (
    ("stratum.ckpool.org", 3336),
    ("public-pool.io", 23330),
)

FREQ_FIELDS = (("min_freq", "Min"), ("start_freq", "Start"), ("max_freq", "Max"))
VOLT_FIELDS = (("min_volt", "Min"), ("start_volt", "Start"), ("max_volt", "Max"))
LIMIT_FIELDS = (
    ("max_temp", "ASIC temp (°C)"),
    ("max_watts", "Watts"),
    ("max_vr_temp", "VR temp (°C)"),
    ("min_input_voltage", "Input voltage (V)"),
    ("max_error_percentage", "Error %"),
    ("max_droop_mv", "Droop (mV)"),
)
ALL_AUTOTUNE_FIELDS = tuple(field for field, _label in (*FREQ_FIELDS, *VOLT_FIELDS, *LIMIT_FIELDS))
GLOBAL_INT_FIELDS = (
    "voltage_step",
    "frequency_step",
    "monitor_interval",
    "refresh_interval",
    "default_target_temp",
    "temp_tolerance",
    "vr_temp_tolerance",
    "ceiling_soak_seconds",
)
START_REQUIRED_FIELDS = (
    "min_freq",
    "max_freq",
    "min_volt",
    "max_volt",
    "max_temp",
    "max_watts",
    "max_vr_temp",
)


def replace_ips_with_names(message, names):
    """Swap known miner IPs for nicknames. Longer addresses are replaced first.

    A nickname that already contains its IP is left alone so Miner-192.168.8.10
    does not become Miner-Miner-192.168.8.10.
    """
    text = "" if message is None else str(message)
    if not text or not names:
        return text
    for ip in sorted(names, key=len, reverse=True):
        name = str(names.get(ip) or "").strip()
        if not ip or not name or name == ip or ip in name:
            continue
        text = re.sub(rf"\b{re.escape(str(ip))}\b", lambda _match, replacement=name: replacement, text)
    return text


def parse_autotuner_value(field, raw):
    """Parse one AutoTuner cell. Frequency and voltage are clamped to the Gamma 601 range."""
    text = str(raw).strip()
    if text == "":
        return ""
    if field in ("min_input_voltage", "max_error_percentage"):
        return float(text)
    number = int(float(text))
    if field in ("min_freq", "max_freq", "start_freq"):
        return max(HARD_MIN_FREQ, min(HARD_MAX_FREQ, number))
    if field in ("min_volt", "max_volt", "start_volt"):
        return max(HARD_MIN_VOLT, min(HARD_MAX_VOLT, number))
    return number


def format_learned_wall(status, stored):
    wall = (status or {}).get("wall_type") or (stored or {}).get("wall_type") or ""
    freq = (status or {}).get("last_good_freq")
    volt = (status or {}).get("last_good_volt")
    if freq in ("", None):
        freq = (stored or {}).get("last_good_freq") or ""
    if volt in ("", None):
        volt = (stored or {}).get("last_good_volt") or ""
    if wall and freq not in ("", None) and volt not in ("", None):
        return f"{wall} {freq}/{volt}"
    if freq not in ("", None) and volt not in ("", None):
        return f"{freq}/{volt}"
    return wall or "-"


def parse_display_number(value):
    if value in (None, "", "-"):
        return None
    cleaned = str(value).replace("°C", "").replace("°", "").replace("%", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


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
    if not isinstance(info, dict) or "hashRate_1m" not in info or info.get("hashRate_1m") is None:
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
    """Short age and the whole seconds used to sort it. Largest unit only."""
    number = _plain_number(seconds)
    if number is None or number < 0:
        return "-", None
    total = int(number)
    if total >= 86400:
        text = f"{total // 86400}d"
    elif total >= 3600:
        text = f"{total // 3600}h"
    elif total >= 60:
        text = f"{total // 60}m"
    else:
        text = f"{total}s"
    return text, total


def format_hash_title(info):
    """Live, 10-minute, and expected hashrate for the GH/s tooltip."""
    if not isinstance(info, dict):
        return ""
    lines = []
    live = format_number(info.get("hashRate"), 2)
    ten = format_number(info.get("hashRate_10m"), 2)
    expected = format_number(info.get("expectedHashrate"), 2)
    if live != "-":
        lines.append(f"Live {live} GH/s")
    if ten != "-":
        lines.append(f"10m {ten} GH/s")
    if expected != "-":
        lines.append(f"Expected {expected} GH/s")
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
    return f"Measured {format_number(actual_mv, 0)} mV, droop {format_number(droop, 0)} mV"


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
    hosts = list(network.hosts())
    if len(hosts) < 2:
        return "", ""
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


def solo_day_odds(hash_ghs, difficulty):
    """Average days per block. None when hashrate or network difficulty is missing."""
    rate = _plain_number(hash_ghs)
    diff = _plain_number(difficulty)
    if rate is None or diff is None or rate <= 0 or diff <= 0:
        return None
    seconds = (diff * 2 ** 32) / (rate * 1_000_000_000)
    return seconds / 86400.0


def format_odds_count(days):
    number = _plain_number(days)
    if number is None or number <= 0:
        return ""
    if number < 10:
        return f"{number:.1f}"
    return format_difficulty(number)


def format_solo_odds(hash_ghs, difficulty):
    """'1 in N today' from fleet hashrate and the current network difficulty."""
    days = solo_day_odds(hash_ghs, difficulty)
    count = format_odds_count(days)
    if not count:
        return ""
    return f"1 in {count} today"


def format_best_share_percent(best, difficulty):
    """How close the best share is to a block, as a percent of network difficulty."""
    share = _plain_number(best)
    diff = _plain_number(difficulty)
    if share is None or diff is None or share <= 0 or diff <= 0:
        return ""
    percent = share / diff * 100.0
    if percent >= 1:
        text = f"{percent:.2f}"
    elif percent >= 0.01:
        text = f"{percent:.4f}"
    elif percent >= 0.000001:
        text = f"{percent:.6f}"
    else:
        text = f"{percent:.2e}"
    return f"Best share is {text}% of the network"


def fleet_summary(rows):
    """Online count, summed hash and watts, and the best share across the table."""
    online = offline = climb = hold = trim = 0
    hash_sum = 0.0
    watt_sum = 0.0
    paired_watts = 0.0
    counted_hash = False
    counted_watts = False
    best = None
    live_phases = {"climb", "hold", "trim", "skipped", "stopped"}
    for row in rows or []:
        phase = str(row.get("phase") or "").strip().lower()
        number = _plain_number(row.get("best_exact"))
        if number is not None and (best is None or number > best):
            best = number
        if phase == "offline":
            offline += 1
            continue
        has_reading = (
            _plain_number(row.get("hash")) is not None or _plain_number(row.get("freq")) is not None
        )
        if phase in live_phases or has_reading:
            online += 1
        if phase == "climb":
            climb += 1
        elif phase == "hold":
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
        "climb": climb,
        "hold": hold,
        "trim": trim,
        "hash": format_number(hash_sum, 2) if counted_hash else "-",
        "watts": format_number(watt_sum, 2) if counted_watts else "-",
        "jth": format_number(joules, 2) if joules is not None else "-",
        "hash_ghs": hash_sum if counted_hash else 0.0,
        "best": best,
    }


def alert_kind(row):
    """The background notice for one row. Offline wins over a fault still on the last sample."""
    phase = str((row or {}).get("phase") or "").strip().lower()
    if phase == "offline":
        return "offline"
    if (row or {}).get("overheat"):
        return "overheat"
    if (row or {}).get("power_fault"):
        return "power_fault"
    return ""


def alert_message(name, kind):
    label = str(name or "Miner").strip() or "Miner"
    if kind == "offline":
        return f"{label} is offline."
    if kind == "overheat":
        return f"{label} is in overheat mode."
    if kind == "power_fault":
        return f"{label} reported a power fault."
    return ""


def show_windows_toast(title, message):
    """A local Windows toast. Other systems do nothing."""
    if platform.system() != "Windows":
        return
    heading = xml_escape(str(title or "Groundhog Gamma Tuner").replace("\r", " ").replace("\n", " "))
    body = xml_escape(str(message or "").replace("\r", " ").replace("\n", " "))
    toast_xml = (
        "<toast><visual><binding template=\"ToastGeneric\">"
        f"<text>{heading}</text><text>{body}</text>"
        "</binding></visual></toast>"
    )
    script = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@'
%s
'@)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($toast)
""" % toast_xml
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


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


def read_network_status(get=None, connect=None):
    """Current block difficulty, and whether the two Stratum V2 ports answer."""
    getter = requests.get if get is None else get
    difficulty = None
    try:
        response = getter(DIFFICULTY_URL, timeout=8)
        response.raise_for_status()
        difficulty = response.json().get("currentDifficulty")
    except Exception:
        difficulty = None
    pools = []
    for host, port in POOLS:
        pools.append({
            "name": host,
            "online": stratum_port_open(host, port, connect=connect),
        })
    return {"difficulty": difficulty, "pools": pools}


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
    limit_number = parse_display_number(limit)
    if value is None or limit_number is None:
        return False
    return value > limit_number


def under_limit(value, limit):
    """True when a reading falls under its floor."""
    number = parse_display_number(value)
    floor = parse_display_number(limit)
    if number is None or floor is None:
        return False
    return number < floor


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
    if phase_name == "climb":
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
        "setpoint": "-",
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
    }


BASELINE_PROMPT = (
    f"Set every miner to the Gamma 601 stock clocks ({STOCK_FREQ} MHz / {STOCK_VOLT} mV) "
    "and forget the saved setpoint?\n\n"
    "The next Start Autotuner will climb or step down from there."
)


def _as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _notice(level, title, message):
    return {"level": level, "title": title, "message": message}


def _fail(message, title="Error", level="error"):
    return {"ok": False, "message": message, "notice": _notice(level, title, message)}


def format_local_time(moment=None):
    """Short time in the Windows clock format. Other systems keep HH:MM:SS."""
    moment = moment or datetime.now()
    if platform.system() != "Windows":
        return moment.strftime("%H:%M:%S")
    try:
        formatted = _windows_short_time(moment)
    except (OSError, AttributeError):
        formatted = ""
    return formatted or moment.strftime("%H:%M:%S")


def _windows_short_time(moment):
    """User short time via GetTimeFormatEx. TIME_NOSECONDS matches the taskbar clock."""
    import ctypes
    from ctypes import wintypes

    class SystemTime(ctypes.Structure):
        _fields_ = [
            ("wYear", wintypes.WORD),
            ("wMonth", wintypes.WORD),
            ("wDayOfWeek", wintypes.WORD),
            ("wDay", wintypes.WORD),
            ("wHour", wintypes.WORD),
            ("wMinute", wintypes.WORD),
            ("wSecond", wintypes.WORD),
            ("wMilliseconds", wintypes.WORD),
        ]

    system_time = SystemTime(
        moment.year,
        moment.month,
        (moment.weekday() + 1) % 7,
        moment.day,
        moment.hour,
        moment.minute,
        moment.second,
        moment.microsecond // 1000,
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_time = kernel.GetTimeFormatEx
    get_time.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(SystemTime),
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    get_time.restype = ctypes.c_int
    buffer = ctypes.create_unicode_buffer(64)
    # TIME_NOSECONDS: the taskbar short time, without a forced seconds field.
    written = get_time(None, 0x00000002, ctypes.byref(system_time), None, buffer, len(buffer))
    if written <= 0:
        return ""
    return buffer.value


class TunerDashboard:
    """Tuner state the page reads and the actions it calls."""

    def __init__(self):
        self._lock = threading.RLock()
        self.running = False
        self.threads = []
        self.stop_event = None
        self._status_refresh_running = False
        self._stop_in_progress = False
        self._baseline_reset_running = False
        self._start_pending = False
        self._rows = []
        self._log = deque(maxlen=LOG_LIMIT)
        self._log_seq = 0
        self._updated = "--:--:--"
        self._network = {
            "difficulty": "-",
            "difficulty_title": "",
            "difficulty_exact": None,
            "pools": [{"name": host, "online": None} for host, _port in POOLS],
        }
        self._latest_firmware = ""
        self._firmware_checked = None
        self._alerts = {}
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

    def run(self):
        """Open the local dashboard window and block until it closes."""
        try:
            import webview
        except ImportError as exc:
            message = "The dashboard window needs pywebview. Install it with: pip install -r requirements.txt"
            print(message)
            raise SystemExit(message) from exc

        self.start_background()
        page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
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
        if platform.system() != "Windows" or self._window is None:
            return
        try:
            self._window.maximize()
        except Exception:
            pass

    def _on_closing(self):
        self._closed.set()
        self._scan_cancel.set()
        self._wake.set()
        if self.stop_event is not None:
            self.stop_event.set()
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
        rows = []
        for miner in get_miners():
            ip = miner["ip"]
            nickname = miner.get("nickname") or f"Miner-{ip}"
            rows.append(blank_miner_row(nickname, ip))
        with self._lock:
            self._rows = rows
            kept = {row["ip"] for row in rows}
            for ip in list(self._alerts):
                if ip not in kept:
                    self._drop_miner_runtime_locked(ip)
        self.log_message(f"Loaded {len(rows)} miners.", "success")
        self._wake.set()

    def log_message(self, message, level="info"):
        """Append one activity line. Tuner threads call this directly."""
        message = replace_ips_with_names(message, self._miner_names())
        timestamp = datetime.now().strftime("%H:%M:%S")
        if level not in ("success", "warning", "error", "info"):
            level = "info"
        with self._lock:
            self._log_seq += 1
            self._log.append({
                "id": self._log_seq,
                "text": f"[{timestamp}] {message}",
                "level": level,
            })

    def get_snapshot(self, since_log_id=0, focused=None):
        """Table, button state, scan progress, and log lines after since_log_id.

        `focused` is whether the window is in front. A background fault can toast.
        """
        since = _coerce_log_id(since_log_id)
        with self._lock:
            if focused is not None:
                self._focused = _as_bool(focused)
            summary = fleet_summary(self._rows)
            difficulty = self._network.get("difficulty_exact")
            latest = self._latest_firmware
            miners = []
            for row in self._rows:
                item = dict(row)
                item["firmware_update"] = firmware_update_version(item.get("name_title"), latest)
                miners.append(item)
            return {
                "updated": self._updated,
                "miners": miners,
                "log": [dict(line) for line in self._log if line["id"] > since],
                "scan": None if self._scan is None else dict(self._scan),
                "controls": self._controls_locked(),
                "prompts": {"baseline": BASELINE_PROMPT},
                "network": self._network_locked(),
                "fleet": {
                    "online": summary["online"],
                    "offline": summary["offline"],
                    "climb": summary["climb"],
                    "hold": summary["hold"],
                    "trim": summary["trim"],
                    "hash": summary["hash"],
                    "watts": summary["watts"],
                    "jth": summary["jth"],
                },
                "odds": {
                    "today": format_solo_odds(summary["hash_ghs"], difficulty),
                    "best": format_best_share_percent(summary["best"], difficulty),
                },
                "scan_range": dict(self._scan_range),
            }

    def _network_locked(self):
        return {
            "difficulty": self._network["difficulty"],
            "difficulty_title": self._network["difficulty_title"],
            "pools": [dict(pool) for pool in self._network["pools"]],
        }

    def _refresh_network(self):
        status = read_network_status()
        number = _plain_number(status.get("difficulty"))
        with self._lock:
            if number is not None:
                self._network["difficulty"] = format_difficulty(number)
                self._network["difficulty_title"] = difficulty_title(number)
                self._network["difficulty_exact"] = number
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
                self._refresh_network()
            except Exception:
                pass
            try:
                self._refresh_firmware_if_due()
            except Exception:
                pass
            if self._closed.wait(NETWORK_REFRESH_SECONDS):
                return

    def start_autotuner(self):
        """Start one tuner thread per enabled miner that has the required limits."""
        with self._lock:
            if self._baseline_reset_running:
                blocked = "baseline"
            elif self._start_pending or self.running or self._stop_in_progress or any(
                thread.is_alive() for thread in self.threads
            ):
                blocked = "running"
            else:
                blocked = None
                self._start_pending = True
        if blocked == "baseline":
            self.log_message("Wait for the baseline reset to finish before starting.", "warning")
            return _fail(
                "Wait for the baseline reset to finish before starting.",
                "Reset in Progress",
                "warning",
            )
        if blocked == "running":
            self.log_message("Autotuner is already running.", "warning")
            return _fail("Autotuner is already running.", "Autotuner Running", "warning")

        notice = None
        try:
            config = load_config()
            interval = config.get("monitor_interval", 5)
            self.log_message("Checking AutoTuner settings before starting...", "info")

            enabled_miners = [miner for miner in config.get("miners", []) if miner.get("enabled", False)]
            ready_miners = []
            missing_settings = []
            for miner in enabled_miners:
                missing = [
                    field for field in START_REQUIRED_FIELDS
                    if field not in miner or miner[field] == "" or miner[field] is None
                ]
                if missing:
                    for field in missing:
                        missing_settings.append((miner["ip"], field))
                else:
                    ready_miners.append(miner)

            if not enabled_miners:
                message = "No miners are enabled for AutoTuning. Please enable at least one miner in settings."
                self.log_message(message, "error")
                return _fail(message, "No Miners Enabled", "warning")

            if missing_settings:
                error_message = "These miners are missing AutoTuner settings and will be skipped:\n\n"
                for ip, field in missing_settings:
                    error_message += f"- Miner {ip}: Missing {field}\n"
                self.log_message(error_message, "error")
                if not ready_miners:
                    return _fail(error_message.strip(), "Incomplete Settings", "error")
                notice = _notice("warning", "Some miners skipped", error_message.strip())

            stop_event = threading.Event()
            threads = []
            self.log_message("Starting autotuning for selected miners...", "success")
            for index, miner in enumerate(ready_miners):
                thread = threading.Thread(
                    target=monitor_and_adjust,
                    args=(
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
                        "stop_event": stop_event,
                        "startup_delay": index * STARTUP_STAGGER_SECONDS,
                    },
                    daemon=True,
                )
                thread.miner_ip = miner["ip"]
                thread.start()
                threads.append(thread)

            with self._lock:
                self.stop_event = stop_event
                self.threads = threads
                self.running = True
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
            if self.stop_event is not None:
                self.stop_event.set()
            threads = list(self.threads)
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
            elif (
                self.running or self._stop_in_progress or self._start_pending
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
        if blocked == "busy":
            self.log_message("Stop the autotuner before resetting to baseline.", "warning")
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

        self.log_message(f"Resetting miners to {STOCK_FREQ} MHz / {STOCK_VOLT} mV.", "info")

        def work():
            try:
                reset_miners_to_baseline(miners, self.log_message, parallel=True)
            finally:
                with self._lock:
                    self._baseline_reset_running = False
                self.log_message(
                    f"Baseline reset finished. Start Autotuner to tune from {STOCK_FREQ} MHz / {STOCK_VOLT} mV.",
                    "success",
                )

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def start_scan(self, start_ip, end_ip):
        """Scan an inclusive IPv4 range. Only a Gamma 601 is saved."""
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
                self.log_message(f"Scan finished. Added {len(found)} miner(s).", "success")

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
        remove_miner(ip)
        with self._lock:
            self._rows = [row for row in self._rows if row["ip"] != ip]
            self._drop_miner_runtime_locked(ip)
        self.log_message("Miner(s) removed successfully.", "success")
        return {"ok": True}

    def edit_miner(self, current_ip, nickname, new_ip):
        """Save a nickname. A new address is checked and must still be a Gamma 601."""
        current_ip = str(current_ip or "").strip()
        new_ip = str(new_ip or "").strip()
        nickname = str(nickname or "").strip()
        if not new_ip:
            return _fail("IP Address is required.")
        miners = get_miners()
        if not any(miner["ip"] == current_ip for miner in miners):
            return _fail(f"Miner with IP {current_ip} was not found.")
        if new_ip != current_ip and any(miner["ip"] == new_ip for miner in miners):
            return _fail(f"Miner with IP {new_ip} already exists.")

        if new_ip == current_ip:
            miner_type = self._miner_type(current_ip)
        else:
            info = get_system_info(new_ip)
            if isinstance(info, str):
                return _fail(info)
            if not is_gamma_601(info):
                return _fail(
                    f"{new_ip} is not a Bitaxe Gamma 601 (BM1370, board 601).",
                    "Not a Gamma 601",
                )
            miner_type = miner_type_from_info(info)

        self._apply_miner_edit(current_ip, nickname, new_ip, miner_type)
        self.log_message(f"Updated miner settings: {nickname} ({miner_type}) at {new_ip}", "success")
        return {"ok": True, "message": f"Updated miner settings: {nickname} ({miner_type}) at {new_ip}"}

    def restart_miner(self, ip):
        """Restart one miner. The page confirms before it calls this."""
        ip = str(ip or "").strip()
        if not ip:
            return _fail("Please select a miner first.", "No Selection", "warning")
        self.log_message(f"Restarting miner at {ip}...", "warning")
        message = restart_bitaxe(ip)
        self.log_message(message, "warning")
        return {"ok": True, "message": message, "notice": _notice("info", "Restart Triggered", message)}

    def get_global_settings(self):
        """Global steps, temperatures, and flatline detection."""
        config = load_config()
        settings = {key: config.get(key, "") for key in GLOBAL_INT_FIELDS}
        settings["flatline_detection_enabled"] = bool(config.get("flatline_detection_enabled", True))
        settings["flatline_hashrate_repeat_count"] = config.get("flatline_hashrate_repeat_count", 5)
        return {"ok": True, "settings": settings}

    def save_global_settings(self, settings):
        """Write global settings. Integer fields must be whole numbers."""
        if not isinstance(settings, dict):
            return _fail("Please enter valid integer values.")
        try:
            new_settings = {key: int(settings[key]) for key in GLOBAL_INT_FIELDS}
            new_settings["flatline_detection_enabled"] = _as_bool(settings.get("flatline_detection_enabled"))
            new_settings["flatline_hashrate_repeat_count"] = int(settings["flatline_hashrate_repeat_count"])
        except (KeyError, TypeError, ValueError):
            return _fail("Please enter valid integer values.")
        config = load_config()
        config.update(new_settings)
        config.pop("enforce_safe_pairing", None)
        save_config(config)
        self.log_message("Global settings updated.", "success")
        return {"ok": True}

    def get_autotuner_settings(self):
        """Per-miner limits. Empty cells fall back to the Gamma 601 defaults for display."""
        miners = get_miners()
        if not miners:
            return _fail(
                "Please add a miner first before modifying AutoTuner settings.",
                "No Miners Found",
                "warning",
            )
        rows = []
        for miner in miners:
            fields = {}
            for field in ALL_AUTOTUNE_FIELDS:
                display = miner.get(field, "")
                if display in ("", None) and field in GAMMA601_LIMITS:
                    display = GAMMA601_LIMITS[field]
                fields[field] = "" if display is None else str(display)
            nickname = miner.get("nickname") or miner["ip"]
            rows.append({
                "ip": miner["ip"],
                "name": nickname,
                "label": f"{nickname} ({miner['ip']})",
                "enabled": bool(miner.get("enabled", False)),
                "fields": fields,
            })
        return {"ok": True, "miners": rows}

    def save_autotuner_settings(self, rows):
        """Save every miner's limits. A blank cell turns that miner off."""
        if not isinstance(rows, list):
            return _fail("Enter a number for each limit.")
        config = load_config()
        by_ip = {miner["ip"]: miner for miner in config.get("miners", [])}
        for row in rows:
            if not isinstance(row, dict):
                return _fail("Enter a number for each limit.")
            miner = by_ip.get(row.get("ip"))
            if miner is None:
                continue
            incoming = row.get("fields") if isinstance(row.get("fields"), dict) else row
            for field in ALL_AUTOTUNE_FIELDS:
                try:
                    miner[field] = parse_autotuner_value(field, incoming.get(field, ""))
                except (TypeError, ValueError):
                    return _fail(
                        f"Enter a number for {field.replace('_', ' ')} on {row.get('ip')}."
                    )
            if any(miner[field] == "" for field in ALL_AUTOTUNE_FIELDS):
                miner["enabled"] = False
            else:
                miner["enabled"] = _as_bool(row.get("enabled"))
        save_config(config)
        self.log_message("Updated AutoTuner settings for all miners.", "success")
        return {"ok": True}

    def set_fullscreen(self, enabled):
        """Fill the work area. The header, table, and log stay; the toolbar does not."""
        enabled = bool(enabled)
        window = self._window
        if window is not None and enabled != self._fullscreen:
            try:
                window.toggle_fullscreen()
            except Exception as exc:
                return _fail(str(exc))
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
            results = [(ip, get_system_info(ip)) for ip in ips]
            self._apply_results(results)
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
                self._apply_one_locked(row, ip, miner_data)
                message = self._note_alert_locked(row)
                if message:
                    messages.append(message)
            self._updated = format_local_time()
        self._deliver_alerts(messages)

    def _apply_one_locked(self, row, ip, miner_data):
        if isinstance(miner_data, str) or not isinstance(miner_data, dict):
            row["phase"] = "offline"
            row["tag"] = "alert"
            row["reason"] = ""
            row["power_fault"] = False
            row["overheat"] = False
            return
        self._maybe_adopt_hostname(ip, miner_data, row)
        stored = get_miner_defaults(ip)
        minute_hash = miner_data.get("hashRate_1m") if "hashRate_1m" in miner_data else None
        row["freq"] = format_number(miner_data.get("frequency"), 0)
        row["mv"] = format_number(miner_data.get("coreVoltage"), 0)
        row["mv_title"] = format_core_voltage_title(
            miner_data.get("coreVoltage"), miner_data.get("coreVoltageActual")
        )
        row["mv_alert"] = droop_alert(
            miner_data.get("coreVoltage"), miner_data.get("coreVoltageActual"), stored
        )
        row["vin"] = format_input_voltage(miner_data.get("voltage"))
        row["asic"] = format_number(miner_data.get("temp"), 1)
        row["vr"] = format_number(miner_data.get("vrTemp"), 1)
        row["hash"] = format_minute_hashrate(miner_data)
        row["hash_title"] = format_hash_title(miner_data)
        row["watts"] = format_number(miner_data.get("power"), 2)
        row["jth"] = format_efficiency(miner_data.get("power"), minute_hash, miner_data.get("power_fault"))
        row["best"] = format_difficulty(miner_data.get("bestDiff"))
        row["best_title"] = difficulty_title(miner_data.get("bestDiff"))
        row["session"] = format_difficulty(miner_data.get("bestSessionDiff"))
        row["session_title"] = difficulty_title(miner_data.get("bestSessionDiff"))
        row["shares"] = format_shares(miner_data.get("sharesAccepted"), miner_data.get("sharesRejected"))
        row["shares_title"] = format_share_title(miner_data)
        row["up"], row["up_seconds"] = format_uptime(miner_data.get("uptimeSeconds"))
        row["name_title"] = format_version_title(miner_data)
        status = get_miner_status(ip)
        row["phase"] = status.get("phase") or "-"
        error = miner_data.get("errorPercentage", status.get("error_percentage"))
        row["error"] = "-" if error in (None, "") else f"{format_number(error, 2)}%"
        row["setpoint"] = format_learned_wall(status, stored)
        row["reason"] = str(status.get("reason") or "").strip()
        row["power_fault"] = _power_fault_set(miner_data.get("power_fault"))
        row["overheat"] = _overheat_mode_set(miner_data.get("overheat_mode"))
        if not row["reason"] and row["overheat"]:
            row["reason"] = "overheat mode"
        elif not row["reason"] and row["power_fault"]:
            row["reason"] = "power fault"
        host, fallback = pool_host(miner_data)
        row["pool"] = host
        row["fallback"] = fallback
        rssi = wifi_reading(miner_data)
        row["wifi"] = "" if rssi is None else format_number(rssi, 0)
        row["wifi_weak"] = wifi_is_weak(rssi)
        row["best_exact"] = _plain_number(miner_data.get("bestDiff"))
        settings = load_config()
        row["asic_level"] = limit_level(
            miner_data.get("temp"), stored.get("max_temp"), _configured_tolerance(settings, "temp_tolerance")
        )
        row["vr_level"] = limit_level(
            miner_data.get("vrTemp"), stored.get("max_vr_temp"), _configured_tolerance(settings, "vr_temp_tolerance")
        )
        row["error_alert"] = over_limit(error, stored.get("max_error_percentage"))
        row["watts_alert"] = over_limit(miner_data.get("power"), stored.get("max_watts"))
        row["vin_alert"] = under_limit(
            normalize_input_voltage(miner_data.get("voltage")), stored.get("min_input_voltage")
        )
        row["tag"] = row_state_tag(
            row["phase"],
            row["asic"],
            row["error"],
            stored.get("max_temp"),
            stored.get("max_error_percentage"),
        )
        if row["power_fault"] or row["overheat"]:
            row["tag"] = "alert"

    def _drop_miner_runtime_locked(self, ip):
        self._alerts.pop(ip, None)

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
                show_windows_toast("Groundhog Gamma Tuner", message)
            except Exception:
                pass

    def _maybe_adopt_hostname(self, ip, miner_data, row):
        stored_name = str(get_miner_defaults(ip).get("nickname") or row.get("name") or "")
        hostname = adopted_hostname(stored_name, ip, miner_data)
        if not hostname:
            return
        update_miner(ip, {"nickname": hostname})
        row["name"] = hostname

    def _apply_miner_edit(self, current_ip, nickname, new_ip, miner_type):
        config = load_config()
        for miner in config.get("miners", []):
            if miner["ip"] == current_ip:
                miner["nickname"] = nickname
                miner["type"] = miner_type
                miner["ip"] = new_ip
                break
        save_config(config)
        self.load_rows()

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

    def _controls_locked(self):
        if self._stop_in_progress:
            status, label = "stopping", "Stopping"
        elif self._baseline_reset_running:
            status, label = "resetting", "Resetting"
        elif self.running:
            status, label = "running", "Running"
        else:
            status, label = "idle", "Idle"
        reset_locked = (
            self._baseline_reset_running
            or self.running
            or self._stop_in_progress
            or self._start_pending
        )
        return {
            "status": status,
            "status_label": label,
            "reset_enabled": not reset_locked,
            "scan_enabled": not self._scan_running,
        }

    def _publish_stopped_phases(self, threads):
        """Leave a finished tuner on Stopped. Keep the saved setpoint fields."""
        stopped_ips = []
        for thread in threads:
            ip = getattr(thread, "miner_ip", "")
            if not ip:
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
            self.log_message("Some tuner threads are still finishing a request.", "warning")
        else:
            self.log_message("Autotuning stopped.", "warning")
        self._publish_stopped_phases(finished)


class DashboardApi:
    """Methods pywebview exposes to the page. Names stay public on purpose."""

    def __init__(self, dashboard):
        self._dashboard = dashboard

    def get_snapshot(self, since_log_id=0, focused=None):
        return self._dashboard.get_snapshot(since_log_id, focused)

    def start_autotuner(self):
        return self._dashboard.start_autotuner()

    def stop_autotuner(self):
        return self._dashboard.stop_autotuner()

    def reset_baseline(self):
        return self._dashboard.reset_baseline()

    def start_scan(self, start_ip, end_ip):
        return self._dashboard.start_scan(start_ip, end_ip)

    def cancel_scan(self):
        return self._dashboard.cancel_scan()

    def remove_miner_address(self, ip):
        return self._dashboard.remove_miner_address(ip)

    def edit_miner(self, current_ip, nickname, new_ip):
        return self._dashboard.edit_miner(current_ip, nickname, new_ip)

    def restart_miner(self, ip):
        return self._dashboard.restart_miner(ip)

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
