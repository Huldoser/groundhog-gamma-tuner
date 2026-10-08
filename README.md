# Groundhog Gamma Tuner

<p align="center">
  <img src="web/logo.png" alt="Groundhog Gamma Tuner" width="360">
</p>

A desktop app that tunes Bitaxe miners running official AxeOS. It watches each miner's AxeOS API and moves frequency and voltage toward the goal you pick (the most hashrate, a balance, or the lowest J/TH) without crossing that board's limits. It runs on Windows, macOS, and Linux as a local window. The page is a file inside that window; nothing listens on the network.

It started as one owner's tuner for six repasted, custom-cooled Gamma 601s on an oversized 5 V supply. That setup is still the one verified board. [docs/decisions.md](docs/decisions.md) lists every choice made for it and what other boards get instead.

![Dashboard while the tuner is running](docs/dashboard.png)

## Supported boards

Every board in the official AxeOS v2.15.3 board list. The app reads the board version and ASIC from the miner and refuses anything else, including firmware forks.

| Family | Board versions | ASIC | Input | Status |
| --- | --- | --- | --- | --- |
| Gamma | 601 | BM1370 | 5 V | Verified |
| Gamma | 600, 602, 603 | BM1370 | 5 V | Experimental |
| Gamma Duo | 650 | 2 × BM1370 | 5 V | Experimental |
| Gamma Turbo | 801 | 2 × BM1370 | 12 V | Experimental |
| Gamma Hex | 1300 | 6 × BM1370 | 12 V | Experimental |
| Supra | 400, 401, 402, 403 | BM1368 | 5 V | Experimental |
| Supra Hex | 701, 702 | 6 × BM1368 | 12 V | Experimental |
| Ultra | 0.11, 201–205, 207 | BM1366 | 5 V | Experimental |
| Hex | 302, 303 | 6 × BM1366 | 12 V | Experimental |
| Naja Duo | 1201 | 2 × BM1373 | 12 V | Experimental |
| Max | 2.2, 102 | BM1397 | 5 V | Experimental |

**Experimental** means nobody has run that board with this tuner yet:

- Its voltage stops at the ASIC's top AxeOS preset (1250 mV on a BM1370), and its frequency at 1.5 × the top frequency preset.
- The first Start asks you to confirm, once per miner.
- Boards without a regulator sensor (Max, Ultra 0.11–205, Supra 400/401) are judged on the ASIC alone, and their core-current limit is off. Their `current` reading is input current.
- If you run one, please [report how it went](../../issues/new?template=board-report.md) so it can be marked verified.

**Firmware:** official AxeOS v2.11.0 or newer. Older firmware uses a different fan setting and is refused. Versions newer than v2.15.x tune with a warning in the log.

## Modes

Each miner has a mode. You pick one for new miners on first start (or in **Global Settings**), and per miner in **AutoTuner Settings**. Picking a mode fills in its limits; you can still change any of them, and the form then says "Custom limits".

| Mode | Goal | Fan | Voltage ceiling | Default caps |
| --- | --- | --- | --- | --- |
| **Max hashrate** | Most hashrate | Manual 100% | The board's hard cap (1500 mV on a Gamma 601) | 70 °C ASIC, 95 °C regulator |
| **Balanced** | More hashrate inside stock-cooler temperatures | AxeOS auto | The top AxeOS preset | 65 °C ASIC, 85 °C regulator, regulator warning current |
| **Efficiency** | Lowest J/TH | AxeOS auto | Stock voltage | 65 °C ASIC, 85 °C regulator |

- **Max hashrate** is for custom cooling and a supply that can take it. It climbs until heat, chip errors, power, or core current stops it. It is not for a stock cooler.
- **Balanced** uses the same climbing logic with stock-cooler limits. AxeOS's own fan control aims 5 °C under the ASIC cap.
- **Efficiency** never raises voltage above stock. Chip errors cost a clock, not more voltage, and voltage is trimmed while energy per good hash goes down. At a heat wall it trims voltage instead of only waiting. When a trim alone adds errors, it tries the trim again two clock steps lower. It usually settles near the lowest voltage the chip runs well. On a stock Gamma that is fewer TH/s for noticeably less power.

`tools/simulate.py` runs the real tuner against a simulated Gamma 601 on a fast clock, so you can compare the modes in seconds:

```bash
python tools/simulate.py --cooling stock --ambient 24 --hours 8
```

## Install

### Download

Prebuilt apps are attached to each [release](../../releases): Windows (x64; also runs on Windows on ARM), macOS (Apple silicon), and Linux (x64). They are not code-signed:

- **Windows:** if SmartScreen says "Windows protected your PC", choose **More info**, then **Run anyway**.
- **macOS:** if it says the app cannot be opened, right-click it, choose **Open**, then **Open** again.
- **Linux:** extract the archive and run `Groundhog Gamma Tuner` inside the folder.

A downloaded app keeps `config.json` and `history.db` in your user data folder:

- Windows: `%APPDATA%\Groundhog Gamma Tuner`
- macOS: `~/Library/Application Support/Groundhog Gamma Tuner`
- Linux: `~/.config/groundhog-gamma-tuner`

### From source

Run from a source checkout, the app keeps its settings next to the scripts. It needs Python 3.13 or newer and the two packages in `requirements.txt` (`requests` and `pywebview`).

#### Windows

Windows 10 or 11, on a regular (x64) PC or a Windows on ARM machine.

- **Python:** the **Windows installer (64-bit)** from [python.org](https://www.python.org/downloads/windows/). On a regular PC that is the usual download. On a Windows on ARM machine, use it rather than the ARM64 installer: the x64 build runs under emulation, but the ARM64 build cannot open the window because pywebview's .NET helper does not load in it. If the ARM64 build is the `python` on PATH, `main.py` restarts itself in the x64 one.
- **WebView2:** Windows 11 includes the Edge WebView2 runtime, and most Windows 10 PCs have it through Edge. If the window does not open on Windows 10, install the [Evergreen WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) from Microsoft.

Open Command Prompt in this folder:

```bat
python -m pip install -U -r requirements.txt
python main.py
```

On a Windows on ARM machine, `pip` on PATH may be the ARM64 one. `py -0p` lists the interpreters; install with the one whose folder does not end in `-arm64`, for example `"%LocalAppData%\Programs\Python\Python313\python.exe" -m pip install -U -r requirements.txt`.

- **Desktop shortcut:** double-click `install-shortcut.bat`. The shortcut runs `run.bat`, which starts the window with no console.
- **Start at logon:** in Task Scheduler (`taskschd.msc`), create a basic task that runs `run.bat` when you log on. On the **Conditions** tab, clear **Start the task only if the computer is on AC power**.

#### macOS

Install Python 3.13 from [python.org](https://www.python.org/downloads/macos/) or Homebrew, then:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -U -r requirements.txt
./run.sh
```

- **Open from Finder:** double-click `Groundhog Gamma Tuner.command`.
- **Start at login:** add that file under System Settings > General > Login Items.

#### Linux

The window uses GTK and WebKit from your distribution:

| Distribution | Packages |
| --- | --- |
| Debian, Ubuntu | `sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1` |
| Fedora | `sudo dnf install python3-gobject webkit2gtk4.1` |
| Arch | `sudo pacman -S python-gobject webkit2gtk-4.1` |

Then make a virtual environment that can see those packages, and run:

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -U -r requirements.txt
./run.sh
```

- **No GTK:** `.venv/bin/python -m pip install "pywebview[qt]"` uses Qt instead.
- **Application menu:** `./install-shortcut.sh` adds the app. `./install-shortcut.sh --autostart` also opens it when you log in.

## First start

On first start the app asks four questions:

1. **Cooling:** stock, upgraded, or custom.
2. **What you want most:** the most hashrate, a balance, or the lowest J/TH. Together with cooling this picks the mode for new miners. **Max hashrate** needs custom cooling and an explicit OK; otherwise "most hashrate" gives Balanced.
3. **Watts per miner:** what your supply and wiring can give each miner. The tuner steps down above it. Blank uses each board's AxeOS max power (40 W on a Gamma).
4. **Outdoor weather:** whether to record it with the history (see below).

Then **Scan Network** finds the miners on your subnet, or you add one by IP. **AutoTuner Settings** shows each miner's mode and limits. **Start Autotuner** starts tuning.

![AutoTuner settings](docs/autotuner.png)

## How it tunes

1. **Opening.** The app checks the live board and firmware, enables overclocking, and sets the fan for the mode. Nothing learned in an earlier session is saved; weather, paste, and voltage change too much between runs. A miner already hashing inside its limits continues from the clocks it runs now. A miner that is off, in overheat mode, faulted, or outside its limits starts from its start clocks (its board's stock clocks unless you change them). **Continue from current clocks** in Global Settings turns this off.
2. **Fast start** (Global Settings, on by default). The app jumps frequency and voltage toward a point under the ASIC and regulator caps: 6 °C under in Max mode, 8 °C with auto fan. It holds each jump about a minute, and fits a line through the watts and temperatures it saw to aim the next one. Any limit crossed on the way sends it back to the last clocks that held. A cool chip reaches its sweet spot in minutes instead of hours.
3. **Steps down.** Frequency comes down when ASIC temperature, regulator temperature, power, core current, input voltage, or core-voltage droop crosses its limit.
   - The tuner never climbs or raises voltage into the core-current cap. The cap stays under the regulator's no-retry shutdown: 29 A of 30 A on a Gamma.
   - Temperature caps stay at least 4 °C under the AxeOS overheat trip (75 °C ASIC, 105 °C regulator). Inside that margin the tuner sheds 20 MHz and 10 mV every 30 seconds, so AxeOS does not cut power and drop the clocks 100 MHz and 100 mV on its own.
   - If AxeOS does trip, the tuner puts the clocks back on the saved minimum once the chip is cool. If AxeOS leaves the chip off in overheat mode, the tuner writes the minimum clocks, clears the flag, and restarts the miner once. In an auto-fan mode it also turns auto fan back on, which AxeOS switches off on a trip.
4. **Steps up.**
   - In Max hashrate and Balanced, voltage rises only when the ASIC error percentage is over the budget (2%). Frequency rises while errors stay inside it.
   - A cool chip climbs up to 4 steps (20 MHz) per settle. Near a cap, or with errors over half the budget, it climbs one step.
   - Each step is kept only if good hashrate held, or in Efficiency mode, only if energy per good hash held.
5. **Holds.** At the frequency ceiling it trims voltage, then holds. The Phase column shows what is holding it. A hold under a hashrate or silicon wall climbs again once the chip is 3 °C cooler, or after 6 hours, while errors are at most half the budget.
6. **Recovers.** It restarts a miner once when its settled errors stay far over the budget (10%), which AxeOS can leave after an overheat recovery. If a soft restart does not clear it, unplug the miner for 30 seconds.

The **Limits** screen shows each board's firmware, regulator, and chip limits, the tuner's hard range, and every mode's preset.

## History and weather

The **History** tab shows how each miner ran.

- While the window is open, every miner is saved every 10 minutes in `history.db` next to `config.json`. Each sample has the clock, the 10-minute hashrate less the error share, the temperatures, and the power. A sample counts as settled once its setpoint and the miner's uptime are at least 10 minutes old, and the results use only settled samples.
- The filters pick one miner or the whole fleet, a period (24 hours to everything, **Since reset**, or **Since change**), and a measure: good hashrate, efficiency (J/TH, lower is better), or clock.
- **Outdoor weather is optional.** Turn it on in the first-start questions or in Global Settings. It then comes from [Open-Meteo](https://open-meteo.com/) every 15 minutes, free for personal use and under CC BY 4.0, and gaps are filled from Open-Meteo's hourly history up to 92 days back. **When it runs best** and **Best combinations** compare results against outdoor temperature, time of day, and sky. That matters most when the miners breathe outdoor air.
- **Settings > Weather Location** sets the place. Search for a city, for example `Moose Jaw, Saskatchewan`. On Windows, **Use This Device's Location** asks Windows location services. That needs Location on, with **Let desktop apps access your location** on. The place name comes from one [Nominatim](https://nominatim.org/) lookup.

### Reset to baseline

Double-click a miner (or right-click and choose **Reset to Baseline**) to write its board's stock clocks and make them its start clocks. **Settings > Reset All to Baseline** does the same for every miner. Both ask first, and the reset is marked in the history. For new paste, a new cooler, or a new supply, set the date under **Edit Miner** ("Hardware changed on"). **Since change** then starts there.

A miner shows as offline after three missed reads in a row (about 15 seconds). One missed read shows "no reply" under its phase. A reboot or a Wi-Fi blip looks like that.

## The author's fleet

The Gamma 601 limits come from six repasted, custom-cooled boards on one Mean Well RSP-320-5 (5 V, 60 A):

- **Max hashrate, verified on the 601:** 70 °C ASIC, 95 °C regulator, up to 1500 mV and 1100 MHz, and 29 A core current.
- **Watt cap:** 50 W per miner, as a runaway guard; the supply is not the limit.
- **Input floor:** 4.9 V, because the 5 V input sags in the wiring.

These numbers are for that cooling and that supply. A stock Gamma, a weaker supply, or a warm room needs other limits, which is what Balanced and Efficiency are for. [docs/decisions.md](docs/decisions.md) has every choice with its reason.

## Development

Install the checkers with the same interpreter, then add the push hook:

```bash
python -m pip install -r requirements-dev.txt
pre-commit install
```

`git push` then runs `ruff check`, `ruff format --check`, and `python -m unittest`, and stops if any fail. Run the same checks by hand with `pre-commit run --all-files --hook-stage pre-push`. GitHub Actions runs them on Windows, macOS, and Linux for every push and pull request.

- **The author's fleet:** `test_fleet.py` pins how a Gamma 601 in Max hashrate mode is tuned to a fingerprint of the tuner's answers taken before other boards and modes existed. A change that moves it changes that fleet's tuning.
- **Every board and mode:** `test_modes.py` checks that each preset stays inside its board's limits, and that randomized readings never push a decision past them.
- **A board AxeOS adds later:** copy its row from that release's `main/device_config.h` into `boards.py`. A board someone has verified gets its own entry in `VERIFIED_LIMITS`.
- **Modes and their presets** live in `modes.py`.
- **OS-specific code** (notifications, the clock format, window placement) lives in `desktop.py`.
- **Prebuilt apps:** `pyinstaller --noconfirm packaging/groundhog-gamma-tuner.spec` builds the app for the OS you run it on. Pushing a `v*` tag builds all three on GitHub and opens a draft release.

## Credit

This project is a heavily modified fork of [bitaxe-temp-monitor](https://github.com/Hurllz/bitaxe-temp-monitor) by [Hurllz](https://github.com/Hurllz). Copyright in the original work remains with Hurllz. Copyright in these modifications is held by Andrey Rychkov. This copy is published at [Huldoser/groundhog-gamma-tuner](https://github.com/Huldoser/groundhog-gamma-tuner).

The upstream project also includes work by [DeanCollier](https://github.com/DeanCollier), Andrew Kuehne ([andewkuehne](https://github.com/andewkuehne)), [mrv777](https://github.com/mrv777), and GUI work credited to Birdman332. The headless web server and Docker setup from that project are not in this fork.

This is an unofficial tool. It is not affiliated with Hurllz, the Bitaxe project, or ESP-Miner.

## Disclaimer

This program writes frequency, voltage, fan, and overclock settings to your miners.

- **Max hashrate** runs above stock clocks and voltage. It is meant for custom cooling and a supply and wiring rated for it.
- **Balanced** and **Efficiency** stay inside stock-cooler temperatures, but still change clocks AxeOS would not change on its own.
- **Experimental boards** have not been run with this tuner by anyone yet.

Overclocking can overheat the ASIC, the regulator, the board, the plug, or the wiring. That can damage hardware or start a fire. A stock cooler, a weak supply, a loose plug, or a warm room makes that more likely. You are responsible for the cooling, power, and limits on your own hardware. Read the settings before you start, and lower them if you are not sure.

The software is provided as is, without warranty. Andrey Rychkov and the other authors are not responsible for damaged hardware, injury, fire, lost mining, or any other loss from using it. The legal warranty disclaimer is in the [MIT License](LICENSE).

## License

MIT. Copyright (c) 2025 Hurllz and Copyright (c) 2026 Andrey Rychkov. See [LICENSE](LICENSE).
