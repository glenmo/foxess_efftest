# foxess_efftest

Dedicated dashboard for an AC-to-AC round-trip efficiency test of the
Fox H3-15.0-SMART hybrid inverter.

Reads live data every 1 s from the existing `fox-monitor` service
(`http://localhost:5000/api/fox/data`), auto-detects discharge / charge
phases by hysteresis on `battery_power_w`, integrates AC and DC energy
per phase, and shows live round-trip efficiency once both phases have
completed.

A daily CSV is written to `./data/YYYY-MM-DD.csv` every 5 s, rotating
at local-midnight.

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
- **Round-trip efficiency** — AC and DC, live once the test has produced
  one complete discharge and one complete charge phase
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
- **Restart resilience.** If the app restarts mid-test the Coulombic
  anchor is re-set to whatever SOC the inverter is reporting at that
  moment, losing accumulated precision since the last anchor. Try not
  to restart mid-test.
