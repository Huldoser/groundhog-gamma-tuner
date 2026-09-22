# Bitaxe Gamma 601 Auto-Tuner

A personal desktop tuner for one setup: custom-cooled Bitaxe Gamma 601 boards (ASIC BM1370, board version 601) on a Windows ARM tablet. The clocks, voltage, temperatures, and power guard in this repository are highly optimized for that cooling and that power supply. The app checks the live board and saves a miner only when AxeOS reports a BM1370 on board 601. It refuses every other board. It is not a tuner for a stock Gamma, and it is not a multi-model Bitaxe app.

It runs as a Python Tk window, watches the miner's AxeOS API, and adjusts frequency and voltage to hold a higher hash rate without crossing that board's limits.

## Credit

This project is a heavily modified fork of [bitaxe-temp-monitor](https://github.com/Hurllz/bitaxe-temp-monitor) by [Hurllz](https://github.com/Hurllz). Copyright in the original work remains with Hurllz. Copyright in these modifications is held by Andrey Rychkov. This copy is published at [Huldoser/bitaxe-temp-monitor](https://github.com/Huldoser/bitaxe-temp-monitor).

The upstream project also includes work by [DeanCollier](https://github.com/DeanCollier), Andrew Kuehne ([andewkuehne](https://github.com/andewkuehne)), [mrv777](https://github.com/mrv777), and GUI work credited to Birdman332. The headless web server, Docker setup, and Linux, macOS, and Raspberry Pi launch paths from that project are not in this fork.

This is an unofficial tool. It is not affiliated with Hurllz or the Bitaxe project.

## What it does

1. Confirms the board is a Gamma 601, enables overclocking, and applies the last good frequency and voltage for that chip, or the starting setpoint.
2. Polls AxeOS and waits until the miner reports the new setpoint before the next step.
3. Lowers frequency if ASIC temperature, regulator temperature, power, input voltage, or core-voltage droop crosses the limit.
4. Raises voltage only when the ASIC error percentage is above the budget, then raises frequency while errors stay inside that budget.
5. Trims voltage down at the ceiling, then holds. The last good setpoint is saved so the next start does not begin from stock. The fan stays at full speed for the whole session, so a warmer room is what moves the clocks.

## This setup

These Gammas use a custom shell with better cooling than a stock board. Most can hold higher clocks than the usual 700–800 MHz, but each BM1370 is different and the tuner stops that chip on heat or errors. It does not assume every board can hold the frequency cap.

The power supply is oversized for this setup and is not the tuning limit. The 50 W figure is a fault guard. If a barrel jack or board trace ever runs hot, lower that miner's watt cap.

Operating targets are about 65°C on the chip and 85°C on the regulator. The tuner stops climbing there and steps frequency down before 70°C on the chip and 90°C on the regulator. A hotter afternoon takes a larger step than a one-degree drift. A cooler night lets a chip that is still under its frequency cap climb again.

AxeOS will still emergency-stop at 75°C on the ASIC or 105°C on the regulator, then restart about 100 MHz and 100 mV lower. These limits stay under that, so the tuner remains the controller. Core voltage stays at or below 1300 mV.

Those targets match this cooling and this power supply. Another board, a stock cooler, or a smaller supply needs its own limits.

## Requirements

- Windows on ARM tablet
- Python 3 for Windows ARM64, from [python.org](https://www.python.org/downloads/windows/). The installer includes Tkinter.
- The `requests` package listed in `requirements.txt`

## Install

Open Command Prompt in this folder:

```bat
pip install -r requirements.txt
```

## Run

```bat
python main.py
```

The tablet and the Gamma 601 need to be on the same network. Add the miner by IP, or scan a range. The app saves a miner only after AxeOS reports a BM1370 on board 601.

## Desktop shortcut

Right-click `launch.bat` and choose **Send to > Desktop (create shortcut)**. That shortcut starts the window with no console. `launch.bat` switches to this folder first. Settings are saved in `config.json` next to the scripts. If that file is missing, the app creates it. `config.example.json` is the starting template, with an empty miner list.

## Start when you log on

Use Task Scheduler so the window opens after you sign in.

1. Press Win+R, type `taskschd.msc`, and press Enter.
2. Choose **Create Basic Task**. Name it `Bitaxe Auto-Tuner`.
3. Trigger: **When I log on**.
4. Action: **Start a program**.
5. Program: the full path to `launch.bat`. Example: `C:\Users\YourName\bitaxe-temp-monitor\launch.bat`
6. On **Conditions**, clear **Start the task only if the computer is on AC power**.

## Disclaimer

This program writes frequency, voltage, fan, and overclock settings to a miner. The limits in this repository were chosen for the author's custom-cooled Gamma 601 boards and oversized power supply. They are above stock clocks. They are for that cooling and that power, not for someone else's board.

Overclocking can overheat the ASIC, the regulator, the board, or the barrel jack. That can damage the hardware or start a fire. A stock cooler, a weak supply, a loose jack, or a warm room makes that more likely.

You are responsible for the cooling, power, and limits on your own hardware. Read the settings before you start, and lower them if you are not sure.

The software is provided as is, without warranty. Andrey Rychkov and the other authors are not responsible for damaged hardware, injury, fire, lost mining, or any other loss from using it. The legal warranty disclaimer is in the [MIT License](LICENSE).

## License

MIT. Copyright (c) 2025 Hurllz and Copyright (c) 2026 Andrey Rychkov. See [LICENSE](LICENSE).
