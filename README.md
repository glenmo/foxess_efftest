# foxess_efftest

Dedicated dashboard for an AC-to-AC round-trip efficiency test of the
Fox H3-15.0-SMART hybrid inverter.

Reads live data every 1 s from the existing `fox-monitor` service
(`http://localhost:5000/api/fox/data`), auto-detects discharge / charge
phases by hysteresis on `battery_power_w`, integrates AC and DC energy
per phase, and shows the round-trip efficiency live as the second phase
accumulates (no longer waits for the charge to fully close).

A daily CSV is written to `./data/YYYY-MM-DD.csv` every 5 s, rotating
at local-midnight.

### Published test result

A worked example using this app is documented in the test report —
available as both PDF (for reading / sharing with vendor engineering)
and DOCX (editable source):

- 📄 [`reports/Fox_H3_Round_Trip_Test_Report_2026-05-18.pdf`](./reports/Fox_H3_Round_Trip_Test_Report_2026-05-18.pdf)
- ✏️ [`reports/Fox_H3_Round_Trip_Test_Report_2026-05-18.docx`](./reports/Fox_H3_Round_Trip_Test_Report_2026-05-18.docx)

The test was run at **Smart Energy Lab** (Victoria, Australia) on a
Fox H3-15.0-SMART paired with nine EQ4800 modules (43.2 kWh nameplate).
Headline result: **92.49 % AC-to-AC round-trip efficiency**, independently
verified by a calibrated Hioki CW12x grid-port logger to within ±2 % once
concurrent household loads (≈1.2 kW continuous) are reconciled. The full
report covers methodology, instrumentation, reconciliation tables,
discussion against the H3 datasheet, and recommendations for future
testing.

Contact: Glen Morris · Smart Energy Lab · glen@smartenergylab.com.au

## What it does

```
fox-monitor (port 5000) ──HTTP──► foxess_efftest (port 8900)
       ▲                                  │
       │ Modbus TCP                       ├─ Auto-phase detector
       │ 192.168.11.81:502                ├─ Wh integrators (AC + DC)
                                          ├─ Coulombic SOC estimator
                                          ├─ Daily CSV writer
                                          └─ Web dashboard
```

Reads, never writes — fox-monitor and this app coexist cleanly on the
same host. No second Modbus connection to the dongle.

## Where the readings come from

The dashboard shows several power values that look similar but measure
**different points** in the AC system. Knowing which is which matters
when interpreting the efficiency result.

```
                  ┌─────────────┐
   Grid ──┬───────│ Grid CT     │──────┬──── House loads
          │       │ (meter_pwr) │      │     (fridge, lights, kettle…)
          │       └─────────────┘      │
          │                            │
          │              ┌─────────────┴─┐
          └──────────────│ Fox H3 AC port │  ◄── ac_power_w
                         │                │
                         │   DC battery   │  ◄── battery_v × battery_a
                         │   port         │
                         └────────────────┘
                                 │
                              Battery
```

- **AC Power** (`ac_power_w`) — measured at the inverter's own AC
  terminals. What the Fox is pushing or pulling on the AC side.
  **This is what the efficiency engine integrates.**
- **Grid Meter** (`meter_power_w`) — measured at the external CT, the
  boundary with the utility. Differs from AC Power by whatever house
  loads sit between them:

  ```
  ac_power_w  ≈  meter_power_w  +  house_loads
  ```

  Example during discharge: inverter pushes 5 kW (`ac_power_w = +5000`)
  but the house is drawing 1.5 kW behind the meter, so only 3.5 kW
  reaches the grid (`meter_power_w = +3500`).
- **Load Power** (`load_power_w`) — power leaving via the Fox's backup
  (EPS) port. **Not used** in the AC-AC test — for round-trip, loads
  should be on the grid side, not the backup port.
- **DC Power** — `battery_v × battery_a`, what crosses the battery
  terminals. Used for the DC half of the round-trip ratio.

For a clean round-trip number, household loads should cancel between
the charge and discharge phases — but they add noise. Ideally run with
a known resistive sink (dump heater, kettle bank) and household loads
off so `ac_power_w` and `meter_power_w` agree to within a few hundred
watts.

## Dashboard

- **SOC (reported)** — integer % from the inverter
- **SOC (Coulombic)** — derived high-precision estimate from `∫ battery_a dt`
  divided by the battery's nominal Ah capacity (configured per install).
  Displayed to 2 decimal places.
- **Battery Power** — signed (+ charge, − discharge) in W or kW
- **Phase** — `idle` / `discharge` / `charge`, with hysteresis confirmation
- **Round-trip efficiency** — AC and DC, computed from the latest discharge
  paired with the next charge (charge does **not** need to be complete; the
  card shows `(live)` while the charge phase is still in progress and
  firms up once the phase closes).
- **Battery Temp** — hottest cell temperature (BMS1 max), the safety-relevant
  metric. Falls back to BMS1 ambient → BMS1 min → legacy `battery_temperature`
  field if a future firmware renames the upstream channel.
- Detail tiles — battery V, A, temp, SoH, AC power, grid meter, PV, PF, freq
- Two charts — SOC vs time, battery & AC power vs time. Legend uses
  solid-fill colour rectangles matching each line.
- Phase summary cards — start / end / duration / DC kWh / AC kWh / avg & peak
- "Reset Coulombic anchor" button — call this when the battery is at a
  known SOC (e.g. fully-charged 100 %) for a clean integration baseline

## Phase detection (hysteresis)

```
idle
  └─ battery_power_w ≤ -200 W for 30 s        → discharge (record start)
  └─ battery_power_w ≥ +200 W for 30 s        → charge    (record start)
discharge / charge
  └─ |battery_power_w| ≤ 50 W for 60 s        → idle (close phase, integrate)
```

Thresholds and confirmation durations are CLI-tunable.

## CSV schema

`./data/YYYY-MM-DD.csv` — one row every 5 s, columns:

```
timestamp,phase,soc_pct,soc_coulombic_pct,battery_v,battery_a,
battery_power_w,battery_temp_c,soh_pct,
ac_power_w,meter_power_w,load_power_w,pv_total_power_w,
grid_frequency,power_factor
```

## Quick start

```bash
# On desky
git clone <repo> ~/foxess_efftest
cd ~/foxess_efftest
sudo bash install.sh --battery-capacity-ah 95    # ← set your battery's Ah
```

Defaults: listens on `0.0.0.0:8900`, upstream `http://localhost:5000`,
CSV dir `./data`. Open `http://desky.local:8900/`.

## SMA WebBox live Modbus probe

This repo also includes a direct, read-only Modbus TCP probe for SMA Sunny
WebBox systems with Sunny SensorBox / MeteoStation channels. It is intended to
replace workflows that depend on CSV files downloaded from the WebBox.

```bash
cd ~/foxess_efftest
source venv/bin/activate
python sma_webbox_probe.py --web-port 8910
```

Open `http://desky.local:8910/`, enter the WebBox IP address, then start the
live probe. The app:

- polls Modbus TCP directly on port 502
- scans multiple unit IDs, because WebBox installations expose devices behind
  different IDs depending on configuration
- reads common SMA candidate registers first
- adaptively scans status, power, energy, and weather register ranges
- displays named values plus raw non-empty register blocks for follow-up
  mapping
- never writes Modbus registers

To install it as a separate service on `desky.local`:

```bash
sudo bash install_sma_webbox_probe.sh --port 8910
```

For a one-shot CLI probe:

```bash
python sma_webbox_probe.py --once --host 192.168.1.50 --unit-ids 1-20,126,255
```

Useful options:

```
--web-port                 dashboard port, default 8910
--host                     WebBox host for --once mode
--port                     Modbus TCP port, default 502
--unit-ids                 IDs to scan, default 1-10,126,255 in CLI
--ranges                   register ranges, e.g. 30001-30100,34601-34680
--include-input-registers  also try function code 04
--timeout                  per-request timeout in seconds
--max-block                largest scan request block, default 20
```

### Working out `--battery-capacity-ah`

```
nominal_Ah  =  pack_kWh × 1000 / pack_nominal_voltage
```

This install: 9 × Fox EQ4800 modules × 4.8 kWh = 43.2 kWh, pack
nominal ≈ 460 V → ~95 Ah. Use the data-sheet Ah if you have it
(more accurate than back-calculating from kWh and a nominal V).

## CLI options

```
--host                  Flask listen address           (default 0.0.0.0)
--port                  Flask listen port              (default 8900)
--upstream              fox-monitor base URL           (default http://localhost:5000)
--poll-interval         API poll interval (s)          (default 1.0)
--csv-interval          CSV log interval (s)           (default 5.0)
--csv-dir               where to write CSVs            (default ./data)
--site-name             page header                    (default "Fox H3 Efficiency Test")
--battery-capacity-ah   nominal battery Ah for SOC est (default 0 = disabled)
--discharge-threshold-w battery_power_w for discharge  (default -200)
--charge-threshold-w    battery_power_w for charge     (default 200)
--idle-threshold-w      |battery_power_w| for idle     (default 50)
--confirm-active-s      seconds before declaring phase (default 30)
--confirm-idle-s        seconds of idle before closing (default 60)
```

To tweak after install, edit `/etc/systemd/system/foxess-efftest.service`
and `sudo systemctl restart foxess-efftest.service`.

## Test protocol — recommended

1. **Set up.** Decommission anything else doing aggressive discharge or
   charge on the AC line during the test window. Loads on the **grid
   side** of the inverter, not the backup port — the AC-AC efficiency
   is measured at `ac_power_w` (inverter AC terminals), so the load
   needs to be on the same side of the meter as those terminals.
2. **Charge to 100 %**, let the battery rest 30 min. The OCV settles
   and the BMS's SOC anchor stabilises.
3. **Click "Reset Coulombic anchor"** on the dashboard when SOC reads
   100 %. This is the integration baseline.
4. **Start discharge** at a steady AC load — ideally 3–5 kW resistive
   so the Wh integral is clean and the phase detector doesn't wobble.
5. Watch the Discharge phase summary fill in. SOC will tick down
   integer-by-integer while the Coulombic SOC moves smoothly.
6. **Stop at ~10 % SOC** (be careful not to hit the BMS cutoff). Let
   the battery rest 5–10 min before reversing.
7. **Start charge** at a similar steady rate to the discharge.
8. When SOC hits 100 % again, the Round-trip efficiency card will show:

   ```
   AC round-trip = AC_out / AC_in
   DC round-trip = DC_out / DC_in     (≈ Coulombic efficiency)
   ```

9. Download the CSV via the link on the dashboard for archival /
   spreadsheet analysis.

## Round-trip computation

The round-trip card uses the **latest discharge phase** paired with the
**next charge phase** (where "next" means started after the discharge
began). Both phases must have accumulated ≥ 500 Wh to filter out brief
sub-experiment blips that would otherwise pollute the ratio.

```
AC round-trip = |AC out during discharge| / |AC in during charge|
DC round-trip = |DC out during discharge| / |DC in during charge|
```

While the charge phase is still in progress, the card shows the live
number with a `(live)` suffix; the value firms up when the phase closes.

> **Earlier bug fixed (May 2026):** the card used to require both phases
> to be fully closed, which meant the in-progress charge was ignored
> and the engine fell back to whichever trivial earlier "charge" blip
> happened to be in the log — giving wildly wrong numbers (e.g. 46,000 %).
> Now resolved.

## Retrospective analysis from CSV

The in-memory phase detector loses state if the service restarts mid-test
(daemon thread; no on-disk persistence yet). The CSV however captures
every reading. To compute the round-trip retrospectively from any day's
CSV:

```bash
python3 <<'PY'
import csv
from pathlib import Path
from datetime import datetime

csv_path = Path.home() / "foxess_efftest" / "data" / "2026-05-18.csv"
with csv_path.open() as f:
    rows = list(csv.DictReader(f))

# Walk phase events (continuous runs of identical phase)
events = []
current = None
prev_t = prev_ac = prev_dc = None
for r in rows:
    t = datetime.fromisoformat(r["timestamp"])
    phase = r.get("phase", "idle")
    ac = float(r.get("ac_power_w") or 0)
    dc = float(r.get("battery_v") or 0) * float(r.get("battery_a") or 0)
    if current is None or current["phase"] != phase:
        if current is not None: events.append(current)
        current = {"phase": phase, "t_start": t, "t_end": t,
                   "ac_wh": 0.0, "dc_wh": 0.0}
    if prev_t is not None:
        dt_h = (t - prev_t).total_seconds() / 3600.0
        if 0 < dt_h < 0.05:                  # trust the trapezoid
            current["ac_wh"] += (ac + prev_ac) / 2 * dt_h
            current["dc_wh"] += (dc + prev_dc) / 2 * dt_h
    current["t_end"] = t
    prev_t, prev_ac, prev_dc = t, ac, dc
if current: events.append(current)

big = [e for e in events if abs(e["ac_wh"]) > 500 or abs(e["dc_wh"]) > 500]
for e in big:
    dur = (e["t_end"] - e["t_start"]).total_seconds() / 60
    print(f"  {e['phase']:>10}  {e['t_start'].strftime('%H:%M')}→"
          f"{e['t_end'].strftime('%H:%M')} ({dur:>5.0f}m)  "
          f"AC={e['ac_wh']/1000:>7.2f} kWh  DC={e['dc_wh']/1000:>7.2f} kWh")

d = [e for e in big if e["phase"] == "discharge"]
c = [e for e in big if e["phase"] == "charge"]
if d and c:
    d, c = d[-1], [x for x in c if x["t_start"] > d[-1]["t_start"]][-1]
    print(f"\n  AC round-trip = {abs(d['ac_wh'])/abs(c['ac_wh'])*100:.2f} %")
    print(f"  DC round-trip = {abs(d['dc_wh'])/abs(c['dc_wh'])*100:.2f} %")
PY
```

## Sanity-checking against an external power logger

For a published-quality result, run a calibrated three-phase power logger
(e.g. Hioki CW12x) at the inverter's grid port in parallel. Expect the
two measurements to disagree by whatever continuous household load is
present during the test — the difference reconciles cleanly:

```
ac_power_w (Fox internal)  ≈  grid_logger Wh ± house_loads_during_test
```

A ~1.2 kW continuous house load over 8 hours of test time accounts for
≈ 9.6 kWh "missing" from the grid-side measurement (5.6 kWh during the
discharge plus 4.0 kWh during the charge). The Fox's internal AC
measurement is the right number for inverter round-trip efficiency;
the external logger's number is the system round-trip including
parasitic loads.

The published test report (linked at the top of this README) contains
a worked reconciliation table.

## Notes

- **Effective sample rate** = `fox-monitor`'s Modbus poll cadence
  (10 s by default). The dashboard refreshes its display every 1 s but
  the values only change when `fox-monitor` publishes new ones. That's
  fine for an efficiency test — battery V, I, temp don't move that
  fast. If you want sharper resolution, lower `--fox-poll` in
  `fox-monitor` (its README warns about going below 5 s on the dongle).
- **Coulombic SOC** is only meaningful with `--battery-capacity-ah`
  set to the battery's nominal Ah. Without that the dashboard shows
  the integer reported SOC only.
- **Daily CSV rotation** is at local-midnight. If the test spans
  midnight, you'll get two CSVs to splice.
- **Restart resilience — Coulombic.** If the app restarts mid-test the
  Coulombic anchor is re-set to whatever SOC the inverter is reporting
  at that moment, losing accumulated precision since the last anchor.
- **Restart resilience — phases.** Phase events are held in memory only;
  a service restart loses today's discharge / charge history and the
  live round-trip card returns `null` until a new pair accumulates.
  Use the CSV analyser above to recover round-trip numbers from any
  day's data — the underlying readings survive even when the in-memory
  phase detector doesn't.
- **Try not to restart mid-test.** Both restart limitations above
  compound — Coulombic precision plus phase history both reset.
- **Battery temp field mapping (May 2026).** fox-monitor publishes
  `bms1_max_temp` / `bms1_min_temp` / `bms1_ambient_temp` (Fox H3
  Modbus registers 37617 / 37618 / 37611). The earlier code looked for
  `battery_temperature` / `bms1_temp` and got `null`. Now uses
  `bms1_max_temp` as the headline (hottest cell — the safety-relevant
  reading) with fallbacks down the BMS register chain.
