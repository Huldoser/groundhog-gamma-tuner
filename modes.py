"""Tuning modes: what a miner is tuned for, and the limits each mode starts from.

A mode sets three things the per-miner limits cannot:

- the objective: most good hashrate, or the lowest energy per good hash
- the fan: locked at 100%, or AxeOS auto fan aiming under the ASIC cap
- how far under the heat caps the fast start aims

It also has a limit preset for every board. Choosing a mode in AutoTuner
Settings fills in that preset; the per-miner limits stay editable after that.
The board's hard range (boards.Limits) is the only absolute cap.

The numbers come from docs/decisions.md, approved on 2026-10-07.
"""

from dataclasses import dataclass

from boards import GAMMA_601

HASHRATE = "hashrate"
EFFICIENCY = "efficiency"

MAX_HASHRATE = "max_hashrate"
BALANCED = "balanced"
EFFICIENCY_MODE = "efficiency"


@dataclass(frozen=True)
class Mode:
    key: str
    name: str
    objective: str
    # True: AxeOS auto fan aiming under the ASIC cap. False: manual 100%.
    auto_fan: bool
    # The fast start aims this far under both temperature caps.
    ramp_headroom_c: float
    summary: str


MODES = {
    MAX_HASHRATE: Mode(
        MAX_HASHRATE,
        "Max hashrate",
        HASHRATE,
        auto_fan=False,
        ramp_headroom_c=6.0,
        summary=(
            "Most hashrate. Fan at 100% the whole session, voltage up to the "
            "board's hard cap. For custom cooling."
        ),
    ),
    BALANCED: Mode(
        BALANCED,
        "Balanced",
        HASHRATE,
        auto_fan=True,
        # The fan speeds up as the chip warms, so a heat projection made at a
        # lower fan speed is less certain.
        ramp_headroom_c=8.0,
        summary=(
            "More hashrate inside stock-cooler limits: 65°C ASIC, 85°C regulator, "
            "voltage up to the top AxeOS preset, AxeOS auto fan."
        ),
    ),
    EFFICIENCY_MODE: Mode(
        EFFICIENCY_MODE,
        "Efficiency",
        EFFICIENCY,
        auto_fan=True,
        ramp_headroom_c=8.0,
        summary=(
            "Lowest J/TH. Voltage never goes over stock and is trimmed down; "
            "chip errors cost a clock, not more voltage. AxeOS auto fan."
        ),
    ),
}
MODE_ORDER = (MAX_HASHRATE, BALANCED, EFFICIENCY_MODE)
DEFAULT_MODE = BALANCED

# Caps for Balanced and Efficiency, and for Max on a board other than the 601.
SAFE_MAX_TEMP = 65
SAFE_MAX_VR_TEMP = 85
MAX_MODE_MAX_TEMP = 70
MAX_MODE_MAX_VR_TEMP = 95
DEFAULT_MAX_ERROR_PERCENTAGE = 2.0
DEFAULT_MAX_DROOP_MV = 40
# AxeOS accepts an auto-fan target from 35 to 66°C (nvs_config.c, temptarget).
FAN_TARGET_RANGE = (35, 66)
# Auto fan aims this far under the ASIC cap.
FAN_TARGET_UNDER_CAP_C = 5


def mode_named(key):
    """The mode for a saved key, or None."""
    return MODES.get(str(key or "").strip())


def default_mode_for(board):
    """Mode for a miner saved before modes existed: the 601 fleet ran Max."""
    return MAX_HASHRATE if board is GAMMA_601 else BALANCED


def mode_for_record(miner, board=GAMMA_601):
    """A saved miner's mode."""
    if isinstance(miner, dict):
        mode = mode_named(miner.get("mode"))
        if mode is not None:
            return mode
    return MODES[default_mode_for(board)]


def preset_limits(mode, board, gamma_601_max=None, default_target_temp=None):
    """The per-miner limits `mode` starts from on `board`.

    `gamma_601_max` is the 601 fleet's own Max preset (config.GAMMA601_LIMITS
    with Default Max Temp applied); Max on a 601 returns it unchanged. Every
    other preset stays inside the board's hard range. A lower Default Max Temp
    still wins. A limit the board has no sensor for comes back blank.
    """
    if isinstance(mode, str):
        mode = MODES[mode]
    if mode.key == MAX_HASHRATE and board is GAMMA_601 and gamma_601_max is not None:
        return dict(gamma_601_max)
    hard = board.limits
    asic = board.asic
    regulator = board.family.regulator
    top_preset = max(asic.voltage_presets)
    if mode.key == MAX_HASHRATE:
        max_volt = hard.max_volt
        max_temp, max_vr_temp = MAX_MODE_MAX_TEMP, MAX_MODE_MAX_VR_TEMP
        max_core_amps = hard.max_core_amps
    else:
        max_volt = min(hard.max_volt, top_preset)
        if mode.objective == EFFICIENCY:
            max_volt = min(max_volt, asic.stock_volt)
        max_temp, max_vr_temp = SAFE_MAX_TEMP, SAFE_MAX_VR_TEMP
        max_core_amps = regulator.iout_warn if board.has_core_current else None
    if default_target_temp is not None:
        try:
            max_temp = min(max_temp, float(default_target_temp))
        except (TypeError, ValueError):
            pass
    limits = {
        "min_freq": hard.min_freq,
        "max_freq": hard.max_freq,
        "start_freq": asic.stock_freq,
        "min_volt": hard.min_volt,
        "max_volt": max_volt,
        "start_volt": min(asic.stock_volt, max_volt),
        "max_temp": max_temp,
        "max_watts": board.family.max_power,
        "max_vr_temp": max_vr_temp,
        "min_input_voltage": board.default_min_input_voltage,
        "max_error_percentage": DEFAULT_MAX_ERROR_PERCENTAGE,
        "max_droop_mv": DEFAULT_MAX_DROOP_MV,
        "max_core_amps": max_core_amps,
    }
    for name in board.unused_limit_fields:
        limits[name] = ""
    return limits


def fan_target(max_temp):
    """AxeOS auto-fan target for an ASIC cap, inside the range AxeOS accepts."""
    low, high = FAN_TARGET_RANGE
    try:
        target = int(float(max_temp)) - FAN_TARGET_UNDER_CAP_C
    except (TypeError, ValueError):
        target = high
    return max(low, min(high, target))


def fan_payload(mode, max_temp):
    """The PATCH that sets the fan for `mode`."""
    if mode.auto_fan:
        return {"autofanspeed": 1, "temptarget": fan_target(max_temp)}
    # AxeOS v2.15.1 takes the manual fan percent as "manualFanSpeed". It
    # renamed "fanspeed" in v2.11.0 and ignores unknown PATCH keys.
    return {"autofanspeed": 0, "manualFanSpeed": 100}
