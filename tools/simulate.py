"""Run the real tuner against a simulated Gamma 601 on a fast clock.

    python tools/simulate.py --hours 8 --cooling stock

Each mode tunes the same simulated miner from stock clocks. The miner is a
model, not a measurement:

- power (as AxeOS reports it) = 5 W + k·f·V²
- ASIC and regulator heat follow ambient + R·P with a first-order lag
- the fan: manual 100%, or AxeOS-style auto fan aiming at temptarget
- chip errors grow once the clock passes what this voltage and heat allow

It shows whether each mode does what it claims: Max the most hashrate,
Efficiency the lowest J/TH, Balanced inside its caps. Numbers from a real
fleet will differ.
"""

import argparse
import math
import os
import random
import sys
import tempfile
import threading
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402

if __name__ == "__main__":
    # Importing autotune loads config.json. A run from the command line must
    # not create one next to the app.
    config.CONFIG_FILE = os.path.join(tempfile.mkdtemp(), "config.json")

import autotune  # noqa: E402
import boards  # noqa: E402
import modes  # noqa: E402

COOLING = {
    # °C per W on the ASIC and the regulator with the fan at 100%.
    "stock": (1.9, 2.6),
    "custom": (0.9, 1.6),
}
K = 0.022  # W per MHz·V²
LAG_SECONDS = 90


class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(float(seconds or 0), 0)


class Miner:
    """One simulated Gamma 601 with its own heat, fan, and silicon."""

    def __init__(self, clock, ambient, cooling, seed):
        self.clock = clock
        self.ambient = ambient
        self.r_asic, self.r_vr = COOLING[cooling]
        self.random = random.Random(seed)
        self.frequency, self.voltage = 525, 1150
        self.temp = self.vr_temp = ambient + 10
        self.auto_fan, self.target, self.fan = False, 60, 100.0
        self.last = clock.time()
        self.samples = []

    def fan_share(self):
        return 1.0 + 0.9 * (1 - self.fan / 100)

    def power(self):
        return 5.0 + K * self.frequency * (self.voltage / 1000) ** 2

    def error_percent(self):
        # This chip runs 560 MHz error-free at 1150 mV and 50°C. Each mV above
        # that buys 2.8 MHz, each mV under it costs 1 MHz (about 410 MHz at
        # 1000 mV), and each °C over 50°C costs 3 MHz.
        volts = self.voltage - 1150
        slope = 2.8 if volts > 0 else 1.0
        limit = 560 + slope * volts - 3 * max(self.temp - 50, 0)
        over = max(self.frequency - limit, 0)
        return 0.15 + (over / 18) ** 2

    def advance(self):
        now = self.clock.time()
        elapsed = max(now - self.last, 0)
        self.last = now
        if elapsed <= 0:
            return
        power = self.power()
        share = 1 - math.exp(-elapsed / LAG_SECONDS)
        if self.auto_fan:
            wanted = 40 + 12 * (self.temp - self.target)
            self.fan += (max(25, min(100, wanted)) - self.fan) * share
        else:
            self.fan = 100.0
        asic = self.ambient + self.r_asic * self.fan_share() * power
        vr = self.ambient + self.r_vr * self.fan_share() * power
        self.temp += (asic - self.temp) * share
        self.vr_temp += (vr - self.vr_temp) * share
        good = self.frequency * 2.04 * (1 - self.error_percent() / 100)
        self.samples.append((now, good * self.random.uniform(0.98, 1.02)))
        self.samples = [s for s in self.samples if now - s[0] <= 600]

    def rate(self, seconds):
        now = self.clock.time()
        window = [rate for when, rate in self.samples if now - when <= seconds]
        return sum(window) / len(window) if window else 0.0

    def info(self):
        self.advance()
        error = self.error_percent()
        minute = self.rate(60)
        return {
            "ASICModel": "BM1370",
            "boardVersion": "601",
            "frequency": self.frequency,
            "coreVoltage": self.voltage,
            "coreVoltageActual": self.voltage - 8,
            "actualFrequency": self.frequency,
            "temp": round(self.temp, 1),
            "temp2": -1,
            "vrTemp": round(self.vr_temp, 1),
            "power": round(self.power(), 2),
            "current": round((self.power() - 5) / (self.voltage / 1000) * 1000),
            "voltage": 5050,
            "hashRate": minute,
            "hashRate_1m": minute,
            "hashRate_10m": self.rate(600),
            "expectedHashrate": self.frequency * 2.04,
            "errorPercentage": round(error, 2),
            "sharesAccepted": 0,
            "sharesRejected": 0,
            "poolDifficulty": 8192,
            "overheat_mode": 0,
            "autofanspeed": 1 if self.auto_fan else 0,
            "fanspeed": round(self.fan),
            "temptarget": self.target,
            "uptimeSeconds": 86400,
        }

    def write(self, frequency, voltage):
        self.advance()
        self.frequency, self.voltage = int(frequency), int(voltage)

    def set_fan(self, payload):
        self.advance()
        self.auto_fan = bool(payload.get("autofanspeed"))
        self.target = int(payload.get("temptarget", self.target))


def run(mode_key, hours, ambient, cooling, seed=1):
    """Tune one simulated miner for `hours` and return the last hour's averages."""
    clock = FakeClock()
    miner = Miner(clock, ambient, cooling, seed)
    board = boards.GAMMA_601
    runtime = dict(config.get_default_config(), default_mode=mode_key)
    limits = config.board_limits(board, runtime, mode_key)
    runtime["miners"] = [dict(limits, ip="sim", board="601", mode=mode_key)]
    stop = threading.Event()
    end = clock.time() + hours * 3600
    log = []
    tail = []

    def wait(event, seconds):
        clock.sleep(seconds if seconds and seconds > 0 else 1)
        if clock.time() >= end:
            event.set()
        if clock.time() >= end - 3600:
            info = miner.info()
            good = info["hashRate_1m"]
            tail.append(
                (
                    info["frequency"],
                    info["coreVoltage"],
                    info["temp"],
                    info["vrTemp"],
                    info["power"],
                    good,
                )
            )
        return event.is_set()

    def write(ip, volt, freq):
        miner.write(freq, volt)
        return f"{ip} -> Applied settings: Voltage = {volt}mV, Frequency = {freq}MHz"

    with (
        mock.patch.object(autotune, "time", clock),
        mock.patch.object(autotune, "_wait", wait),
        mock.patch.object(autotune, "load_config", lambda: runtime),
        mock.patch.object(autotune, "get_system_info", lambda ip: miner.info()),
        mock.patch.object(autotune, "set_system_settings", write),
        mock.patch.object(
            autotune, "patch_system", lambda ip, p: (miner.set_fan(p), (True, ""))[1]
        ),
        mock.patch.object(autotune, "restart_bitaxe", lambda ip: f"{ip} -> Restarted."),
    ):
        autotune.monitor_and_adjust(
            "sim",
            "BM1370 601",
            5,
            lambda message, level="info": log.append(message),
            limits["min_freq"],
            limits["max_freq"],
            limits["min_volt"],
            limits["max_volt"],
            limits["max_temp"],
            limits["max_watts"],
            limits["start_freq"],
            limits["start_volt"],
            limits["max_vr_temp"],
            stop_event=stop,
        )
    count = len(tail) or 1
    freq, volt, temp, vr, power, good = (
        sum(col) / count for col in zip(*tail, strict=True)
    )
    return {
        "mode": modes.MODES[mode_key].name,
        "MHz": round(freq),
        "mV": round(volt),
        "ASIC °C": round(temp, 1),
        "peak ASIC °C": max(row[2] for row in tail) if tail else 0,
        "VR °C": round(vr, 1),
        "W": round(power, 1),
        "good GH/s": round(good),
        "J/TH": round(power / good * 1000, 2) if good else None,
        "caps": f"{limits['max_temp']:g}/{limits['max_vr_temp']:g} °C, "
        f"{limits['max_volt']} mV",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hours", type=float, default=6)
    parser.add_argument("--ambient", type=float, default=24)
    parser.add_argument("--cooling", choices=sorted(COOLING), default="stock")
    args = parser.parse_args()
    print(
        f"Simulated Gamma 601, {args.cooling} cooling, {args.ambient:g}°C ambient, "
        f"{args.hours:g} h. Averages over the last hour."
    )
    for key in modes.MODE_ORDER:
        print(run(key, args.hours, args.ambient, args.cooling))


if __name__ == "__main__":
    main()
