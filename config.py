import copy
import ipaddress
import json
import os
import tempfile
import threading

import requests

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
HARD_MAX_VOLT = 1400
DEFAULT_MIN_INPUT_VOLTAGE = 4.9
DEFAULT_MAX_ERROR_PERCENTAGE = 2.0
DEFAULT_MAX_DROOP_MV = 40
DEFAULT_CEILING_SOAK_SECONDS = 30 * 60

# Gamma 601 clocks as they ship from the factory.
STOCK_FREQ = 525
STOCK_VOLT = 1150

# Per-miner caps for a custom-cooled Gamma 601. Still editable per chip.
# The tuner holds near 65°C on the ASIC and 85°C on the regulator, and steps
# down above 68°C / 88°C. max_watts is a runaway guard, not the performance limit.
# max_freq is the hard cap so a strong chip is not stopped early.
GAMMA601_LIMITS = {
    "min_freq": HARD_MIN_FREQ,
    "max_freq": HARD_MAX_FREQ,
    "start_freq": STOCK_FREQ,
    "min_volt": HARD_MIN_VOLT,
    "max_volt": 1300,
    "start_volt": STOCK_VOLT,
    "max_temp": 68,
    "max_watts": 50,
    "max_vr_temp": 88,
    "min_input_voltage": DEFAULT_MIN_INPUT_VOLTAGE,
    "max_error_percentage": DEFAULT_MAX_ERROR_PERCENTAGE,
    "max_droop_mv": DEFAULT_MAX_DROOP_MV,
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

    detected_miners = []
    config = load_config()
    addresses = list(range(int(start_ip), int(end_ip) + 1))
    total = len(addresses)

    for index, ip in enumerate(addresses, start=1):
        if should_cancel is not None and should_cancel():
            break
        ip_str = str(ipaddress.IPv4Address(ip))
        if on_progress is not None:
            on_progress(index, total, ip_str)
        try:
            response = requests.get(f"http://{ip_str}/api/system/info", timeout=1)
            if response.status_code == 200:
                miner_info = response.json()
                if not is_gamma_601(miner_info):
                    print(f"Skipping {ip_str}: not a Bitaxe Gamma 601.")
                    continue
                model = miner_type_from_info(miner_info)

                # Prevent duplicate miner entries
                if not any(m["ip"] == ip_str for m in config["miners"]):
                    detected = new_miner_record(
                        model, ip_str, miner_name_from_info(miner_info, ip_str), config
                    )
                    detected_miners.append(detected)
                    print(
                        f"Detected miner: {model} at {ip_str}, added as {detected_miners[-1]['nickname']}"
                    )

        except (requests.exceptions.RequestException, ValueError):
            continue

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
            with open(CONFIG_FILE, "r") as file:
                loaded = json.load(file)
        except json.JSONDecodeError:
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

        _drop_daily_reset(loaded)
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
        _drop_daily_reset(config)
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


def _drop_daily_reset(config):
    """Daily reset is no longer a setting. Drop leftover keys on load and save."""
    if isinstance(config, dict):
        config.pop("daily_reset_enabled", None)
        config.pop("daily_reset_time", None)


def _write_config(config):
    """Write config.json via a temp file in the same directory, then rename it."""
    config_path = os.path.abspath(CONFIG_FILE)
    directory = os.path.dirname(config_path) or "."
    fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as file:
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
    """A miner row with empty learned fields. Limits are filled by gamma_601_limits."""
    record = {
        "nickname": nickname,
        "type": miner_type,
        "ip": ip,
        "enabled": True,
        "last_good_freq": "",
        "last_good_volt": "",
        "wall_type": "",
        "wall_timestamp": "",
        "target_hashrate": "",
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
        "default_target_temp": 68,
        "temp_tolerance": 3,
        "vr_temp_tolerance": 3,
        "refresh_interval": 180,
        "ceiling_soak_seconds": DEFAULT_CEILING_SOAK_SECONDS,
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
