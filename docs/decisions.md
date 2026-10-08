# Tuning decisions

This tuner was built for one setup: six repasted, custom-cooled Bitaxe Gamma 601 boards on one oversized 5 V supply (Mean Well RSP-320-5), run from a Windows ARM tablet. This file lists every choice made for that setup, why it was made, and what it should become so other boards, operating systems, and tuning goals can use the app.

Hardware facts come from ESP-Miner (AxeOS) **v2.15.3**, the newest stable release on 2026-10-07. Each fact names the source file it came from.

## How to read the "For others" column

- **Keep**: fine for everyone as it is.
- **Board**: comes from the board table below.
- **Mode**: comes from the tuning mode (Max hashrate, Balanced, or Efficiency).
- **Option**: a user setting or a first-run question.
- **Rework**: needs a code change before anyone else uses it.

Values marked *proposed* are not decided yet.

## Decided on 2026-10-07

The owner approved these. They change what the tuner does on someone else's hardware.

1. **Balanced and Efficiency caps**: 65 °C ASIC, 85 °C regulator, and the core-current cap at the regulator's warning level (25 A on a Gamma) (rows 14, 15, 24).
2. **Voltage ceilings by mode**:
   - Max: the top AxeOS preset + 250 mV (1500 mV on a Gamma) on verified boards.
   - Balanced: the top AxeOS preset (1250 mV on a Gamma).
   - Efficiency: the stock voltage.
   - (row 10)
3. **Experimental boards** (every board except the 601):
   - Voltage stops at the top AxeOS preset and frequency at 1.5 × the top frequency preset.
   - The first Start asks for a confirmation, once per miner.
   - A board is promoted once an owner reports a clean run.
   - (rows 8, 10)
4. **Input floor**: 0.98 × the nominal input, so 4.9 V on 5 V boards and 11.76 V on 12 V boards (row 23).

A new miner starts in the mode the first-run setup picked (Balanced on a fresh install). On an experimental board even Max hashrate stays inside the board's AxeOS presets.

## What is built (2026-10-07)

| Milestone | What it does |
| --- | --- |
| M0 | This record. |
| M1 | `boards.py`: every board in AxeOS v2.15.3, with limits from its ASIC presets. The 601's values stay exactly as they were. |
| M2 | Every board tunes. A board with no regulator sensor is judged on the ASIC alone. Input current is never read as core current. The tuner reads the hotter of `temp` and `temp2`, counts voltage domains in core-current math, and scales the input floor and fast-start headroom to the board. Unverified boards ask for a confirmation once. |
| M3 | Modes (`modes.py`): Max hashrate, Balanced, and Efficiency, each with a preset per board, a fan policy (manual 100% or AxeOS auto fan), and fast-start headroom. Efficiency judges steps on energy per good hash. |
| M4 | Windows, macOS, and Linux. `desktop.py` holds notifications and window placement, plus the `run.sh` / `.command` / `install-shortcut.sh` launchers, CI on all three, and a per-user data folder for packaged builds. |
| M5 | The first-run setup (cooling, goal, supply watts, weather). Pools come from what the miners report. Weather is opt-in. "Repasted" is now "Hardware changed". |
| M6 | The firmware range (v2.11.0 and up, warning past v2.15.x), the README, the agent rules (`AGENTS.md`), and a board-report issue template. |
| M7 | `packaging/groundhog-gamma-tuner.spec` and a release workflow that builds all three systems and opens a draft release. |

Your fleet's config migrates once: each miner gets `"board": "601"` and `"mode": "max_hashrate"`, and the config gets `default_mode` Max hashrate, `setup_done` true, and weather on. No limit changes.

## Efficiency mode design

Power is about P0 + k·f·V², so energy per hash is about P0/f + k·V². On a Gamma at 600 MHz / 1150 mV, 10 mV more costs about 1.3% more J/TH, and 5 MHz less costs about 0.2%. So Efficiency:

- never raises voltage above stock, and answers chip errors with a lower clock while there is one
- keeps each step only when energy per good hash held (`efficiency_step_paid`)
- trims voltage at a heat wall instead of only waiting
- tries a trim that lost once more two clock steps lower, because a lower voltage also lowers the clock the chip can run

`tools/simulate.py` runs the real session against a simulated Gamma 601 (power 5 + 0.022·f·V² W, thermal lag, a chip that runs 560 MHz at 1150 mV). Averages over the last of 8 hours at 24 °C ambient:

| Cooling | Mode | Clocks | Good GH/s | J/TH | Peak ASIC |
| --- | --- | --- | --- | --- | --- |
| Stock | Max hashrate | 610 MHz / 1180 mV | 1222 | 19.4 | 69 °C |
| Stock | Balanced | 515 MHz / 1140 mV | 1047 | 18.8 | 64 °C |
| Stock | Efficiency | 395 MHz / 1000 mV | 797 | 17.2 | 61 °C |
| Custom | Max hashrate | 995 MHz / 1320 mV | 2027 | 21.3 | 63 °C |
| Custom | Balanced | 585 MHz / 1160 mV | 1177 | 19.0 | 58 °C |
| Custom | Efficiency | 430 MHz / 1000 mV | 865 | 16.7 | 46 °C |

The model is not a measurement. It shows that each mode does what its name says, and `test_simulation.py` keeps it that way.

## Boards AxeOS supports

From `main/device_config.h`, `main/power/TPS546.c`, and `main/power/power.c`.

| Family | Board versions | ASIC × count | Input | Regulator / power sensor | Board share of power | Regulator current warning / shutdown |
| --- | --- | --- | --- | --- | --- | --- |
| Max | 2.2, 102 | BM1397 × 1 | 5 V | DS4432U / INA260 | measured at the input | none reported |
| Ultra | 0.11, 201–205 | BM1366 × 1 | 5 V | DS4432U / INA260 | measured at the input | none reported |
| Ultra | 207 | BM1366 × 1 | 5 V | TPS546 | 5 W | 25 A / 30 A |
| Hex | 302, 303 | BM1366 × 6, 3 voltage domains | 12 V | TPS546 | 12 W | 25 A / 30 A |
| Supra | 400, 401 | BM1368 × 1 | 5 V | DS4432U / INA260 | measured at the input | none reported |
| Supra | 402, 403 | BM1368 × 1 | 5 V | TPS546 | 5 W | 25 A / 30 A |
| Gamma | 600–603 | BM1370 × 1 | 5 V | TPS546 | 5 W | 25 A / 30 A |
| GammaDuo | 650 | BM1370 (XP clocks) × 2 | 5 V | TPS546 | 5 W | 25 A / 30 A |
| SupraHex | 701, 702 | BM1368 × 6, 3 voltage domains | 12 V | TPS546 | 25 W | 25 A / 30 A |
| GammaTurbo | 801 | BM1370 × 2 | 12 V | TPS546 (dual phase) | 10 W | 50 A / 55 A |
| NajaDuo | 1201 | BM1373 × 2, 2 voltage domains | 12 V | TPS546 (dual phase) | 0 W | 50 A / 55 A |
| GammaHex | 1300 | BM1370 × 6, 2 voltage domains | 12 V | TPS546 (four phase) | 25 W | 150 A / 160 A |

| ASIC | Stock clocks | Frequency presets (MHz) | Voltage presets (mV) | Small cores | Self-test pass |
| --- | --- | --- | --- | --- | --- |
| BM1397 | 425 MHz / 1400 mV | 400–600 | 1100–1500 | 672 | 85% |
| BM1366 | 485 MHz / 1200 mV | 400–575 | 1100–1300 | 894 | 85% |
| BM1368 | 490 MHz / 1166 mV | 400–575 | 1100–1300 | 1276 | 80% |
| BM1370 | 525 MHz / 1150 mV | 400–690 | 1000–1250 | 2040 | 85% |
| BM1370 on GammaDuo | 400 MHz / 1150 mV | 350–410 | 1000–1250 | 2040 | 85% |
| BM1370 on GammaHex | 690 MHz / 1200 mV | 400–690 | 1000–1250 | 2040 | 85% |
| BM1373 (reports "BM1372/BM1373") | 327 MHz / 1000 mV | 327–410 | 1000–1250 | 6725 | 85% |

What these mean for the tuner:

- **DS4432U boards** (Max, Ultra 0.11–205, Supra 400/401):
  - `current` is the 5 V **input** current, not core current, so the core-current cap must be skipped.
  - `power` is input power with no board share added.
  - `vrTemp` reads 0, because there is no regulator sensor.
  - The tuner judges these boards on the ASIC alone and leaves the regulator and core-current limits blank.
- **Multiple voltage domains:** the regulator outputs `coreVoltage × domains`, and `coreVoltageActual` is reported per domain (`main/power/vcore.c`). The tuner's core-current math uses the regulator voltage.
- **Multi-chip boards:** they report a second ASIC temperature as `temp2`. AxeOS trips when either `temp` or `temp2` passes 75 °C, so the tuner reads the hotter one.
- **ASIC count:** `/api/system/info` has no `asicCount` field. The count comes from the board table, keyed by `boardVersion` and checked against `ASICModel`.
- **Fan:** auto-fan PATCH keys are `autofanspeed` and `temptarget` (35–66 °C, default 60). On an overheat trip, AxeOS turns auto fan off and sets the fan to 100% (`main/tasks/power_management_task.c`). A mode that uses auto fan has to turn it back on after a trip.
- **Low input:** AxeOS shows "Danger: Low Voltage" under 0.949 × `nominalVoltage` on every board.

## Scope and platform

| # | Decision | This fleet | Why | For others |
| --- | --- | --- | --- | --- |
| 1 | Supported boards | BM1370 on board 601 only (`is_gamma_601`) | The only board owned and tested | **Board:** every board in the table above tunes. 601 verified, the rest experimental (done) |
| 2 | Firmware | Official AxeOS, v2.15.1 response shape | Behavior checked against that source (trip ratchet, fan keys) | **Rework:** official AxeOS from v2.11.0 (where `manualFanSpeed` replaced `fanspeed`) up to the newest tested. Forks refused (done) |
| 3 | Operating system | Windows ARM tablet, 64-bit (x64) Python under emulation | pywebview's .NET helper does not load in ARM64 Python | **Keep** the relaunch on Windows ARM. Regular x64 Windows 10 and 11 start directly. Add macOS and Linux (done) |
| 4 | No server, Docker, Pi, or headless mode | A local file inside a window | Simple, and nothing listens on the network | **Keep** |
| 5 | Where data lives | `config.json` and `history.db` beside the scripts | One portable folder | **Keep** for a source checkout. A packaged app uses the per-user data folder (done) |
| 6 | Dependencies | Latest PyPI release, no upper bound, no pywebview GUI extras | Windows WebView2 is all the window needs | **Rework:** allow the Linux GTK or Qt backend (done) |

## Clock and voltage ranges

| # | Decision | This fleet | Why | For others |
| --- | --- | --- | --- | --- |
| 7 | Lowest frequency | 350 MHz | The lowest BM1370 preset, taken from the GammaDuo list; the Gamma list starts at 400 | **Board:** the ASIC's lowest preset |
| 8 | Highest frequency | 1100 MHz, as both hard cap and default | A strong chip may climb; heat or errors stop it | **Board** for the hard cap (experimental: 1.5 × the top preset, *proposed*). **Mode** for the default (done) |
| 9 | Lowest voltage | 1000 mV | The lowest BM1370 preset, and the regulator refuses less | **Board:** the ASIC's lowest preset |
| 10 | Highest voltage | 1500 mV, as both hard cap and default | Repasted fleet that runs at 1300+ mV on purpose | **Mode** (*proposed*): Max = top preset + 250 mV on verified boards, top preset on experimental ones. Balanced = top preset. Efficiency = stock voltage (done) |
| 11 | Stock and start clocks | 525 MHz / 1150 mV | Gamma factory clocks | **Board:** the ASIC table above |
| 12 | Voltage drops only at the frequency floor | A weak chip settles at a lower clock, not a lower voltage | Hashrate comes first | **Mode:** keep for Max and Balanced. Efficiency reverses it, because voltage is what costs energy |
| 13 | Step sizes | 5 MHz and 10 mV | Fine enough for the BM1370 | **Keep** |

## Temperature

| # | Decision | This fleet | Why | For others |
| --- | --- | --- | --- | --- |
| 14 | ASIC cap | 70 °C | Repasted. 1 °C under the trip guard | **Mode** (*proposed*): Max 70, Balanced 65, Efficiency 65 (done) |
| 15 | Regulator cap | 95 °C | Repasted; the regulator is rated far hotter | **Mode** (*proposed*): Max 95, others 85. Skipped on boards with no regulator sensor (done) |
| 16 | Firmware trip and margin | 75 °C ASIC / 105 °C regulator, 4 °C margin, so caps stay at or under 71 / 101 | AxeOS cuts power, then restarts 100 MHz and 100 mV lower, with no floor | **Keep:** the same thresholds on every board in v2.15.3 |
| 17 | Trip guard | Shed 20 MHz and 10 mV every 30 s inside the margin | Get ahead of the firmware's own cut | **Keep** |
| 18 | Hysteresis | 3 °C on the ASIC and the regulator | Stops flapping at a cap | **Keep** |
| 19 | Size of a heat retreat | One step per 3 °C over, plus one voltage step while errors are at most half the budget | A hot afternoon needs a bigger cut | **Keep** |
| 20 | Climbing again after a wall | Once 3 °C cooler, or after 6 hours | Day and night with the windows open | **Keep** |
| 21 | One-off raises of saved caps | Step 2 (repaste: 70 °C / 95 °C / 1500 mV) and step 3 (29 A) raised saved caps on load | This fleet's history | **Removed:** an update never changes a saved value. New defaults reach new miners only, and the file an older version saved is kept as `config.backup-vN.json` (done) |

## Power and wiring

| # | Decision | This fleet | Why | For others |
| --- | --- | --- | --- | --- |
| 22 | Watt cap | 50 W, as a runaway guard rather than a limit | Oversized supply | **Option:** a first-run question about the supply. The default is the AxeOS `maxPower` for the family (Gamma 40 W, Hex 90 W, GammaHex 180 W) (done) |
| 23 | Input floor | 4.9 V | 5 V supply with loss in the wiring | **Board** (*proposed*): 0.98 × nominal, so 4.9 V on 5 V boards and 11.76 V on 12 V boards (done) |
| 24 | Core current cap | 29 A cap and 29 A hard limit | The regulator shuts down with no retry at 30 A | **Board:** hard limit = 29/30 of the regulator's shutdown, rounded down to 0.5 A: 29 A on 30 A regulators, 53 A on 55 A, 154.5 A on 160 A. A flat −1 A margin would leave a 160 A regulator 0.6% of room. **Mode**: Max at the hard limit, others at the regulator's warning level. None on DS4432U boards (done) |
| 25 | Droop limit | 40 mV, flagged after 3 reads | The rail lags right after a voltage change | **Keep.** Check it on DS4432U boards |
| 26 | Board share of power | 5 W added to the regulator's output | How AxeOS reports Gamma power | **Board:** the family's offset; 0 where power is measured at the input (done) |
| 27 | ASIC-off detection | Power at or under 5.5 W | A Gamma reads 5 W with the ASIC off | **Board:** on TPS546 boards, the family's offset + 0.5 W. On DS4432U boards power alone cannot tell, because input power includes the ESP32, so the tuner relies on the hashrate and overheat signals there (done) |
| 28 | Supply and wiring notes on the Limits screen | Barrel plug, 18 AWG wire, about 46 W at full push | One 5 V supply feeding six boards | **Board:** text for each family; 12 V boards use other connectors (done for the Gamma family; other families show no wiring notes yet) |

## Quality signals

| # | Decision | This fleet | Why | For others |
| --- | --- | --- | --- | --- |
| 29 | Error budget | 2% | A balance between errors and clocks | **Keep** for every mode. J/TH already counts only good hashrate |
| 30 | Rejected shares | Over 1% of at least 100 judged shares; above-target has to hold for 2 windows | One reject must not move the clocks | **Keep** |
| 31 | Hashrate shortfall | Under 85% of expected | The BM1370 self-test threshold | **Board:** 80% on the BM1368, 85% on the others (done) |
| 32 | Restart on high errors | One restart at 10% (or 5× the budget) | AxeOS can leave the ASIC broken after a trip | **Keep** |
| 33 | Flatline detection | Off | Not needed on this fleet | **Keep** (off by default) |
| 34 | `errorPercentage` required | A miner without it is skipped | v2.15.1 always reports it on a Gamma | **Keep:** every ASIC driver in v2.15.3 reads its error-count register (0x4C), so every board reports a real `errorPercentage`. Only older firmware is skipped |

## Session behavior

| # | Decision | This fleet | Why | For others |
| --- | --- | --- | --- | --- |
| 35 | Nothing learned is saved | Every session starts fresh | Weather, paste, and voltage change between runs | **Keep** |
| 36 | Continue from the live clocks | On | A restart does not drop a healthy miner to stock | **Keep** |
| 37 | Fast start | Aims 6 °C, 2 W, 1.5 A, and 0.05 V under the caps; adds 0.3 mV per MHz | The mV-per-MHz figure was measured on these miners at 525–955 MHz | **Board** for mV per MHz. **Mode** for the headroom, which grows with auto fan (done) |
| 38 | Multi-step climb | Up to 4 steps (20 MHz) at once | Reaches the ceiling faster while the chip is cool | **Keep** |
| 39 | Fan | Manual 100% for the whole session | The room decides the clocks; maximum cooling | **Mode:** Max 100%. Balanced and Efficiency use AxeOS auto fan, and turn it back on after a trip (done) |
| 40 | Timing | Reads every 5 s, tunes every 180 s, 3 s between miners at start | Fast enough; the stagger avoids writing to every miner at once | **Keep** |
| 41 | Overheat-latch recovery | Write the floor clocks, clear the flag, restart once | AxeOS saves under 1000 mV after a trip from under 1100 mV | **Keep.** Check the floor for each ASIC |
| 42 | Restart when the ASIC is off | Once, after 5 minutes | A failed start would otherwise hold forever | **Keep** |
| 43 | Reset to baseline | Stock clocks; double-click a row | A clean start after a change | **Keep**, with stock clocks from the board |

## Dashboard and extras

| # | Decision | This fleet | Why | For others |
| --- | --- | --- | --- | --- |
| 44 | Pool status | stratum.ckpool.org:3336 (SV2) and public-pool.io:23330, hard-coded | This fleet's solo pools | **Rework:** show the pools the miners report, behind its own Internet switch, off in a new install (done) |
| 45 | Network difficulty and block odds | mempool.space every 60 s | Solo-mining odds | **Option:** its own Internet switch, off in a new install (done) |
| 46 | Firmware update notice | Stable ESP-Miner tags on GitHub | Stay on official releases | **Option:** its own Internet switch, off in a new install (done) |
| 47 | Weather and history | Open-Meteo every 15 minutes, outdoor temperature | The miners breathe outdoor air through open windows | **Option:** opt in on first start (done) |
| 48 | Device location | Windows location services, then one Nominatim lookup | A tablet with Location on | **Rework:** only when the user clicks **Use This Device's Location**, never on its own, rounded to about 1 km before it is saved or sent. Elsewhere, search for a city (done) |
| 49 | History sampling | Every 10 minutes, settled after 10 minutes, 3 °C bands, 92 days of backfill | Readable daily patterns | **Keep** |
| 50 | Repaste date per miner | Drives "Since reset" | The fleet gets repasted | **Rework:** label it "Hardware change", for paste, cooler, or supply changes (done) |
| 51 | Notifications | Windows toast only | Tablet | **Rework:** a notification on each OS (done) |
| 52 | Window | Opens maximized, Windows fullscreen snapping | A tablet in a fixed spot | **Keep** on Windows. Plain maximize elsewhere (done) |
| 53 | Alerts | Offline after 3 missed reads, weak Wi-Fi at −70 dBm, current shown amber within 1 A of the cap | Matches this network | **Keep** |
| 54 | Network scan | Up to 1024 addresses, 32 at a time, starting from the local /24 | Home network | **Keep** |
| 55 | Units and language | °C, English | The owner's preference | **Keep** for now. °F could be an option later |
| 56 | How changes land | Direct pushes to `main`, a pre-push check, no CI | Solo project | **Rework:** add CI on all three systems. Other contributors use pull requests (done) |
