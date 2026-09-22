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


def _reset_one_miner_to_baseline(miner, log_callback):
    """Write factory clocks for one miner and forget its learned setpoint."""
    ip = (miner or {}).get("ip")
    if not ip:
        return
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


def reset_miners_to_baseline(miners, log_callback, stagger_seconds=STARTUP_STAGGER_SECONDS, parallel=False):
    """Write factory clocks and forget the learned setpoint for each saved miner.

    A failed write is logged and does not skip clearing that miner or the ones after it.
    Min, max, temperature, and power limits are left as the user set them.
    parallel=True starts every write together and ignores the stagger.
    """
    miners = list(miners or [])
    if parallel:
        threads = []
        for miner in miners:
            thread = threading.Thread(
                target=_reset_one_miner_to_baseline,
                args=(miner, log_callback),
                daemon=True,
            )
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        return
    for index, miner in enumerate(miners):
        if index > 0 and stagger_seconds:
            time.sleep(stagger_seconds)
        _reset_one_miner_to_baseline(miner, log_callback)


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


_RUNNING_LIMIT_FIELDS = (
    "min_freq",
    "max_freq",
    "min_volt",
    "max_volt",
    "max_temp",
    "max_watts",
    "max_vr_temp",
)


def refresh_running_limits(limits, record):
    """Apply caps saved while a session is already running.

    A missing field keeps the value already in use. A reversed frequency or
    voltage pair is ignored so a bad edit cannot invert a live session.
    """
    if not isinstance(record, dict):
        return limits
    updated = dict(limits)
    changed = False
    for key in _RUNNING_LIMIT_FIELDS:
        value = coerce_limit(record.get(key))
        if value is None or value == updated.get(key):
            continue
        updated[key] = value
        changed = True
    if not changed:
        return limits
    if updated["min_freq"] > updated["max_freq"] or updated["min_volt"] > updated["max_volt"]:
        return limits
    return clamp_limits(updated)


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
    if "good hashrate" in text or "low hashrate" in text:
        return "hash"
    if "rejected shares" in text:
        return "reject"
    if "input sag" in text:
        return "input"
    if "silicon" in text or "above target" in text:
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


def good_hashrate(hash_rate, error_percentage):
    """Measured hashes that were not invalid ASIC jobs.

    None when the rate or the error percentage is missing. Error percentage is
    the share of invalid jobs, reported separately from hashrate.
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


# BM1370 self-test treats delivered hashrate under 85% of expected as a miss.
# hashRate_10m covers ten minutes, so it is only compared after the clocks have
# been sitting still for that long.
HASHRATE_SHORTFALL_RATIO = 0.85
HASHRATE_10M_SETTLE_SECONDS = 10 * 60

# Pool reject share that steps frequency down. Stale and non-silicon results are left out.
# One counted reject must not cross the limit, so a sample needs more than 100 judged shares.
DEFAULT_MAX_REJECT_SHARE = 0.01
MIN_JUDGED_SHARES = 100
ABOVE_TARGET_WINDOWS = 2
_STALE_REJECT_MARKERS = ("job not found", "stale")
_ABOVE_TARGET_MARKERS = ("above target", "low difficulty")
_HARDWARE_REJECT_MARKERS = ("invalid", "hardware")
_IGNORED_REJECT_MARKERS = ("duplicate", "ntime", "worker", "unauthorized", "mismatch")

# BM1370 expected hashrate is frequency * 2040 / 1000 GH/s. A 5 MHz step is 10.2 GH/s.
BM1370_HASHRATE_PER_MHZ = 2.04


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
    if any(marker in text for marker in _HARDWARE_REJECT_MARKERS):
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
        for value, name in ((accepted, "accepted"), (hardware, "hardware"), (above, "above")):
            number = _as_float(value)
            if number is not None and number > 0:
                setattr(self, name, getattr(self, name) + number)

    def judge(self, minimum=MIN_JUDGED_SHARES, limit=DEFAULT_MAX_REJECT_SHARE):
        """Return (hardware share or None, above-target is sustained).

        None means the sample is still too small. The counters reset once it qualifies.
        """
        judged = self.accepted + self.hardware + self.above
        if judged < minimum:
            return None, False
        hardware_total = self.accepted + self.hardware
        hardware_share = (self.hardware / hardware_total) if hardware_total > 0 else 0.0
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


def expected_step_gain(frequency_step):
    """GH/s a BM1370 should gain from this many MHz, before errors."""
    step = _as_float(frequency_step)
    if step is None or step <= 0:
        step = 1
    return step * BM1370_HASHRATE_PER_MHZ


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

    Any block clears once voltage rises or a hot retreat has cooled. A block
    that came from hardware rejects also clears after a later clean share sample.
    `share` is None until that sample is large enough to judge.
    """
    if blocked_frequency is None:
        return False
    if blocked_voltage is not None and voltage is not None and voltage > blocked_voltage:
        return True
    if needs_cool and cooled:
        return True
    judged = _as_float(share)
    reject_limit = DEFAULT_MAX_REJECT_SHARE if limit is None else float(limit)
    return bool(for_rejects) and judged is not None and judged <= reject_limit


def setpoint_to_remember(confirmed, probe, pending):
    """Clocks worth saving when tuning stops.

    An open probe has not proved its new clocks, so keep the clocks it left.
    A retreat that was written and not yet confirmed should not resume on the
    clocks just abandoned. Otherwise keep the last confirmed clocks.
    """
    if isinstance(probe, dict) and probe.get("from_freq") is not None and probe.get("from_volt") is not None:
        return int(probe["from_freq"]), int(probe["from_volt"])
    if pending is not None and confirmed is not None:
        pending_frequency = int(pending[0])
        pending_voltage = int(pending[1])
        confirmed_frequency = int(confirmed[0])
        confirmed_voltage = int(confirmed[1])
        if pending_frequency < confirmed_frequency or pending_voltage < confirmed_voltage:
            return pending_frequency, pending_voltage
    if confirmed is not None:
        return int(confirmed[0]), int(confirmed[1])
    return None


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


def cooled_after_retreat(temp, vr_temp, max_temp, max_vr_temp, temp_tolerance, vr_temp_tolerance):
    """True when both reported sensors are at least one tolerance band under their caps.

    A missing regulator reading does not keep the latch. The climb still waits
    for that sensor on its own.
    """
    temp_value = _as_float(temp)
    if temp_value is None:
        return False
    asic_band = temp_tolerance if temp_tolerance and temp_tolerance > 0 else 0
    if temp_value > (max_temp - asic_band):
        return False
    vr_value = _as_float(vr_temp)
    if vr_value is None:
        return True
    vr_band = vr_temp_tolerance if vr_temp_tolerance and vr_temp_tolerance > 0 else 0
    return vr_value <= (max_vr_temp - vr_band)


def frequency_step_paid(baseline_good, current_good, baseline_actual=None, current_actual=None, band=None):
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
    if baseline_clock is not None and current_clock is not None and current_clock <= baseline_clock:
        return False
    if band is None:
        band = expected_step_gain(5)
    return good_hashrate_held(baseline_good, current_good, band)


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
    thermal_hold=False,
    reject_share=None,
    max_reject_share=DEFAULT_MAX_REJECT_SHARE,
    hashrate_short=False,
    blocked_frequency=None,
    above_target_high=False,
):
    """Choose the next frequency and voltage.

    Returns (frequency, voltage, reason). Voltage is never raised while stepping down.
    Climb while both sensors are at or under their caps. `thermal_hold` is set after
    a thermal retreat and blocks the next climb until the caller clears it.
    `blocked_frequency` is a clock that just failed for errors or rejects. The climb
    stops short of it until the caller clears the block.
    A missing regulator temperature blocks climbs and voltage increases.
    `tier_list`, `expected_hashrate`, and `shares_rejected_delta` stay in the
    signature so older callers keep working.
    """
    del tier_list, expected_hashrate, shares_rejected_delta
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

    rate = _as_float(hash_rate)
    if rate is not None and rate <= 0:
        return current_frequency, current_voltage, "holding at zero hashrate"

    error = _as_float(error_percentage)
    error_budget = DEFAULT_MAX_ERROR_PERCENTAGE if max_error_percentage is None else float(max_error_percentage)
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
    reject_limit = DEFAULT_MAX_REJECT_SHARE if max_reject_share is None else float(max_reject_share)
    if not above_target_bad and share is not None and share > reject_limit:
        return _apply_step_down(
            current_frequency, current_voltage, min_freq, max_freq, min_volt, max_volt,
            frequency_step, voltage_step, "rejected shares"
        )

    if quality_bad:
        if phase == "trim" and trim_good_voltage is not None and current_voltage < int(trim_good_voltage):
            restored = _clamp(int(trim_good_voltage), min_volt, max_volt)
            return _guard_bounds(
                current_frequency, current_voltage, current_frequency, restored,
                min_freq, max_freq, min_volt, max_volt, "restore voltage"
            )
        if phase == "hold" or thermal_hold or current_voltage >= max_volt:
            return _apply_step_down(
                current_frequency, current_voltage, min_freq, max_freq, min_volt, max_volt,
                frequency_step, voltage_step, quality_tag
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
            frequency_step, voltage_step, quality_tag
        )

    if thermal_hold and phase == "climb":
        return current_frequency, current_voltage, "holding after thermal retreat"

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
    blocked = coerce_limit(blocked_frequency)
    if blocked is not None and new_frequency > current_frequency and new_frequency >= blocked:
        return current_frequency, current_voltage, "holding after frequency retreat"
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
        _publish_status(bitaxe_ip, phase="skipped", reason="missing settings")
        return
    if limits["min_freq"] > limits["max_freq"] or limits["min_volt"] > limits["max_volt"]:
        log_callback(f"{bitaxe_ip} -> AutoTuner limits are reversed. Skipping tuning.", "error")
        _publish_status(bitaxe_ip, phase="skipped", reason="limits reversed")
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
        _publish_status(bitaxe_ip, phase="skipped", reason="not a gamma 601")
        return
    if not _reports_error_percentage(info):
        log_callback(
            f"{bitaxe_ip} -> Skipping tuning. Firmware did not report errorPercentage.",
            "error",
        )
        _publish_status(bitaxe_ip, phase="skipped", reason="no error percentage")
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
    # Frequency step or voltage trim waiting on a settled good-hashrate reading.
    # hash_ceiling is the last clock that still paid, and only after the higher
    # clock failed at max voltage or while a thermal retreat is latched.
    probe = None
    hash_ceiling = None
    blocked_frequency = None
    blocked_voltage = None
    blocked_needs_cool = False
    blocked_for_rejects = False
    reject_sample = RejectSample()
    saved_signature = None
    error_samples = []
    window_positive_hash = False
    window_zero_hash = False
    zero_restarted = False
    thermal_hold = False
    setpoint_since = None
    last_accepted = None
    last_rejected = None
    last_reasons = None
    _publish_status(bitaxe_ip, phase=phase, wall_type=limit_wall, reason="")

    while not event.is_set():
        try:
            now = time.time()
            if now - last_config_refresh > 5:
                runtime = load_config()
                record = _miner_record(bitaxe_ip)
                min_input_voltage = _record_float(record, "min_input_voltage", DEFAULT_MIN_INPUT_VOLTAGE)
                max_error_percentage = _record_float(record, "max_error_percentage", DEFAULT_MAX_ERROR_PERCENTAGE)
                max_droop_mv = _record_float(record, "max_droop_mv", DEFAULT_MAX_DROOP_MV)
                limits = refresh_running_limits(limits, record)
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
                if setpoint_since is None:
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
                if _as_float(reported_frequency) is not None and _as_float(reported_voltage) is not None:
                    confirmed = (int(float(reported_frequency)), int(float(reported_voltage)))
                    if setpoint_since is None:
                        setpoint_since = now
                pending = None
            elif (
                confirmed is None
                and pending is None
                and _as_float(reported_frequency) is not None
                and _as_float(reported_voltage) is not None
            ):
                confirmed = (int(float(reported_frequency)), int(float(reported_voltage)))
                if setpoint_since is None:
                    setpoint_since = now
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

            if pending is None and confirmed is not None:
                if error_percentage is not None:
                    error_samples.append(error_percentage)
                if live_hash is not None or minute_hash is not None:
                    positive_hash = (
                        (live_hash is not None and live_hash > 0)
                        or (minute_hash is not None and minute_hash > 0)
                    )
                    if positive_hash:
                        window_positive_hash = True
                        zero_restarted = False
                    else:
                        window_zero_hash = True

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
                error_samples.clear()
                window_positive_hash = False
                window_zero_hash = False
                last_shares = None
                last_accepted = None
                last_rejected = None
                last_reasons = None
                reject_sample.reset()
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
                last_shares = rejected_now
            if isinstance(reasons_now, list):
                last_reasons = reasons_now

            window_error = average_error(error_samples)
            if window_error is not None:
                error_percentage = window_error
            error_samples.clear()
            if window_zero_hash and not window_positive_hash:
                average_hashrate = 0
            else:
                average_hashrate = (sum(rolling_hashrate) / len(rolling_hashrate)) if rolling_hashrate else None
            window_zero_hash = False
            window_positive_hash = False

            hashrate_short = False
            if setpoint_since is not None and (time.time() - setpoint_since) >= HASHRATE_10M_SETTLE_SECONDS:
                hashrate_short = hashrate_well_below_expected(info.get("hashRate_10m"), expected_hashrate)

            error_budget = max_error_percentage
            error_ok = (
                error_percentage is not None
                and error_percentage <= error_budget
                and not hashrate_short
            )
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
            if probe_ready:
                current_good = good_hashrate(measured_hashrate(info), error_percentage)
                kind = probe.get("kind") or "frequency"
                band = expected_step_gain(frequency_step)
                if kind == "trim":
                    paid = good_hashrate_held(probe["baseline"], current_good, band)
                else:
                    paid = frequency_step_paid(
                        probe["baseline"],
                        current_good,
                        probe.get("actual"),
                        info.get("actualFrequency"),
                        band,
                    )
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
                        and not clock_stuck
                        and not thermal_hold
                        and _as_float(vr_temp) is not None
                        and confirmed[1] < limits["max_volt"]
                    )
                    if can_raise_voltage:
                        raised_voltage = min(limits["max_volt"], confirmed[1] + voltage_step)
                        log_callback(
                            f"{bitaxe_ip} -> {failed_frequency} MHz lost good hashrate. "
                            f"Raising voltage to {raised_voltage} mV and retrying.",
                            "info",
                        )
                        applied_settings = set_system_settings(bitaxe_ip, raised_voltage, failed_frequency)
                        log_callback(applied_settings, "info")
                        last_tune_time = time.time()
                        if settings_were_applied(applied_settings):
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
                        log_callback(
                            f"{bitaxe_ip} -> {confirmed[1]} mV lowered good hashrate. "
                            f"Restoring {back_voltage} mV.",
                            "info",
                        )
                    else:
                        clock_note = f" PLL stayed at {actual_now:g} MHz." if clock_stuck else ""
                        log_callback(
                            f"{bitaxe_ip} -> {failed_frequency} MHz did not hold good hashrate. "
                            f"Stepping back to {back_frequency} MHz.{clock_note}",
                            "info",
                        )
                    reverted = _same_setpoint((back_frequency, back_voltage), confirmed)
                    if not reverted:
                        applied_settings = set_system_settings(bitaxe_ip, back_voltage, back_frequency)
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
                        probe = None
                        if kind == "trim":
                            phase = "hold"
                            hold_since = time.time()
                            ceiling_saved = False
                            trim_good_voltage = back_voltage
                            log_callback(
                                f"{bitaxe_ip} -> Holding {back_frequency} MHz / {back_voltage} mV.",
                                "success",
                            )
                        else:
                            hash_ceiling = back_frequency
                            limit_wall = wall_type_from_reason("step frequency down after good hashrate")
                        retreat_reason = (
                            "restore voltage" if kind == "trim" else "step frequency down after good hashrate"
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
                if kind == "trim":
                    trim_good_voltage = confirmed[1]
                    log_callback(
                        f"{bitaxe_ip} -> Kept {confirmed[1]} mV. Good hashrate held.",
                        "success",
                    )
                else:
                    log_callback(
                        f"{bitaxe_ip} -> Kept {confirmed[0]} MHz. Good hashrate held.",
                        "success",
                    )
                probe = None

            def choose(frequency, voltage):
                climb_cap = limits["max_freq"]
                if hash_ceiling is not None:
                    climb_cap = min(climb_cap, hash_ceiling)
                return decide_adjustment(
                    frequency,
                    voltage,
                    limits["min_freq"],
                    climb_cap,
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
                    thermal_hold=thermal_hold,
                    reject_share=share,
                    hashrate_short=hashrate_short,
                    blocked_frequency=blocked_frequency,
                    above_target_high=above_target_high,
                )

            pll_frequency = rounded_pll_frequency(info.get("actualFrequency"))
            decision_frequency = pll_frequency if pll_frequency is not None else confirmed[0]
            new_frequency, new_voltage, reason = choose(decision_frequency, confirmed[1])
            reported_frequency_number = _as_float(reported_frequency)
            reported_voltage_number = _as_float(reported_voltage)
            jump_frequency = pll_frequency if pll_frequency is not None else (
                int(reported_frequency_number) if reported_frequency_number is not None else None
            )
            if (
                jump_frequency is not None
                and reported_voltage_number is not None
                and _proposal_jumps_above_report(
                    new_frequency,
                    new_voltage,
                    jump_frequency,
                    int(reported_voltage_number),
                    frequency_step,
                    voltage_step,
                )
            ):
                confirmed = (jump_frequency, int(reported_voltage_number))
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
                decision_frequency = confirmed[0]

            if _same_setpoint((new_frequency, new_voltage), (decision_frequency, confirmed[1])):
                new_frequency = confirmed[0]
                new_voltage = confirmed[1]

            if reason == "increase frequency" and good_hashrate(measured_hashrate(info), error_percentage) is None:
                new_frequency = confirmed[0]
                new_voltage = confirmed[1]
                reason = "holding for good hashrate"

            if reason == "holding at zero hashrate":
                _publish_status(
                    bitaxe_ip,
                    phase=phase,
                    wall_type=limit_wall,
                    error_percentage=error_percentage,
                    reason=reason,
                )
                if not zero_restarted:
                    log_callback(
                        f"{bitaxe_ip} -> Hashrate is 0 GH/s after settle. Restarting...",
                        "error",
                    )
                    log_callback(restart_bitaxe(bitaxe_ip), "warning")
                    zero_restarted = True
                    error_samples.clear()
                    hashrate_history.clear()
                    rolling_hashrate.clear()
                    last_shares = None
                    last_accepted = None
                    last_rejected = None
                    last_reasons = None
                    reject_sample.reset()
                    settle_until = time.time() + refresh_interval
                    last_tune_time = time.time()
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
            if phase == "climb" and reason == "increase frequency" and confirmed[0] >= climb_cap:
                # The applied clock is already at the cap. A PLL reading a few MHz
                # under that cap must not keep the session in climb forever.
                reason = "frequency ceiling"
                new_frequency = confirmed[0]
                new_voltage = confirmed[1]

            if reason == "frequency ceiling" and phase == "climb":
                phase = "trim"
                trim_good_voltage = confirmed[1]
                log_callback(f"{bitaxe_ip} -> Frequency ceiling. Trimming voltage.", "info")
                _publish_status(bitaxe_ip, phase=phase, reason="frequency ceiling")
                if _wait(event, interval):
                    break
                continue

            leaving_hold = phase in ("hold", "trim") and (
                reason.startswith("step frequency")
                or reason.startswith("step voltage")
                or "silicon wall" in reason
                or "above target" in reason
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
            if wall == "thermal":
                thermal_hold = True
            log_callback(f"{bitaxe_ip} -> {reason}.", "info")
            _publish_status(
                bitaxe_ip,
                phase=phase,
                wall_type=limit_wall,
                error_percentage=error_percentage,
                reason=reason,
            )

            if not _same_setpoint((new_frequency, new_voltage), confirmed):
                applied_settings = set_system_settings(bitaxe_ip, new_voltage, new_frequency)
                log_callback(applied_settings, "info")
                last_tune_time = time.time()
                if settings_were_applied(applied_settings):
                    if reason == "increase frequency":
                        probe = {
                            "kind": "frequency",
                            "from_freq": confirmed[0],
                            "from_volt": confirmed[1],
                            "to_freq": new_frequency,
                            "to_volt": new_voltage,
                            "baseline": good_hashrate(measured_hashrate(info), error_percentage),
                            "actual": _as_float(info.get("actualFrequency")),
                        }
                    elif reason == "trim voltage":
                        probe = {
                            "kind": "trim",
                            "from_freq": confirmed[0],
                            "from_volt": confirmed[1],
                            "to_freq": new_frequency,
                            "to_volt": new_voltage,
                            "baseline": good_hashrate(measured_hashrate(info), error_percentage),
                            "actual": None,
                        }
                    elif probe is not None:
                        probe = None
                    if new_frequency < confirmed[0] and _blocks_reclimb(reason):
                        blocked_frequency = confirmed[0]
                        blocked_voltage = confirmed[1]
                        blocked_needs_cool = not cooled
                        blocked_for_rejects = "rejected shares" in (reason or "").lower()
                    if error_ok and reason in ("increase frequency", "increase voltage", "trim voltage"):
                        signature = (confirmed[0], confirmed[1], limit_wall)
                        if signature != saved_signature:
                            remember_setpoint(bitaxe_ip, confirmed[0], confirmed[1], limit_wall)
                            saved_signature = signature
                    pending = (new_frequency, new_voltage)
                    setpoint_since = None
                    settle_until = time.time() + refresh_interval
                    hashrate_history.clear()
                    rolling_hashrate.clear()
                    error_samples.clear()
                    if leaving_hold:
                        remember_setpoint(bitaxe_ip, new_frequency, new_voltage, limit_wall)
                        saved_signature = (new_frequency, new_voltage, limit_wall)
                else:
                    log_callback(f"{bitaxe_ip} -> Miner rejected the change. Setpoint left unchanged.", "warning")
            else:
                # Clocks stayed put. Wait out another full settle before the next
                # error decision so one poll cannot walk the clocks down.
                last_tune_time = time.time()
            if phase == "hold" and hold_since is not None and not ceiling_saved and _same_setpoint(
                (new_frequency, new_voltage), confirmed
            ) and reason in (
                "holding",
                "holding after thermal retreat",
                "holding after frequency retreat",
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

    remembered = setpoint_to_remember(confirmed, probe, pending)
    if remembered is not None:
        remember_setpoint(bitaxe_ip, remembered[0], remembered[1], limit_wall)
    log_callback(f"{bitaxe_ip} -> Autotuning stopped.", "warning")
