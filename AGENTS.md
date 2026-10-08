# AGENTS.md

Groundhog Gamma Tuner tunes Bitaxe miners running official AxeOS from a desktop window on Windows, macOS, and Linux. It started as one owner's tuner for six custom-cooled Gamma 601s; that fleet's numbers stay as they are (see `docs/decisions.md`).

## Commands

- Run the app: `python main.py` (or `run.bat`, `run.sh`, `Groundhog Gamma Tuner.command`).
- After every change, run the push check before you finish. If `pre-commit` is not on PATH, use `.venv/bin/pre-commit` with the same arguments. It runs `ruff check`, `ruff format --check`, and `python -m unittest`, and only reports; it does not rewrite files.

  ```sh
  pre-commit run --all-files --hook-stage pre-push
  ```

- GitHub Actions (`.github/workflows/ci.yml`) runs the same checks on Windows, macOS, and Linux and measures branch coverage. The Linux job fails under 100%, so check coverage before you finish:

  ```sh
  python -m coverage run -m unittest
  python -m coverage report
  ```

## Supported boards and operating systems

- Boards are the rows of `boards.BOARDS`, copied from ESP-Miner `main/device_config.h` at `AXEOS_SOURCE`. A board AxeOS adds later is one new row from that release. Do not guess hardware numbers; cite the source file and tag.
- Keep the live `boards.board_for_info` check and the session check that the live board matches the saved one. Forks and unknown boards are refused.
- Only boards in `VERIFIED_LIMITS` are verified (today the 601). Every other board tunes inside `preset_limits` and needs a one-time `experimental_ok` confirmation on Start.
- Official AxeOS from `AXEOS_MIN_VERSION` (v2.11.0). Versions newer than `AXEOS_TESTED_VERSION` tune with a warning.
- The window is pywebview: WebView2 on Windows, Cocoa on macOS, GTK (or Qt) on Linux. The page is a local file inside the window. Do not add Docker, a network web server, Flask, a headless mode, or a Raspberry Pi service; the owner chose a desktop window only.
- Windows 10 and 11, on x64 PCs and on Windows on ARM. The window process must be 64-bit (x64) Python: on an x64 PC it starts directly; pywebview's .NET helper does not load in the ARM64 build, so `main.py` restarts an ARM64 process in the x64 one. Windows-11-only calls (square window corners) must fail quietly on Windows 10. macOS and Linux start directly.
- OS-specific code lives in `desktop.py` and stays behind a platform check, so every module imports on all three systems and the tests run on all three in CI.
- A packaged build (`packaging/groundhog-gamma-tuner.spec`) keeps `config.json` and `history.db` in `config.data_dir()`; a source checkout keeps them beside the scripts.

## Privacy

Privacy matters most to the owner.

- No account, analytics, telemetry, crash reports, or update pings. Nothing is ever sent to the author.
- Every read that leaves the local network gets its own switch (`weather_enabled` or one of `config.INTERNET_SWITCHES`), off in a new config and explained in the first-run setup, Global Settings > Internet, and the README's Privacy table.
- The location is only ever one the user typed or asked this device for.

## Dependencies

- Direct dependencies of the window live only in `requirements.txt`: today `requests` and `pywebview`. The push checks (`ruff`, `pre-commit`) and `coverage` for the CI gate live in `requirements-dev.txt` and are not part of the window install.
- When adding or changing a package, check PyPI and require at least the current release: `package>=X.Y.Z`. Do not pin an older release. Do not add an upper bound.
- Hold a newer release only when it breaks the window on one of the three systems (64-bit WebView2 on Windows, Cocoa on macOS, GTK on Linux), needs a Python older than 3.13 to stop working, or breaks the AxeOS calls this app makes. A newer pywebview does not change the 64-bit interpreter requirement.
- Leave transitive packages (`pythonnet`, `pyobjc`, `urllib3`, `certifi`, and the rest) to `requests` and `pywebview`. Do not add pywebview GUI extras (`gtk`, `qt`, `cef`) to `requirements.txt`.
- On Linux the window uses the distribution's GTK and WebKit (`python3-gi`, `gir1.2-webkit2-4.1`) through a `--system-site-packages` venv; `pip install "pywebview[qt]"` is the documented fallback, and the Linux release build bundles Qt that way.
- PyInstaller and Pillow are build tools. The release workflow installs them; they are not in either requirements file.

## Checks and tests

- No Ruff or test issues are allowed in code this change owns. A failure is related when it is in a file you changed, or it started because of your change. Fix it and run the check again. Apply Ruff's safe fixes and the formatter only to those files:

  ```sh
  python -m ruff check --fix path/to/file.py
  python -m ruff format path/to/file.py
  ```

- Fix a finding instead of silencing it: remove an unused name rather than adding `# noqa`.
- A failure in a file you did not change can be another agent's in-progress edit. Leave that file alone. Do not revert it, reformat it, or weaken the check to make the run green.
- New code needs tests for every branch. Reach Windows and macOS code with mocks (fake `ctypes.WinDLL`, a patched `sys.platform`), not a real system. Only a branch that cannot run gets `# pragma: no branch` or `# pragma: no cover`, with a comment saying why.
- Tests use `unittest.TestCase` and `unittest.mock`, not pytest. Keep them free of OS-specific paths and calls, and leave the network and live miners out.
- Session tests patch miner I/O with `patched_io` in `test_autotune.py`. `FakeMiner` and `run_session` in `test_autotune_session_paths.py` script a miner and can stop a session at the wait after a given status or log line.
- Upgrade tests in `test_upgrade.py` load `config.json` as each older version saved it.

## Tuning decisions

Applies to `autotune.py`, `boards.py`, `modes.py`, `tools/simulate.py`, and their tests.

### `decide_adjustment`

- It stays pure: inputs in, `(frequency, voltage, reason)` out. It does not call HTTP, read `config.json`, or start threads. A new branch gets a `DecisionTests` case that calls `decide_adjustment` directly.
- Frequency steps down on heat, power, input sag, power fault, core-voltage droop, and rejected shares. It also steps down when quality is bad (error percentage, short hashrate, or above-target) and voltage cannot rise.
- Within `FIRMWARE_TRIP_MARGIN_C` of the AxeOS overheat trip (75°C ASIC, 105°C VR), the trip guard sheds `TRIP_GUARD_FREQUENCY_STEPS` and one voltage step. `clamp_limits` keeps `max_temp` and `max_vr_temp` under that margin.
- Clocks under `min_freq` or `min_volt` (AxeOS saves clocks 100 MHz / 100 mV lower after a trip) return `restore floor` once the chip is cool.
- A quality retreat at the frequency floor holds (`shed_voltage_at_floor=False`); it never lowers voltage. A heat retreat sheds one voltage step while errors are at most half the budget, and at the floor only while errors fit the budget.
- Voltage rises when quality is bad and it is under the cap. At the ceiling, trim lowers voltage.

### Sessions

- The session sets the fan from the miner's mode (`modes.fan_payload`): manual 100% in Max hashrate, AxeOS auto fan aiming 5°C under the ASIC cap in Balanced and Efficiency. `fan_matches` notices when AxeOS turned auto fan off after a trip, and the session puts it back.
- Nothing learned is saved to `config.json` or read back from it, and `history.db` is for the dashboard only. A session opens at `live_start_clocks` (the clocks a healthy miner reports now, inside its limits) when `continue_from_live` is on, otherwise at the start clocks.
- With `fast_start` on, `ramp_session` jumps toward `RAMP_HEADROOM_C` under the caps before the fine-tuning loop starts. Its math is pure (`ramp_voltage`, `thermal_fit`, `ramp_target`) and gets `DecisionTests` cases like `decide_adjustment`.
- Shared session tests run with `continue_from_live` and `fast_start` off; fast-start session tests turn `fast_start` on.

### Board limits

- Board hardware and hard ranges live in `boards.py`: the 601's are `VERIFIED_LIMITS["601"]`, and `HARD_*`, `STOCK_*`, and `TPS546_*` in `config.py` are aliases of that board.
- Pure functions take board values as arguments that default to the 601's (`clamp_limits(limits, board)`, `ramp_target(..., board=)`, `asic_off_watts`, `vr_temp_required`, `hashrate_per_mhz`). A board without a regulator sensor passes `vr_temp_required=False`, so a missing `vrTemp` does not hold the climb; that branch keeps its own `DecisionTests` case.
- Leave `GAMMA601_LIMITS` and the 601's `VERIFIED_LIMITS` unchanged unless the user asks. The user asked for the current values on 2026-10-06 (70°C ASIC, 95°C VR, 1500 mV max and hard cap) after repasting the fleet, and for 29 A core current. Those numbers match this cooling and this supply.
- An update never changes a value the user saved: new defaults reach new miners only, and `_record_limits_version` just stamps `LIMITS_VERSION` on an older config. Do not add a load-time step that rewrites saved limits; tell the user to change them in AutoTuner Settings instead.
- `HARD_MIN_FREQ` is 350 MHz, the lowest BM1370 preset in AxeOS v2.15.1. `HARD_MIN_VOLT` stays 1000 mV, the lowest BM1370 voltage preset, because voltage is stepped down only after frequency is already at its floor. The trip guard and a heat retreat with error margin are the exceptions.

### Modes

- Each miner has a mode (`modes.py`): Max hashrate, Balanced, or Efficiency. A mode sets the objective, the fan, the fast-start headroom, and a limit preset per board; the per-miner limits stay the source of truth for the session.
- `objective=EFFICIENCY` in `decide_adjustment` answers chip errors with a lower clock while there is one (never more voltage), and trims voltage at a heat wall above `trim_floor_voltage`.
- The session judges Efficiency steps with `efficiency_step_paid` (energy per good hash), retries a trim that lost `EFFICIENCY_TRIM_FREQUENCY_STEPS` lower, and never raises voltage to retry a step. Hashrate modes keep the old judgement on good hashrate.

### Pinned results

- `tools/simulate.py` runs `monitor_and_adjust` against a simulated Gamma 601 on a fake clock. `test_simulation.py` checks that Max gets the most hashrate, Efficiency the lowest J/TH, and Balanced stays inside its caps. A change to the session that moves those results needs a reason.
- `test_fleet.py` pins the owner's fleet (Gamma 601, Max hashrate) to `FLEET_DECISIONS_SHA256`, a fingerprint of the tuner's answers taken from the code before boards and modes existed. Other boards and modes must not move it. Update it only when the user asks to change how their fleet is tuned.
- `EveryBoardTests` in `test_modes.py` keeps every board's presets and decisions inside its limits.

## AxeOS client and network reads

- Miner traffic is these three calls. Do not add another endpoint.
  - `GET http://{ip}/api/system/info`
  - `PATCH http://{ip}/api/system` with `coreVoltage`, `frequency`, `overclockEnabled`, and fan keys `autofanspeed` / `manualFanSpeed` / `temptarget` (35–66°C). GET reports the live fan percent as `fanspeed`; AxeOS ignores a PATCH `fanspeed`.
  - `POST http://{ip}/api/system/restart`
- `get_system_info` returns a dict or an error string (check `isinstance(info, str)` and log it as an error). `patch_system` returns `(ok, error_text)`.
- A missing `errorPercentage` skips that miner (every ASIC reports it in v2.15.3).
- Read `ASICModel` and `boardVersion` through `boards.board_for_info`, the ASIC temperature through `boards.asic_temp` (the hotter of `temp` and `temp2`), and core current through `core_current_amps(info, board)`: on INA260 boards `current` is input current and `vrTemp` is 0. `asicCount` is not in the reply; it comes from the board table.
- The network panel probes the TCP port of each pool the miners report (`stratumURL`/`stratumPort` and the fallback pair). Two other HTTP reads stay as they are, and they are not miner endpoints: `DIFFICULTY_URL` for mempool difficulty and `FIRMWARE_RELEASES_URL` for the official stable-tag notice, both in `dashboard.py`. Each of the three runs only while its switch in `config.INTERNET_SWITCHES` is on (`pool_check_enabled`, `network_stats_enabled`, `firmware_check_enabled`); `_network_loop` reads them every round.
- Weather reads live in `weather.py` and are not miner endpoints either: Open-Meteo forecast (current every 15 minutes, hourly for backfill), Open-Meteo geocoding for a typed place, and one Nominatim reverse lookup with the app's `User-Agent` after a device location. The device location comes from Windows `GeoCoordinateWatcher` through PowerShell, with no extra package, only when the user clicks for it, and is rounded to `weather.DEVICE_DECIMALS` before it is named or saved.

## `config.json`

- Writes go through `save_config` / `_write_config`: hold `_config_lock`, write a temp file in the same directory, then `os.replace` onto `config.json`. Never open `config.json` for writing directly.
- A `JSONDecodeError` keeps `_last_good_config`. `_drop_retired_keys` removes `RETIRED_GLOBAL_KEYS` and each miner's `RETIRED_MINER_KEYS` (learned setpoints from older versions) on load and save.
- `detect_miners` checks `boards.board_for_info` before `new_miner_record`, and the record saves that board's version as `board`. `add_miner` does not perform that check.
- `board_limits(board, config)` fills a new record: the 601 gets `GAMMA601_LIMITS`, other boards get preset-bound caps (65°C / 85°C, the regulator's warning current, the family's `maxPower`), and a limit the board has no sensor for (`Board.unused_limit_fields`) is saved blank. A miner on an unverified board gets `experimental_ok` once its first Start is confirmed.
- An update never changes a value the user saved: it only adds missing keys, drops `RETIRED_*_KEYS`, and stamps versions. Before it rewrites the file it keeps the original as `config.backup-v<saved config_version>.json` (gitignored, never overwritten).
- `_record_boards` brings an older config up to `CONFIG_VERSION` once: version 1 gives each miner its board ("601" before boards were recorded), version 2 its mode (Max hashrate on a 601) and marks an existing config `setup_done` with weather on, version 3 turns every `INTERNET_SWITCHES` key on, as those reads ran before they had switches.
- A fresh config has `default_mode` Balanced, `setup_done` false, `weather_enabled` and every `INTERNET_SWITCHES` key false, and a blank `supply_watts` until the first-run setup sets them.
- `config.data_dir()` puts the file beside the scripts, or in the user's data folder for a packaged build. `config.json` stays gitignored.

## Releases and saved data

Until the first release (`test_fixtures/released/` is empty), the owner is the only user, so `config.json` and `history.db` may change shape without a migration. The owner's own config must still load (`test_fleet.py`, `test_upgrade.py`).

From the first release on:

- Before each `v*` tag, run `python tools/snapshot_release.py vX.Y.Z` on the code being released and commit `test_fixtures/released/vX.Y.Z/`. The release workflow runs `--check` and refuses a tag without a matching snapshot.
- `test_upgrade.ReleasedVersionTests` loads every released snapshot with the current code and checks that each saved value is kept and history can still be read and written.
- A change that renames, removes, or changes the meaning of a saved key needs a `CONFIG_VERSION` bump and a step in `_record_boards` that converts the old value. A new or changed `history.db` column needs a schema migration in `history.py`, not just a new `CREATE TABLE`.
- When `ReleasedVersionTests` fails, write the migration. Never edit, delete, or regenerate a released snapshot to make it pass.

## Dashboard window

Applies to `dashboard.py`, `history.py`, `weather.py`, and `web/`.

- The window loads an absolute path to `web/index.html` with `js_api=DashboardApi`. A relative `url` makes pywebview start its own server.

  ```python
  page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
  webview.create_window("Groundhog Gamma Tuner", url=page, js_api=DashboardApi(self))
  ```

- A new page action is a public method on `DashboardApi` plus a call through `api()` in `web/app.js` (`const bridge = api(); if (bridge) bridge.start_autotuner();`). Method names stay public; pywebview skips `_` names. The page polls `get_snapshot`.
- The page is `web/index.html`, `web/app.css`, `web/app.js`, and `web/history.js` (the History screen and the weather location dialog). Keep it plain: no npm, framework, bundler, chart library, or extra listener. Charts are inline SVG drawn in `history.js`, with the categorical `--series-*` colors in fixed order by miner.
- OS-specific helpers (notifications, the clock format, Windows window placement) live in `desktop.py`; `dashboard.py` calls `notify`, `format_local_time`, and `snap_fullscreen_window`. The window opens maximized on every OS.
- On first start (`setup_done` false) the page opens the setup dialog: `get_setup_state`, then `complete_setup` maps cooling and goal to the mode for new miners through `setup_mode`.
- AutoTuner Settings sends each row's `presets` per mode, the board's `unused` fields (no sensor), and a `custom` flag. The network panel shows the pools the miners report (`pool_targets`), not a fixed list.
- The History screen reads `get_history`. The display loop hands each round of readings to `history.HistoryRecorder`, which writes `history.db` next to `config.json` every 10 minutes. Outdoor weather is read only while `weather_enabled` is on. Tests that touch it run under a temporary config.
