import threading
import time

import requests
from config import (
    DEFAULT_CEILING_SOAK_SECONDS,
    DEFAULT_MAX_DROOP_MV,
    DEFAULT_MAX_ERROR_PERCENTAGE,
    DEFAULT_MIN_INPUT_VOLTAGE,
    HARD_MAX_FREQ,
    HARD_MAX_VOLT,
    HARD_MIN_FREQ,
    HARD_MIN_VOLT,
    STOCK_FREQ,
    STOCK_VOLT,
    is_gamma_601,
    load_config,
    update_miner,
)

# Load global configuration for callers that still use the module-level defaults.
config = load_config()
VOLTAGE_STEP = config.get("voltage_step", 10)
FREQUENCY_STEP = config.get("frequency_step", 5)
MONITOR_INTERVAL = config.get("monitor_interval", 5)
TEMP_TOLERANCE = config.get("temp_tolerance", 3)
VR_TEMP_TOLERANCE = config.get("vr_temp_tolerance", 3)

# Seconds between each miner's first settings write so a swarm does not
# reboot every ASIC on the same cycle.
STARTUP_STAGGER_SECONDS = 3

# Callers that do not pass their own stop event share this one.
_default_stop_event = threading.Event()

_status_lock = threading.Lock()
_miner_status = {}


def begin_autotune_session():
    """Start a new shared session. Threads already running keep the previous event."""
    global _default_stop_event
    _default_stop_event = threading.Event()


def stop_autotuning():
    """Stop tuners that were started without their own stop event."""
    _default_stop_event.set()


def get_miner_status(ip):
    """Latest phase, error, and wall published by a tuner thread."""
    with _status_lock:
        return dict(_miner_status.get(ip) or {})


def _publish_status(ip, **fields):
    with _status_lock:
        row = _miner_status.setdefault(ip, {})
        row.update(fields)


def _clear_miner_status(ip):
    with _status_lock:
        _miner_status.pop(ip, None)


def reset_miners_to_baseline(miners, log_callback, stagger_seconds=STARTUP_STAGGER_SECONDS):
    """Write factory clocks and forget the learned setpoint for each saved miner.

    A failed write is logged and does not skip clearing that miner or the ones after it.
    Min, max, temperature, and power limits are left as the user set them.
    """
    for index, miner in enumerate(list(miners or [])):
        if index > 0 and stagger_seconds:
            time.sleep(stagger_seconds)
        ip = (miner or {}).get("ip")
        if not ip:
            continue
        message = set_system_settings(ip, STOCK_VOLT, STOCK_FREQ)
        log_callback(message, "success" if settings_were_applied(message) else "error")
        update_miner(ip, {
            "last_good_freq": "",
            "last_good_volt": "",
            "wall_type": "",
            "wall_timestamp": "",
            "target_hashrate": "",
            "start_freq": STOCK_FREQ,
            "start_volt": STOCK_VOLT,
        })
        _clear_miner_status(ip)


def _wait(stop_event, seconds):
    """Wait up to `seconds`. Return True if tuning was asked to stop."""
    if seconds is None or seconds <= 0:
        return stop_event.is_set()
    return stop_event.wait(timeout=seconds)


def coerce_limit(value):
    """Return an int limit, or None when the value is missing or not numeric."""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_int(value, default):
    number = _as_float(value)
    if number is None or number <= 0:
        return default
    return int(number)


def _non_negative_float(value, default):
    number = _as_float(value)
    if number is None or number < 0:
        return default
    return number


def _clamp(value, low, high):
    return max(low, min(high, value))


def clamp_limits(limits):
    """Pull user limits inside the Gamma 601 hard range. Max stays at or above min."""
    clamped = dict(limits)
    clamped["min_freq"] = _clamp(int(clamped["min_freq"]), HARD_MIN_FREQ, HARD_MAX_FREQ)
    clamped["max_freq"] = _clamp(int(clamped["max_freq"]), clamped["min_freq"], HARD_MAX_FREQ)
    clamped["min_volt"] = _clamp(int(clamped["min_volt"]), HARD_MIN_VOLT, HARD_MAX_VOLT)
    clamped["max_volt"] = _clamp(int(clamped["max_volt"]), clamped["min_volt"], HARD_MAX_VOLT)
    return clamped


def normalize_input_voltage(value):
    """Return input voltage in volts. Readings above 20 are millivolts."""
    number = _as_float(value)
    if number is None:
        return None
    if number > 20:
        return number / 1000.0
    return number


def _record_float(record, key, default):
    if not record or key not in record or record.get(key) in ("", None):
        return default
    number = _as_float(record.get(key))
    if number is None:
        return default
    return number


def _miner_record(ip):
    for miner in load_config().get("miners", []):
        if miner.get("ip") == ip:
            return miner
    return {}


def remember_setpoint(ip, frequency, voltage, wall_type):
    """Persist a proven setpoint. No-op when that IP is not in the loaded config."""
    if not any(miner.get("ip") == ip for miner in load_config().get("miners", [])):
        return
    update_miner(ip, {
        "last_good_freq": int(frequency),
        "last_good_volt": int(voltage),
        "wall_type": wall_type or "",
        "wall_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


def wall_type_from_reason(reason):
    text = (reason or "").lower()
    if "input sag" in text:
        return "input"
    if "silicon" in text:
        return "silicon"
    if "power limit" in text or "power fault" in text or "droop" in text:
        return "power"
    if text.startswith("step frequency") or text.startswith("step voltage") or "temperature" in text:
        return "thermal"
    return ""


def _thermal_frequency_steps(overshoot, tolerance):
    """How many frequency steps to shed for this many degrees over a cap.

    One step inside a single tolerance band, two inside two bands, and three
    past that. A zero tolerance still sheds one step.
    """
    if overshoot <= 0:
        return 1
    band = tolerance if tolerance and tolerance > 0 else 0
    if band <= 0 or overshoot <= band:
        return 1
    if overshoot <= (2 * band):
        return 2
    return 3


def _step_down(frequency, voltage, min_freq, min_volt, frequency_step, voltage_step, frequency_steps=1):
    """Drop frequency first. Drop voltage only after frequency is already at its floor."""
    drop = frequency_step * max(int(frequency_steps), 1)
    if frequency - drop >= min_freq:
        return frequency - drop, voltage, "step frequency down"
    if frequency > min_freq:
        return min_freq, voltage, "step frequency down"
    if voltage - voltage_step >= min_volt:
        return frequency, voltage - voltage_step, "step voltage down"
    if voltage > min_volt:
        return frequency, min_volt, "step voltage down"
    return frequency, voltage, "holding at minimum"


def _overheat_mode_set(value):
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false")
    try:
        return float(value) != 0
    except (TypeError, ValueError):
        return bool(value)


def _power_fault_set(value):
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "none", "null")
    return bool(value)


def _needs_immediate_retreat(
    temp,
    vr_temp,
    power,
    max_temp,
    max_vr_temp,
    max_watts,
    input_voltage,
    min_input_voltage,
    core_voltage_actual,
    current_voltage,
    max_droop_mv,
    power_fault,
    overheat_mode,
):
    """True when a confirmed setpoint should step down without waiting out a climb interval.

    A watt-cap breach steps down on the next poll, by one frequency step. Error retreats
    still wait, so a noisy error sample cannot walk the clocks down.
    """
    if temp is None or power is None:
        return False
    if _overheat_mode_set(overheat_mode) or power <= 0.5:
        return False
    if float(temp) > max_temp:
        return True
    vr_value = _as_float(vr_temp)
    if vr_value is not None and vr_value > max_vr_temp:
        return True
    if float(power) > max_watts:
        return True
    if min_input_voltage is not None and input_voltage is not None and input_voltage < min_input_voltage:
        return True
    if _power_fault_set(power_fault):
        return True
    actual_voltage = _as_float(core_voltage_actual)
    droop_limit = _as_float(max_droop_mv)
    if (
        actual_voltage is not None
        and droop_limit is not None
        and current_voltage is not None
        and (current_voltage - actual_voltage) > droop_limit
    ):
        return True
    return False


def _proposal_jumps_above_report(
    new_frequency,
    new_voltage,
    reported_frequency,
    reported_voltage,
    frequency_step,
    voltage_step,
):
    """True when a write would move more than one step above the clocks the miner reports."""
    frequency_step = max(int(frequency_step), 1)
    voltage_step = max(int(voltage_step), 1)
    if reported_frequency is not None and new_frequency > reported_frequency + frequency_step:
        return True
    if reported_voltage is not None and new_voltage > reported_voltage + voltage_step:
        return True
    return False


def expected_hashrate_from_info(info, frequency):
    """Prefer the device's expectedHashrate. Fall back to the core formula only when it is non-zero."""
    reported = _as_float(info.get("expectedHashrate"))
    if reported is not None and reported > 0:
        return reported
    small = _as_float(info.get("smallCoreCount"))
    asics = _as_float(info.get("asicCount"))
    if not small or not asics or not frequency:
        return 0
    formula = frequency * (small * asics) / 1000
    return formula if formula > 0 else 0


def _guard_bounds(current_frequency, current_voltage, new_frequency, new_voltage,
                  min_freq, max_freq, min_volt, max_volt, reason):
    new_frequency = _clamp(int(new_frequency), min_freq, max_freq)
    new_voltage = _clamp(int(new_voltage), min_volt, max_volt)
    if new_frequency < current_frequency and new_voltage > current_voltage:
        new_voltage = current_voltage
    return new_frequency, new_voltage, reason


def _apply_step_down(current_frequency, current_voltage, min_freq, max_freq, min_volt, max_volt,
                     frequency_step, voltage_step, reason_tag, frequency_steps=1):
    new_frequency, new_voltage, step_reason = _step_down(
        current_frequency, current_voltage, min_freq, min_volt, frequency_step, voltage_step,
        frequency_steps,
    )
    if step_reason == "holding at minimum" or not reason_tag:
        chosen = step_reason
    else:
        chosen = f"{step_reason} after {reason_tag}"
    return _guard_bounds(
        current_frequency, current_voltage, new_frequency, new_voltage,
        min_freq, max_freq, min_volt, max_volt, chosen
    )


def decide_adjustment(
    current_frequency,
    current_voltage,
    min_freq,
    max_freq,
    min_volt,
    max_volt,
    max_temp,
    max_watts,
    max_vr_temp,
    temp,
    vr_temp,
    power,
    hash_rate,
    expected_hashrate,
    shares_rejected_delta,
    overheat_mode,
    frequency_step,
    voltage_step,
    temp_tolerance,
    tier_list=None,
    error_percentage=None,
    input_voltage=None,
    min_input_voltage=None,
    core_voltage_actual=None,
    max_droop_mv=DEFAULT_MAX_DROOP_MV,
    power_fault=None,
    phase="climb",
    trim_good_voltage=None,
    max_error_percentage=DEFAULT_MAX_ERROR_PERCENTAGE,
    vr_temp_tolerance=3,
):
    """Choose the next frequency and voltage.

    Returns (frequency, voltage, reason). Voltage is never raised while stepping down.
    Either temperature sensor can hold the climb or force a step down.
    A missing regulator temperature blocks climbs and voltage increases.
    `tier_list` is accepted and ignored so older callers keep working.
    """
    del tier_list, expected_hashrate
    frequency_step = max(int(frequency_step), 1)
    voltage_step = max(int(voltage_step), 1)
    temp_tolerance = temp_tolerance if temp_tolerance is not None else 0
    vr_tolerance = vr_temp_tolerance if vr_temp_tolerance is not None else 0
    phase = phase or "climb"

    if temp is None or power is None:
        return current_frequency, current_voltage, "holding for telemetry"

    if _overheat_mode_set(overheat_mode) or power <= 0.5:
        return current_frequency, current_voltage, "holding while the miner is offline or in overheat mode"

    temp_value = float(temp)
    vr_value = _as_float(vr_temp)
    power_value = float(power)
    over_temp = temp_value > max_temp
    over_power = power_value > max_watts
    over_vr = vr_value is not None and vr_value > max_vr_temp
    if over_temp or over_power or over_vr:
        tag = None if (over_temp or over_vr) else "power limit"
        frequency_steps = 1
        if over_temp or over_vr:
            counts = []
            if over_temp:
                counts.append(_thermal_frequency_steps(temp_value - max_temp, temp_tolerance))
            if over_vr:
                counts.append(_thermal_frequency_steps(vr_value - max_vr_temp, vr_tolerance))
            frequency_steps = max(counts)
        return _apply_step_down(
            current_frequency, current_voltage, min_freq, max_freq, min_volt, max_volt,
            frequency_step, voltage_step, tag, frequency_steps
        )

    if min_input_voltage is not None and input_voltage is not None and input_voltage < min_input_voltage:
        return _apply_step_down(
            current_frequency, current_voltage, min_freq, max_freq, min_volt, max_volt,
            frequency_step, voltage_step, "input sag"
        )

    if _power_fault_set(power_fault):
        return _apply_step_down(
            current_frequency, current_voltage, min_freq, max_freq, min_volt, max_volt,
            frequency_step, voltage_step, "power fault"
        )

    actual_voltage = _as_float(core_voltage_actual)
    droop_limit = _as_float(max_droop_mv)
    if actual_voltage is not None and droop_limit is not None and (current_voltage - actual_voltage) > droop_limit:
        return _apply_step_down(
            current_frequency, current_voltage, min_freq, max_freq, min_volt, max_volt,
            frequency_step, voltage_step, "core voltage droop"
        )

    error = _as_float(error_percentage)
    error_budget = DEFAULT_MAX_ERROR_PERCENTAGE if max_error_percentage is None else float(max_error_percentage)
    error_high = error is not None and error > error_budget
    asic_in_band = temp_value >= (max_temp - temp_tolerance)
    vr_in_band = vr_value is not None and vr_value >= (max_vr_temp - vr_tolerance)
    in_band = asic_in_band or vr_in_band

    if shares_rejected_delta and shares_rejected_delta > 0 and not error_high:
        return current_frequency, current_voltage, "holding after a pool reject"

    if error_high:
        if phase == "trim" and trim_good_voltage is not None and current_voltage < int(trim_good_voltage):
            restored = _clamp(int(trim_good_voltage), min_volt, max_volt)
            return _guard_bounds(
                current_frequency, current_voltage, current_frequency, restored,
                min_freq, max_freq, min_volt, max_volt, "restore voltage"
            )
        if phase == "hold" or in_band or current_voltage >= max_volt:
            return _apply_step_down(
                current_frequency, current_voltage, min_freq, max_freq, min_volt, max_volt,
                frequency_step, voltage_step, "silicon wall"
            )
        if vr_value is None:
            return current_frequency, current_voltage, "holding for telemetry"
        stepped = min(max_volt, current_voltage + voltage_step)
        if stepped > current_voltage:
            return _guard_bounds(
                current_frequency, current_voltage, current_frequency, stepped,
                min_freq, max_freq, min_volt, max_volt, "increase voltage"
            )
        return _apply_step_down(
            current_frequency, current_voltage, min_freq, max_freq, min_volt, max_volt,
            frequency_step, voltage_step, "silicon wall"
        )

    if in_band:
        return current_frequency, current_voltage, "holding inside temperature band"

    if error is None:
        return current_frequency, current_voltage, "holding for error percentage"

    if hash_rate is None or hash_rate <= 0:
        return current_frequency, current_voltage, "holding"

    if phase == "trim":
        if current_voltage - voltage_step >= min_volt:
            new_voltage = current_voltage - voltage_step
            return _guard_bounds(
                current_frequency, current_voltage, current_frequency, new_voltage,
                min_freq, max_freq, min_volt, max_volt, "trim voltage"
            )
        if current_voltage > min_volt:
            return _guard_bounds(
                current_frequency, current_voltage, current_frequency, min_volt,
                min_freq, max_freq, min_volt, max_volt, "trim voltage"
            )
        return current_frequency, current_voltage, "trim complete"

    if phase == "hold":
        return current_frequency, current_voltage, "holding"

    if current_frequency >= max_freq:
        return current_frequency, current_voltage, "frequency ceiling"

    if vr_value is None:
        return current_frequency, current_voltage, "holding for telemetry"

    new_frequency = min(max_freq, current_frequency + frequency_step)
    return _guard_bounds(
        current_frequency, current_voltage, new_frequency, current_voltage,
        min_freq, max_freq, min_volt, max_volt, "increase frequency"
    )


def get_system_info(bitaxe_ip):
    """Fetch system info from Bitaxe API."""
    try:
        response = requests.get(f"http://{bitaxe_ip}/api/system/info", timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return f"Error fetching system info from {bitaxe_ip}: {e}"


def patch_system(bitaxe_ip, settings):
    """PATCH /api/system. Returns (ok, error_text)."""
    try:
        response = requests.patch(f"http://{bitaxe_ip}/api/system", json=settings, timeout=10)
        response.raise_for_status()
        return True, ""
    except requests.exceptions.RequestException as e:
        return False, str(e)


def set_system_settings(bitaxe_ip, core_voltage, frequency):
    """Set frequency and core voltage. Overclock must be on or AxeOS ignores values past the dropdown."""
    settings = {
        "coreVoltage": int(core_voltage),
        "frequency": int(frequency),
        "overclockEnabled": 1,
    }
    ok, error = patch_system(bitaxe_ip, settings)
    if not ok:
        return f"{bitaxe_ip} -> Error setting system settings: {error}"
    return f"{bitaxe_ip} -> Applied settings: Voltage = {int(core_voltage)}mV, Frequency = {int(frequency)}MHz"


def settings_were_applied(message):
    return isinstance(message, str) and "Error setting system settings" not in message


def restart_bitaxe(bitaxe_ip):
    """Restart the Bitaxe using the API."""
    try:
        response = requests.post(f"http://{bitaxe_ip}/api/system/restart", timeout=10)
        response.raise_for_status()
        return f"{bitaxe_ip} -> Restart initiated."
    except requests.exceptions.RequestException as e:
        return f"{bitaxe_ip} -> Error restarting system: {e}"


def _numbers_match(reported, requested):
    reported_value = _as_float(reported)
    requested_value = _as_float(requested)
    if reported_value is None or requested_value is None:
        return False
    return int(reported_value) == int(requested_value)


def _same_setpoint(left, right):
    return int(left[0]) == int(right[0]) and int(left[1]) == int(right[1])


def _apply_fan(bitaxe_ip, payload, log_callback):
    ok, error = patch_system(bitaxe_ip, payload)
    if ok:
        log_callback(f"{bitaxe_ip} -> Fan updated.", "info")
    else:
        log_callback(f"{bitaxe_ip} -> Fan update failed: {error}", "warning")


def _reports_error_percentage(info):
    return isinstance(info, dict) and "errorPercentage" in info and info.get("errorPercentage") is not None


def monitor_and_adjust(bitaxe_ip, bitaxe_type, interval, log_callback,
                       min_freq, max_freq, min_volt, max_volt,
                       max_temp, max_watts, start_freq=None, start_volt=None, max_vr_temp=None,
                       stop_event=None, startup_delay=0):
    """Monitor and auto-adjust one Gamma 601. A missing limit skips this miner only."""
    del bitaxe_type  # Kept so existing callers can still pass the miner type.
    event = stop_event if stop_event is not None else _default_stop_event

    limits = {
        "min_freq": coerce_limit(min_freq),
        "max_freq": coerce_limit(max_freq),
        "min_volt": coerce_limit(min_volt),
        "max_volt": coerce_limit(max_volt),
        "max_temp": coerce_limit(max_temp),
        "max_watts": coerce_limit(max_watts),
        "max_vr_temp": coerce_limit(max_vr_temp),
    }
    missing = [name for name, value in limits.items() if value is None]
    if missing:
        log_callback(
            f"{bitaxe_ip} -> Missing AutoTuner settings ({', '.join(missing)}). Skipping tuning.",
            "error",
        )
        _publish_status(bitaxe_ip, phase="skipped")
        return
    if limits["min_freq"] > limits["max_freq"] or limits["min_volt"] > limits["max_volt"]:
        log_callback(f"{bitaxe_ip} -> AutoTuner limits are reversed. Skipping tuning.", "error")
        _publish_status(bitaxe_ip, phase="skipped")
        return
    limits = clamp_limits(limits)

    if _wait(event, startup_delay):
        log_callback(f"{bitaxe_ip} -> Autotuning stopped.", "warning")
        return

    info = None
    while not event.is_set():
        info = get_system_info(bitaxe_ip)
        if event.is_set():
            break
        if isinstance(info, dict):
            break
        log_callback(
            info if isinstance(info, str) else f"{bitaxe_ip} -> Unexpected system info format: {info}",
            "error",
        )
        if _wait(event, interval):
            break
    if event.is_set() or not isinstance(info, dict):
        log_callback(f"{bitaxe_ip} -> Autotuning stopped.", "warning")
        return

    if not is_gamma_601(info):
        log_callback(
            f"{bitaxe_ip} -> Skipping tuning. This tuner only runs on a Bitaxe Gamma 601 (BM1370).",
            "error",
        )
        _publish_status(bitaxe_ip, phase="skipped")
        return
    if not _reports_error_percentage(info):
        log_callback(
            f"{bitaxe_ip} -> Skipping tuning. Firmware did not report errorPercentage.",
            "error",
        )
        _publish_status(bitaxe_ip, phase="skipped")
        return

    record = _miner_record(bitaxe_ip)
    min_input_voltage = _record_float(record, "min_input_voltage", DEFAULT_MIN_INPUT_VOLTAGE)
    max_error_percentage = _record_float(record, "max_error_percentage", DEFAULT_MAX_ERROR_PERCENTAGE)
    max_droop_mv = _record_float(record, "max_droop_mv", DEFAULT_MAX_DROOP_MV)

    start_frequency = coerce_limit(start_freq)
    start_voltage = coerce_limit(start_volt)
    if start_frequency is None:
        start_frequency = limits["min_freq"]
    if start_voltage is None:
        start_voltage = limits["min_volt"]
    saved_frequency = coerce_limit(record.get("last_good_freq"))
    saved_voltage = coerce_limit(record.get("last_good_volt"))
    if saved_frequency is not None and saved_voltage is not None:
        start_frequency = saved_frequency
        start_voltage = saved_voltage
    start_frequency = _clamp(start_frequency, limits["min_freq"], limits["max_freq"])
    start_voltage = _clamp(start_voltage, limits["min_volt"], limits["max_volt"])
    if saved_frequency is not None and saved_voltage is not None:
        log_callback(
            f"{bitaxe_ip} -> Resuming at {start_frequency} MHz / {start_voltage} mV.",
            "info",
        )

    _apply_fan(bitaxe_ip, {"autofanspeed": 0, "fanspeed": 100}, log_callback)

    runtime = load_config()
    refresh_interval = runtime.get("refresh_interval", 180)
    applied_settings = set_system_settings(bitaxe_ip, start_voltage, start_frequency)
    log_callback(applied_settings, "info")

    confirmed = None
    pending = (start_frequency, start_voltage) if settings_were_applied(applied_settings) else None
    if pending is None:
        log_callback(f"{bitaxe_ip} -> Initial settings were not applied. Waiting for the miner to report its setpoint.", "warning")
    now = time.time()
    settle_until = now + refresh_interval if pending is not None else now
    last_tune_time = now if pending is not None else 0

    hashrate_history = []
    rolling_hashrate = []
    last_shares = None
    last_config_refresh = 0
    phase = "climb"
    trim_good_voltage = None
    hold_since = None
    ceiling_saved = False
    limit_wall = record.get("wall_type") or ""
    saved_signature = None
    _publish_status(bitaxe_ip, phase=phase, wall_type=limit_wall)

    while not event.is_set():
        try:
            now = time.time()
            if now - last_config_refresh > 5:
                runtime = load_config()
                record = _miner_record(bitaxe_ip)
                min_input_voltage = _record_float(record, "min_input_voltage", DEFAULT_MIN_INPUT_VOLTAGE)
                max_error_percentage = _record_float(record, "max_error_percentage", DEFAULT_MAX_ERROR_PERCENTAGE)
                max_droop_mv = _record_float(record, "max_droop_mv", DEFAULT_MAX_DROOP_MV)
                last_config_refresh = now

            voltage_step = _positive_int(runtime.get("voltage_step"), 10)
            frequency_step = _positive_int(runtime.get("frequency_step"), 5)
            temp_tolerance = _non_negative_float(runtime.get("temp_tolerance"), 3)
            vr_temp_tolerance = _non_negative_float(runtime.get("vr_temp_tolerance"), 3)
            interval = _non_negative_float(runtime.get("monitor_interval", interval), 5)
            refresh_interval = _non_negative_float(runtime.get("refresh_interval"), 180)
            soak_seconds = _non_negative_float(runtime.get("ceiling_soak_seconds"), DEFAULT_CEILING_SOAK_SECONDS)
            flatline_repeat_count = runtime.get("flatline_hashrate_repeat_count", 5)
            flatline_enabled = runtime.get("flatline_detection_enabled", True)

            info = get_system_info(bitaxe_ip)
            if event.is_set():
                break
            if isinstance(info, str) or not isinstance(info, dict):
                log_callback(info if isinstance(info, str) else f"{bitaxe_ip} -> Unexpected system info format: {info}", "error")
                if _wait(event, interval):
                    break
                continue

            reported_frequency = info.get("frequency")
            reported_voltage = info.get("coreVoltage")
            now = time.time()

            if pending is not None and _numbers_match(reported_frequency, pending[0]) and _numbers_match(reported_voltage, pending[1]):
                confirmed = pending
                pending = None
                log_callback(
                    f"{bitaxe_ip} -> Confirmed {confirmed[0]} MHz / {confirmed[1]} mV.",
                    "success",
                )
            elif pending is not None and now >= settle_until:
                log_callback(
                    f"{bitaxe_ip} -> Settings were not confirmed by the miner. Keeping the reported setpoint.",
                    "warning",
                )
                if _as_float(reported_frequency) is not None and _as_float(reported_voltage) is not None:
                    confirmed = (int(float(reported_frequency)), int(float(reported_voltage)))
                pending = None
            elif (
                confirmed is None
                and pending is None
                and _as_float(reported_frequency) is not None
                and _as_float(reported_voltage) is not None
            ):
                confirmed = (int(float(reported_frequency)), int(float(reported_voltage)))
                log_callback(
                    f"{bitaxe_ip} -> Using reported setpoint {confirmed[0]} MHz / {confirmed[1]} mV.",
                    "info",
                )

            temp = info.get("temp") if "temp" in info else None
            vr_temp = info.get("vrTemp") if "vrTemp" in info else None
            live_hash = _as_float(info.get("hashRate"))
            minute_hash = _as_float(info.get("hashRate_1m"))
            # Tune on the 1-minute rate when the miner reports one. Flatline still
            # watches the live rate, which moves unless the board is actually stuck.
            decision_hash = minute_hash if minute_hash is not None and minute_hash > 0 else live_hash
            power = info.get("power") if "power" in info else None
            settling = pending is not None or now < settle_until
            error_percentage = _as_float(info.get("errorPercentage")) if _reports_error_percentage(info) else None

            if not settling and live_hash is not None and live_hash > 0:
                sample = round(live_hash, 2)
                hashrate_history.append(sample)
                if len(hashrate_history) > flatline_repeat_count:
                    hashrate_history.pop(0)

            if not settling and decision_hash is not None and decision_hash > 0:
                rolling_hashrate.append(decision_hash)
                if len(rolling_hashrate) > 3:
                    rolling_hashrate.pop(0)

            if (
                not settling
                and flatline_enabled
                and flatline_repeat_count > 0
                and len(hashrate_history) == flatline_repeat_count
                and len(set(hashrate_history)) == 1
            ):
                log_callback(
                    f"{bitaxe_ip} -> Flatline detected ({hashrate_history[-1]} GH/s). Restarting...",
                    "error",
                )
                log_callback(restart_bitaxe(bitaxe_ip), "warning")
                hashrate_history.clear()
                rolling_hashrate.clear()
                last_shares = None
                settle_until = time.time() + refresh_interval
                last_tune_time = time.time()
                if _wait(event, interval):
                    break
                continue

            # The table shows each sample. Log phase changes, limit hits, restarts, and errors.
            expected_hashrate = expected_hashrate_from_info(info, confirmed[0] if confirmed else start_frequency)
            _publish_status(
                bitaxe_ip,
                phase=phase,
                error_percentage=error_percentage,
                wall_type=limit_wall,
                last_good_freq=confirmed[0] if confirmed else "",
                last_good_volt=confirmed[1] if confirmed else "",
            )

            clocks_confirmed = pending is None and confirmed is not None
            immediate_retreat = clocks_confirmed and _needs_immediate_retreat(
                _as_float(temp),
                _as_float(vr_temp),
                _as_float(power),
                limits["max_temp"],
                limits["max_vr_temp"],
                limits["max_watts"],
                normalize_input_voltage(info.get("voltage")),
                min_input_voltage,
                _as_float(info.get("coreVoltageActual")),
                confirmed[1],
                max_droop_mv,
                info.get("power_fault"),
                info.get("overheat_mode"),
            )
            if not clocks_confirmed or (settling and not immediate_retreat):
                if _wait(event, interval):
                    break
                continue

            if now - last_tune_time < refresh_interval and not immediate_retreat:
                if _wait(event, interval):
                    break
                continue

            shares = info.get("sharesRejected")
            shares_value = _as_float(shares)
            shares_delta = 0
            if last_shares is not None and shares_value is not None:
                shares_delta = shares_value - last_shares
            if shares_value is not None:
                last_shares = shares_value

            average_hashrate = (sum(rolling_hashrate) / len(rolling_hashrate)) if rolling_hashrate else None
            error_budget = max_error_percentage
            error_ok = error_percentage is not None and error_percentage <= error_budget
            if phase == "trim" and error_ok:
                trim_good_voltage = confirmed[1]

            def choose(frequency, voltage):
                return decide_adjustment(
                    frequency,
                    voltage,
                    limits["min_freq"],
                    limits["max_freq"],
                    limits["min_volt"],
                    limits["max_volt"],
                    limits["max_temp"],
                    limits["max_watts"],
                    limits["max_vr_temp"],
                    _as_float(temp),
                    _as_float(vr_temp),
                    _as_float(power),
                    average_hashrate,
                    expected_hashrate,
                    shares_delta,
                    info.get("overheat_mode"),
                    frequency_step,
                    voltage_step,
                    temp_tolerance,
                    error_percentage=error_percentage,
                    input_voltage=normalize_input_voltage(info.get("voltage")),
                    min_input_voltage=min_input_voltage,
                    core_voltage_actual=_as_float(info.get("coreVoltageActual")),
                    max_droop_mv=max_droop_mv,
                    power_fault=info.get("power_fault"),
                    phase=phase,
                    trim_good_voltage=trim_good_voltage,
                    max_error_percentage=max_error_percentage,
                    vr_temp_tolerance=vr_temp_tolerance,
                )

            new_frequency, new_voltage, reason = choose(confirmed[0], confirmed[1])
            reported_frequency_number = _as_float(reported_frequency)
            reported_voltage_number = _as_float(reported_voltage)
            if (
                reported_frequency_number is not None
                and reported_voltage_number is not None
                and _proposal_jumps_above_report(
                    new_frequency,
                    new_voltage,
                    int(reported_frequency_number),
                    int(reported_voltage_number),
                    frequency_step,
                    voltage_step,
                )
            ):
                confirmed = (int(reported_frequency_number), int(reported_voltage_number))
                log_callback(
                    f"{bitaxe_ip} -> Reported {confirmed[0]} MHz / {confirmed[1]} mV. "
                    "Following the miner instead of jumping.",
                    "warning",
                )
                new_frequency, new_voltage, reason = choose(confirmed[0], confirmed[1])
                if new_frequency > confirmed[0] + frequency_step:
                    new_frequency = confirmed[0] + frequency_step
                if new_voltage > confirmed[1] + voltage_step:
                    new_voltage = confirmed[1] + voltage_step

            if reason == "frequency ceiling" and phase == "climb":
                phase = "trim"
                trim_good_voltage = confirmed[1]
                log_callback(f"{bitaxe_ip} -> Frequency ceiling. Trimming voltage.", "info")
                _publish_status(bitaxe_ip, phase=phase)
                if _wait(event, interval):
                    break
                continue

            leaving_hold = phase in ("hold", "trim") and (
                reason.startswith("step frequency") or reason.startswith("step voltage") or "silicon wall" in reason
            )
            if reason in ("trim complete", "restore voltage"):
                if phase != "hold":
                    phase = "hold"
                    hold_since = time.time()
                    ceiling_saved = False
                    log_callback(f"{bitaxe_ip} -> Holding {confirmed[0]} MHz / {new_voltage} mV.", "success")
            elif leaving_hold:
                phase = "climb"
                hold_since = None
                ceiling_saved = False
                trim_good_voltage = None

            wall = wall_type_from_reason(reason)
            if wall:
                limit_wall = wall
            log_callback(f"{bitaxe_ip} -> {reason}.", "info")
            _publish_status(bitaxe_ip, phase=phase, wall_type=limit_wall, error_percentage=error_percentage)

            if not _same_setpoint((new_frequency, new_voltage), confirmed):
                applied_settings = set_system_settings(bitaxe_ip, new_voltage, new_frequency)
                log_callback(applied_settings, "info")
                last_tune_time = time.time()
                if settings_were_applied(applied_settings):
                    if error_ok and reason in ("increase frequency", "increase voltage", "trim voltage"):
                        signature = (confirmed[0], confirmed[1], limit_wall)
                        if signature != saved_signature:
                            remember_setpoint(bitaxe_ip, confirmed[0], confirmed[1], limit_wall)
                            saved_signature = signature
                    pending = (new_frequency, new_voltage)
                    settle_until = time.time() + refresh_interval
                    hashrate_history.clear()
                    rolling_hashrate.clear()
                    if leaving_hold:
                        remember_setpoint(bitaxe_ip, new_frequency, new_voltage, limit_wall)
                        saved_signature = (new_frequency, new_voltage, limit_wall)
                else:
                    log_callback(f"{bitaxe_ip} -> Miner rejected the change. Setpoint left unchanged.", "warning")
            elif phase == "hold" and hold_since is not None and not ceiling_saved and reason in (
                "holding",
                "holding inside temperature band",
                "holding after a pool reject",
            ):
                if time.time() - hold_since >= soak_seconds:
                    remember_setpoint(bitaxe_ip, confirmed[0], confirmed[1], limit_wall)
                    saved_signature = (confirmed[0], confirmed[1], limit_wall)
                    ceiling_saved = True
                    _publish_status(
                        bitaxe_ip,
                        phase=phase,
                        wall_type=limit_wall,
                        last_good_freq=confirmed[0],
                        last_good_volt=confirmed[1],
                    )
                    log_callback(
                        f"{bitaxe_ip} -> Saved {confirmed[0]} MHz / {confirmed[1]} mV"
                        f"{(' after ' + limit_wall) if limit_wall else ''}.",
                        "success",
                    )

            if _wait(event, interval):
                break

        except Exception as e:
            log_callback(f"{bitaxe_ip} -> UNCAUGHT ERROR: {str(e)}", "error")
            if _wait(event, interval or 5):
                break

    log_callback(f"{bitaxe_ip} -> Autotuning stopped.", "warning")
