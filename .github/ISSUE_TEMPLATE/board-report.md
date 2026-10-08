---
name: Board report
about: Tell us how the tuner ran on your board, so an experimental board can be verified
title: "Board report: <family> <board version>"
labels: board-report
---

**Board:** <!-- for example Supra 402 -->
**AxeOS version:** <!-- the version the miner shows -->
**Mode:** <!-- Max hashrate, Balanced, or Efficiency -->
**Cooling:** <!-- stock / upgraded / custom: what you changed -->
**Supply:** <!-- the brand and rating, and how it connects -->
**Room temperature:** <!-- about -->

### How it went

- Clocks it settled at (MHz / mV):
- Good hashrate and power:
- Did AxeOS ever trip (overheat mode, a power fault, or a regulator shutdown)?
- Anything the tuner did that looked wrong:

### Miner info

Paste the reply from `http://<miner-ip>/api/system/info` while it was tuning. **Remove `ssid`, `macAddr`, `hostname`, `ipv4`, `ipv6`, `stratumUser`, and `fallbackStratumUser` first.**

```json

```

### Log

Paste the activity-log lines for this miner from the first few minutes, and from any trip or step-down.

```text

```
