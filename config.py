import copy
import ipaddress
import json
import os
import tempfile
import threading

import requests

SYSTEM_INFO_TIMEOUT = 10
# Addresses a network scan probes at the same time.
SCAN_WORKERS = 32

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
CONFIG_CORRUPT_MESSAGE = (
    "config.json is damaged and was not loaded. "
    "Fix that file before saving. The empty defaults were not written over it."
)
_config_lock = threading.RLock()
_last_good_config = None
_config_corrupt = False

# Gamma 601 hard range. The UI and the tuner both stay inside this.
# 350 MHz is the lowest BM1370 clock in the AxeOS v2.15.1 preset list (Gamma Duo).
# The Gamma list itself starts at 400. The API will store a lower number; this app will not.
# Voltage stays at 1000 mV, the lowest BM1370 voltage preset. The tuner sheds voltage
# only after frequency is already at its floor, so a lower voltage is not used to settle a weak chip.
HARD_MIN_FREQ = 350
HARD_MAX_FREQ = 1100
HARD_MIN_VOLT = 1000
HARD_MAX_VOLT = 1500
DEFAULT_MIN_INPUT_VOLTAGE = 4.9
DEFAULT_MAX_ERROR_PERCENTAGE = 2.0
DEFAULT_MAX_DROOP_MV = 40
# Regulator output current. AxeOS sets the Gamma's TPS546 to warn at 25 A and
# shut down with no retry at 30 A. The tuner stays at or under this cap, and a
# saved cap is never above HARD_MAX_CORE_AMPS. At 1.4 V, 28 A is also about
# 44 W at the 5 V plug.
DEFAULT_MAX_CORE_AMPS = 28.0
HARD_MAX_CORE_AMPS = 29.0
# Settings and learned values an older version saved. Each session now starts
# fresh, so they are dropped on load and save.
RETIRED_GLOBAL_KEYS = (
    "daily_reset_enabled",
    "daily_reset_time",
    "ceiling_soak_seconds",
)
RETIRED_MINER_KEYS = (
    "last_good_freq",
    "last_good_volt",
    "wall_type",
    "wall_timestamp",
    "target_hashrate",
)

# AxeOS v2.15.1 cuts ASIC power above 75°C on the ASIC or 105°C on the regulator.
# After it cools, it saves clocks 100 MHz and 100 mV lower, with no floor.
# The regulator refuses anything under 1000 mV, so a trip from under 1100 mV
# leaves the ASIC off with overheat_mode set until the clocks are rewritten and
# the miner restarts. The tuner does that (autotune.overheat_latched).
# User caps and the tuner's emergency retreat stay this margin under those trips.
FIRMWARE_ASIC_TRIP_C = 75.0
FIRMWARE_VR_TRIP_C = 105.0
FIRMWARE_TRIP_MARGIN_C = 4.0

# Gamma 601 clocks as they ship from the factory.
STOCK_FREQ = 525
STOCK_VOLT = 1150

# Per-miner caps for a repasted, custom-cooled Gamma 601. Still editable per chip.
# The tuner climbs while the ASIC is at or under 70°C and the regulator at or
# under 95°C, and steps down above them. 70°C stays 1°C under the trip guard
# and 5°C under the AxeOS cutoff; the regulator chip itself is rated far hotter. After a heat retreat it waits until
# both are a tolerance band under their caps before climbing again. max_watts is a runaway guard, not the performance limit.
# max_freq is the hard cap so a strong chip is not stopped early.
GAMMA601_LIMITS = {
    "min_freq": HARD_MIN_FREQ,
    "max_freq": HARD_MAX_FREQ,
    "start_freq": STOCK_FREQ,
    "min_volt": HARD_MIN_VOLT,
    "max_volt": 1500,
    "start_volt": STOCK_VOLT,
    "max_temp": 70,
    "max_watts": 50,
    "max_vr_temp": 95,
    "min_input_voltage": DEFAULT_MIN_INPUT_VOLTAGE,
    "max_error_percentage": DEFAULT_MAX_ERROR_PERCENTAGE,
    "max_droop_mv": DEFAULT_MAX_DROOP_MV,
    "max_core_amps": DEFAULT_MAX_CORE_AMPS,
}


def is_gamma_601(miner_info):
    """True only for a single-ASIC BM1370 on board version 601."""
    if not isinstance(miner_info, dict):
        return False
    asic = str(miner_info.get("ASICModel") or "").strip().upper()
    board = str(miner_info.get("boardVersion") or "").strip()
    return asic == "BM1370" and board == "601"


def miner_type_from_info(miner_info):
    """Build a display type from the fields AxeOS actually returns."""
    asic = str(miner_info.get("ASICModel") or "").strip()
    board = str(miner_info.get("boardVersion") or "").strip()
    if asic and board:
        return f"{asic} {board}"
    if asic or board:
        return asic or board
    device = str(miner_info.get("deviceModel") or miner_info.get("model") or "").strip()
    return device or "Unknown"


def placeholder_nickname(ip):
    """Name used until AxeOS reports a hostname."""
    return f"Miner-{ip}"


def is_placeholder_nickname(nickname, ip):
    """True when the saved name is blank or still the generated fallback."""
    text = str(nickname or "").strip()
    return text == "" or text == placeholder_nickname(ip)


def miner_name_from_info(miner_info, ip, nickname=""):
    """Prefer a typed nickname, then the AxeOS hostname, then Miner-{ip}."""
    typed = str(nickname or "").strip()
    if typed:
        return typed
    hostname = ""
    if isinstance(miner_info, dict):
        hostname = str(miner_info.get("hostname") or "").strip()
    return hostname or placeholder_nickname(ip)


def adopted_hostname(nickname, ip, miner_info):
    """Hostname to store when the saved name is still a placeholder. Otherwise None."""
    if not isinstance(miner_info, dict):
        return None
    hostname = str(miner_info.get("hostname") or "").strip()
    if not hostname or not is_placeholder_nickname(nickname, ip):
        return None
    if str(nickname or "").strip() == hostname:
        return None
    return hostname


def detect_miners(start_ip, end_ip, on_progress=None, should_cancel=None):
    """Scan a user-defined IP range and detect Bitaxe miners.

    on_progress(index, total, ip) runs before each address.
    should_cancel() stops the scan before the next address. Miners already found are saved.
    """

    # Convert IPs to IPv4 objects
    try:
        start_ip = ipaddress.IPv4Address(start_ip)
        end_ip = ipaddress.IPv4Address(end_ip)
    except ipaddress.AddressValueError:
        print("Error: Invalid IP range provided.")
        return []

    config = load_config()
    known_ips = {m["ip"] for m in config["miners"]}
    addresses = list(range(int(start_ip), int(end_ip) + 1))
    total = len(addresses)
    found = {}
    next_index = 0
    stopped = False
    claim_lock = threading.Lock()

    def claim():
        """Next (index, ip) to probe, or None once the range is done or cancelled.

        Progress is reported in address order, before that address is probed.
        """
        nonlocal next_index, stopped
        with claim_lock:
            if stopped or next_index >= total:
                return None
            if should_cancel is not None and should_cancel():
                stopped = True
                return None
            index = next_index + 1
            ip_str = str(ipaddress.IPv4Address(addresses[next_index]))
            next_index += 1
            if on_progress is not None:
                on_progress(index, total, ip_str)
            return index, ip_str

    def probe(ip_str):
        try:
            response = requests.get(
                f"http://{ip_str}/api/system/info", timeout=SYSTEM_INFO_TIMEOUT
            )
            if response.status_code != 200:
                return None
            miner_info = response.json()
        except (requests.exceptions.RequestException, ValueError):
            return None
        if not is_gamma_601(miner_info):
            print(f"Skipping {ip_str}: not a Bitaxe Gamma 601.")
            return None
        return miner_info

    def worker():
        while True:
            claimed = claim()
            if claimed is None:
                return
            index, ip_str = claimed
            miner_info = probe(ip_str)
            # Prevent duplicate miner entries
            if miner_info is None or ip_str in known_ips:
                continue
            model = miner_type_from_info(miner_info)
            detected = new_miner_record(
                model, ip_str, miner_name_from_info(miner_info, ip_str), config
            )
            with claim_lock:
                found[index] = detected
            print(
                f"Detected miner: {model} at {ip_str}, added as {detected['nickname']}"
            )

    # Probe several addresses at once. One at a time, each empty address could
    # cost the full timeout, so a /24 took many minutes.
    workers = [
        threading.Thread(target=worker, daemon=True)
        for _ in range(min(SCAN_WORKERS, total))
    ]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    detected_miners = [found[index] for index in sorted(found)]

    if not detected_miners:
        return []

    # Reload under the config lock. The scan holds the list it loaded at the
    # start, and a rename, delete, or settings save during the scan has to win.
    with _config_lock:
        fresh = load_config()
        known = {miner.get("ip") for miner in fresh.get("miners", [])}
        added = [miner for miner in detected_miners if miner.get("ip") not in known]
        if not added:
            return []
        fresh.setdefault("miners", []).extend(added)
        if save_config(fresh) is False:
            print(CONFIG_CORRUPT_MESSAGE)
            return []
    return added


def config_problem():
    """Error text when this process could not parse config.json. Empty when it is fine."""
    with _config_lock:
        if _config_corrupt and _last_good_config is None:
            return CONFIG_CORRUPT_MESSAGE
        return ""


def load_config():
    """Load configuration settings from config.json.

    A partial or corrupt file does not replace the last config that parsed.
    A fresh process keeps the damaged file on disk and reports config_problem().
    """
    global _last_good_config, _config_corrupt
    with _config_lock:
        if not os.path.exists(CONFIG_FILE):
            default = get_default_config()
            _write_config(default)
            _last_good_config = copy.deepcopy(default)
            _config_corrupt = False
            return default

        try:
            # utf-8-sig also reads a file Notepad saved with a byte-order mark.
            # Without an encoding, Windows would decode it as cp1252.
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as file:
                loaded = json.load(file)
            if not isinstance(loaded, dict):
                raise ValueError("config.json is not a JSON object")
        except ValueError:
            # JSONDecodeError and UnicodeDecodeError are both ValueErrors.
            if _last_good_config is not None:
                return copy.deepcopy(_last_good_config)
            _config_corrupt = True
            return get_default_config()
        except FileNotFoundError:
            default = get_default_config()
            _write_config(default)
            _last_good_config = copy.deepcopy(default)
            _config_corrupt = False
            return default

        _drop_retired_keys(loaded)
        if _raise_limits(loaded):
            _write_config(loaded)
        _last_good_config = copy.deepcopy(loaded)
        _config_corrupt = False
        return loaded


def save_config(config):
    """Save configuration settings to config.json atomically.

    Returns False when the file on disk is damaged and this process has no
    parsed copy to replace it with.
    """
    global _last_good_config, _config_corrupt
    with _config_lock:
        if _config_corrupt and _last_good_config is None:
            return False
        _drop_retired_keys(config)
        _write_config(config)
        _last_good_config = copy.deepcopy(config)
        _config_corrupt = False
        return True


def modify_config(mutator):
    """Load, change, and save config.json while holding the config lock.

    `mutator` receives the config dict and may change it in place. Return False
    to leave the file unchanged. A setpoint learned on another thread cannot
    land between this load and this save.
    """
    with _config_lock:
        config = load_config()
        if _config_corrupt and _last_good_config is None:
            return False
        if mutator(config) is False:
            return None
        if save_config(config) is False:
            return False
        return config


# Bumped when the default caps rise and saved miners should follow once.
LIMITS_VERSION = 2
# Caps raised for every saved miner on the first load after the fleet was
# repasted. A saved value that is already higher stays.
RAISED_LIMITS = {"max_temp": 70, "max_vr_temp": 95, "max_volt": 1500}


def _raise_limits(config):
    """Raise saved miners to RAISED_LIMITS once. True when the config changed.

    `limits_version` records that it ran, so a cap lowered later is not
    raised again on the next load.
    """
    if not isinstance(config, dict):
        return False
    try:
        version = int(config.get("limits_version", 1))
    except (TypeError, ValueError):
        version = 1
    if version >= LIMITS_VERSION:
        return False
    try:
        default_temp = float(config.get("default_target_temp"))
    except (TypeError, ValueError):
        default_temp = None
    if default_temp is not None and default_temp < RAISED_LIMITS["max_temp"]:
        config["default_target_temp"] = RAISED_LIMITS["max_temp"]
    for miner in config.get("miners") or []:
        if not isinstance(miner, dict):
            continue
        for key, value in RAISED_LIMITS.items():
            try:
                saved = float(miner.get(key))
            except (TypeError, ValueError):
                saved = None
            if saved is None or saved < value:
                miner[key] = value
    config["limits_version"] = LIMITS_VERSION
    return True


def _drop_retired_keys(config):
    """Drop settings and learned setpoints an older version saved."""
    if not isinstance(config, dict):
        return
    for key in RETIRED_GLOBAL_KEYS:
        config.pop(key, None)
    for miner in config.get("miners") or []:
        if isinstance(miner, dict):
            for key in RETIRED_MINER_KEYS:
                miner.pop(key, None)


def _write_config(config):
    """Write config.json via a temp file in the same directory, then rename it."""
    config_path = os.path.abspath(CONFIG_FILE)
    directory = os.path.dirname(config_path) or "."
    fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(config, file, indent=4)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, config_path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def gamma_601_limits(config=None):
    """Caps for a new Gamma 601. Max temp follows Default Max Temp when that is set."""
    limits = dict(GAMMA601_LIMITS)
    if config is not None and config.get("default_target_temp") not in (None, ""):
        limits["max_temp"] = config.get("default_target_temp")
    return limits


def new_miner_record(miner_type, ip, nickname, config=None):
    """A new miner row. Limits are filled by gamma_601_limits."""
    record = {
        "nickname": nickname,
        "type": miner_type,
        "ip": ip,
        "enabled": True,
        "repasted_on": "",
    }
    for key in GAMMA601_LIMITS:
        record.setdefault(key, "")
    if config is not None:
        record.update(gamma_601_limits(config))
    return record


def get_default_config():
    return {
        "voltage_step": 10,
        "frequency_step": 5,
        "monitor_interval": 5,
        "default_target_temp": 70,
        "temp_tolerance": 3,
        "vr_temp_tolerance": 3,
        "refresh_interval": 180,
        "fast_start": True,
        "limits_version": LIMITS_VERSION,
        "flatline_detection_enabled": False,
        "flatline_hashrate_repeat_count": 5,
        "miners": [],
    }


def get_miner_defaults(miner_ip):
    """Returns the AutoTuner settings for a given miner's IP address."""
    config = load_config()
    for miner in config["miners"]:
        if miner["ip"] == miner_ip:
            return miner  # Return the miner's settings
    return {}  # Return empty dict if not found


def add_miner(miner_type, ip, nickname=""):
    """Adds a new miner with default settings based on type, including nickname."""

    def mutate(config):
        if any(miner["ip"] == ip for miner in config["miners"]):
            print(f"Error: Miner with IP {ip} already exists.")
            return False
        config["miners"].append(new_miner_record(miner_type, ip, nickname, config))

    if modify_config(mutate) is None:
        return
    print(f"Added new miner: ({miner_type}) at {ip} with nickname '{nickname}'")


def remove_miner(ip):
    """Removes a miner from the config by IP address."""

    def mutate(config):
        miners = config.get("miners", [])
        kept = [miner for miner in miners if miner["ip"] != ip]
        if len(kept) == len(miners):
            print(f"Error: Miner with IP {ip} not found.")
            return False
        config["miners"] = kept

    if modify_config(mutate) is None:
        return
    print(f"Removed miner with IP: {ip}")


def update_miner(ip, new_settings):
    """Updates an existing miner's settings in config.json under one lock."""
    global _last_good_config, _config_corrupt
    with _config_lock:
        config = load_config()
        if _config_corrupt and _last_good_config is None:
            print(CONFIG_CORRUPT_MESSAGE)
            return
        updated = False
        for miner in config.get("miners", []):
            if miner.get("ip") == ip:
                miner.update(new_settings)
                updated = True
                break

        if not updated:
            print(f"Error: Miner {ip} not found.")
            return

        _write_config(config)
        _last_good_config = copy.deepcopy(config)
        _config_corrupt = False
        print(f"Updated miner {ip} settings successfully.")


def get_miners():
    """Returns the list of configured miners."""
    return load_config().get("miners", [])


def reset_config():
    """Resets configuration to default settings."""
    save_config(get_default_config())
    print("Configuration reset to default.")


if __name__ == "__main__":
    print("Scanning for Bitaxe miners...")
    miners = detect_miners("192.168.0.1", "192.168.0.255")  # Example default scan range
    if miners:
        print(f"Found {len(miners)} miners: {miners}")
    else:
        print("No miners found.")
