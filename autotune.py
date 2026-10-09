import math
import threading
import time

import requests

from boards import (
    GAMMA_601,
    TPS546_DEFAULT,
    asic_temp,
    board_for_info,
    board_for_record,
    board_list_note,
    firmware_check,
)
from config import (
    DEFAULT_MAX_DROOP_MV,
    DEFAULT_MAX_ERROR_PERCENTAGE,
    FIRMWARE_ASIC_TRIP_C,
    FIRMWARE_TRIP_MARGIN_C,
    FIRMWARE_VR_TRIP_C,
    SYSTEM_INFO_TIMEOUT,
    load_config,
    update_miner,
)
from modes import (
    EFFICIENCY,
    HASHRATE,
    MAX_HASHRATE,
    MODES,
    fan_payload,
    mode_for_record,
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


def _reset_one_miner_to_baseline(miner, log_callback):
    """Write factory clocks for one miner and set its start clocks to them.

    Returns False when the miner did not accept the write. An empty address
    is not a failure.
    """
    ip = (miner or {}).get("ip")
    if not ip:
        return True
    stock_freq, stock_volt = board_for_record(miner).stock_clocks
    message = set_system_settings(ip, stock_volt, stock_freq)
    applied = settings_were_applied(message)
    log_callback(message, "success" if applied else "error")
    update_miner(ip, {"start_freq": stock_freq, "start_volt": stock_volt})
    _clear_miner_status(ip)
    return applied


def reset_miners_to_baseline(
    miners, log_callback, stagger_seconds=STARTUP_STAGGER_SECONDS, parallel=False
):
    """Write factory clocks and forget the learned setpoint for each saved miner.

    A failed write is logged and does not skip clearing that miner or the ones after it.
    Min, max, temperature, and power limits are left as the user set them.
    parallel=True starts every write together and ignores the stagger.
    Returns how many miners rejected the write.
    """
    miners = list(miners or [])
    failed = 0
    failed_lock = threading.Lock()

    def run(miner):
        nonlocal failed
        if _reset_one_miner_to_baseline(miner, log_callback):
            return
        with failed_lock:
            failed += 1

    if parallel:
        threads = []
        for miner in miners:
            thread = threading.Thread(target=run, args=(miner,), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        return failed
    for index, miner in enumerate(miners):
        if index > 0 and stagger_seconds:
            time.sleep(stagger_seconds)
        run(miner)
    return failed


def restart_was_accepted(message):
    """True when restart_bitaxe did not return an error string."""
    return isinstance(message, str) and "Error restarting system" not in message


def restart_miners(miners, log_callback, stagger_seconds=STARTUP_STAGGER_SECONDS):
    """POST a restart for each saved miner.

    A failed restart is logged and does not skip the miners after it.
    Returns how many restarts were rejected.
    """
    started = 0
    failed = 0
    for miner in list(miners or []):
        ip = str((miner or {}).get("ip") or "").strip()
        if not ip:
            continue
        if started and stagger_seconds:
            time.sleep(stagger_seconds)
        started += 1
        log_callback(f"Restarting miner at {ip}...", "warning")
        message = restart_bitaxe(ip)
        log_callback(message, "warning")
        if not restart_was_accepted(message):
            failed += 1
    return failed


def _wait(stop_event, seconds):
    """Wait up to `seconds`. Return True if tuning was asked to stop."""
    if seconds is None or seconds <= 0:
        return stop_event.is_set()
    return stop_event.wait(timeout=seconds)


def coerce_limit(value):
    """Return an int limit, or None when the value is missing or not numeric."""
    number = coerce_real_limit(value)
    return None if number is None else int(number)


def coerce_real_limit(value):
    """Return a float limit, or None when the value is missing or not numeric.

    ASIC temperature, power, and regulator temperature keep a decimal. Frequency
    and voltage stay on coerce_limit.
    """
    if value is None or value == "":
        return None
    return _as_float(value)


def _as_float(value):
    """A finite float, or None. NaN and infinity count as missing."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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


# Highest ASIC and regulator caps the tuner will use. A cap at the firmware
# trip would let AxeOS cut power and drop the clocks 100 MHz / 100 mV first.
TRIP_SAFE_MAX_TEMP = FIRMWARE_ASIC_TRIP_C - FIRMWARE_TRIP_MARGIN_C
TRIP_SAFE_MAX_VR_TEMP = FIRMWARE_VR_TRIP_C - FIRMWARE_TRIP_MARGIN_C


def clamp_limits(limits, board=GAMMA_601):
    """Pull user limits inside the board's hard range. Max stays at or above min.

    Temperature caps are pulled under the AxeOS overheat trip.
    """
    hard = board.limits
    clamped = dict(limits)
    clamped["min_freq"] = _clamp(int(clamped["min_freq"]), hard.min_freq, hard.max_freq)
    clamped["max_freq"] = _clamp(
        int(clamped["max_freq"]), clamped["min_freq"], hard.max_freq
    )
    clamped["min_volt"] = _clamp(int(clamped["min_volt"]), hard.min_volt, hard.max_volt)
    clamped["max_volt"] = _clamp(
        int(clamped["max_volt"]), clamped["min_volt"], hard.max_volt
    )
    if clamped.get("max_temp") is not None:
        clamped["max_temp"] = min(clamped["max_temp"], TRIP_SAFE_MAX_TEMP)
    if clamped.get("max_vr_temp") is not None:
        clamped["max_vr_temp"] = min(clamped["max_vr_temp"], TRIP_SAFE_MAX_VR_TEMP)
    return clamped


_RUNNING_LIMIT_FIELDS = (
    "min_freq",
    "max_freq",
    "min_volt",
    "max_volt",
    "max_temp",
    "max_watts",
    "max_vr_temp",
)
_REAL_LIMIT_FIELDS = ("max_temp", "max_watts", "max_vr_temp")


def refresh_running_limits(limits, record, board=GAMMA_601):
    """Apply caps saved while a session is already running.

    A missing field keeps the value already in use. A reversed frequency or
    voltage pair is ignored so a bad edit cannot invert a live session.
    """
    if not isinstance(record, dict):
        return limits
    updated = dict(limits)
    changed = False
    for key in _RUNNING_LIMIT_FIELDS:
        if key in _REAL_LIMIT_FIELDS:
            value = coerce_real_limit(record.get(key))
        else:
            value = coerce_limit(record.get(key))
        if value is None or value == updated.get(key):
            continue
        updated[key] = value
        changed = True
    if not changed:
        return limits
    if (
        updated["min_freq"] > updated["max_freq"]
        or updated["min_volt"] > updated["max_volt"]
    ):
        return limits
    return clamp_limits(updated, board)


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


def core_current_amps(info, board=GAMMA_601):
    """Regulator output current in amps, or None. AxeOS reports milliamps.

    None on a board whose `current` is the input current (INA260 boards).
    """
    if not isinstance(info, dict) or not board.has_core_current:
        return None
    number = _as_float(info.get("current"))
    if number is None or number <= 0:
        return None
    return number / 1000.0 if number > 200 else number


def core_amps_cap(record, board=GAMMA_601):
    """This miner's current cap, never over the board's hard cap.

    None when the board reports no core current.
    """
    hard = board.limits.max_core_amps
    if hard is None:
        return None
    cap = _record_float(record, "max_core_amps", hard)
    if cap is None or cap <= 0:
        cap = hard
    return min(float(cap), hard)


def _over_current(core_current, max_core_amps):
    current = _as_float(core_current)
    cap = _as_float(max_core_amps)
    return current is not None and cap is not None and current > cap


def _miner_record(ip):
    for miner in load_config().get("miners", []):
        if miner.get("ip") == ip:
            return miner
    return {}


def wall_type_from_reason(reason):
    text = (reason or "").lower()
    if "good hashrate" in text or "low hashrate" in text:
        return "hash"
    if "rejected shares" in text:
        return "reject"
    if "input sag" in text:
        return "input"
    if "silicon" in text or "above target" in text:
        return "silicon"
    if "current limit" in text:
        return "current"
    if "droop" in text:
        return "droop"
    if "power limit" in text or "power fault" in text:
        return "power"
    if "frequency cap" in text:
        return ""
    if (
        text.startswith("step frequency")
        or text.startswith("step voltage")
        or "temperature" in text
    ):
        return "thermal"
    return ""


def _thermal_frequency_steps(overshoot, tolerance):
    """How many frequency steps to shed for this many degrees over a cap.

    One step per tolerance band, with no cap, so a large overshoot sheds more
    than three steps. A zero tolerance still sheds one step.
    """
    if overshoot <= 0:
        return 1
    band = tolerance if tolerance and tolerance > 0 else 0
    if band <= 0:
        return 1
    # Readings carry decimals, so 3.5°C over a 3°C band is two bands.
    return max(1, math.ceil(overshoot / band))


# A climb far under every cap takes up to this many frequency steps at once.
MAX_CLIMB_FREQUENCY_STEPS = 4
# Efficiency: a voltage trim that cost energy per hash is tried once more this
# many frequency steps lower. Lower voltage lowers the clock a chip can run, so
# the two have to come down together to find a more efficient point.
EFFICIENCY_TRIM_FREQUENCY_STEPS = 2


def climb_frequency_steps(
    frequency,
    temp,
    vr_temp,
    power,
    max_temp,
    max_vr_temp,
    max_watts,
    temp_tolerance,
    vr_tolerance,
    frequency_step,
    max_steps=MAX_CLIMB_FREQUENCY_STEPS,
    core_current=None,
    max_core_amps=None,
    vr_temp_required=True,
):
    """How many frequency steps one climb may take on this much headroom.

    At a fixed voltage, power grows at most in proportion to frequency, so one
    step adds at most `power * step / frequency` W, and at most
    `temp * step / frequency` °C over an ambient above 0°C. The regulator's
    conduction loss grows with the square of current, so its rise counts twice.
    The jump still has to leave both sensors a tolerance band under their caps
    and keep power under its cap. A missing reading, or a sensor within two
    bands of its cap, takes one step. A band is at least 1°C. A board with
    no regulator sensor (`vr_temp_required` false) is judged on the ASIC.
    """
    max_steps = max(int(max_steps or 1), 1)
    clock = _as_float(frequency)
    watts = _as_float(power)
    if max_steps == 1 or clock is None or clock <= 0 or watts is None or watts <= 0:
        return 1
    share = max(int(frequency_step), 1) / clock
    allowed = max_steps
    for value, cap, tolerance, weight in (
        (_usable_temp(temp), max_temp, temp_tolerance, 1),
        (_usable_temp(vr_temp), max_vr_temp, vr_tolerance, 2),
    ):
        if value is None and cap is max_vr_temp and not vr_temp_required:
            continue
        if value is None:
            return 1
        band = max(float(tolerance or 0), 1.0)
        room = cap - band - value
        if room < band:
            return 1
        allowed = min(allowed, int(room // (weight * value * share)))
    allowed = min(allowed, int((max_watts - watts) // (watts * share)))
    # At a fixed voltage, core current grows in proportion to frequency.
    current = _as_float(core_current)
    cap = _as_float(max_core_amps)
    if current is not None and current > 0 and cap is not None:
        allowed = min(allowed, int((cap - current) // (current * share)))
    return _clamp(allowed, 1, max_steps)


def _step_down(
    frequency,
    voltage,
    min_freq,
    min_volt,
    frequency_step,
    voltage_step,
    frequency_steps=1,
    voltage_steps=0,
):
    """Drop frequency first. Drop voltage only after frequency is already at its floor.

    `voltage_steps` also sheds that many voltage steps with a frequency drop.
    That voltage never goes under `min_volt`, and a voltage already under it stays.
    """
    drop = frequency_step * max(int(frequency_steps), 1)
    shed = voltage_step * max(int(voltage_steps), 0)
    lowered = max(voltage - shed, min(voltage, min_volt)) if shed else voltage
    if frequency - drop >= min_freq:
        return frequency - drop, lowered, "step frequency down"
    if frequency > min_freq:
        return min_freq, lowered, "step frequency down"
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


# AxeOS v2.15.1 reports Gamma power as regulator output plus a fixed 5 W for
# the rest of the board, so a board with the ASIC powered off still reads 5 W.
# At or under this the ASIC is off. The lowest running clocks read well above it.
# Other boards use their own `limits.asic_off_watts`.
ASIC_OFF_POWER_WATTS = GAMMA_601.limits.asic_off_watts


def _asic_off_power(power, asic_off_watts):
    """True when a power reading says the ASIC is off. False when it cannot tell."""
    return power is not None and asic_off_watts is not None and power <= asic_off_watts


def overheat_ready_to_clear(
    info,
    max_temp,
    max_vr_temp,
    asic_off_watts=ASIC_OFF_POWER_WATTS,
    vr_temp_required=True,
):
    """True when a sticky overheat flag can be cleared.

    Both sensors have a positive reading at or under their caps, and the
    board is drawing more than the idle floor. A powered-down Gamma 601
    reports no ASIC temperature, so that latch stays. A board with no
    regulator sensor is judged on the ASIC alone.
    """
    if not isinstance(info, dict) or not _overheat_mode_set(info.get("overheat_mode")):
        return False
    temp = _usable_temp(asic_temp(info))
    power = _as_float(info.get("power"))
    temp_cap = _as_float(max_temp)
    if (
        temp is None
        or power is None
        or temp_cap is None
        or _asic_off_power(power, asic_off_watts)
        or temp > temp_cap
    ):
        return False
    if not vr_temp_required:
        return True
    vr_temp = _usable_temp(info.get("vrTemp"))
    vr_cap = _as_float(max_vr_temp)
    return vr_temp is not None and vr_cap is not None and vr_temp <= vr_cap


def _power_fault_set(value):
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "none", "null")
    return bool(value)


def _usable_temp(value):
    """Temperature in °C, or None when the sensor is missing or not positive."""
    number = _as_float(value)
    if number is None or number <= 0:
        return None
    return number


def _usable_core_voltage(value):
    """Measured core voltage in mV, or None when it is missing or not positive."""
    number = _as_float(value)
    if number is None or number <= 0:
        return None
    return number


# Inside the trip margin the retreat sheds this many frequency steps and one
# voltage step, and may repeat after this many seconds instead of a full settle.
TRIP_GUARD_FREQUENCY_STEPS = 4
TRIP_GUARD_SETTLE_SECONDS = 30
# AxeOS saves clocks 100 MHz and 100 mV lower after an overheat trip. A drop
# this large that the session did not write is that trip.
FIRMWARE_DROP_FREQ_MHZ = 50
FIRMWARE_DROP_VOLT_MV = 50
# overheat_mode still set this long, with the regulator cool, means AxeOS
# could not bring the chip back after its cool-down.
OVERHEAT_LATCH_SECONDS = 120
# AxeOS leaves its cool-down once the regulator is at or under 95°C. A few
# degrees under that, the cool-down is over for certain.
OVERHEAT_LATCH_VR_C = FIRMWARE_VR_TRIP_C - 10 - FIRMWARE_TRIP_MARGIN_C
# Boards whose ASIC sensor reads while the ASIC is off cool under this before
# AxeOS restarts them (SAFE_TEMP in power_management_task.c).
OVERHEAT_SAFE_ASIC_C = 45.0
# The ASIC reading as powered off this long without overheat mode or a user
# pause earns one restart. A failed start or a regulator that shut itself off
# otherwise holds forever.
ASIC_OFF_RESTART_SECONDS = 5 * 60


# A hold that stopped under a hashrate or silicon wall climbs again once the
# chip is this much cooler than when it hit that wall, or after this long.
# Errors grow with heat, so a wall found on a hot afternoon may not be there at night.
RECLIMB_COOLER_C = 3.0
RECLIMB_AFTER_SECONDS = 6 * 60 * 60
# Settled errors this far over budget, with no trial open, are not a silicon
# wall. AxeOS can leave the ASIC like that after an overheat recovery. One
# restart per episode re-initialises it.
HIGH_ERROR_RESTART_PERCENT = 10.0


def hold_may_reclimb(
    temp,
    wall_temp,
    wall_since,
    now,
    max_temp,
    temp_tolerance,
    error_percentage,
    max_error_percentage,
):
    """True when a hold under a wall should try climbing again.

    Errors have to be at most half the budget and the chip a full tolerance
    band under its cap. Then the chip has to be RECLIMB_COOLER_C cooler than
    at the wall, or the wall has to be RECLIMB_AFTER_SECONDS old.
    """
    temp_value = _usable_temp(temp)
    error = _as_float(error_percentage)
    budget = _as_float(max_error_percentage)
    if temp_value is None or error is None or budget is None or wall_since is None:
        return False
    if error > budget / 2:
        return False
    band = temp_tolerance if temp_tolerance and temp_tolerance > 0 else 0
    if temp_value > max_temp - band:
        return False
    wall_value = _usable_temp(wall_temp)
    if wall_value is not None and temp_value <= wall_value - RECLIMB_COOLER_C:
        return True
    return now - wall_since >= RECLIMB_AFTER_SECONDS


def floor_hold_should_climb(
    phase,
    frequency,
    voltage,
    min_freq,
    max_freq,
    max_volt,
    thermal_hold,
    safety_hold,
    cooled,
):
    """True when a hold or trim on the frequency floor should go back to climbing.

    The floor is where retreats land, not a wall a climb found. A hold answers
    errors by stepping frequency, which the floor cannot do, and a trim there
    swings voltage up and down without ever finishing. Either keeps a cool chip
    at its slowest clock for good. Climbing raises voltage for errors first.
    A chip still in a heat or safety hold, at its voltage cap, or with no room
    above the floor stays put.
    """
    if phase not in ("hold", "trim"):
        return False
    if thermal_hold or safety_hold or not cooled:
        return False
    return int(frequency) <= int(min_freq) < int(max_freq) and int(voltage) < int(
        max_volt
    )


def error_restart_limit(max_error_percentage):
    """Settled error percentage that earns the ASIC a restart."""
    budget = _as_float(max_error_percentage)
    if budget is None or budget < 0:
        budget = DEFAULT_MAX_ERROR_PERCENTAGE
    return max(HIGH_ERROR_RESTART_PERCENT, 5 * budget)


def near_firmware_trip(temp, vr_temp):
    """True when either sensor is inside the margin under the AxeOS overheat trip."""
    temp_value = _usable_temp(temp)
    if temp_value is not None and temp_value >= TRIP_SAFE_MAX_TEMP:
        return True
    vr_value = _usable_temp(vr_temp)
    return vr_value is not None and vr_value >= TRIP_SAFE_MAX_VR_TEMP


def firmware_lowered_clocks(expected, reported):
    """True when the miner reports clocks well under the ones this session set.

    Neither clock may be above `expected`, and one has to be down by at least
    the AxeOS drop threshold. Missing clocks are not a drop.
    """
    if expected is None or reported is None:
        return False
    frequency_drop = int(expected[0]) - int(reported[0])
    voltage_drop = int(expected[1]) - int(reported[1])
    if frequency_drop < 0 or voltage_drop < 0:
        return False
    return (
        frequency_drop >= FIRMWARE_DROP_FREQ_MHZ
        or voltage_drop >= FIRMWARE_DROP_VOLT_MV
    )


def floor_setpoint(frequency, voltage, min_freq, min_volt):
    """The clocks raised onto the configured floor. Clocks above it stay."""
    return max(int(frequency), int(min_freq)), max(int(voltage), int(min_volt))


def overheat_latched(info, overheat_since, now, max_vr_temp, vr_temp_required=True):
    """True when AxeOS left overheat mode on after its own recovery failed.

    The flag has been set for OVERHEAT_LATCH_SECONDS and the regulator reads at
    or under its cap and under OVERHEAT_LATCH_VR_C. AxeOS v2.15.1 cools for 30 s
    and resumes once the regulator is at or under 95°C, then clears the flag
    only if the ASIC starts. Still set after that means the restart failed.

    Power is not checked. The restart turns the regulator back on at the saved
    voltage before it tries the ASIC, so a failed start can read well above
    the 5 W the board shows with the regulator off.

    A board with no regulator sensor cools until its ASIC reads at or under
    OVERHEAT_SAFE_ASIC_C, when that sensor still reads with the ASIC off.
    """
    if not isinstance(info, dict) or not _overheat_mode_set(info.get("overheat_mode")):
        return False
    if overheat_since is None or now - overheat_since < OVERHEAT_LATCH_SECONDS:
        return False
    if not vr_temp_required:
        temp = _usable_temp(asic_temp(info))
        return temp is None or temp <= OVERHEAT_SAFE_ASIC_C
    vr_value = _usable_temp(info.get("vrTemp"))
    vr_cap = _as_float(max_vr_temp)
    if vr_value is None or vr_cap is None:
        return False
    return vr_value <= min(vr_cap, OVERHEAT_LATCH_VR_C)


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
    core_current=None,
    max_core_amps=None,
    asic_off_watts=ASIC_OFF_POWER_WATTS,
):
    """True when the clocks the miner is running are past a safety limit.

    The session steps down on the next poll for the first breach. It does not
    ask again until a drop already written has finished its settle, so a
    heatsink that has not moved yet cannot walk the clocks down. Error retreats
    still wait, so a noisy error sample cannot walk the clocks down. A missing ASIC
    temperature or power reading does not hide a breach on the sensors that did report.
    """
    temp_value = _usable_temp(temp)
    power_value = _as_float(power)
    vr_value = _usable_temp(vr_temp)
    if temp_value is not None and temp_value > max_temp:
        return True
    if vr_value is not None and vr_value > max_vr_temp:
        return True
    if near_firmware_trip(temp_value, vr_value):
        return True
    if power_value is not None and power_value > max_watts:
        return True
    if _over_current(core_current, max_core_amps):
        return True
    if _overheat_mode_set(overheat_mode) or _asic_off_power(
        power_value, asic_off_watts
    ):
        return False
    if (
        min_input_voltage is not None
        and input_voltage is not None
        and input_voltage < min_input_voltage
    ):
        return True
    if _power_fault_set(power_fault):
        return True
    actual_voltage = _usable_core_voltage(core_voltage_actual)
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
    """True when a write would move more than one step above the clocks the miner reports.

    `frequency_step` is the frequency move the write meant to make, so a
    multi-step climb from the reported clock is not a jump.
    """
    frequency_step = max(int(frequency_step), 1)
    voltage_step = max(int(voltage_step), 1)
    if (
        reported_frequency is not None
        and new_frequency > reported_frequency + frequency_step
    ):
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


def good_hashrate(hash_rate, error_percentage):
    """Measured hashes that were not invalid ASIC jobs.

    None when the rate or the error percentage is missing. AxeOS v2.15.1
    computes errorPercentage from the ASIC's own counters: error hashrate over
    total hashrate across about one second. It is not a share count.
    """
    rate = _as_float(hash_rate)
    error = _as_float(error_percentage)
    if rate is None or rate <= 0 or error is None:
        return None
    kept = 1 - (max(error, 0.0) / 100.0)
    if kept < 0:
        return 0
    return rate * kept


def measured_hashrate(info):
    """Prefer the 1-minute rate. Use the live rate only when the miner omits it."""
    if not isinstance(info, dict):
        return None
    if "hashRate_1m" in info and info.get("hashRate_1m") is not None:
        minute = _as_float(info.get("hashRate_1m"))
        if minute is not None and minute > 0:
            return minute
        return None
    live = _as_float(info.get("hashRate"))
    if live is not None and live > 0:
        return live
    return None


def _reported_rate(info, key):
    """(present, value). Present is false when the miner omitted the field."""
    if not isinstance(info, dict) or key not in info or info.get(key) is None:
        return False, None
    return True, _as_float(info.get(key))


def board_hashrate_is_dead(info):
    """True when the latest reported rates are not positive.

    A 1-minute rate of 0 with a positive live rate is not dead: the live
    rate is still hashing. A missing field is not a zero. The session uses
    this so an earlier positive sample cannot hide a board that has stopped.
    """
    minute_present, minute = _reported_rate(info, "hashRate_1m")
    live_present, live = _reported_rate(info, "hashRate")
    live_positive = live is not None and live > 0
    minute_positive = minute is not None and minute > 0
    if minute_present:
        if minute_positive or live_positive:
            return False
        return True
    if live_present:
        return not live_positive
    return False


def pool_is_down(info):
    """True when AxeOS reports a pool difficulty that is not positive.

    A missing difficulty is not a stall, so older samples keep the old rules.
    A zero difficulty means the pool is not handing out work. A dead or short
    hashrate then is not the chip. AxeOS v2.15.1 only reads 0 here from boot
    until the first difficulty, and keeps the last value after a pool drops.
    When every pool is unreachable it powers the ASIC off instead, and the
    ASIC_OFF_POWER_WATTS hold covers that.
    """
    if not isinstance(info, dict) or "poolDifficulty" not in info:
        return False
    difficulty = _as_float(info.get("poolDifficulty"))
    return difficulty is None or difficulty <= 0


def minute_rate_failed(info):
    """True when the 1-minute rate is present and not positive, but live hashrate is.

    That is a failed sample for an open probe, not a missing one and not a
    dead board.
    """
    minute_present, minute = _reported_rate(info, "hashRate_1m")
    if not minute_present or (minute is not None and minute > 0):
        return False
    _live_present, live = _reported_rate(info, "hashRate")
    return live is not None and live > 0


# BM1370 self-test treats delivered hashrate under 85% of expected as a miss.
# hashRate_1m is the last minute of samples. A climb compares it once this
# setpoint has been confirmed for that long. hashRate_10m replaces it after
# the clocks have been still for ten minutes.
HASHRATE_SHORTFALL_RATIO = 0.85
HASHRATE_1M_SETTLE_SECONDS = 60
HASHRATE_10M_SETTLE_SECONDS = 10 * 60
# hashRate_1m is already a one-minute average. It has to sit still for longer
# than that minute, with the live rate also still, before it counts as a hang.
FLATLINE_STILL_SECONDS = HASHRATE_1M_SETTLE_SECONDS

# Pool reject share that steps frequency down. Stale and non-silicon results are left out.
# One counted reject must not cross the limit, so a sample needs more than 100 judged shares.
DEFAULT_MAX_REJECT_SHARE = 0.01
MIN_JUDGED_SHARES = 100
ABOVE_TARGET_WINDOWS = 2
# AxeOS v2.15.1 stores the pool's own text. Match those phrases, not the
# substring "invalid", which also appears on protocol and stale job errors.
_STALE_REJECT_MARKERS = (
    "job not found",
    "stale",
    "invalid jobid",
    "invalid job id",
    "invalid-job-id",
)
_ABOVE_TARGET_MARKERS = (
    "above target",
    "low difficulty",
    "difficulty too low",
    "difficulty-too-low",
)
_HARDWARE_REJECT_MARKERS = ("hardware",)
_IGNORED_REJECT_MARKERS = ("duplicate", "ntime", "worker", "unauthorized", "mismatch")

# BM1370 expected hashrate is frequency * 2040 / 1000 GH/s. A 5 MHz step is 10.2 GH/s.
# Other boards use their own `hashrate_per_mhz`.
BM1370_HASHRATE_PER_MHZ = GAMMA_601.hashrate_per_mhz


def average_error(samples):
    """Mean of the settle-window error samples. None when the window is empty."""
    values = []
    for sample in samples or []:
        number = _as_float(sample)
        if number is not None:
            values.append(number)
    if not values:
        return None
    return sum(values) / len(values)


def hashrate_well_below_expected(hash_rate, expected, ratio=HASHRATE_SHORTFALL_RATIO):
    """True when a settled hashrate is under the BM1370 self-test fraction of expected."""
    rate = _as_float(hash_rate)
    target = _as_float(expected)
    if rate is None or target is None or target <= 0:
        return False
    return rate < (target * ratio)


def rounded_pll_frequency(value):
    """Nearest whole MHz for a PLL reading. None when the miner did not report one."""
    number = _as_float(value)
    if number is None or number <= 0:
        return None
    return int(number + 0.5)


def reject_reason_class(message):
    """Classify a pool reject string.

    Stale and non-silicon results do not move the clocks. Above-target results
    are a silicon signal. Other hardware results can step frequency down.
    """
    text = str(message or "").lower()
    if any(marker in text for marker in _ABOVE_TARGET_MARKERS):
        return "above"
    if any(marker in text for marker in _STALE_REJECT_MARKERS):
        return "stale"
    if any(marker in text for marker in _IGNORED_REJECT_MARKERS):
        return "ignore"
    # A bare "Invalid" is a bad share. "Invalid JobID" and the other
    # "Invalid …" protocol strings are classified above.
    if text.strip() == "invalid" or any(
        marker in text for marker in _HARDWARE_REJECT_MARKERS
    ):
        return "hardware"
    return "ignore"


def _stale_reject_reason(message):
    return reject_reason_class(message) == "stale"


def _reject_reason_counts(reasons):
    counts = {}
    if not isinstance(reasons, list):
        return counts
    for item in reasons:
        if not isinstance(item, dict):
            continue
        count = _as_float(item.get("count"))
        if count is None:
            continue
        counts[str(item.get("message") or "")] = count
    return counts


def reject_class_deltas(previous, current):
    """New rejects in this window, split by reject_reason_class."""
    previous_counts = _reject_reason_counts(previous)
    totals = {"stale": 0.0, "above": 0.0, "hardware": 0.0, "ignore": 0.0}
    for message, count in _reject_reason_counts(current).items():
        delta = count - previous_counts.get(message, 0.0)
        if delta > 0:
            totals[reject_reason_class(message)] += delta
    return totals


def stale_reject_delta(previous, current):
    """How many new rejects are stale pool results, from the reason counters."""
    return reject_class_deltas(previous, current)["stale"]


class RejectSample:
    """Accumulate judged shares until the sample is large enough to act on.

    Above-target has to stay high for two full samples. One difficulty-change
    burst does not move the clocks.
    """

    def __init__(self):
        self.accepted = 0.0
        self.hardware = 0.0
        self.above = 0.0
        self.above_streak = 0

    def reset(self):
        self.accepted = 0.0
        self.hardware = 0.0
        self.above = 0.0
        self.above_streak = 0

    def add(self, accepted, hardware, above):
        for value, name in (
            (accepted, "accepted"),
            (hardware, "hardware"),
            (above, "above"),
        ):
            number = _as_float(value)
            if number is not None and number > 0:
                setattr(self, name, getattr(self, name) + number)

    def judge(self, minimum=MIN_JUDGED_SHARES, limit=DEFAULT_MAX_REJECT_SHARE):
        """Return (hardware share or None, above-target is sustained).

        None means the sample is still too small. The counters reset once it qualifies.
        Hardware share uses every judged share, including above-target results.
        """
        judged = self.accepted + self.hardware + self.above
        if judged < minimum:
            return None, False
        # Above-target shares count toward the sample size, so they stay in
        # this denominator. One hardware reject in a full sample is then 1%.
        hardware_share = (self.hardware / judged) if judged > 0 else 0.0
        above_total = self.accepted + self.above
        above_share = (self.above / above_total) if above_total > 0 else 0.0
        if above_share > limit:
            self.above_streak += 1
        else:
            self.above_streak = 0
        sustained = self.above_streak >= ABOVE_TARGET_WINDOWS
        self.accepted = 0.0
        self.hardware = 0.0
        self.above = 0.0
        return hardware_share, sustained


def expected_step_gain(frequency_step, hashrate_per_mhz=BM1370_HASHRATE_PER_MHZ):
    """GH/s the board should gain from this many MHz, before errors."""
    step = _as_float(frequency_step)
    if step is None or step <= 0:
        step = 1
    return step * hashrate_per_mhz


def good_hashrate_held(baseline_good, current_good, band):
    """True when good hashrate did not fall by more than `band` GH/s.

    None when either sample is missing, so the caller waits.
    """
    if baseline_good is None or current_good is None:
        return None
    allowed = _as_float(band)
    if allowed is None or allowed < 0:
        allowed = 0
    return current_good >= (baseline_good - allowed)


def _blocks_reclimb(reason):
    """True when this retreat should not be climbed back into immediately."""
    text = (reason or "").lower()
    return (
        "silicon wall" in text
        or "low hashrate" in text
        or "rejected shares" in text
        or "above target" in text
    )


def _quality_frequency_retreat(reason):
    """True when frequency stepped down for errors, low hashrate, or above-target."""
    text = (reason or "").lower()
    if "step frequency" not in text:
        return False
    return "silicon wall" in text or "low hashrate" in text or "above target" in text


def frequency_block_cleared(
    blocked_frequency,
    voltage,
    blocked_voltage,
    needs_cool,
    cooled,
    for_rejects,
    share,
    limit=DEFAULT_MAX_REJECT_SHARE,
):
    """True when a frequency retreat may be climbed again.

    Any block clears once voltage rises. `needs_cool` is only for a thermal
    retreat, and that block also clears once the chip has cooled. A reject
    block also clears after a later clean share sample. Silicon blocks stay
    until voltage rises. `share` is None until that sample is large enough to judge.
    """
    if blocked_frequency is None:
        return False
    if (
        blocked_voltage is not None
        and voltage is not None
        and voltage > blocked_voltage
    ):
        return True
    if needs_cool and cooled:
        return True
    judged = _as_float(share)
    reject_limit = DEFAULT_MAX_REJECT_SHARE if limit is None else float(limit)
    return bool(for_rejects) and judged is not None and judged <= reject_limit


def reject_share(accepted_delta, rejected_delta, stale_delta=0):
    """Share of new rejects that are not stale. None when the window had no judged shares."""
    accepted = _as_float(accepted_delta)
    rejected = _as_float(rejected_delta)
    if accepted is None or rejected is None or accepted < 0 or rejected < 0:
        return None
    stale = _as_float(stale_delta)
    if stale is None or stale < 0:
        stale = 0.0
    if stale > rejected:
        stale = rejected
    bad = rejected - stale
    total = accepted + bad
    if total <= 0:
        return None
    return bad / total


# Heat uses temp_tolerance as the gap under the cap. Power and droop have no
# tolerance field, so a retreat does not climb again until the reading is
# back inside the cap by this margin. Input releases at its floor.
POWER_HOLD_MARGIN_WATTS = 1
CURRENT_HOLD_MARGIN_AMPS = 1
DROOP_HOLD_MARGIN_MV = 5


def _at_or_under(value, cap, margin):
    """True when `value` is at or under `cap` minus a non-negative margin."""
    reading = _as_float(value)
    limit = _as_float(cap)
    if reading is None or limit is None:
        return False
    gap = _as_float(margin)
    if gap is None or gap < 0:
        gap = 0
    if gap > limit:
        gap = 0
    return reading <= (limit - gap)


def safety_hold_cleared(
    power,
    max_watts,
    input_voltage,
    min_input_voltage,
    core_voltage_actual,
    current_voltage,
    max_droop_mv,
    power_fault,
    core_current=None,
    max_core_amps=None,
):
    """True when a power, current, sag, droop, or fault hold may release.

    Power and droop have to be back inside their caps. Input has to be back
    at its floor. A power fault or a missing power reading keeps the hold.
    A missing input or core-voltage reading does not, once the readings that
    did arrive are inside their limits.
    """
    if _power_fault_set(power_fault):
        return False
    if not _at_or_under(power, max_watts, POWER_HOLD_MARGIN_WATTS):
        return False
    if _as_float(max_core_amps) is not None and _as_float(core_current) is not None:
        if not _at_or_under(core_current, max_core_amps, CURRENT_HOLD_MARGIN_AMPS):
            return False
    if (
        min_input_voltage is not None
        and input_voltage is not None
        and input_voltage < min_input_voltage
    ):
        return False
    actual = _usable_core_voltage(core_voltage_actual)
    droop_limit = _as_float(max_droop_mv)
    if actual is not None and droop_limit is not None and current_voltage is not None:
        if not _at_or_under(
            current_voltage - actual, droop_limit, DROOP_HOLD_MARGIN_MV
        ):
            return False
    return True


def _safety_hold_kind(reason):
    """'power', 'input' or 'current' when this retreat should block the next climb.

    Core voltage droop holds like power; only the dashboard tells them apart.
    """
    wall = wall_type_from_reason(reason)
    if wall == "droop":
        return "power"
    if wall in ("power", "input", "current"):
        return wall
    return ""


def cooled_after_retreat(
    temp, vr_temp, max_temp, max_vr_temp, temp_tolerance, vr_temp_tolerance
):
    """True when both reported sensors are at least one tolerance band under their caps.

    A missing regulator reading does not keep the latch. The climb still waits
    for that sensor on its own.
    """
    temp_value = _usable_temp(temp)
    if temp_value is None:
        return False
    asic_band = temp_tolerance if temp_tolerance and temp_tolerance > 0 else 0
    if temp_value > (max_temp - asic_band):
        return False
    vr_value = _usable_temp(vr_temp)
    if vr_value is None:
        return True
    vr_band = vr_temp_tolerance if vr_temp_tolerance and vr_temp_tolerance > 0 else 0
    return vr_value <= (max_vr_temp - vr_band)


def frequency_step_paid(
    baseline_good, current_good, baseline_actual=None, current_actual=None, band=None
):
    """True when a settled frequency step did not give up more good hashrate than it is worth.

    None when either good-hashrate sample is missing, so the caller waits.
    A dip inside `band` GH/s is noise. The default band is one 5 MHz BM1370 step.
    When both PLL clocks are known, a clock that did not rise did not pay,
    even if the 1-minute rate twitched upward.
    """
    if baseline_good is None or current_good is None:
        return None
    baseline_clock = _as_float(baseline_actual)
    current_clock = _as_float(current_actual)
    if (
        baseline_clock is not None
        and current_clock is not None
        and current_clock <= baseline_clock
    ):
        return False
    if band is None:
        band = expected_step_gain(5)
    return good_hashrate_held(baseline_good, current_good, band)


def energy_per_hash(power, good):
    """Watts per good GH/s (J/TH divided by 1000), or None when a reading is missing."""
    watts = _as_float(power)
    rate = _as_float(good)
    if watts is None or rate is None or watts <= 0 or rate <= 0:
        return None
    return watts / rate


def efficiency_step_paid(
    baseline_power,
    baseline_good,
    current_power,
    current_good,
    ratio,
    baseline_actual=None,
    current_actual=None,
):
    """True when a settled step did not cost more energy per good hash than noise.

    The Efficiency objective keeps a step on this instead of on good hashrate.
    `ratio` is the noise allowance as a share of the baseline: one step's
    expected hashrate over the baseline hashrate, the same slack the hashrate
    test gives. None when a reading is missing, so the caller waits. As with
    frequency_step_paid, a clock whose PLL did not rise did not pay.
    """
    baseline = energy_per_hash(baseline_power, baseline_good)
    current = energy_per_hash(current_power, current_good)
    if baseline is None or current is None:
        return None
    baseline_clock = _as_float(baseline_actual)
    current_clock = _as_float(current_actual)
    if (
        baseline_clock is not None
        and current_clock is not None
        and current_clock <= baseline_clock
    ):
        return False
    allowance = _as_float(ratio)
    if allowance is None or allowance < 0:
        allowance = 0
    return current <= baseline * (1 + allowance)


def _guard_bounds(
    current_frequency,
    current_voltage,
    new_frequency,
    new_voltage,
    min_freq,
    max_freq,
    min_volt,
    max_volt,
    reason,
):
    requested = int(new_frequency)
    new_frequency = _clamp(requested, min_freq, max_freq)
    # A min above the live clock must not turn a retreat into a higher frequency.
    # A climb proposal is already above the live clock and is left alone.
    if new_frequency > current_frequency and requested <= current_frequency:
        new_frequency = current_frequency
    requested_voltage = int(new_voltage)
    new_voltage = _clamp(requested_voltage, min_volt, max_volt)
    # The same holds for voltage. Only a raise proposal may lift it to the floor.
    if new_voltage > current_voltage and requested_voltage <= current_voltage:
        new_voltage = current_voltage
    if new_frequency < current_frequency and new_voltage > current_voltage:
        new_voltage = current_voltage
    return new_frequency, new_voltage, reason


def _apply_frequency_cap(
    current_frequency,
    current_voltage,
    min_freq,
    max_freq,
    min_volt,
    max_volt,
    frequency_step,
    voltage_step,
):
    """Step a live clock down onto a max frequency it is already above.

    The floor for this drop is the cap, so one decision lands on the cap
    instead of walking under it. Voltage stays put.
    """
    gap = int(current_frequency) - int(max_freq)
    steps = max(1, (gap + frequency_step - 1) // frequency_step)
    new_frequency, new_voltage, step_reason = _step_down(
        current_frequency,
        current_voltage,
        max_freq,
        min_volt,
        frequency_step,
        voltage_step,
        steps,
    )
    if step_reason == "holding at minimum":
        chosen = "frequency ceiling"
    else:
        chosen = "step frequency down after frequency cap"
    return _guard_bounds(
        current_frequency,
        current_voltage,
        new_frequency,
        new_voltage,
        min_freq,
        max_freq,
        min_volt,
        max_volt,
        chosen,
    )


def _apply_step_down(
    current_frequency,
    current_voltage,
    min_freq,
    max_freq,
    min_volt,
    max_volt,
    frequency_step,
    voltage_step,
    reason_tag,
    frequency_steps=1,
    voltage_steps=0,
    shed_voltage_at_floor=True,
):
    """Step down with _step_down, or hold at the frequency floor.

    `shed_voltage_at_floor=False` holds instead of lowering voltage once
    frequency is at its floor. Errors and short hashrate come from too
    little voltage, so a lower one only makes them worse.
    """
    new_frequency, new_voltage, step_reason = _step_down(
        current_frequency,
        current_voltage,
        min_freq,
        min_volt,
        frequency_step,
        voltage_step,
        frequency_steps,
        voltage_steps,
    )
    if step_reason == "step voltage down" and not shed_voltage_at_floor:
        return current_frequency, current_voltage, "holding at minimum"
    if step_reason == "holding at minimum" or not reason_tag:
        chosen = step_reason
    else:
        chosen = f"{step_reason} after {reason_tag}"
    return _guard_bounds(
        current_frequency,
        current_voltage,
        new_frequency,
        new_voltage,
        min_freq,
        max_freq,
        min_volt,
        max_volt,
        chosen,
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
    thermal_hold=False,
    safety_hold=False,
    reject_share=None,
    max_reject_share=DEFAULT_MAX_REJECT_SHARE,
    hashrate_short=False,
    blocked_frequency=None,
    above_target_high=False,
    droop_voltage=None,
    max_climb_steps=1,
    core_current=None,
    max_core_amps=None,
    asic_off_watts=ASIC_OFF_POWER_WATTS,
    vr_temp_required=True,
    objective=HASHRATE,
    trim_floor_voltage=None,
):
    """Choose the next frequency and voltage.

    Returns (frequency, voltage, reason). Voltage is never raised while stepping down.
    Climb while both sensors are at or under their caps. `thermal_hold` is set after
    a thermal retreat and blocks the next climb until the caller clears it.
    `safety_hold` does the same after a power, sag, droop, or fault retreat.
    `blocked_frequency` is a clock that just failed for errors or rejects. The climb
    stops short of it until the caller clears the block.
    A missing ASIC or regulator temperature blocks climbs and voltage increases.
    A breach on any sensor that did report still steps down.
    `droop_voltage` scores sag against an earlier setpoint. The step still uses
    the live voltage. Pass it for one settle after a voltage raise.
    `max_climb_steps` lets a climb take more than one frequency step while
    errors are at most half the budget and `climb_frequency_steps` finds the
    headroom. The climb still stops short of `blocked_frequency`.
    `core_current` over `max_core_amps` steps down like power. A climb or a
    voltage raise that would carry the current past the cap does not happen.
    `asic_off_watts` is the board's power reading with the ASIC off.
    `vr_temp_required` is false on a board with no regulator sensor; a missing
    regulator reading then does not hold the climb.
    `objective` EFFICIENCY answers chip errors with a lower clock while there
    is one, never more voltage: at the clocks a Bitaxe runs, 10 mV costs
    several times the energy per hash that 5 MHz does. Held at a heat wall,
    it trims voltage one step instead of only waiting, staying above
    `trim_floor_voltage` (a trim that already failed at this clock).
    `tier_list`, `expected_hashrate`, and `shares_rejected_delta` stay in the
    signature so older callers keep working.
    """
    del tier_list, expected_hashrate, shares_rejected_delta
    frequency_step = max(int(frequency_step), 1)
    voltage_step = max(int(voltage_step), 1)
    temp_tolerance = temp_tolerance if temp_tolerance is not None else 0
    vr_tolerance = vr_temp_tolerance if vr_temp_tolerance is not None else 0
    phase = phase or "climb"

    temp_value = _usable_temp(temp)
    vr_value = _usable_temp(vr_temp)
    power_value = _as_float(power)
    over_temp = temp_value is not None and temp_value > max_temp
    over_power = power_value is not None and power_value > max_watts
    over_vr = vr_value is not None and vr_value > max_vr_temp
    over_current = _over_current(core_current, max_core_amps)
    current_value = _as_float(core_current)
    current_cap = _as_float(max_core_amps)
    error = _as_float(error_percentage)
    error_budget = (
        DEFAULT_MAX_ERROR_PERCENTAGE
        if max_error_percentage is None
        else float(max_error_percentage)
    )
    near_trip = near_firmware_trip(temp_value, vr_value)
    if over_temp or over_power or over_vr or near_trip or over_current:
        if over_temp or over_vr:
            tag = None
        elif over_current and not over_power:
            tag = "current limit"
        else:
            tag = "power limit"
        frequency_steps = 1
        voltage_steps = 0
        shed_at_floor = True
        if over_temp or over_vr:
            counts = []
            if over_temp:
                counts.append(
                    _thermal_frequency_steps(temp_value - max_temp, temp_tolerance)
                )
            if over_vr:
                counts.append(
                    _thermal_frequency_steps(vr_value - max_vr_temp, vr_tolerance)
                )
            frequency_steps = max(counts)
            # Voltage raised for an earlier, higher clock is extra heat at a
            # lower one. Shed a step with the frequency while errors show
            # margin, so the heat budget goes to frequency instead.
            if error is not None and error <= error_budget / 2:
                voltage_steps = 1
            # At the floor, a lower voltage trades heat for errors. Only do
            # that while errors still fit the budget.
            shed_at_floor = error is None or error <= error_budget
        if near_trip:
            # AxeOS would cut power and drop 100 MHz / 100 mV. Shed voltage
            # with frequency so the heat falls before it gets there.
            tag = "trip guard"
            frequency_steps = max(frequency_steps, TRIP_GUARD_FREQUENCY_STEPS)
            voltage_steps = 1
            shed_at_floor = True
        return _apply_step_down(
            current_frequency,
            current_voltage,
            min_freq,
            max_freq,
            min_volt,
            max_volt,
            frequency_step,
            voltage_step,
            tag,
            frequency_steps,
            voltage_steps,
            shed_at_floor,
        )

    if _overheat_mode_set(overheat_mode) or _asic_off_power(
        power_value, asic_off_watts
    ):
        return (
            current_frequency,
            current_voltage,
            "holding while the miner is offline or in overheat mode",
        )

    if (
        min_input_voltage is not None
        and input_voltage is not None
        and input_voltage < min_input_voltage
    ):
        return _apply_step_down(
            current_frequency,
            current_voltage,
            min_freq,
            max_freq,
            min_volt,
            max_volt,
            frequency_step,
            voltage_step,
            "input sag",
        )

    if _power_fault_set(power_fault):
        return _apply_step_down(
            current_frequency,
            current_voltage,
            min_freq,
            max_freq,
            min_volt,
            max_volt,
            frequency_step,
            voltage_step,
            "power fault",
        )

    actual_voltage = _usable_core_voltage(core_voltage_actual)
    droop_limit = _as_float(max_droop_mv)
    droop_from = current_voltage if droop_voltage is None else droop_voltage
    if (
        actual_voltage is not None
        and droop_limit is not None
        and droop_from is not None
        and (droop_from - actual_voltage) > droop_limit
    ):
        return _apply_step_down(
            current_frequency,
            current_voltage,
            min_freq,
            max_freq,
            min_volt,
            max_volt,
            frequency_step,
            voltage_step,
            "core voltage droop",
        )

    if (
        (current_frequency < min_freq or current_voltage < min_volt)
        and temp_value is not None
        and vr_value is not None
        and power_value is not None
        and cooled_after_retreat(
            temp_value,
            vr_value,
            max_temp,
            max_vr_temp,
            temp_tolerance,
            vr_tolerance,
        )
    ):
        # AxeOS can save clocks under the floor after an overheat trip.
        # Once the chip is cool, go straight back to the floor.
        floor_frequency, floor_voltage = floor_setpoint(
            current_frequency, current_voltage, min_freq, min_volt
        )
        return _guard_bounds(
            current_frequency,
            current_voltage,
            floor_frequency,
            floor_voltage,
            min_freq,
            max_freq,
            min_volt,
            max_volt,
            "restore floor",
        )

    if current_frequency > max_freq:
        return _apply_frequency_cap(
            current_frequency,
            current_voltage,
            min_freq,
            max_freq,
            min_volt,
            max_volt,
            frequency_step,
            voltage_step,
        )

    if temp_value is None or power_value is None:
        return current_frequency, current_voltage, "holding for telemetry"

    rate = _as_float(hash_rate)
    if rate is not None and rate <= 0:
        return current_frequency, current_voltage, "holding at zero hashrate"

    error_high = error is not None and error > error_budget
    short_hash = bool(hashrate_short)
    above_target_bad = bool(above_target_high)
    quality_bad = error_high or short_hash or above_target_bad
    if above_target_bad and not error_high and not short_hash:
        quality_tag = "above target"
    elif short_hash and not error_high:
        quality_tag = "low hashrate"
    else:
        quality_tag = "silicon wall"

    share = _as_float(reject_share)
    reject_limit = (
        DEFAULT_MAX_REJECT_SHARE
        if max_reject_share is None
        else float(max_reject_share)
    )
    if not above_target_bad and share is not None and share > reject_limit:
        return _apply_step_down(
            current_frequency,
            current_voltage,
            min_freq,
            max_freq,
            min_volt,
            max_volt,
            frequency_step,
            voltage_step,
            "rejected shares",
            shed_voltage_at_floor=False,
        )

    if quality_bad:
        if (
            phase == "trim"
            and trim_good_voltage is not None
            and current_voltage < int(trim_good_voltage)
        ):
            restored = _clamp(int(trim_good_voltage), min_volt, max_volt)
            return _guard_bounds(
                current_frequency,
                current_voltage,
                current_frequency,
                restored,
                min_freq,
                max_freq,
                min_volt,
                max_volt,
                "restore voltage",
            )
        # Core current grows about in proportion to voltage at a fixed clock.
        raise_crosses_current = (
            current_value is not None
            and current_value > 0
            and current_cap is not None
            and current_value * (current_voltage + voltage_step) / current_voltage
            > current_cap
        )
        # Power is about P0 + k·f·V², so J/TH is about P0/f + k·V². A 5 MHz
        # drop only moves the small P0/f share; a 10 mV raise moves the V²
        # share by about 2·dV/V.
        prefer_clock = objective == EFFICIENCY and current_frequency > min_freq
        if (
            phase == "hold"
            or thermal_hold
            or safety_hold
            or current_voltage >= max_volt
            or raise_crosses_current
            or prefer_clock
        ):
            return _apply_step_down(
                current_frequency,
                current_voltage,
                min_freq,
                max_freq,
                min_volt,
                max_volt,
                frequency_step,
                voltage_step,
                quality_tag,
                shed_voltage_at_floor=False,
            )
        if vr_value is None and vr_temp_required:
            return current_frequency, current_voltage, "holding for telemetry"
        stepped = min(max_volt, current_voltage + voltage_step)
        if stepped > current_voltage:
            return _guard_bounds(
                current_frequency,
                current_voltage,
                current_frequency,
                stepped,
                min_freq,
                max_freq,
                min_volt,
                max_volt,
                "increase voltage",
            )
        # Unreachable: voltage is under max_volt here and the step is at least 1 mV.
        return _apply_step_down(  # pragma: no cover
            current_frequency,
            current_voltage,
            min_freq,
            max_freq,
            min_volt,
            max_volt,
            frequency_step,
            voltage_step,
            quality_tag,
            shed_voltage_at_floor=False,
        )

    if thermal_hold and phase == "climb":
        # At the heat wall, less voltage is less heat per hash. The caller
        # judges the trim on energy per good hash and puts it back if it lost.
        floor = min_volt
        if trim_floor_voltage is not None:
            floor = max(min_volt, int(trim_floor_voltage) + voltage_step)
        if (
            objective == EFFICIENCY
            and error is not None
            and error <= error_budget
            and current_voltage - voltage_step >= floor
        ):
            return _guard_bounds(
                current_frequency,
                current_voltage,
                current_frequency,
                current_voltage - voltage_step,
                min_freq,
                max_freq,
                min_volt,
                max_volt,
                "trim voltage",
            )
        return current_frequency, current_voltage, "holding after thermal retreat"

    if safety_hold and phase == "climb":
        kind = "input" if safety_hold == "input" else "power"
        return current_frequency, current_voltage, f"holding after {kind} retreat"

    if error is None:
        return current_frequency, current_voltage, "holding for error percentage"

    if hash_rate is None or hash_rate <= 0:
        return current_frequency, current_voltage, "holding"

    if phase == "trim":
        if current_voltage - voltage_step >= min_volt:
            new_voltage = current_voltage - voltage_step
            return _guard_bounds(
                current_frequency,
                current_voltage,
                current_frequency,
                new_voltage,
                min_freq,
                max_freq,
                min_volt,
                max_volt,
                "trim voltage",
            )
        if current_voltage > min_volt:
            return _guard_bounds(
                current_frequency,
                current_voltage,
                current_frequency,
                min_volt,
                min_freq,
                max_freq,
                min_volt,
                max_volt,
                "trim voltage",
            )
        return current_frequency, current_voltage, "trim complete"

    if phase == "hold":
        return current_frequency, current_voltage, "holding"

    if current_frequency >= max_freq:
        return current_frequency, current_voltage, "frequency ceiling"

    if vr_value is None and vr_temp_required:
        return current_frequency, current_voltage, "holding for telemetry"

    # Core current grows in proportion to frequency at a fixed voltage.
    if (
        current_value is not None
        and current_cap is not None
        and current_value * (current_frequency + frequency_step) / current_frequency
        > current_cap
    ):
        return current_frequency, current_voltage, "holding at current limit"

    steps = 1
    if error <= error_budget / 2:
        steps = climb_frequency_steps(
            current_frequency,
            temp_value,
            vr_value,
            power_value,
            max_temp,
            max_vr_temp,
            max_watts,
            temp_tolerance,
            vr_tolerance,
            frequency_step,
            max_climb_steps,
            current_value,
            current_cap,
            vr_temp_required,
        )
    new_frequency = min(max_freq, current_frequency + frequency_step * steps)
    blocked = coerce_limit(blocked_frequency)
    while steps > 1 and blocked is not None and new_frequency >= blocked:
        steps -= 1
        new_frequency = min(max_freq, current_frequency + frequency_step * steps)
    if (
        blocked is not None
        and new_frequency > current_frequency
        and new_frequency >= blocked
    ):
        return current_frequency, current_voltage, "holding after frequency retreat"
    return _guard_bounds(
        current_frequency,
        current_voltage,
        new_frequency,
        current_voltage,
        min_freq,
        max_freq,
        min_volt,
        max_volt,
        "increase frequency",
    )


def get_system_info(bitaxe_ip):
    """Fetch system info from Bitaxe API."""
    try:
        response = requests.get(
            f"http://{bitaxe_ip}/api/system/info", timeout=SYSTEM_INFO_TIMEOUT
        )
        response.raise_for_status()
        return response.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        return f"Error fetching system info from {bitaxe_ip}: {e}"


# A miner that has not answered for this long gets one error in the log. A
# shorter gap is a reboot or a Wi-Fi blip and gets one warning.
NO_REPLY_ALERT_SECONDS = 60


def reply_problem(message):
    """Plain words for a failed system-info read, from its error text."""
    text = str(message or "").lower()
    if "timed out" in text or "timeout" in text:
        return "timed out"
    if "refused" in text or "errno 111" in text:
        return "connection refused"
    if (
        "no route" in text
        or "unreachable" in text
        or "errno 113" in text
        or "errno 101" in text
    ):
        return "unreachable"
    if "reset" in text or "aborted" in text or "errno 104" in text:
        return "connection dropped"
    if "expecting" in text or "json" in text or "unexpected" in text:
        return "bad reply"
    return "connection error"


class ReplyWatch:
    """One log line when a miner stops answering, one when it is back.

    The tuner polls every few seconds. Logging each failed poll made a
    reboot look like an outage.
    """

    def __init__(self, bitaxe_ip, log_callback, clock=None):
        self._ip = bitaxe_ip
        self._log = log_callback
        self._clock = clock or time.time
        self._since = None
        self._misses = 0
        self._alerted = False

    def failed(self, message):
        now = self._clock()
        problem = reply_problem(message)
        self._misses += 1
        if self._since is None:
            self._since = now
            self._log(f"{self._ip} -> No reply ({problem}). Retrying.", "warning")
        elif not self._alerted and now - self._since >= NO_REPLY_ALERT_SECONDS:
            self._alerted = True
            self._log(
                f"{self._ip} -> No reply for {int(now - self._since)} s "
                f"({problem}). Still retrying.",
                "error",
            )

    def answered(self):
        if self._since is None:
            return
        gone = int(self._clock() - self._since)
        self._log(
            f"{self._ip} -> Answering again after {gone} s "
            f"({self._misses} missed read{'s' if self._misses != 1 else ''}).",
            "info",
        )
        self._since = None
        self._misses = 0
        self._alerted = False


def patch_system(bitaxe_ip, settings):
    """PATCH /api/system. Returns (ok, error_text)."""
    try:
        response = requests.patch(
            f"http://{bitaxe_ip}/api/system", json=settings, timeout=10
        )
        response.raise_for_status()
        return True, ""
    except requests.exceptions.RequestException as e:
        return False, str(e)


def set_system_settings(bitaxe_ip, core_voltage, frequency):
    """Set frequency and core voltage.

    AxeOS v2.15.1 applies any value in range. overclockEnabled only unlocks the
    free-entry fields on its own settings page, and is sent to match that page.
    """
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


def _restore_is_confirmed(probe, confirmed):
    """True when a voltage restore has been echoed by the miner.

    The probe stays in place until then, so a stop during the settle keeps the
    clocks that were put back instead of the trim that just failed.
    """
    if not isinstance(probe, dict) or not probe.get("restore") or confirmed is None:
        return False
    origin_frequency = probe.get("from_freq")
    origin_voltage = probe.get("from_volt")
    if origin_frequency is None or origin_voltage is None:
        return False
    return _same_setpoint(confirmed, (origin_frequency, origin_voltage))


def _apply_fan(bitaxe_ip, payload, log_callback):
    ok, error = patch_system(bitaxe_ip, payload)
    if ok:
        log_callback(f"{bitaxe_ip} -> Fan updated.", "info")
    else:
        log_callback(f"{bitaxe_ip} -> Fan update failed: {error}", "warning")


# AxeOS v2.15.1 takes the manual fan percent as "manualFanSpeed". It renamed
# "fanspeed" in v2.11.0 and ignores unknown PATCH keys, so the old key would turn
# auto fan off and leave the fan on whatever manual speed was saved before.
# GET still reports the live fan percent as "fanspeed".
MANUAL_FULL_FAN = fan_payload(MODES[MAX_HASHRATE], None)


def fan_matches(info, payload):
    """True when AxeOS reports the fan the way `payload` set it.

    Manual payloads have to read back as manual 100%. Auto payloads have to
    read back as auto, at the target when the firmware reports one. AxeOS
    turns auto off and runs 100% after an overheat trip, so this is how a
    session notices and puts auto back.
    """
    if not payload.get("autofanspeed"):
        return _fan_is_manual_full(info)
    if not isinstance(info, dict) or not _overheat_mode_set(info.get("autofanspeed")):
        return False
    wanted = payload.get("temptarget")
    reported = _as_float(info.get("temptarget"))
    return wanted is None or reported is None or int(reported) == int(wanted)


def _fan_is_manual_full(info):
    """True when AxeOS reports manual fan control at 100%."""
    if not isinstance(info, dict):
        return False
    if "autofanspeed" not in info or "fanspeed" not in info:
        return False
    if _overheat_mode_set(info.get("autofanspeed")):
        return False
    speed = _as_float(info.get("fanspeed"))
    return speed is not None and speed >= 100


def _reports_error_percentage(info):
    return (
        isinstance(info, dict)
        and "errorPercentage" in info
        and info.get("errorPercentage") is not None
    )


def _is_safety_retreat(reason):
    """True for heat, power, sag, fault, or droop. Quality retreats stay in the session."""
    text = (reason or "").lower()
    if not (text.startswith("step frequency") or text.startswith("step voltage")):
        return False
    return not any(
        tag in text for tag in ("silicon", "hashrate", "above target", "rejected")
    )


# Fast start. A fresh session jumps from its start clocks toward a point
# RAMP_HEADROOM_C under the ASIC and regulator caps, judging the heat from what
# each jump did. Fine tuning in 5 MHz steps takes over from there.
RAMP_SETTLE_SECONDS = 60
# A chip still warming by RAMP_WARMING_C across the last RAMP_TREND_POLLS polls
# holds the jump longer, up to RAMP_MAX_SETTLES settles in all.
RAMP_WARMING_C = 0.5
RAMP_TREND_POLLS = 3
RAMP_MAX_SETTLES = 3
RAMP_HEADROOM_C = 6.0
RAMP_POWER_HEADROOM_W = 2.0
RAMP_INPUT_HEADROOM_V = 0.05
RAMP_CURRENT_HEADROOM_A = 1.5
RAMP_MAX_JUMP_MHZ = 100
# One jump covers this share of the estimated distance. The estimate improves
# with every jump, so the last ones are short.
RAMP_FRACTION = 0.75
RAMP_DONE_MHZ = 15
# Core voltage added per MHz on the way up, from these miners at 525-955 MHz.
# A little under what they settled at: errors raise it, and extra voltage would
# spend heat the clock could use.
RAMP_MV_PER_MHZ = GAMMA_601.limits.mv_per_mhz
RAMP_ERROR_VOLTAGE_STEPS = 2
RAMP_MAX_MOVES = 30
# AxeOS adds this much to Gamma power for the rest of the board. It does not
# change with the clocks. Other boards use their own `board_power_w`.
BOARD_POWER_W = GAMMA_601.board_power_w
RAMP_MIN_C_PER_W = 0.2
RAMP_MAX_C_PER_W = 6.0


def ramp_headroom(board=GAMMA_601):
    """Fast-start headroom as `(watts, amps, input volts)` for this board.

    The Gamma's 2 W, 1.5 A, and 0.05 V scale with ASIC count, the regulator's
    shutdown current, and the nominal input.
    """
    family = board.family
    return (
        RAMP_POWER_HEADROOM_W * family.asic_count,
        RAMP_CURRENT_HEADROOM_A
        * family.regulator.iout_fault
        / TPS546_DEFAULT.iout_fault,
        RAMP_INPUT_HEADROOM_V * family.nominal_voltage / 5,
    )


def fast_start_enabled(runtime):
    """True unless Global Settings turned the fast start off."""
    value = (runtime or {}).get("fast_start", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def continue_from_live_enabled(runtime):
    """True unless Global Settings turned off continuing from live clocks."""
    value = (runtime or {}).get("continue_from_live", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def live_start_clocks(info, limits, asic_off_watts=ASIC_OFF_POWER_WATTS):
    """The clocks a healthy miner is running now, as `(frequency, voltage)`, or None.

    A restarted session continues from these instead of the start clocks.
    They come from the miner, not from anything saved. None when a clock is
    missing or outside this miner's limits (AxeOS saves clocks under the floor
    after a trip), or the miner is in overheat mode, has a power fault, has
    its ASIC off, or has stopped hashing with the pool still up.
    """
    if not isinstance(info, dict):
        return None
    frequency = coerce_limit(info.get("frequency"))
    voltage = coerce_limit(info.get("coreVoltage"))
    if frequency is None or voltage is None:
        return None
    if not limits["min_freq"] <= frequency <= limits["max_freq"]:
        return None
    if not limits["min_volt"] <= voltage <= limits["max_volt"]:
        return None
    if _overheat_mode_set(info.get("overheat_mode")) or _power_fault_set(
        info.get("power_fault")
    ):
        return None
    if _asic_off_power(_as_float(info.get("power")), asic_off_watts):
        return None
    if board_hashrate_is_dead(info) and not pool_is_down(info):
        return None
    return frequency, voltage


def ramp_voltage(
    frequency,
    start_frequency,
    start_voltage,
    floor_voltage,
    max_volt,
    voltage_step,
    mv_per_mhz=RAMP_MV_PER_MHZ,
):
    """Core voltage for a fast-start clock.

    The start voltage plus `mv_per_mhz` for each MHz above the start clock,
    in whole voltage steps. Never under `floor_voltage`, the voltage errors
    already asked for, and never over `max_volt`.
    """
    step = max(int(voltage_step), 1)
    rise = mv_per_mhz * max(0, int(frequency) - int(start_frequency))
    line = int(start_voltage) + step * int(round(rise / step))
    return int(min(int(max_volt), max(int(floor_voltage), line)))


def thermal_fit(points):
    """(ambient °C, °C per W) through settled `(watts, °C)` points, or None.

    A least-squares line through two or more points spread over at least 1 W.
    The slope stays inside RAMP_MIN_C_PER_W..RAMP_MAX_C_PER_W, so one noisy
    reading cannot promise a cold chip.
    """
    pairs = [
        (float(watts), float(temp))
        for watts, temp in points
        if watts is not None and temp is not None
    ]
    if len(pairs) < 2:
        return None
    watts = [item[0] for item in pairs]
    if max(watts) - min(watts) < 1.0:
        return None
    mean_watts = sum(watts) / len(watts)
    mean_temp = sum(item[1] for item in pairs) / len(pairs)
    spread = sum((value - mean_watts) ** 2 for value in watts)
    slope = (
        sum((value - mean_watts) * (temp - mean_temp) for value, temp in pairs) / spread
    )
    slope = _clamp(slope, RAMP_MIN_C_PER_W, RAMP_MAX_C_PER_W)
    return mean_temp - slope * mean_watts, slope


def ramp_target(
    sample,
    history,
    limits,
    start,
    frequency_step,
    voltage_step,
    min_input_voltage=None,
    max_core_amps=None,
    board=GAMMA_601,
    headroom_c=RAMP_HEADROOM_C,
):
    """The next fast-start jump as `(frequency, voltage, aim_frequency, aim_temp)`.

    `sample` is the settled reading at the current clocks: `frequency`,
    `voltage`, `power`, `temp`, `vr_temp`, and `input_voltage`. `history` holds
    the earlier settled readings. Power is projected as the board's fixed
    share plus a term in f·V², scaled from this reading. Heat follows a line
    fitted through all the readings; with only one, the line runs through
    0 °C, which over-states the rise. The aim is the highest clock whose
    projection stays RAMP_HEADROOM_C under both temperature caps, under the
    power cap, and RAMP_CURRENT_HEADROOM_A under `max_core_amps`. One jump covers RAMP_FRACTION of the way, at most
    RAMP_MAX_JUMP_MHZ.

    None when fine tuning should take over: the aim is within RAMP_DONE_MHZ,
    a reading is missing, or the input is near its floor.

    `board` sets the board's share of the power reading, the voltage per MHz,
    the headroom (`ramp_headroom`), and how many chips share the regulator's
    output. A board with no regulator sensor is projected on the ASIC alone.
    """
    board_power_w = board.board_power_w
    mv_per_mhz = board.limits.mv_per_mhz
    power_headroom, current_headroom, input_headroom = ramp_headroom(board)
    frequency = coerce_limit(sample.get("frequency"))
    voltage = coerce_limit(sample.get("voltage"))
    power = _as_float(sample.get("power"))
    temp = _usable_temp(sample.get("temp"))
    vr_temp = _usable_temp(sample.get("vr_temp"))
    if None in (frequency, voltage, power, temp) or frequency <= 0:
        return None
    if vr_temp is None and board.has_vr_temp:
        return None
    input_voltage = _as_float(sample.get("input_voltage"))
    if (
        min_input_voltage is not None
        and input_voltage is not None
        and input_voltage < min_input_voltage + input_headroom
    ):
        return None
    scale = (power - board_power_w) / (frequency * (voltage / 1000.0) ** 2)
    if scale <= 0:
        return None
    readings = [*history, sample]
    asic = thermal_fit(
        [(item.get("power"), _usable_temp(item.get("temp"))) for item in readings]
    ) or (0.0, temp / power)
    regulator = None
    if vr_temp is not None:
        regulator = thermal_fit(
            [
                (item.get("power"), _usable_temp(item.get("vr_temp")))
                for item in readings
            ]
        ) or (0.0, vr_temp / power)
    temp_cap = limits["max_temp"] - headroom_c
    vr_cap = limits["max_vr_temp"] - headroom_c
    watts_cap = limits["max_watts"] - power_headroom
    amps_cap = (
        None
        if _as_float(max_core_amps) is None
        else float(max_core_amps) - current_headroom
    )
    # The regulator drives every voltage domain in series.
    domains = board.family.voltage_domains
    step = max(int(frequency_step), 1)

    def clocks_at(clock):
        return clock, ramp_voltage(
            clock,
            start[0],
            start[1],
            voltage,
            limits["max_volt"],
            voltage_step,
            mv_per_mhz,
        )

    aim = frequency
    aim_temp = temp
    for clock in range(frequency + step, int(limits["max_freq"]) + 1, step):
        _clock, volts = clocks_at(clock)
        watts = board_power_w + scale * clock * (volts / 1000.0) ** 2
        projected = asic[0] + asic[1] * watts
        amps = (watts - board_power_w) / (volts * domains / 1000.0)
        if (
            projected > temp_cap
            or (regulator is not None and regulator[0] + regulator[1] * watts > vr_cap)
            or watts > watts_cap
            or (amps_cap is not None and amps > amps_cap)
        ):
            break
        aim = clock
        aim_temp = projected
    gap = aim - frequency
    if gap < RAMP_DONE_MHZ:
        return None
    jump = min(RAMP_MAX_JUMP_MHZ, gap * RAMP_FRACTION)
    jump = max(step, int(jump // step) * step)
    next_frequency, next_voltage = clocks_at(frequency + jump)
    return next_frequency, next_voltage, aim, aim_temp


def _ramp_breach(
    info, clocks, limits, min_input_voltage, max_core_amps=None, board=GAMMA_601
):
    """Plain text for a limit this reading crosses at the ramp clocks, or ""."""
    asic_off_watts = board.limits.asic_off_watts
    temp = _usable_temp(asic_temp(info))
    vr_temp = _usable_temp(info.get("vrTemp"))
    power = _as_float(info.get("power"))
    if _overheat_mode_set(info.get("overheat_mode")):
        return "overheat mode"
    if _power_fault_set(info.get("power_fault")):
        return "power fault"
    if _asic_off_power(power, asic_off_watts):
        return "the ASIC is off"
    if _over_current(core_current_amps(info, board), max_core_amps):
        return "the core current limit"
    if _needs_immediate_retreat(
        temp,
        vr_temp,
        power,
        limits["max_temp"],
        limits["max_vr_temp"],
        limits["max_watts"],
        normalize_input_voltage(info.get("voltage")),
        min_input_voltage,
        None,
        clocks[1],
        None,
        None,
        None,
        asic_off_watts=asic_off_watts,
    ):
        return "a heat, power, or input limit"
    return ""


def _mean(values):
    kept = [value for value in values if value is not None]
    return sum(kept) / len(kept) if kept else None


def _ramp_settle(
    bitaxe_ip,
    clocks,
    limits,
    interval,
    settle_seconds,
    min_input_voltage,
    stop_event,
    max_core_amps=None,
    board=GAMMA_601,
):
    """Hold `clocks` until the chip settles.

    Returns `(sample, info, problem)`, or None when tuning was stopped. The
    sample averages the last RAMP_TREND_POLLS polls; errors average every poll.
    `problem` names a limit crossed on any poll, or is empty.
    """
    began = time.time()
    polls = []
    info = None
    while True:
        if _wait(stop_event, interval):
            return None
        polled = get_system_info(bitaxe_ip)
        if not isinstance(polled, dict):
            if time.time() - began > settle_seconds * RAMP_MAX_SETTLES:
                return None, info, "the miner stopped answering"
            continue
        info = polled
        if not (
            _numbers_match(info.get("frequency"), clocks[0])
            and _numbers_match(info.get("coreVoltage"), clocks[1])
        ):
            if time.time() - began > settle_seconds * RAMP_MAX_SETTLES:
                return None, info, "the miner did not take the new clocks"
            continue
        problem = _ramp_breach(
            info, clocks, limits, min_input_voltage, max_core_amps, board
        )
        if problem:
            return None, info, problem
        polls.append(
            {
                "temp": _usable_temp(asic_temp(info)),
                "vr_temp": _usable_temp(info.get("vrTemp")),
                "power": _as_float(info.get("power")),
                "input_voltage": normalize_input_voltage(info.get("voltage")),
                "error": _as_float(info.get("errorPercentage")),
            }
        )
        elapsed = time.time() - began
        if elapsed < settle_seconds:
            continue
        recent = polls[-RAMP_TREND_POLLS:]
        temps = [poll["temp"] for poll in recent if poll["temp"] is not None]
        warming = len(temps) >= 2 and temps[-1] - temps[0] >= RAMP_WARMING_C
        if warming and elapsed < settle_seconds * RAMP_MAX_SETTLES:
            continue
        sample = {
            "frequency": clocks[0],
            "voltage": clocks[1],
            "temp": _mean(poll["temp"] for poll in recent),
            "vr_temp": _mean(poll["vr_temp"] for poll in recent),
            "power": _mean(poll["power"] for poll in recent),
            "input_voltage": _mean(poll["input_voltage"] for poll in recent),
            "error": _mean(poll["error"] for poll in polls),
        }
        return sample, info, ""


def _ramp_quality(
    sample, info, max_error_percentage, settled_for, ratio=HASHRATE_SHORTFALL_RATIO
):
    """ "dead", "errors", "low hashrate", or "" for a settled ramp reading.

    `ratio` is the board's AxeOS self-test pass share (80% on a BM1368).
    """
    if board_hashrate_is_dead(info) and not pool_is_down(info):
        return "dead"
    error = sample.get("error")
    budget = (
        DEFAULT_MAX_ERROR_PERCENTAGE
        if max_error_percentage is None
        else max_error_percentage
    )
    if error is not None and error > budget:
        return "errors"
    if settled_for >= HASHRATE_1M_SETTLE_SECONDS and hashrate_well_below_expected(
        info.get("hashRate_1m"),
        expected_hashrate_from_info(info, sample["frequency"]),
        ratio,
    ):
        return "low hashrate"
    return ""


def ramp_session(
    bitaxe_ip,
    frequency,
    voltage,
    info,
    limits,
    interval,
    settle_seconds,
    frequency_step,
    voltage_step,
    min_input_voltage,
    max_error_percentage,
    stop_event,
    log_callback,
    max_core_amps=None,
    board=GAMMA_601,
    headroom_c=RAMP_HEADROOM_C,
):
    """Fast start: jump from the start clocks toward the heat caps.

    Returns `(frequency, voltage, info)` for fine tuning to start from, or
    None when tuning was stopped. The clocks returned are the last ones that
    settled with good errors and hashrate. A refused write, a crossed limit,
    or a dead board ends the ramp there. A jump with errors even at max
    voltage is split in half until the gap is under RAMP_DONE_MHZ, and later
    jumps stay under it. The caller writes the clocks.
    """
    start = (int(frequency), int(voltage))
    current = start
    good = None
    good_info = info
    failed_frequency = None
    last_info = info
    history = []
    began = time.time()
    _publish_status(bitaxe_ip, phase="ramp", reason="fast start")
    log_callback(
        f"{bitaxe_ip} -> Fast start from {start[0]} MHz / {start[1]} mV.", "info"
    )

    def finish(clocks, note):
        """End on `clocks`, with the last reading taken at them when it differs."""
        minutes = (time.time() - began) / 60
        log_callback(
            f"{bitaxe_ip} -> Fast start {note} after {minutes:.0f} min. "
            f"Fine tuning from {clocks[0]} MHz / {clocks[1]} mV.",
            "info",
        )
        reading = last_info if clocks == current else good_info
        return clocks[0], clocks[1], reading

    for _move in range(RAMP_MAX_MOVES):
        applied = set_system_settings(bitaxe_ip, current[1], current[0])
        log_callback(applied, "info")
        if not settings_were_applied(applied):
            return finish(good or start, "stopped: the miner refused a change")
        settle_began = time.time()
        settled = _ramp_settle(
            bitaxe_ip,
            current,
            limits,
            interval,
            settle_seconds,
            min_input_voltage,
            stop_event,
            max_core_amps,
            board,
        )
        if settled is None:
            return None
        sample, polled, problem = settled
        if polled is not None:
            last_info = polled
        if problem:
            return finish(good or start, f"stepped back after {problem}")
        quality = _ramp_quality(
            sample,
            polled,
            max_error_percentage,
            time.time() - settle_began,
            board.asic.self_test_ratio,
        )
        if quality == "dead":
            return finish(good or start, "stepped back after the board stopped hashing")
        if quality:
            raised = min(
                int(limits["max_volt"]),
                current[1] + RAMP_ERROR_VOLTAGE_STEPS * int(voltage_step),
            )
            if raised <= current[1]:
                # Silicon, not heat. Split the jump instead of giving it all back.
                failed_frequency = current[0]
                step = max(int(frequency_step), 1)
                if good is None or current[0] - good[0] <= RAMP_DONE_MHZ:
                    return finish(
                        good or current,
                        f"found {quality} at {current[0]} MHz even at {current[1]} mV",
                    )
                middle = good[0] + ((current[0] - good[0]) // 2 // step) * step
                log_callback(
                    f"{bitaxe_ip} -> Fast start: {quality} at {current[0]} MHz even "
                    f"at {current[1]} mV. Trying {middle} MHz.",
                    "info",
                )
                current = (middle, current[1])
                continue
            log_callback(
                f"{bitaxe_ip} -> Fast start: {quality} at {current[0]} MHz. "
                f"Raising voltage to {raised} mV.",
                "info",
            )
            current = (current[0], raised)
            continue
        good = current
        good_info = polled
        target = ramp_target(
            sample,
            history,
            limits,
            start,
            frequency_step,
            voltage_step,
            min_input_voltage,
            max_core_amps,
            board,
            headroom_c,
        )
        history.append(sample)
        if target is not None and failed_frequency is not None:
            # Stay under a clock that already failed at max voltage.
            ceiling = failed_frequency - max(int(frequency_step), 1)
            if target[0] > ceiling:
                gap = ceiling - current[0]
                target = (
                    None
                    if gap < RAMP_DONE_MHZ
                    else (
                        current[0]
                        + (gap // 2 // int(frequency_step)) * int(frequency_step),
                        target[1],
                        ceiling,
                        target[3],
                    )
                )
        if target is None:
            return finish(current, "done")
        next_frequency, next_voltage, aim, aim_temp = target
        log_callback(
            f"{bitaxe_ip} -> Fast start: {current[0]} MHz / {current[1]} mV ran "
            f"{sample['temp']:.0f}°C at {sample['power']:.1f} W. Next "
            f"{next_frequency} MHz / {next_voltage} mV, aiming near {aim} MHz "
            f"({aim_temp:.0f}°C).",
            "info",
        )
        current = (next_frequency, next_voltage)
    return finish(good or start, "done")


def opening_setpoint(
    frequency,
    voltage,
    info,
    limits,
    frequency_step,
    voltage_step,
    temp_tolerance,
    vr_temp_tolerance,
    min_input_voltage,
    max_error_percentage,
    max_droop_mv,
    max_core_amps=None,
    board=GAMMA_601,
):
    """Clocks for the first write. A live breach lowers them. A cool chip does not climb."""
    if not isinstance(info, dict):
        return frequency, voltage, ""
    new_frequency, new_voltage, reason = decide_adjustment(
        current_frequency=frequency,
        current_voltage=voltage,
        min_freq=limits["min_freq"],
        max_freq=limits["max_freq"],
        min_volt=limits["min_volt"],
        max_volt=limits["max_volt"],
        max_temp=limits["max_temp"],
        max_watts=limits["max_watts"],
        max_vr_temp=limits["max_vr_temp"],
        temp=asic_temp(info),
        vr_temp=info.get("vrTemp") if "vrTemp" in info else None,
        power=info.get("power") if "power" in info else None,
        hash_rate=measured_hashrate(info),
        expected_hashrate=expected_hashrate_from_info(info, frequency),
        shares_rejected_delta=0,
        overheat_mode=info.get("overheat_mode"),
        frequency_step=frequency_step,
        voltage_step=voltage_step,
        temp_tolerance=temp_tolerance,
        error_percentage=_as_float(info.get("errorPercentage")),
        input_voltage=normalize_input_voltage(info.get("voltage")),
        min_input_voltage=min_input_voltage,
        core_voltage_actual=info.get("coreVoltageActual"),
        max_droop_mv=max_droop_mv,
        power_fault=info.get("power_fault"),
        phase="climb",
        max_error_percentage=max_error_percentage,
        vr_temp_tolerance=vr_temp_tolerance,
        core_current=core_current_amps(info, board),
        max_core_amps=max_core_amps,
        asic_off_watts=board.limits.asic_off_watts,
        vr_temp_required=board.has_vr_temp,
    )
    if (new_frequency < frequency or new_voltage < voltage) and _is_safety_retreat(
        reason
    ):
        return new_frequency, new_voltage, reason
    return frequency, voltage, ""


def monitor_and_adjust(
    bitaxe_ip,
    bitaxe_type,
    interval,
    log_callback,
    min_freq,
    max_freq,
    min_volt,
    max_volt,
    max_temp,
    max_watts,
    start_freq=None,
    start_volt=None,
    max_vr_temp=None,
    stop_event=None,
    startup_delay=0,
):
    """Monitor and auto-adjust one miner. A missing limit skips this miner only."""
    del bitaxe_type  # Kept so existing callers can still pass the miner type.
    event = stop_event if stop_event is not None else _default_stop_event
    # The saved board sets the hard range. The live reply has to match it.
    # The saved mode sets the objective, the fan, and the fast-start headroom.
    saved = _miner_record(bitaxe_ip)
    board = board_for_record(saved)
    mode = mode_for_record(saved, board)
    asic_off_watts = board.limits.asic_off_watts

    limits = {
        "min_freq": coerce_limit(min_freq),
        "max_freq": coerce_limit(max_freq),
        "min_volt": coerce_limit(min_volt),
        "max_volt": coerce_limit(max_volt),
        "max_temp": coerce_real_limit(max_temp),
        "max_watts": coerce_real_limit(max_watts),
        "max_vr_temp": coerce_real_limit(max_vr_temp),
    }
    if limits["max_vr_temp"] is None and not board.has_vr_temp:
        # No regulator sensor, so this cap is never compared.
        limits["max_vr_temp"] = TRIP_SAFE_MAX_VR_TEMP
    missing = [name for name, value in limits.items() if value is None]
    if missing:
        log_callback(
            f"{bitaxe_ip} -> Missing AutoTuner settings ({', '.join(missing)}). Skipping tuning.",
            "error",
        )
        _publish_status(bitaxe_ip, phase="skipped", reason="missing settings")
        return
    if (
        limits["min_freq"] > limits["max_freq"]
        or limits["min_volt"] > limits["max_volt"]
    ):
        log_callback(
            f"{bitaxe_ip} -> AutoTuner limits are reversed. Skipping tuning.", "error"
        )
        _publish_status(bitaxe_ip, phase="skipped", reason="limits reversed")
        return
    requested_caps = (limits["max_temp"], limits["max_vr_temp"])
    limits = clamp_limits(limits, board)
    if (limits["max_temp"], limits["max_vr_temp"]) != requested_caps:
        log_callback(
            f"{bitaxe_ip} -> Temperature caps are too close to the AxeOS overheat trip. "
            f"Using {limits['max_temp']:g}°C ASIC / {limits['max_vr_temp']:g}°C VR.",
            "warning",
        )

    if _wait(event, startup_delay):
        log_callback(f"{bitaxe_ip} -> Autotuning stopped.", "warning")
        return

    replies = ReplyWatch(bitaxe_ip, log_callback)
    info = None
    while not event.is_set():
        info = get_system_info(bitaxe_ip)
        if event.is_set():
            break
        if isinstance(info, dict):
            replies.answered()
            break
        replies.failed(info)
        if _wait(event, interval):
            break
    if event.is_set() or not isinstance(info, dict):
        log_callback(f"{bitaxe_ip} -> Autotuning stopped.", "warning")
        return

    live_board = board_for_info(info)
    if live_board is None:
        log_callback(
            f"{bitaxe_ip} -> Skipping tuning. {board_list_note(info)}", "error"
        )
        _publish_status(bitaxe_ip, phase="skipped", reason="not on the board list")
        return
    if live_board is not board:
        log_callback(
            f"{bitaxe_ip} -> Skipping tuning. The miner reports a {live_board.name}, "
            f"but it was saved as a {board.name}. Remove it and add it again.",
            "error",
        )
        _publish_status(bitaxe_ip, phase="skipped", reason="board changed")
        return
    firmware_problem, firmware_note = firmware_check(info)
    if firmware_problem:
        log_callback(f"{bitaxe_ip} -> Skipping tuning. {firmware_problem}", "error")
        _publish_status(bitaxe_ip, phase="skipped", reason="firmware too old")
        return
    if firmware_note:
        log_callback(f"{bitaxe_ip} -> {firmware_note}", "warning")
    if not _reports_error_percentage(info):
        log_callback(
            f"{bitaxe_ip} -> Skipping tuning. Firmware did not report errorPercentage.",
            "error",
        )
        _publish_status(bitaxe_ip, phase="skipped", reason="no error percentage")
        return

    record = _miner_record(bitaxe_ip)
    min_input_voltage = _record_float(
        record, "min_input_voltage", board.default_min_input_voltage
    )
    max_error_percentage = _record_float(
        record, "max_error_percentage", DEFAULT_MAX_ERROR_PERCENTAGE
    )
    max_droop_mv = _record_float(record, "max_droop_mv", DEFAULT_MAX_DROOP_MV)
    max_core_amps = core_amps_cap(record, board)

    start_frequency = coerce_limit(start_freq)
    start_voltage = coerce_limit(start_volt)
    if start_frequency is None:
        start_frequency = limits["min_freq"]
    if start_voltage is None:
        start_voltage = limits["min_volt"]
    # Nothing learned in an earlier session carries over: weather, paste, and
    # voltage all change. A healthy miner continues from the clocks it is
    # running now; anything else starts from the start clocks.
    start_frequency = _clamp(start_frequency, limits["min_freq"], limits["max_freq"])
    start_voltage = _clamp(start_voltage, limits["min_volt"], limits["max_volt"])

    runtime = load_config()
    live = live_start_clocks(info, limits, asic_off_watts)
    if live is not None and continue_from_live_enabled(runtime):
        start_frequency, start_voltage = live
        log_callback(
            f"{bitaxe_ip} -> Continuing from the miner's {live[0]} MHz / {live[1]} mV.",
            "info",
        )
    interval = _non_negative_float(runtime.get("monitor_interval", interval), 5)
    frequency_step = _positive_int(runtime.get("frequency_step"), 5)
    voltage_step = _positive_int(runtime.get("voltage_step"), 10)
    temp_tolerance = _non_negative_float(runtime.get("temp_tolerance"), 3)
    vr_temp_tolerance = _non_negative_float(runtime.get("vr_temp_tolerance"), 3)
    refresh_interval = _non_negative_float(runtime.get("refresh_interval"), 180)
    max_climb_steps = _positive_int(
        runtime.get("max_climb_steps"), MAX_CLIMB_FREQUENCY_STEPS
    )
    requested_frequency = start_frequency
    requested_voltage = start_voltage
    # After a voltage raise, score droop against the previous setpoint until
    # this settle ends. The measured rail lags the new setpoint.
    droop_reference = None
    droop_grace_until = 0.0

    def _arm_droop_grace(previous_voltage, new_voltage):
        nonlocal droop_reference, droop_grace_until
        previous = _as_float(previous_voltage)
        updated = _as_float(new_voltage)
        if previous is None or updated is None or updated <= previous:
            return
        droop_reference = int(previous)
        droop_grace_until = last_tune_time + refresh_interval

    def _fan():
        return fan_payload(mode, limits["max_temp"])

    def _opening_from(sample):
        return opening_setpoint(
            requested_frequency,
            requested_voltage,
            sample,
            limits,
            frequency_step,
            voltage_step,
            temp_tolerance,
            vr_temp_tolerance,
            min_input_voltage,
            max_error_percentage,
            max_droop_mv,
            max_core_amps,
            board,
        )

    log_callback(
        f"{bitaxe_ip} -> Tuning as a {board.name} in {mode.name} mode.", "info"
    )
    _apply_fan(bitaxe_ip, _fan(), log_callback)
    start_frequency, start_voltage, opening_reason = _opening_from(info)
    # A downward opening is applied even if the fan has not read back yet.
    # An overclock waits until AxeOS reports the mode's fan setting.
    if not opening_reason and not fan_matches(info, _fan()):
        while not event.is_set():
            if _wait(event, interval):
                break
            try:
                polled = get_system_info(bitaxe_ip)
            except Exception as exc:
                log_callback(f"{bitaxe_ip} -> UNCAUGHT ERROR: {exc}", "error")
                continue
            if isinstance(polled, str) or not isinstance(polled, dict):
                replies.failed(polled)
                continue
            replies.answered()
            info = polled
            start_frequency, start_voltage, opening_reason = _opening_from(info)
            if opening_reason or fan_matches(info, _fan()):
                break
            _apply_fan(bitaxe_ip, _fan(), log_callback)
            fan_text = "auto" if mode.auto_fan else "manual 100%"
            log_callback(
                f"{bitaxe_ip} -> Waiting for the fan to hold {fan_text}.",
                "info",
            )
    if event.is_set() or (not opening_reason and not fan_matches(info, _fan())):
        log_callback(f"{bitaxe_ip} -> Autotuning stopped.", "warning")
        return
    if opening_reason:
        log_callback(
            f"{bitaxe_ip} -> Live sample is over a limit. "
            f"Opening at {start_frequency} MHz / {start_voltage} mV.",
            "warning",
        )
    elif fast_start_enabled(runtime):
        ramped = ramp_session(
            bitaxe_ip,
            start_frequency,
            start_voltage,
            info,
            limits,
            interval,
            min(RAMP_SETTLE_SECONDS, refresh_interval),
            frequency_step,
            voltage_step,
            min_input_voltage,
            max_error_percentage,
            event,
            log_callback,
            max_core_amps,
            board,
            mode.ramp_headroom_c,
        )
        if ramped is None:
            log_callback(f"{bitaxe_ip} -> Autotuning stopped.", "warning")
            return
        requested_frequency, requested_voltage, info = ramped
        # The ramp may stop on a hot reading. Let the opening check step down
        # from its clocks before fine tuning starts.
        start_frequency, start_voltage, opening_reason = _opening_from(info)
    applied_settings = set_system_settings(bitaxe_ip, start_voltage, start_frequency)
    log_callback(applied_settings, "info")

    confirmed = None
    pending = (
        (start_frequency, start_voltage)
        if settings_were_applied(applied_settings)
        else None
    )
    if pending is None:
        log_callback(
            f"{bitaxe_ip} -> Initial settings were not applied. Waiting for the miner to report its setpoint.",
            "warning",
        )
    now = time.time()
    settle_until = now + refresh_interval if pending is not None else now
    last_tune_time = now if pending is not None else 0
    if pending is not None:
        _arm_droop_grace(info.get("coreVoltage"), start_voltage)

    hashrate_history = []
    rolling_hashrate = []
    last_config_refresh = 0
    phase = "climb"
    trim_good_voltage = None
    # A silicon frequency retreat below max voltage stays in hold until the
    # chip looks healthy. Entering trim immediately would raise voltage.
    trim_after_retreat = False
    # Efficiency trims voltage at a heat wall. A trim that lost stays out:
    # (frequency, voltage) of the first voltage that did not pay.
    wall_trim_floor = None
    limit_wall = ""
    # Frequency step or voltage trim waiting on a settled good-hashrate reading.
    # hash_ceiling is the last clock that still paid, after a higher clock
    # failed for hashrate. A request whose PLL did not move is not a ceiling:
    # pll_retry_frequency is the next request to try.
    probe = None
    hash_ceiling = None
    pll_retry_frequency = None
    # Frequency that already failed one probe because the 1-minute rate was
    # zero while live hashrate was still up. A second miss locks the ceiling.
    minute_retry_frequency = None
    # A multi-step climb that failed is not a wall. Climbs under the clock it
    # tried go one step at a time.
    single_step_until = None
    blocked_frequency = None
    blocked_voltage = None
    blocked_needs_cool = False
    blocked_for_rejects = False
    reject_sample = RejectSample()
    if pending is not None and start_frequency < requested_frequency:
        opening_wall = wall_type_from_reason(opening_reason)
        # Every opening that lowers the clocks names a wall.
        if opening_wall:  # pragma: no branch
            limit_wall = opening_wall
    error_samples = []
    window_positive_hash = False
    window_zero_hash = False
    zero_restarted = False
    error_restarted = False
    error_restart_noted = False
    # ASIC temperature and time when the current hashrate or silicon wall was hit.
    wall_temp = None
    wall_since = None
    flatline_restarted = False
    flatline_since = None
    flatline_live = None
    pool_down_logged = False
    # An opening heat drop has to latch the cool-down. Otherwise the next
    # tune interval climbs again while the chip is still inside the band.
    thermal_hold = wall_type_from_reason(opening_reason) == "thermal"
    safety_hold = _safety_hold_kind(opening_reason)
    safety_settle_until = settle_until if _is_safety_retreat(opening_reason) else 0.0
    # Inside the trip margin a safety drop may repeat sooner than a full settle.
    trip_settle_until = (
        min(settle_until, now + TRIP_GUARD_SETTLE_SECONDS)
        if _is_safety_retreat(opening_reason)
        else 0.0
    )
    overheat_since = None
    overheat_recovered = False
    hardware_fault_restarted = False
    hardware_fault_noted = False
    asic_off_since = None
    asic_off_restarted = False
    fan_retry_at = 0.0
    setpoint_since = None
    last_accepted = None
    last_rejected = None
    last_reasons = None

    def _restart_on_floor(message):
        """Restart the miner after putting clocks under the floor back on it.

        A restart replays the clocks AxeOS saved, so the floor is written first.
        The next decision waits out a fresh settle.
        """
        nonlocal pending, setpoint_since, settle_until, last_tune_time
        nonlocal last_accepted, last_rejected, last_reasons
        floor = floor_setpoint(
            confirmed[0], confirmed[1], limits["min_freq"], limits["min_volt"]
        )
        if not _same_setpoint(floor, confirmed):
            applied = set_system_settings(bitaxe_ip, floor[1], floor[0])
            log_callback(applied, "info")
            if settings_were_applied(applied):
                pending = floor
                setpoint_since = None
        log_callback(message, "error")
        log_callback(restart_bitaxe(bitaxe_ip), "warning")
        error_samples.clear()
        hashrate_history.clear()
        rolling_hashrate.clear()
        last_accepted = None
        last_rejected = None
        last_reasons = None
        reject_sample.reset()
        settle_until = time.time() + refresh_interval
        last_tune_time = time.time()

    _publish_status(bitaxe_ip, phase=phase, wall_type=limit_wall, reason="")

    while not event.is_set():
        try:
            now = time.time()
            if now - last_config_refresh > 5:
                runtime = load_config()
                record = _miner_record(bitaxe_ip)
                min_input_voltage = _record_float(
                    record, "min_input_voltage", board.default_min_input_voltage
                )
                max_error_percentage = _record_float(
                    record, "max_error_percentage", DEFAULT_MAX_ERROR_PERCENTAGE
                )
                max_droop_mv = _record_float(
                    record, "max_droop_mv", DEFAULT_MAX_DROOP_MV
                )
                max_core_amps = core_amps_cap(record, board)
                limits = refresh_running_limits(limits, record, board)
                last_config_refresh = now

            voltage_step = _positive_int(runtime.get("voltage_step"), 10)
            frequency_step = _positive_int(runtime.get("frequency_step"), 5)
            temp_tolerance = _non_negative_float(runtime.get("temp_tolerance"), 3)
            vr_temp_tolerance = _non_negative_float(runtime.get("vr_temp_tolerance"), 3)
            interval = _non_negative_float(runtime.get("monitor_interval", interval), 5)
            refresh_interval = _non_negative_float(runtime.get("refresh_interval"), 180)
            max_climb_steps = _positive_int(
                runtime.get("max_climb_steps"), MAX_CLIMB_FREQUENCY_STEPS
            )
            flatline_repeat_count = runtime.get("flatline_hashrate_repeat_count", 5)
            flatline_enabled = runtime.get("flatline_detection_enabled", False)

            info = get_system_info(bitaxe_ip)
            if event.is_set():
                break
            if isinstance(info, str) or not isinstance(info, dict):
                replies.failed(info)
                if _wait(event, interval):
                    break
                continue
            replies.answered()

            if not fan_matches(info, _fan()) and time.time() >= fan_retry_at:
                _apply_fan(bitaxe_ip, _fan(), log_callback)
                fan_retry_at = time.time() + max(float(interval or 0), 0)

            reported_frequency = info.get("frequency")
            reported_voltage = info.get("coreVoltage")
            now = time.time()

            reported_pair = None
            if (
                _as_float(reported_frequency) is not None
                and _as_float(reported_voltage) is not None
            ):
                reported_pair = (
                    int(float(reported_frequency)),
                    int(float(reported_voltage)),
                )
            # The lowest clocks this session has written or seen echoed. A
            # retreat still in flight counts, so it is not mistaken for a trip.
            session_clocks = confirmed
            if confirmed is not None and pending is not None:
                session_clocks = (
                    min(int(confirmed[0]), int(pending[0])),
                    min(int(confirmed[1]), int(pending[1])),
                )
            overheat_now = _overheat_mode_set(info.get("overheat_mode"))
            if not overheat_now and firmware_lowered_clocks(
                session_clocks, reported_pair
            ):
                # AxeOS tripped on heat and saved clocks 100 MHz / 100 mV lower.
                # Start over from there in climb, cooled first, instead of
                # holding whatever the firmware left.
                log_callback(
                    f"{bitaxe_ip} -> Miner reports {reported_pair[0]} MHz / "
                    f"{reported_pair[1]} mV, under the {session_clocks[0]} MHz / "
                    f"{session_clocks[1]} mV this session set. "
                    "AxeOS overheat protection lowered the clocks.",
                    "warning",
                )
                limit_wall = "thermal"
                confirmed = reported_pair
                pending = None
                probe = None
                pll_retry_frequency = None
                minute_retry_frequency = None
                phase = "climb"
                trim_good_voltage = None
                trim_after_retreat = False
                thermal_hold = True
                droop_reference = None
                setpoint_since = now
                settle_until = now + refresh_interval
                last_tune_time = now
                hashrate_history.clear()
                rolling_hashrate.clear()
                error_samples.clear()
                window_positive_hash = False
                window_zero_hash = False
                last_accepted = None
                last_rejected = None
                last_reasons = None
                reject_sample.reset()
                _publish_status(
                    bitaxe_ip,
                    phase=phase,
                    wall_type=limit_wall,
                    reason="firmware overheat",
                )
                if _wait(event, interval):
                    break
                continue

            if overheat_now:
                if overheat_since is None:
                    overheat_since = now
            else:
                overheat_since = None
                overheat_recovered = False
            if not overheat_recovered and overheat_latched(
                info, overheat_since, now, limits["max_vr_temp"], board.has_vr_temp
            ):
                # AxeOS clears overheat_mode only when the ASIC comes back. At
                # its lowered clocks it may not, and a restart alone replays
                # them. Write the floor, clear the flag, then restart once.
                overheat_recovered = True
                base = (
                    reported_pair
                    or confirmed
                    or (
                        limits["min_freq"],
                        limits["min_volt"],
                    )
                )
                floor_frequency, floor_voltage = floor_setpoint(
                    base[0], base[1], limits["min_freq"], limits["min_volt"]
                )
                log_callback(
                    f"{bitaxe_ip} -> Overheat mode held for {OVERHEAT_LATCH_SECONDS}s "
                    f"with the ASIC off. Writing {floor_frequency} MHz / "
                    f"{floor_voltage} mV and restarting.",
                    "error",
                )
                applied_settings = set_system_settings(
                    bitaxe_ip, floor_voltage, floor_frequency
                )
                log_callback(applied_settings, "info")
                cleared, clear_error = patch_system(bitaxe_ip, {"overheat_mode": 0})
                if not cleared:
                    log_callback(
                        f"{bitaxe_ip} -> Could not clear overheat mode: {clear_error}",
                        "warning",
                    )
                log_callback(restart_bitaxe(bitaxe_ip), "warning")
                if settings_were_applied(applied_settings):
                    pending = (floor_frequency, floor_voltage)
                probe = None
                pll_retry_frequency = None
                minute_retry_frequency = None
                phase = "climb"
                trim_good_voltage = None
                trim_after_retreat = False
                thermal_hold = True
                limit_wall = "thermal"
                droop_reference = None
                setpoint_since = None
                settle_until = time.time() + refresh_interval
                last_tune_time = time.time()
                hashrate_history.clear()
                rolling_hashrate.clear()
                error_samples.clear()
                window_positive_hash = False
                window_zero_hash = False
                last_accepted = None
                last_rejected = None
                last_reasons = None
                reject_sample.reset()
                _publish_status(
                    bitaxe_ip,
                    phase=phase,
                    wall_type=limit_wall,
                    reason="overheat restart",
                )
                if _wait(event, interval):
                    break
                continue

            power_now = _as_float(info.get("power"))
            asic_off = (
                not overheat_now
                and not _overheat_mode_set(info.get("miningPaused"))
                and not info.get("hardware_fault")
                and _asic_off_power(power_now, asic_off_watts)
            )
            if not asic_off:
                asic_off_since = None
                if (
                    power_now is not None
                    and asic_off_watts is not None
                    and power_now > asic_off_watts
                ):
                    asic_off_restarted = False
            elif asic_off_since is None:
                asic_off_since = now
            elif (
                not asic_off_restarted
                and now - asic_off_since >= ASIC_OFF_RESTART_SECONDS
            ):
                # Nothing is mining and AxeOS is not saying why. Put clocks
                # under the floor back on it, then restart once. A pool that is
                # down also reads like this; the restart then changes nothing.
                asic_off_restarted = True
                base = reported_pair or confirmed
                if base is not None:
                    floor = floor_setpoint(
                        base[0], base[1], limits["min_freq"], limits["min_volt"]
                    )
                    if not _same_setpoint(floor, base):
                        applied_settings = set_system_settings(
                            bitaxe_ip, floor[1], floor[0]
                        )
                        log_callback(applied_settings, "info")
                        if settings_were_applied(applied_settings):
                            pending = floor
                            setpoint_since = None
                log_callback(
                    f"{bitaxe_ip} -> The ASIC has been off for "
                    f"{int(now - asic_off_since)}s without overheat mode or a "
                    "pause. Restarting...",
                    "error",
                )
                log_callback(restart_bitaxe(bitaxe_ip), "warning")
                settle_until = time.time() + refresh_interval
                last_tune_time = time.time()
                _publish_status(
                    bitaxe_ip,
                    phase=phase,
                    wall_type=limit_wall,
                    reason="asic off restart",
                )
                if _wait(event, interval):
                    break
                continue

            hardware_fault = str(info.get("hardware_fault") or "").strip()
            if hardware_fault:
                # AxeOS v2.15.1 sets this when it cannot drive the fan. It stops
                # mining and only a reboot clears it. Restart once; if the fault
                # comes back, leave the miner stopped and say so.
                if not hardware_fault_restarted:
                    hardware_fault_restarted = True
                    hardware_fault_noted = False
                    log_callback(
                        f"{bitaxe_ip} -> AxeOS reports a hardware fault "
                        f"({hardware_fault}) and stopped mining. Restarting...",
                        "error",
                    )
                    log_callback(restart_bitaxe(bitaxe_ip), "warning")
                    settle_until = time.time() + refresh_interval
                    last_tune_time = time.time()
                elif not hardware_fault_noted:
                    hardware_fault_noted = True
                    log_callback(
                        f"{bitaxe_ip} -> Hardware fault ({hardware_fault}) is back "
                        "after a restart. Check the fan and its cable.",
                        "error",
                    )
                _publish_status(
                    bitaxe_ip,
                    phase=phase,
                    wall_type=limit_wall,
                    reason="hardware fault",
                )
                if _wait(event, interval):
                    break
                continue
            if hardware_fault_restarted and not board_hashrate_is_dead(info):
                hardware_fault_restarted = False
                hardware_fault_noted = False

            if (
                pending is not None
                and _numbers_match(reported_frequency, pending[0])
                and _numbers_match(reported_voltage, pending[1])
            ):
                confirmed = pending
                pending = None
                if _restore_is_confirmed(probe, confirmed):
                    probe = None
                # Every write clears setpoint_since, so it is always unset here.
                if setpoint_since is None:  # pragma: no branch
                    setpoint_since = now
                pll_note = ""
                pll_at_confirm = rounded_pll_frequency(info.get("actualFrequency"))
                if pll_at_confirm is not None and pll_at_confirm != confirmed[0]:
                    pll_note = f" PLL is {pll_at_confirm} MHz."
                log_callback(
                    f"{bitaxe_ip} -> Confirmed {confirmed[0]} MHz / {confirmed[1]} mV.{pll_note}",
                    "success",
                )
            elif pending is not None and now >= settle_until:
                log_callback(
                    f"{bitaxe_ip} -> Settings were not confirmed by the miner. Keeping the reported setpoint.",
                    "warning",
                )
                if (
                    _as_float(reported_frequency) is not None
                    and _as_float(reported_voltage) is not None
                ):
                    confirmed = (
                        int(float(reported_frequency)),
                        int(float(reported_voltage)),
                    )
                    # Every write clears setpoint_since, so it is always unset here.
                    if setpoint_since is None:  # pragma: no branch
                        setpoint_since = now
                pending = None
            elif (
                confirmed is None
                and pending is None
                and _as_float(reported_frequency) is not None
                and _as_float(reported_voltage) is not None
            ):
                confirmed = (
                    int(float(reported_frequency)),
                    int(float(reported_voltage)),
                )
                # Nothing set setpoint_since before the first confirmed setpoint.
                if setpoint_since is None:  # pragma: no branch
                    setpoint_since = now
                log_callback(
                    f"{bitaxe_ip} -> Using reported setpoint {confirmed[0]} MHz / {confirmed[1]} mV.",
                    "info",
                )

            temp = asic_temp(info)
            vr_temp = info.get("vrTemp") if "vrTemp" in info else None
            live_hash = _as_float(info.get("hashRate"))
            minute_hash = _as_float(info.get("hashRate_1m"))
            # Tune on the 1-minute rate when the miner reports one. Flatline watches
            # that same rate. A live sample that sits still is not a hang.
            decision_hash = (
                minute_hash
                if minute_hash is not None and minute_hash > 0
                else live_hash
            )
            power = info.get("power") if "power" in info else None
            settling = pending is not None or now < settle_until
            error_percentage = (
                _as_float(info.get("errorPercentage"))
                if _reports_error_percentage(info)
                else None
            )

            if pending is None and confirmed is not None:
                if error_percentage is not None:
                    error_samples.append(error_percentage)
                if live_hash is not None or minute_hash is not None:
                    positive_hash = (live_hash is not None and live_hash > 0) or (
                        minute_hash is not None and minute_hash > 0
                    )
                    if positive_hash:
                        window_positive_hash = True
                        zero_restarted = False
                    else:
                        window_zero_hash = True

            flatline_live_moved = False
            # An open trial owns this window. A steady minute rate must not
            # reboot the miner while that trial is still waiting to be judged.
            # hashRate_1m is a one-minute average, so a few identical polls are
            # a normal reading. A moving live rate is not a hang either. Both
            # have to sit still for a full minute before a restart.
            if (
                not settling
                and probe is None
                and minute_hash is not None
                and minute_hash > 0
            ):
                sample = round(minute_hash, 2)
                live_sample = (
                    round(live_hash, 2)
                    if live_hash is not None and live_hash > 0
                    else None
                )
                if hashrate_history and sample != hashrate_history[-1]:
                    flatline_restarted = False
                    flatline_since = time.time()
                    flatline_live = live_sample
                elif not hashrate_history:
                    flatline_since = time.time()
                    flatline_live = live_sample
                elif live_sample is not None and live_sample != flatline_live:
                    flatline_since = time.time()
                    flatline_live = live_sample
                    flatline_live_moved = True
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
                and flatline_since is not None
                and not flatline_live_moved
                and (time.time() - flatline_since) >= FLATLINE_STILL_SECONDS
            ):
                frozen_rate = hashrate_history[-1]
                hashrate_history.clear()
                flatline_since = None
                flatline_live = None
                rolling_hashrate.clear()
                error_samples.clear()
                window_positive_hash = False
                window_zero_hash = False
                last_accepted = None
                last_rejected = None
                last_reasons = None
                reject_sample.reset()
                settle_until = time.time() + refresh_interval
                last_tune_time = time.time()
                if not flatline_restarted:
                    flatline_restarted = True
                    log_callback(
                        f"{bitaxe_ip} -> Flatline detected ({frozen_rate} GH/s). Restarting...",
                        "error",
                    )
                    log_callback(restart_bitaxe(bitaxe_ip), "warning")
                else:
                    log_callback(
                        f"{bitaxe_ip} -> Hashrate still flat after restart. Holding.",
                        "error",
                    )
                if _wait(event, interval):
                    break
                continue

            # The table shows each sample. Log phase changes, limit hits, restarts, and errors.
            expected_hashrate = expected_hashrate_from_info(
                info, confirmed[0] if confirmed else start_frequency
            )
            _publish_status(
                bitaxe_ip,
                phase=phase,
                error_percentage=error_percentage,
                wall_type=limit_wall,
            )

            reported_frequency_number = _as_float(reported_frequency)
            reported_voltage_number = _as_float(reported_voltage)
            reported_clocks = None
            if (
                reported_frequency_number is not None
                and reported_voltage_number is not None
            ):
                reported_clocks = (
                    int(reported_frequency_number),
                    int(reported_voltage_number),
                )
            clocks_confirmed = pending is None and confirmed is not None
            # An unconfirmed write still steps down from the clocks the miner
            # is reporting. A climb keeps waiting until that write echoes.
            retreat_clocks = (
                reported_clocks
                if pending is not None and reported_clocks is not None
                else confirmed
            )
            if droop_reference is not None and now >= droop_grace_until:
                droop_reference = None
            retreat_voltage = None
            if retreat_clocks is not None:
                retreat_voltage = (
                    droop_reference
                    if droop_reference is not None
                    else retreat_clocks[1]
                )
            # A safety drop already written waits out its settle. The next hot
            # sample must not shed another step before the heatsink has moved.
            # Inside the trip margin the next drop only waits a short settle.
            retreat_open = now >= safety_settle_until or (
                near_firmware_trip(_as_float(temp), _as_float(vr_temp))
                and now >= trip_settle_until
            )
            immediate_retreat = retreat_open and _needs_immediate_retreat(
                _as_float(temp),
                _as_float(vr_temp),
                _as_float(power),
                limits["max_temp"],
                limits["max_vr_temp"],
                limits["max_watts"],
                normalize_input_voltage(info.get("voltage")),
                min_input_voltage,
                _as_float(info.get("coreVoltageActual")),
                retreat_voltage,
                max_droop_mv,
                info.get("power_fault"),
                info.get("overheat_mode"),
                core_current_amps(info, board),
                max_core_amps,
                asic_off_watts,
            )
            if retreat_clocks is None:
                immediate_retreat = False
            if (
                immediate_retreat
                and pending is not None
                and reported_clocks is not None
            ):
                confirmed = reported_clocks
            if not immediate_retreat and (not clocks_confirmed or settling):
                if _wait(event, interval):
                    break
                continue

            if now - last_tune_time < refresh_interval and not immediate_retreat:
                if _wait(event, interval):
                    break
                continue

            accepted_now = _as_float(info.get("sharesAccepted"))
            rejected_now = _as_float(info.get("sharesRejected"))
            reasons_now = info.get("sharesRejectedReasons")
            shares_delta = 0
            share = None
            above_target_high = False
            if (
                last_accepted is not None
                and last_rejected is not None
                and accepted_now is not None
                and rejected_now is not None
                and accepted_now >= last_accepted
                and rejected_now >= last_rejected
            ):
                shares_delta = rejected_now - last_rejected
                classes = reject_class_deltas(last_reasons, reasons_now)
                reject_sample.add(
                    accepted_now - last_accepted,
                    classes["hardware"],
                    classes["above"],
                )
                share, above_target_high = reject_sample.judge(
                    MIN_JUDGED_SHARES,
                    DEFAULT_MAX_REJECT_SHARE,
                )
            if accepted_now is not None:
                last_accepted = accepted_now
            if rejected_now is not None:
                last_rejected = rejected_now
            if isinstance(reasons_now, list):
                last_reasons = reasons_now

            window_error = average_error(error_samples)
            if window_error is not None:
                error_percentage = window_error
            error_samples.clear()
            # The latest sample wins. An earlier positive rate in this window
            # must not keep a dead board looking healthy.
            if board_hashrate_is_dead(info):
                average_hashrate = 0
            elif window_zero_hash and not window_positive_hash:
                average_hashrate = 0
            else:
                average_hashrate = (
                    (sum(rolling_hashrate) / len(rolling_hashrate))
                    if rolling_hashrate
                    else None
                )
            window_zero_hash = False
            window_positive_hash = False

            hashrate_short = False
            if setpoint_since is not None:
                settled_for = time.time() - setpoint_since
                if settled_for >= HASHRATE_10M_SETTLE_SECONDS:
                    hashrate_short = hashrate_well_below_expected(
                        info.get("hashRate_10m"),
                        expected_hashrate,
                        board.asic.self_test_ratio,
                    )
                elif settled_for >= HASHRATE_1M_SETTLE_SECONDS:
                    hashrate_short = hashrate_well_below_expected(
                        info.get("hashRate_1m"),
                        expected_hashrate,
                        board.asic.self_test_ratio,
                    )
            if pool_is_down(info):
                hashrate_short = False

            error_budget = max_error_percentage
            if (
                probe is None
                and error_percentage is not None
                and error_percentage >= error_restart_limit(error_budget)
                and not pool_is_down(info)
            ):
                if not error_restarted:
                    error_restarted = True
                    error_restart_noted = False
                    _restart_on_floor(
                        f"{bitaxe_ip} -> Errors averaged {error_percentage:.1f}% "
                        "after settle. That is not a silicon wall. Restarting..."
                    )
                    _publish_status(
                        bitaxe_ip,
                        phase=phase,
                        wall_type=limit_wall,
                        error_percentage=error_percentage,
                        reason="high error restart",
                    )
                    if _wait(event, interval):
                        break
                    continue
                if not error_restart_noted:
                    error_restart_noted = True
                    log_callback(
                        f"{bitaxe_ip} -> Errors still {error_percentage:.1f}% after "
                        "restart. Tuning continues.",
                        "error",
                    )
            elif error_percentage is not None and error_percentage <= error_budget:
                error_restarted = False
                error_restart_noted = False
            error_ok = (
                error_percentage is not None
                and error_percentage <= error_budget
                and not hashrate_short
            )
            if (
                trim_after_retreat
                and error_ok
                and probe is None
                and confirmed is not None
            ):
                # The retreated clock is healthy. Trim can lower voltage now.
                # Doing this while errors are still high would raise voltage.
                if phase == "hold":
                    phase = "trim"
                trim_after_retreat = False
            if phase == "trim" and error_ok and probe is None:
                trim_good_voltage = confirmed[1]

            cooled = cooled_after_retreat(
                _as_float(temp),
                _as_float(vr_temp),
                limits["max_temp"],
                limits["max_vr_temp"],
                temp_tolerance,
                vr_temp_tolerance,
            )
            if thermal_hold and cooled:
                thermal_hold = False
            if safety_hold and safety_hold_cleared(
                _as_float(power),
                limits["max_watts"],
                normalize_input_voltage(info.get("voltage")),
                min_input_voltage,
                _as_float(info.get("coreVoltageActual")),
                droop_reference if droop_reference is not None else confirmed[1],
                max_droop_mv,
                info.get("power_fault"),
                core_current_amps(info, board),
                max_core_amps,
            ):
                safety_hold = ""
            if frequency_block_cleared(
                blocked_frequency,
                confirmed[1],
                blocked_voltage,
                blocked_needs_cool,
                cooled,
                blocked_for_rejects,
                share,
            ):
                blocked_frequency = None
                blocked_voltage = None
                blocked_needs_cool = False
                blocked_for_rejects = False

            probe_ready = (
                probe is not None
                and not immediate_retreat
                and pending is None
                and confirmed is not None
                and confirmed[0] == probe["to_freq"]
                and confirmed[1] == probe["to_volt"]
                and now >= settle_until
            )
            # A dead board on an open trial steps back to the clocks it left.
            # The pool being down is not that failure: the trial stays open
            # and the zero-hashrate path holds instead of rebooting.
            # A 1-minute rate of 0 with live hashrate still up fails the trial.
            if probe_ready and board_hashrate_is_dead(info) and pool_is_down(info):
                probe_ready = False
            elif probe_ready and board_hashrate_is_dead(info):
                back_frequency = probe["from_freq"]
                back_voltage = probe["from_volt"]
                failed_frequency = probe["to_freq"]
                kind = probe.get("kind") or "frequency"
                jumped = bool(probe.get("jumped"))
                if kind == "trim":
                    log_callback(
                        f"{bitaxe_ip} -> {confirmed[1]} mV stopped hashing. "
                        f"Restoring {back_voltage} mV.",
                        "info",
                    )
                else:
                    single_note = " Climbing one step at a time." if jumped else ""
                    log_callback(
                        f"{bitaxe_ip} -> {failed_frequency} MHz stopped hashing. "
                        f"Stepping back to {back_frequency} MHz.{single_note}",
                        "info",
                    )
                reverted = _same_setpoint((back_frequency, back_voltage), confirmed)
                # A probe always moves off the clocks it started from.
                if not reverted:  # pragma: no branch
                    applied_settings = set_system_settings(
                        bitaxe_ip, back_voltage, back_frequency
                    )
                    log_callback(applied_settings, "info")
                    last_tune_time = time.time()
                    reverted = settings_were_applied(applied_settings)
                    if reverted:
                        pending = (back_frequency, back_voltage)
                        setpoint_since = None
                        settle_until = time.time() + refresh_interval
                        hashrate_history.clear()
                        rolling_hashrate.clear()
                        error_samples.clear()
                        window_positive_hash = False
                        window_zero_hash = False
                    else:
                        log_callback(
                            f"{bitaxe_ip} -> Miner rejected the change. Setpoint left unchanged.",
                            "warning",
                        )
                if reverted:
                    if kind == "trim" and probe.get("wall"):
                        probe["restore"] = True
                        wall_trim_floor = (back_frequency, probe["to_volt"])
                        retreat_reason = "restore voltage"
                    elif kind == "trim":
                        # Keep the probe until the miner echoes the restore.
                        probe["restore"] = True
                        phase = "hold"
                        trim_good_voltage = back_voltage
                        retreat_reason = "restore voltage"
                    elif jumped:
                        probe = None
                        single_step_until = failed_frequency
                        minute_retry_frequency = None
                        pll_retry_frequency = None
                        retreat_reason = "retry single steps"
                    else:
                        probe = None
                        hash_ceiling = back_frequency
                        wall_temp = _usable_temp(temp)
                        wall_since = time.time()
                        minute_retry_frequency = None
                        pll_retry_frequency = None
                        limit_wall = wall_type_from_reason(
                            "step frequency down after good hashrate"
                        )
                        retreat_reason = "step frequency down after good hashrate"
                    _publish_status(
                        bitaxe_ip,
                        phase=phase,
                        wall_type=limit_wall,
                        error_percentage=error_percentage,
                        reason=retreat_reason,
                    )
                if _wait(event, interval):
                    break
                continue
            if probe_ready:
                current_good = good_hashrate(measured_hashrate(info), error_percentage)
                kind = probe.get("kind") or "frequency"
                band = expected_step_gain(frequency_step, board.hashrate_per_mhz)
                if mode.objective == EFFICIENCY:
                    baseline_good = _as_float(probe["baseline"])
                    paid = efficiency_step_paid(
                        probe.get("baseline_power"),
                        probe["baseline"],
                        _as_float(info.get("power")),
                        current_good,
                        band / baseline_good if baseline_good else 0,
                        None if kind == "trim" else probe.get("actual"),
                        None if kind == "trim" else info.get("actualFrequency"),
                    )
                elif kind == "trim":
                    paid = good_hashrate_held(probe["baseline"], current_good, band)
                else:
                    paid = frequency_step_paid(
                        probe["baseline"],
                        current_good,
                        probe.get("actual"),
                        info.get("actualFrequency"),
                        band,
                    )
                # A step that held its hashrate can still be under the self-test
                # fraction. That is not a keep. The 1-minute check is what makes
                # this true during a climb; the 10-minute rate is not settled yet.
                under_expected = paid is True and kind != "trim" and hashrate_short
                if under_expected:
                    paid = False
                paid_before_minute = paid
                minute_failed = minute_rate_failed(info)
                if minute_failed:
                    paid = False
                if paid is None:
                    log_callback(f"{bitaxe_ip} -> holding for good hashrate.", "info")
                    last_tune_time = time.time()
                    _publish_status(
                        bitaxe_ip,
                        phase=phase,
                        wall_type=limit_wall,
                        error_percentage=error_percentage,
                        reason="holding for good hashrate",
                    )
                    if _wait(event, interval):
                        break
                    continue
                if not paid:
                    back_frequency = probe["from_freq"]
                    back_voltage = probe["from_volt"]
                    failed_frequency = probe["to_freq"]
                    # A multi-step climb that misses steps back without raising
                    # voltage or locking a ceiling. Single steps find the wall.
                    jumped = bool(probe.get("jumped"))
                    baseline_clock = _as_float(probe.get("actual"))
                    actual_now = _as_float(info.get("actualFrequency"))
                    clock_stuck = (
                        kind != "trim"
                        and baseline_clock is not None
                        and actual_now is not None
                        and actual_now <= baseline_clock
                    )
                    can_raise_voltage = (
                        kind != "trim"
                        and mode.objective != EFFICIENCY
                        and not jumped
                        and not clock_stuck
                        and not thermal_hold
                        and not safety_hold
                        and _usable_temp(vr_temp) is not None
                        and confirmed[1] < limits["max_volt"]
                    )
                    retry_frequency = failed_frequency + frequency_step
                    pll_can_retry = (
                        clock_stuck and retry_frequency <= limits["max_freq"]
                    )
                    # A zero 1-minute rate with live hashrate still up is a bad
                    # sample, not a wall. The first one steps back and may be
                    # tried again. A real miss, or a second one, locks the ceiling.
                    glitch_only = (
                        minute_failed
                        and paid_before_minute is not False
                        and not under_expected
                        and not clock_stuck
                    )
                    first_minute_retry = (
                        glitch_only and minute_retry_frequency != failed_frequency
                    )
                    short_text = (
                        f"under {round(board.asic.self_test_ratio * 100)}% of expected"
                    )
                    lower_frequency = (
                        probe["to_freq"]
                        - EFFICIENCY_TRIM_FREQUENCY_STEPS * frequency_step
                    )
                    can_lower_clock = (
                        kind == "trim"
                        and mode.objective == EFFICIENCY
                        and not probe.get("lowered")
                        and lower_frequency >= limits["min_freq"]
                    )
                    if can_lower_clock:
                        log_callback(
                            f"{bitaxe_ip} -> {confirmed[1]} mV cost energy per hash "
                            f"at {confirmed[0]} MHz. Trying {lower_frequency} MHz "
                            f"/ {confirmed[1]} mV.",
                            "info",
                        )
                        applied_settings = set_system_settings(
                            bitaxe_ip, confirmed[1], lower_frequency
                        )
                        log_callback(applied_settings, "info")
                        last_tune_time = time.time()
                        if settings_were_applied(applied_settings):
                            # Judged against the clocks the trim started from.
                            probe = dict(probe, to_freq=lower_frequency, lowered=True)
                            pending = (lower_frequency, confirmed[1])
                            setpoint_since = None
                            settle_until = time.time() + refresh_interval
                            hashrate_history.clear()
                            rolling_hashrate.clear()
                            error_samples.clear()
                        else:
                            log_callback(
                                f"{bitaxe_ip} -> Miner rejected the change. Setpoint left unchanged.",
                                "warning",
                            )
                        _publish_status(
                            bitaxe_ip,
                            phase=phase,
                            wall_type=limit_wall,
                            error_percentage=error_percentage,
                            reason="trim voltage",
                        )
                        if _wait(event, interval):
                            break
                        continue
                    if can_raise_voltage:
                        raised_voltage = min(
                            limits["max_volt"], confirmed[1] + voltage_step
                        )
                        if glitch_only:
                            miss = "had no 1-minute rate"
                        elif under_expected:
                            miss = short_text
                        else:
                            miss = "lost good hashrate"
                        log_callback(
                            f"{bitaxe_ip} -> {failed_frequency} MHz {miss}. "
                            f"Raising voltage to {raised_voltage} mV and retrying.",
                            "info",
                        )
                        applied_settings = set_system_settings(
                            bitaxe_ip, raised_voltage, failed_frequency
                        )
                        log_callback(applied_settings, "info")
                        last_tune_time = time.time()
                        if settings_were_applied(applied_settings):
                            _arm_droop_grace(confirmed[1], raised_voltage)
                            probe = dict(probe)
                            probe["to_volt"] = raised_voltage
                            pending = (failed_frequency, raised_voltage)
                            setpoint_since = None
                            settle_until = time.time() + refresh_interval
                            hashrate_history.clear()
                            rolling_hashrate.clear()
                            error_samples.clear()
                        else:
                            log_callback(
                                f"{bitaxe_ip} -> Miner rejected the change. Setpoint left unchanged.",
                                "warning",
                            )
                        _publish_status(
                            bitaxe_ip,
                            phase=phase,
                            wall_type=limit_wall,
                            error_percentage=error_percentage,
                            reason="increase voltage",
                        )
                        if _wait(event, interval):
                            break
                        continue
                    if kind == "trim":
                        lost = (
                            "cost energy per hash"
                            if mode.objective == EFFICIENCY
                            else "lowered good hashrate"
                        )
                        log_callback(
                            f"{bitaxe_ip} -> {confirmed[1]} mV {lost}. "
                            f"Restoring {back_voltage} mV.",
                            "info",
                        )
                    elif jumped:
                        if glitch_only:
                            miss = "had no 1-minute rate"
                        elif under_expected:
                            miss = short_text
                        else:
                            miss = "did not hold good hashrate"
                        log_callback(
                            f"{bitaxe_ip} -> {failed_frequency} MHz {miss}. "
                            f"Stepping back to {back_frequency} MHz and climbing "
                            "one step at a time.",
                            "info",
                        )
                    elif pll_can_retry:
                        log_callback(
                            f"{bitaxe_ip} -> {failed_frequency} MHz did not move the PLL "
                            f"({actual_now:g} MHz). Next request is {retry_frequency} MHz.",
                            "info",
                        )
                    elif first_minute_retry:
                        log_callback(
                            f"{bitaxe_ip} -> {failed_frequency} MHz had no 1-minute rate. "
                            f"Stepping back to {back_frequency} MHz.",
                            "info",
                        )
                    else:
                        clock_note = (
                            f" PLL stayed at {actual_now:g} MHz." if clock_stuck else ""
                        )
                        miss = (
                            short_text
                            if under_expected
                            else "did not hold good hashrate"
                        )
                        log_callback(
                            f"{bitaxe_ip} -> {failed_frequency} MHz {miss}. "
                            f"Stepping back to {back_frequency} MHz.{clock_note}",
                            "info",
                        )
                    reverted = _same_setpoint((back_frequency, back_voltage), confirmed)
                    # A probe always moves off the clocks it started from.
                    if not reverted:  # pragma: no branch
                        applied_settings = set_system_settings(
                            bitaxe_ip, back_voltage, back_frequency
                        )
                        log_callback(applied_settings, "info")
                        last_tune_time = time.time()
                        reverted = settings_were_applied(applied_settings)
                        if reverted:
                            pending = (back_frequency, back_voltage)
                            setpoint_since = None
                            settle_until = time.time() + refresh_interval
                            hashrate_history.clear()
                            rolling_hashrate.clear()
                            error_samples.clear()
                        else:
                            log_callback(
                                f"{bitaxe_ip} -> Miner rejected the change. Setpoint left unchanged.",
                                "warning",
                            )
                    if reverted:
                        if kind == "trim" and probe.get("wall"):
                            # Keep the probe until the miner echoes the restore.
                            # The climb goes on; this voltage stays out at this clock.
                            probe["restore"] = True
                            wall_trim_floor = (back_frequency, probe["to_volt"])
                            retreat_reason = "restore voltage"
                        elif kind == "trim":
                            # Keep the probe until the miner echoes the restore.
                            probe["restore"] = True
                            phase = "hold"
                            trim_good_voltage = back_voltage
                            log_callback(
                                f"{bitaxe_ip} -> Holding {back_frequency} MHz / {back_voltage} mV.",
                                "success",
                            )
                            retreat_reason = "restore voltage"
                        else:
                            probe = None
                            if jumped:
                                single_step_until = failed_frequency
                                minute_retry_frequency = None
                                pll_retry_frequency = None
                                retreat_reason = "retry single steps"
                            elif pll_can_retry:
                                pll_retry_frequency = retry_frequency
                                retreat_reason = "retry frequency"
                            elif first_minute_retry:
                                minute_retry_frequency = failed_frequency
                                pll_retry_frequency = None
                                retreat_reason = "retry minute rate"
                            else:
                                hash_ceiling = back_frequency
                                wall_temp = _usable_temp(temp)
                                wall_since = time.time()
                                minute_retry_frequency = None
                                pll_retry_frequency = None
                                limit_wall = wall_type_from_reason(
                                    "step frequency down after good hashrate"
                                )
                                retreat_reason = (
                                    "step frequency down after good hashrate"
                                )
                        _publish_status(
                            bitaxe_ip,
                            phase=phase,
                            wall_type=limit_wall,
                            error_percentage=error_percentage,
                            reason=retreat_reason,
                        )
                    if _wait(event, interval):
                        break
                    continue
                held = (
                    "Energy per hash held"
                    if mode.objective == EFFICIENCY
                    else "Good hashrate held"
                )
                if kind == "trim":
                    trim_good_voltage = confirmed[1]
                    log_callback(
                        f"{bitaxe_ip} -> Kept {confirmed[1]} mV. {held}.",
                        "success",
                    )
                else:
                    log_callback(
                        f"{bitaxe_ip} -> Kept {confirmed[0]} MHz. {held}.",
                        "success",
                    )
                probe = None
                pll_retry_frequency = None
                minute_retry_frequency = None

            if (
                phase == "hold"
                and probe is None
                and pending is None
                and (hash_ceiling is not None or blocked_frequency is not None)
                and not thermal_hold
                and not safety_hold
                and not hashrate_short
                and hold_may_reclimb(
                    temp,
                    wall_temp,
                    wall_since,
                    now,
                    limits["max_temp"],
                    temp_tolerance,
                    error_percentage,
                    error_budget,
                )
            ):
                # The wall may have been heat, not silicon. Try again from here.
                wall_frequency = (
                    blocked_frequency if blocked_frequency is not None else hash_ceiling
                )
                log_callback(
                    f"{bitaxe_ip} -> Errors and heat have room under the "
                    f"{wall_frequency} MHz wall. Climbing again.",
                    "info",
                )
                hash_ceiling = None
                blocked_frequency = None
                blocked_voltage = None
                blocked_needs_cool = False
                blocked_for_rejects = False
                wall_temp = None
                wall_since = None
                phase = "climb"
                trim_good_voltage = None
                trim_after_retreat = False
                _publish_status(
                    bitaxe_ip,
                    phase=phase,
                    wall_type=limit_wall,
                    error_percentage=error_percentage,
                    reason="climb again",
                )

            if (
                probe is None
                and pending is None
                and floor_hold_should_climb(
                    phase,
                    confirmed[0],
                    confirmed[1],
                    limits["min_freq"],
                    limits["max_freq"],
                    limits["max_volt"],
                    thermal_hold,
                    safety_hold,
                    cooled,
                )
            ):
                # A wall at the floor leaves nowhere to hold. Drop it and climb,
                # so errors raise voltage instead of holding the slowest clock.
                log_callback(
                    f"{bitaxe_ip} -> Cool at the {limits['min_freq']} MHz floor. "
                    "Climbing again.",
                    "info",
                )
                hash_ceiling = None
                blocked_frequency = None
                blocked_voltage = None
                blocked_needs_cool = False
                blocked_for_rejects = False
                wall_temp = None
                wall_since = None
                phase = "climb"
                trim_good_voltage = None
                trim_after_retreat = False
                _publish_status(
                    bitaxe_ip,
                    phase=phase,
                    wall_type=limit_wall,
                    error_percentage=error_percentage,
                    reason="climb again",
                )

            climb_cap = limits["max_freq"]
            if hash_ceiling is not None:
                climb_cap = min(climb_cap, hash_ceiling)
            decision = {
                "min_freq": limits["min_freq"],
                "max_freq": climb_cap,
                "min_volt": limits["min_volt"],
                "max_volt": limits["max_volt"],
                "max_temp": limits["max_temp"],
                "max_watts": limits["max_watts"],
                "max_vr_temp": limits["max_vr_temp"],
                "temp": _as_float(temp),
                "vr_temp": _as_float(vr_temp),
                "power": _as_float(power),
                "hash_rate": average_hashrate,
                "expected_hashrate": expected_hashrate,
                "shares_rejected_delta": shares_delta,
                "overheat_mode": info.get("overheat_mode"),
                "frequency_step": frequency_step,
                "voltage_step": voltage_step,
                "temp_tolerance": temp_tolerance,
                "error_percentage": error_percentage,
                "input_voltage": normalize_input_voltage(info.get("voltage")),
                "min_input_voltage": min_input_voltage,
                "core_voltage_actual": _as_float(info.get("coreVoltageActual")),
                "max_droop_mv": max_droop_mv,
                "core_current": core_current_amps(info, board),
                "max_core_amps": max_core_amps,
                "asic_off_watts": asic_off_watts,
                "vr_temp_required": board.has_vr_temp,
                "objective": mode.objective,
                "trim_floor_voltage": (
                    wall_trim_floor[1]
                    if wall_trim_floor is not None
                    and confirmed[0] >= wall_trim_floor[0]
                    else None
                ),
                "droop_voltage": droop_reference,
                "power_fault": info.get("power_fault"),
                "phase": phase,
                "trim_good_voltage": trim_good_voltage,
                "max_error_percentage": max_error_percentage,
                "vr_temp_tolerance": vr_temp_tolerance,
                "thermal_hold": thermal_hold,
                "safety_hold": safety_hold,
                "reject_share": share,
                "hashrate_short": hashrate_short,
                "blocked_frequency": blocked_frequency,
                "above_target_high": above_target_high,
                "max_climb_steps": (
                    1
                    if single_step_until is not None
                    and confirmed[0] < single_step_until
                    else max_climb_steps
                ),
            }

            reported_frequency_number = _as_float(reported_frequency)
            reported_voltage_number = _as_float(reported_voltage)
            new_frequency, new_voltage, reason = decide_adjustment(
                confirmed[0], confirmed[1], **decision
            )
            report_frequency = (
                int(reported_frequency_number)
                if reported_frequency_number is not None
                else None
            )
            # The PLL judges whether the last step moved. The next request stays
            # within the climb it chose above the last confirmed setpoint, or
            # follows the miner's own frequency field when that setpoint is stale.
            climb_jump = frequency_step
            if reason == "increase frequency":
                climb_jump = max(frequency_step, new_frequency - confirmed[0])
            follow_report = (
                report_frequency is not None
                and reported_voltage_number is not None
                and (
                    _proposal_jumps_above_report(
                        new_frequency,
                        new_voltage,
                        report_frequency,
                        int(reported_voltage_number),
                        climb_jump,
                        voltage_step,
                    )
                    or (
                        new_frequency < confirmed[0]
                        and report_frequency < confirmed[0]
                        and new_frequency >= report_frequency
                    )
                )
            )
            if follow_report:
                confirmed = (report_frequency, int(reported_voltage_number))
                log_callback(
                    f"{bitaxe_ip} -> Reported {confirmed[0]} MHz / {confirmed[1]} mV. "
                    "Following the miner instead of jumping.",
                    "warning",
                )
                new_frequency, new_voltage, reason = decide_adjustment(
                    confirmed[0], confirmed[1], **decision
                )
                # A floor restore lands on the floor in one write.
                if reason != "restore floor":
                    if new_frequency > confirmed[0] + frequency_step:
                        new_frequency = confirmed[0] + frequency_step
                    if new_voltage > confirmed[1] + voltage_step:
                        new_voltage = confirmed[1] + voltage_step

            if reason in ("increase voltage", "trim voltage", "restore voltage"):
                new_frequency = confirmed[0]
            climb_jumped = False
            if (
                pll_retry_frequency is not None
                and reason == "increase frequency"
                and confirmed[0] < pll_retry_frequency <= climb_cap
                and new_frequency < pll_retry_frequency
            ):
                new_frequency = pll_retry_frequency
            elif reason == "increase frequency":
                new_frequency = min(
                    new_frequency, confirmed[0] + frequency_step * max_climb_steps
                )
                climb_jumped = new_frequency > confirmed[0] + frequency_step
            # decide_adjustment only climbs above the clocks it is given.
            if (
                reason == "increase frequency" and new_frequency < confirmed[0]
            ):  # pragma: no cover
                new_frequency = confirmed[0]
                new_voltage = confirmed[1]
                reason = "holding"

            if (
                reason == "increase frequency"
                and good_hashrate(measured_hashrate(info), error_percentage) is None
            ):
                new_frequency = confirmed[0]
                new_voltage = confirmed[1]
                reason = "holding for good hashrate"

            if reason in ("increase frequency", "increase voltage") and not (
                fan_matches(info, _fan())
            ):
                new_frequency = confirmed[0]
                new_voltage = confirmed[1]
                reason = "holding for fan"

            if reason == "holding at zero hashrate" and pool_is_down(info):
                if not pool_down_logged:
                    log_callback(
                        f"{bitaxe_ip} -> Hashrate is 0 GH/s and the pool is down. Holding.",
                        "warning",
                    )
                    pool_down_logged = True
                _publish_status(
                    bitaxe_ip,
                    phase=phase,
                    wall_type=limit_wall,
                    error_percentage=error_percentage,
                    reason="holding for pool",
                )
                last_tune_time = time.time()
                if _wait(event, interval):
                    break
                continue
            if not (pool_is_down(info) and board_hashrate_is_dead(info)):
                pool_down_logged = False

            if reason == "holding at zero hashrate":
                _publish_status(
                    bitaxe_ip,
                    phase=phase,
                    wall_type=limit_wall,
                    error_percentage=error_percentage,
                    reason=reason,
                )
                if not zero_restarted:
                    _restart_on_floor(
                        f"{bitaxe_ip} -> Hashrate is 0 GH/s after settle. Restarting..."
                    )
                    zero_restarted = True
                else:
                    log_callback(
                        f"{bitaxe_ip} -> Hashrate still 0 GH/s after restart. Holding.",
                        "error",
                    )
                if _wait(event, interval):
                    break
                continue

            climb_cap = limits["max_freq"]
            if hash_ceiling is not None:
                climb_cap = min(climb_cap, hash_ceiling)
            # decide_adjustment already stops at climb_cap; this guards a change there.
            if (
                phase == "climb"
                and reason == "increase frequency"
                and confirmed[0] >= climb_cap
            ):  # pragma: no cover
                # The applied clock is already at the cap. A PLL reading a few MHz
                # under that cap must not keep the session in climb forever.
                reason = "frequency ceiling"
                new_frequency = confirmed[0]
                new_voltage = confirmed[1]

            if reason == "frequency ceiling" and phase == "climb":
                phase = "trim"
                trim_good_voltage = confirmed[1]
                log_callback(
                    f"{bitaxe_ip} -> Frequency ceiling. Trimming voltage.", "info"
                )
                _publish_status(bitaxe_ip, phase=phase, reason="frequency ceiling")
                if _wait(event, interval):
                    break
                continue

            if pending is not None and _same_setpoint(
                (new_frequency, new_voltage), pending
            ):
                # This retreat is already in flight. Wait until it echoes
                # instead of sending the same clocks on every poll.
                if _wait(event, interval):
                    break
                continue

            leaving_hold = phase in ("hold", "trim") and (
                reason.startswith("step frequency")
                or reason.startswith("step voltage")
                or "silicon wall" in reason
                or "above target" in reason
            )
            was_trimming = phase == "trim"
            was_holding_retreat = trim_after_retreat and phase == "hold"
            clocks_unchanged = _same_setpoint((new_frequency, new_voltage), confirmed)
            if reason == "trim complete" or (
                reason == "restore voltage" and clocks_unchanged
            ):
                # Both reasons only come from the trim phase.
                if phase != "hold":  # pragma: no branch
                    phase = "hold"
                    log_callback(
                        f"{bitaxe_ip} -> Holding {confirmed[0]} MHz / {new_voltage} mV.",
                        "success",
                    )
            elif leaving_hold:
                phase = "climb"
                trim_good_voltage = None
                if not _quality_frequency_retreat(reason):
                    trim_after_retreat = False

            wall = wall_type_from_reason(reason)
            if wall:
                limit_wall = wall
            if wall == "thermal":
                thermal_hold = True
            hold_kind = _safety_hold_kind(reason)
            if hold_kind:
                safety_hold = hold_kind
            # The dashboard names the reading that stopped the climb on the supply.
            wall_reading = {}
            if wall == "input":
                wall_reading = {
                    "wall_input_v": normalize_input_voltage(info.get("voltage")),
                    "wall_watts": _as_float(info.get("power")),
                }
            log_callback(f"{bitaxe_ip} -> {reason}.", "info")
            _publish_status(
                bitaxe_ip,
                phase=phase,
                wall_type=limit_wall,
                error_percentage=error_percentage,
                reason=reason,
                **wall_reading,
            )

            if not clocks_unchanged:
                applied_settings = set_system_settings(
                    bitaxe_ip, new_voltage, new_frequency
                )
                log_callback(applied_settings, "info")
                last_tune_time = time.time()
                if settings_were_applied(applied_settings):
                    _arm_droop_grace(confirmed[1], new_voltage)
                    if _is_safety_retreat(reason):
                        safety_settle_until = time.time() + refresh_interval
                        trip_settle_until = time.time() + min(
                            TRIP_GUARD_SETTLE_SECONDS, refresh_interval
                        )
                    if reason == "restore voltage":
                        phase = "hold"
                        trim_after_retreat = False
                        log_callback(
                            f"{bitaxe_ip} -> Holding {new_frequency} MHz / {new_voltage} mV.",
                            "success",
                        )
                    if new_frequency < confirmed[0] or (
                        pll_retry_frequency is not None
                        and new_frequency >= pll_retry_frequency
                    ):
                        pll_retry_frequency = None
                    if reason == "increase frequency":
                        probe = {
                            "kind": "frequency",
                            "jumped": climb_jumped,
                            "from_freq": confirmed[0],
                            "from_volt": confirmed[1],
                            "to_freq": new_frequency,
                            "to_volt": new_voltage,
                            "baseline": good_hashrate(
                                measured_hashrate(info), error_percentage
                            ),
                            "baseline_power": _as_float(info.get("power")),
                            "actual": _as_float(info.get("actualFrequency")),
                        }
                    elif reason == "trim voltage":
                        probe = {
                            "kind": "trim",
                            # A heat-wall trim (Efficiency) keeps climbing after.
                            "wall": phase == "climb",
                            "from_freq": confirmed[0],
                            "from_volt": confirmed[1],
                            "to_freq": new_frequency,
                            "to_volt": new_voltage,
                            "baseline": good_hashrate(
                                measured_hashrate(info), error_percentage
                            ),
                            "baseline_power": _as_float(info.get("power")),
                            "actual": None,
                        }
                    elif probe is not None:
                        probe = None
                    if new_frequency < confirmed[0] and _blocks_reclimb(reason):
                        blocked_frequency = confirmed[0]
                        blocked_voltage = confirmed[1]
                        wall_temp = _usable_temp(temp)
                        wall_since = time.time()
                        # Cool-down clears a thermal hold on its own. A silicon
                        # or reject block stays until voltage rises, and a
                        # reject block can also clear on a clean share sample.
                        blocked_needs_cool = False
                        blocked_for_rejects = (
                            "rejected shares" in (reason or "").lower()
                        )
                    if new_frequency < confirmed[0] and _quality_frequency_retreat(
                        reason
                    ):
                        trim_good_voltage = new_voltage
                        trim_after_retreat = True
                        if int(new_voltage) >= int(limits["max_volt"]):
                            phase = "trim"
                            if not was_trimming:
                                log_callback(
                                    f"{bitaxe_ip} -> Frequency retreat. Trimming voltage.",
                                    "info",
                                )
                                _publish_status(
                                    bitaxe_ip,
                                    phase=phase,
                                    wall_type=limit_wall,
                                    error_percentage=error_percentage,
                                    reason="frequency retreat",
                                )
                        else:
                            # Stay in hold so the next bad sample steps frequency
                            # again. Trim starts after this clock looks healthy.
                            phase = "hold"
                            if not was_holding_retreat:
                                log_callback(
                                    f"{bitaxe_ip} -> Frequency retreat. Holding voltage.",
                                    "info",
                                )
                                _publish_status(
                                    bitaxe_ip,
                                    phase=phase,
                                    wall_type=limit_wall,
                                    error_percentage=error_percentage,
                                    reason="frequency retreat",
                                )
                    pending = (new_frequency, new_voltage)
                    setpoint_since = None
                    settle_until = time.time() + refresh_interval
                    hashrate_history.clear()
                    rolling_hashrate.clear()
                    error_samples.clear()
                else:
                    log_callback(
                        f"{bitaxe_ip} -> Miner rejected the change. Setpoint left unchanged.",
                        "warning",
                    )
            else:
                # Clocks stayed put. Wait out another full settle before the next
                # error decision so one poll cannot walk the clocks down.
                last_tune_time = time.time()

            if _wait(event, interval):
                break

        except Exception as e:
            log_callback(f"{bitaxe_ip} -> UNCAUGHT ERROR: {str(e)}", "error")
            if _wait(event, interval or 5):
                break

    log_callback(f"{bitaxe_ip} -> Autotuning stopped.", "warning")
