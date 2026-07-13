# power_report

Daily & weekly **electricity reports** for the host: min / max / average
wattage over the period, energy consumed (kWh), and the cost in **MDL** and
**USD**.

Power is measured the same way [vigil-tui](https://github.com/GIN-SYSTEMS/vigil-tui)
does — by reading the Linux kernel's **Intel RAPL** energy counters under
`/sys/class/powercap` and deriving watts from the counter delta over elapsed
time. Because RAPL exposes a *cumulative* energy counter (microjoules), the
energy total for a period is computed **exactly** (sum of counter deltas), not
by integrating sampled wattage.

## How it measures power

A source is auto-detected once at startup (`source: auto`):

| Priority | Source | What it covers |
|---|---|---|
| 1 | `psys` (`intel-rapl:1`) | Whole-SoC **platform** power — package + DRAM + on-package rails. Closest proxy to real draw. |
| 2 | `package` (`intel-rapl:0` + `dram`) | CPU package power, plus the DRAM controller if exposed. |
| 3 | `estimate` | `CPU% × cpu_tdp_watts`. No sensors needed — always works. |

Override with `source: psys | package | estimate` if you want to pin one.

> **Not a wall meter.** RAPL/psys measures the SoC, *not* the wall socket — it
> excludes the display, USB peripherals (external drives / hubs), and PSU /
> charger losses, so the reported cost is a **lower bound** on true draw. Set
> `overhead_watts` to add a flat always-on adder and calibrate toward a real
> plug meter.

## Reports

- **Daily** — last 24 h (default `0 9 * * *`, i.e. 09:00 in the bot's `TZ`).
- **Weekly** — last 7 days (default `0 9 * * 1`, Mon 09:00), with a per-day
  breakdown and an extrapolated 30-day monthly bill.

Both are broadcast to every `ALLOWED_USER_IDS` recipient. Omit a `*_schedule`
to disable that report. The **⚡ Power** button in the Telegram *Plugins* menu
shows live totals and a **Send report now** action for on-demand / testing.

Samples are aggregated into hourly buckets and persisted to `history_path`
(default `/data/power_history.json`), so reports survive bot restarts. On
restart the energy counter re-baselines (no double counting); a sampling gap
longer than `gap_reset_seconds` is skipped rather than mis-attributed.

## Requirements

The container must see the host RAPL tree — already wired in
`docker-compose.yml`:

```yaml
- /sys/devices/virtual/powercap/intel-rapl:/host/powercap:ro
```

It's mounted **outside `/sys`** on purpose: runc masks
`/sys/devices/virtual/powercap` with an empty tmpfs by default (a PLATYPUS /
CVE-2020-8694 side-channel mitigation), so a `/sys/...` bind mount just gets
re-masked. Binding the `intel-rapl` subtree to `/host/powercap` sidesteps the
mask; the plugin reads it via `powercap_root: /host/powercap`. `energy_uj` is
root-owned `0400`; the bot runs as root, so it can read it. With no Intel RAPL
present (or the mount removed) the plugin silently falls back to `estimate`.

Adding the mounts requires one `docker compose up -d` (recreate). After that,
tariff/schedule edits in `plugins.yml` (when `PLUGINS_YML_PATH=/data/plugins.yml`)
take effect on a plain `docker restart pi-control-bot`.

## Config

```yaml
power_report:
  mdl_per_kwh: 3.59          # electricity tariff
  mdl_per_usd: 17.63         # FX rate for the USD figure
  source: auto               # auto | psys | package | estimate
  cpu_tdp_watts: 15.0        # estimate-fallback ceiling
  overhead_watts: 0.0        # flat adder for draw RAPL can't see
  sample_interval_seconds: 20
  daily_schedule: "0 9 * * *"
  weekly_schedule: "0 9 * * 1"
  history_path: /data/power_history.json
  retention_days: 9          # bucket history kept on disk
  gap_reset_seconds: 120     # dt above this => re-baseline (skip sample)
  flush_interval_seconds: 300
```
