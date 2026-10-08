"""Every board official AxeOS supports, and the limits the tuner uses on each.

The hardware rows mirror ESP-Miner v2.15.3: main/device_config.h (ASICs,
families, boards), main/power/TPS546.c (regulator setups), and
main/power/power.c (how power and current are read). A board AxeOS adds
later is one new BOARDS row copied from that release.

A board someone has run with this tuner has its limits in VERIFIED_LIMITS.
Every other board gets limits from its ASIC's AxeOS presets and stays
experimental until an owner reports a clean run. Every board in the table
tunes; an experimental one asks for a confirmation on its first Start.
"""

import math
from dataclasses import dataclass, field

AXEOS_SOURCE = "ESP-Miner v2.15.3"
# v2.11.0 renamed the manual fan key to manualFanSpeed; older firmware would
# take the fan off auto and leave it wherever it was. v2.15.3 is the newest
# release whose source this table and the tuner were checked against.
AXEOS_MIN_VERSION = (2, 11, 0)
AXEOS_TESTED_VERSION = (2, 15, 3)

TPS546 = "TPS546"
DS4432U = "DS4432U"

VERIFIED = "verified"
EXPERIMENTAL = "experimental"


@dataclass(frozen=True)
class Asic:
    """One ASIC setup from device_config.h. `model` is what ASICModel reports."""

    model: str
    stock_freq: int
    stock_volt: int
    frequency_presets: tuple
    voltage_presets: tuple
    small_cores: int
    # Share of expected hashrate the AxeOS self-test passes.
    self_test_ratio: float


@dataclass(frozen=True)
class Regulator:
    """A TPS546 setup from TPS546.c. Volts, amps, and °C."""

    vin_on: float
    vin_off: float
    vin_ov_fault: float
    vout_min: float
    vout_max: float
    iout_warn: float
    iout_fault: float
    ot_warn: int = 105
    ot_fault: int = 145


@dataclass(frozen=True)
class Family:
    name: str
    asic: Asic
    asic_count: int
    # maxPower in /api/system/info.
    max_power: int
    # Watts AxeOS adds to the TPS546 output for the rest of the board.
    power_offset: float
    nominal_voltage: int
    # The TPS546 drives coreVoltage times this many chips in series.
    voltage_domains: int
    regulator: Regulator


@dataclass(frozen=True)
class Limits:
    """The hard range a board's saved limits are clamped into."""

    min_freq: int
    max_freq: int
    min_volt: int
    max_volt: int
    # None when the board reports no core current.
    max_core_amps: float | None
    # Watts at or under this mean the ASIC is off. None when power alone cannot tell.
    asic_off_watts: float | None
    # Core voltage the fast start adds per MHz.
    mv_per_mhz: float


@dataclass(frozen=True)
class Board:
    version: str
    family: Family
    vr_chip: str
    status: str = EXPERIMENTAL
    limits: Limits = field(default=None, compare=False)

    @property
    def name(self):
        return f"{self.family.name} {self.version}"

    @property
    def asic(self):
        return self.family.asic

    @property
    def verified(self):
        return self.status == VERIFIED

    @property
    def has_vr_temp(self):
        """DS4432U boards have no regulator sensor. AxeOS reports vrTemp 0."""
        return self.vr_chip == TPS546

    @property
    def has_core_current(self):
        """On INA260 boards `current` is the 5 V input current, not core current."""
        return self.vr_chip == TPS546

    @property
    def board_power_w(self):
        """Watts AxeOS adds to the regulator output. INA260 boards read input power."""
        return float(self.family.power_offset) if self.vr_chip == TPS546 else 0.0

    @property
    def hashrate_per_mhz(self):
        """Expected GH/s per MHz across every ASIC on the board."""
        return self.asic.small_cores * self.family.asic_count / 1000

    @property
    def stock_clocks(self):
        return self.asic.stock_freq, self.asic.stock_volt

    @property
    def default_min_input_voltage(self):
        """Input floor: 98% of the nominal input (4.9 V on a 5 V board)."""
        return round(INPUT_FLOOR_SHARE * self.family.nominal_voltage, 2)

    @property
    def unused_limit_fields(self):
        """Per-miner limits this board has no sensor for."""
        fields = []
        if not self.has_vr_temp:
            fields.append("max_vr_temp")
        if not self.has_core_current:
            fields.append("max_core_amps")
        return tuple(fields)


def _presets(*values):
    return tuple(values)


BM1397 = Asic(
    "BM1397",
    425,
    1400,
    _presets(400, 425, 450, 475, 485, 500, 525, 550, 575, 600),
    _presets(1100, 1150, 1200, 1250, 1300, 1350, 1400, 1450, 1500),
    672,
    0.85,
)
BM1366 = Asic(
    "BM1366",
    485,
    1200,
    _presets(400, 425, 450, 475, 485, 500, 525, 550, 575),
    _presets(1100, 1150, 1200, 1250, 1300),
    894,
    0.85,
)
BM1368 = Asic(
    "BM1368",
    490,
    1166,
    _presets(400, 425, 450, 475, 485, 490, 500, 525, 550, 575),
    _presets(1100, 1150, 1166, 1200, 1250, 1300),
    1276,
    0.80,
)
_BM1370_VOLTS = _presets(1000, 1060, 1100, 1150, 1200, 1250)
BM1370 = Asic(
    "BM1370",
    525,
    1150,
    _presets(400, 490, 525, 550, 600, 625, 690),
    _BM1370_VOLTS,
    2040,
    0.85,
)
# The GammaDuo's BM1370 setup. It still reports ASICModel "BM1370".
BM1370_XP = Asic(
    "BM1370", 400, 1150, _presets(350, 375, 380, 400, 410), _BM1370_VOLTS, 2040, 0.85
)
# The GammaHex's BM1370 setup.
BM1370_HEX = Asic(
    "BM1370",
    690,
    1200,
    _presets(400, 490, 525, 550, 600, 625, 690),
    _BM1370_VOLTS,
    2040,
    0.85,
)
BM1373 = Asic(
    "BM1372/BM1373",
    327,
    1000,
    _presets(327, 350, 375, 380, 400, 410),
    _presets(1000, 1060, 1100, 1150, 1200, 1250),
    6725,
    0.85,
)

TPS546_DEFAULT = Regulator(4.8, 4.5, 6.5, 1.0, 2.0, 25.0, 30.0)
TPS546_HEX = Regulator(11.5, 11.0, 14.8, 2.5, 4.5, 25.0, 30.0)
TPS546_GAMMA_TURBO = Regulator(11.0, 10.5, 14.8, 1.0, 3.0, 50.0, 55.0)
TPS546_NAJA_DUO = Regulator(11.0, 10.5, 14.0, 1.8, 3.0, 50.0, 55.0)
TPS546_GAMMA_HEX = Regulator(11.0, 10.5, 14.0, 2.0, 3.0, 150.0, 160.0)

MAX = Family("Max", BM1397, 1, 25, 5, 5, 1, TPS546_DEFAULT)
ULTRA = Family("Ultra", BM1366, 1, 25, 5, 5, 1, TPS546_DEFAULT)
HEX = Family("Hex", BM1366, 6, 90, 12, 12, 3, TPS546_HEX)
SUPRA = Family("Supra", BM1368, 1, 40, 5, 5, 1, TPS546_DEFAULT)
GAMMA = Family("Gamma", BM1370, 1, 40, 5, 5, 1, TPS546_DEFAULT)
GAMMA_DUO = Family("GammaDuo", BM1370_XP, 2, 40, 5, 5, 1, TPS546_DEFAULT)
SUPRA_HEX = Family("SupraHex", BM1368, 6, 120, 25, 12, 3, TPS546_HEX)
GAMMA_TURBO = Family("GammaTurbo", BM1370, 2, 60, 10, 12, 1, TPS546_GAMMA_TURBO)
NAJA_DUO = Family("NajaDuo", BM1373, 2, 60, 0, 12, 2, TPS546_NAJA_DUO)
GAMMA_HEX = Family("GammaHex", BM1370_HEX, 6, 180, 25, 12, 2, TPS546_GAMMA_HEX)

# Core voltage the fast start adds per MHz until a board has its own number.
DEFAULT_MV_PER_MHZ = 0.3
# Watts over the board's own share that still mean the ASIC is off.
ASIC_OFF_MARGIN_W = 0.5
# Share of the regulator's shutdown current the tuner may use, as a fraction.
# On the Gamma's 30 A shutdown that is 29 A; it leaves room for an overshoot
# between polls, and the room grows with the regulator.
CURRENT_CAP_SHARE = (29, 30)
# Default input floor as a share of the nominal input.
INPUT_FLOOR_SHARE = 0.98

# Limits for boards someone has run with this tuner.
VERIFIED_LIMITS = {
    # Six repasted, custom-cooled boards on an oversized 5 V supply.
    # 350 MHz is the lowest BM1370 preset (the GammaDuo list). 1500 mV and
    # 1100 MHz were chosen for that cooling. 29 A stays 1 A under the
    # regulator's 30 A shutdown. 0.3 mV per MHz was measured at 525-955 MHz.
    "601": Limits(
        min_freq=350,
        max_freq=1100,
        min_volt=1000,
        max_volt=1500,
        max_core_amps=29.0,
        asic_off_watts=5.5,
        mv_per_mhz=0.3,
    ),
}


def _round_to(value, step):
    return int(step * round(value / step))


def _current_cap(regulator):
    """CURRENT_CAP_SHARE of the shutdown current, rounded down to 0.5 A."""
    share, whole = CURRENT_CAP_SHARE
    return math.floor(regulator.iout_fault * share * 2 / whole) / 2


def preset_limits(family, vr_chip):
    """Limits for a board nobody has verified: inside its ASIC's AxeOS presets.

    Frequency may reach 1.5 times the top preset; heat, current, and power
    still stop a climb first. Voltage stops at the top preset.
    """
    asic = family.asic
    tps546 = vr_chip == TPS546
    return Limits(
        min_freq=min(asic.frequency_presets),
        max_freq=_round_to(max(asic.frequency_presets) * 1.5, 5),
        min_volt=min(asic.voltage_presets),
        max_volt=max(asic.voltage_presets),
        max_core_amps=_current_cap(family.regulator) if tps546 else None,
        asic_off_watts=family.power_offset + ASIC_OFF_MARGIN_W if tps546 else None,
        mv_per_mhz=DEFAULT_MV_PER_MHZ,
    )


def _board(version, family, vr_chip):
    verified = VERIFIED_LIMITS.get(version)
    return Board(
        version=version,
        family=family,
        vr_chip=vr_chip,
        status=VERIFIED if verified else EXPERIMENTAL,
        limits=verified or preset_limits(family, vr_chip),
    )


BOARDS = (
    _board("2.2", MAX, DS4432U),
    _board("102", MAX, DS4432U),
    _board("0.11", ULTRA, DS4432U),
    _board("201", ULTRA, DS4432U),
    _board("202", ULTRA, DS4432U),
    _board("203", ULTRA, DS4432U),
    _board("204", ULTRA, DS4432U),
    _board("205", ULTRA, DS4432U),
    _board("207", ULTRA, TPS546),
    _board("302", HEX, TPS546),
    _board("303", HEX, TPS546),
    _board("400", SUPRA, DS4432U),
    _board("401", SUPRA, DS4432U),
    _board("402", SUPRA, TPS546),
    _board("403", SUPRA, TPS546),
    _board("600", GAMMA, TPS546),
    _board("601", GAMMA, TPS546),
    _board("602", GAMMA, TPS546),
    _board("603", GAMMA, TPS546),
    _board("650", GAMMA_DUO, TPS546),
    _board("701", SUPRA_HEX, TPS546),
    _board("702", SUPRA_HEX, TPS546),
    _board("801", GAMMA_TURBO, TPS546),
    _board("1201", NAJA_DUO, TPS546),
    _board("1300", GAMMA_HEX, TPS546),
)
BOARDS_BY_VERSION = {board.version: board for board in BOARDS}
GAMMA_601 = BOARDS_BY_VERSION["601"]


def board_named(version):
    """The BOARDS row for a saved board version, or None."""
    return BOARDS_BY_VERSION.get(str(version or "").strip())


def board_for_info(info):
    """The BOARDS row this /api/system/info reply describes, or None.

    The board version has to be in the table and the reported ASIC has to be
    the one AxeOS puts on that board. A fork or a board AxeOS added after
    AXEOS_SOURCE is None.
    """
    if not isinstance(info, dict):
        return None
    board = board_named(info.get("boardVersion"))
    if board is None:
        return None
    reported = str(info.get("ASICModel") or "").strip().upper()
    if reported != board.asic.model.upper():
        return None
    return board


def board_list_note(info):
    """Why a reply is not a board in the table, for a log line."""
    if not isinstance(info, dict):
        return "The miner did not answer."
    version = str(info.get("boardVersion") or "").strip() or "unknown"
    asic = str(info.get("ASICModel") or "").strip() or "unknown ASIC"
    board = board_named(version)
    if board is not None:
        return (
            f"Board {version} reports {asic}, but AxeOS puts a {board.asic.model} "
            "on it. This tuner only runs official AxeOS."
        )
    return (
        f"Board {version} ({asic}) is not in this tuner's board list "
        f"({AXEOS_SOURCE}). This tuner only runs official AxeOS."
    )


def firmware_version(info):
    """(major, minor, patch) from the reply's `version`, or None."""
    text = str((info or {}).get("version") or "").strip().lstrip("vV")
    parts = text.split("-", 1)[0].split(".")
    if len(parts) < 3:
        return None
    try:
        return tuple(int(part) for part in parts[:3])
    except ValueError:
        return None


def firmware_check(info):
    """(problem, note) for the AxeOS version this reply reports.

    `problem` stops tuning: firmware older than AXEOS_MIN_VERSION. `note` is a
    warning only: a version newer than the one this tuner was checked against,
    or one it cannot read.
    """
    version = firmware_version(info)
    shown = str((info or {}).get("version") or "unknown").strip()
    oldest = ".".join(str(part) for part in AXEOS_MIN_VERSION)
    newest = ".".join(str(part) for part in AXEOS_TESTED_VERSION)
    if version is None:
        if not str((info or {}).get("version") or "").strip():
            return "", ""
        return "", f"Could not read the AxeOS version ({shown})."
    if version < AXEOS_MIN_VERSION:
        return (
            f"AxeOS {shown} is older than v{oldest}, the oldest this tuner "
            "supports. Update the firmware first.",
            "",
        )
    if version[:2] > AXEOS_TESTED_VERSION[:2]:
        return "", (
            f"AxeOS {shown} is newer than v{newest}, the newest this tuner was "
            "checked against."
        )
    return "", ""


def _reading(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def asic_temp(info):
    """The hotter ASIC reading. AxeOS trips on either `temp` or `temp2`.

    Multi-chip boards report a second sensor as temp2. Others report -1 there,
    and then `temp` comes back exactly as the miner sent it.
    """
    if not isinstance(info, dict):
        return None
    temp = info.get("temp")
    second = _reading(info.get("temp2"))
    if second is None:
        return temp
    first = _reading(temp)
    return second if first is None else max(first, second)


def board_for_record(miner):
    """A saved miner's board. Miners saved before boards were recorded are 601s."""
    if isinstance(miner, dict):
        board = board_named(miner.get("board"))
        if board is not None:
            return board
    return GAMMA_601
