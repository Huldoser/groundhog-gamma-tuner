# Roadmap

What comes next for Groundhog Tuner, as of 2026-10-08. Items are sorted by priority, then by size:

- **S:** a few days.
- **M:** about a week.
- **L:** several weeks.
- **Hardware:** needs a board, not code.

Each item gets its own design before work starts. The order changes as people report boards and problems. To suggest something, [open an issue](../../../issues/new).

## Scope

The app tunes boards running **official AxeOS**, which today means the Bitaxe family. Forks and clones are refused. The first board outside that family to consider is the NerdQAxe++ (see [Later](#later)), once several Bitaxe boards are verified.

## Now

| Item | Size | Why |
| --- | --- | --- |
| **Power check** | S | A weak or sagging 5 V supply is the most common Bitaxe problem: the regulator needs 4.8 V to start and shuts off at 4.5 V. The tuner already stops climbing at the input floor and marks it "input sag", but does not say that the supply or the cable, not the chip, is the limit, or what to check. It should say so in plain words ("held back by the supply: 4.89 V at 18 W"), and flag a large gap between the core voltage set and the one measured. |
| **Flatline watchdog** | M | The "Flatline of Death" is the most-discussed open AxeOS bug ([ESP-Miner #1053](https://github.com/bitaxeorg/ESP-Miner/issues/1053)): the hashrate freezes and no shares arrive until a restart. The tuner can already detect it and restart the miner once, but that is off by default and only works while a miner is being tuned. It should be on for new installs, also watch miners that are not being tuned, show a notice, and count flatlines in History. |
| **First release** | M | The release workflow builds Windows, macOS, and Linux apps, but has never run on a tag. Without a download, only people who use git and Python can run the app. Each build needs a test on its system first. |
| **Board report export** | M | Only the Gamma 601 is verified, out of about 25 board versions. One click should copy what the [board report](../.github/ISSUE_TEMPLATE/board-report.md) asks for: board, firmware, mode, the clocks it settled at, temperatures, and trips. It should leave out the IP address, hostname, Wi-Fi name, MAC address, and pool user, which people now have to delete by hand. |

## Next

| Item | Size | Why |
| --- | --- | --- |
| **Electricity cost** | S | A price per kWh that you type in, giving cost per day and per TH on the dashboard and in History. Nothing is looked up online. |
| **Fan cap per miner** | M | Fan noise is a common reason people stop mining at home, and NerdQAxe firmware added a temperature target to run quietly. In Balanced and Efficiency, you pick a top fan speed and the tuner treats the heat as a limit, lowering clocks instead of spinning the fan up. Max hashrate keeps the fan at 100%. |
| **Schedules** | M | A different mode by time of day: Efficiency at night, during peak electricity prices, or outside solar hours. AxeOS owners ask for the same in [ESP-Miner #601](https://github.com/bitaxeorg/ESP-Miner/issues/601) (scheduled display off). |
| **More verified boards, starting with a stock Gamma 601** | Hardware | Every board except the author's upgraded 601s is experimental. This needs [reports](../.github/ISSUE_TEMPLATE/board-report.md) or [donated boards](../README.md#send-a-board). |

## Later

| Item | Size | Why |
| --- | --- | --- |
| **Notice for a new best difficulty** | S | The alert solo miners like most in monitoring apps. The dashboard already shows the best difficulty. |
| **History export to CSV** | S | So people can chart and share their own data. The file stays on your computer. |
| **Code-signed builds** | M | Removes the "Windows protected your PC" and "cannot be opened" warnings. It costs money every year, so it depends on [sponsors](../README.md#support-the-project). |
| **NerdQAxe++ support** | L | The most popular AxeOS-based miner outside the Bitaxe family. Its firmware is a fork with its own fan control, four BM1370 chips, and a 12 V input, so the tuner needs a separate board model and an owner to test it. |

## Not planned

- **A network server, Docker, Umbrel, Home Assistant, or a headless mode.** The app opens no port. Monitoring apps already cover those.
- **Closed clones and other forks** (for example Lucky Miner firmware). Their safety behavior differs from official AxeOS.
- **Telemetry, or any internet feature that starts switched on.** See [Privacy](../README.md#privacy).
- **Phone apps.** Maybe some day; not planned.
