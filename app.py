#!/usr/bin/env python3
"""
Fox ESS H3 — AC-to-AC round-trip efficiency test dashboard.

Reads live data every 1 s from the existing fox-monitor service
(http://localhost:5000/api/fox/data), auto-detects discharge / charge
phases by hysteresis on battery_power_w, integrates AC and DC energy
per phase, and computes the round-trip efficiency once both phases
have completed.

A daily CSV is written to ./data/YYYY-MM-DD.csv every 5 s, rotating
at midnight local time.

Typical use:
  - 100 % SOC → discharge to ~10 % SOC (loads on grid side of inverter)
  - 10 % SOC → charge back to 100 % SOC (grid → inverter → battery)
  - Once both phases complete the dashboard shows AC and DC round-trip
    efficiency. CSV captures everything for offline analysis.
"""

import argparse
import csv
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from typing import Optional

import requests
from flask import Flask, jsonify, render_template, request, send_from_directory

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("foxess_efftest")

# --------------------------------------------------------------------------- #
# Config (filled in by main())
# --------------------------------------------------------------------------- #
class Config:
    upstream = "http://localhost:5000"
    poll_interval = 1.0           # seconds between API polls (display cadence)
    csv_interval = 5.0            # seconds between CSV log rows
    csv_dir = "./data"
    site_name = "Fox H3 Efficiency Test"

    # Phase-detection thresholds (W and seconds)
    discharge_threshold_w = -200  # battery_power_w below this → discharging
    charge_threshold_w    = +200  # battery_power_w above this → charging
    idle_threshold_w      = 50    # |battery_power_w| below this → idle
    confirm_active_s      = 30    # need this many seconds of signal to enter
    confirm_idle_s        = 60    # need this many seconds of |P|<idle to leave

    # Chart downsampling (display only — CSV stays at csv_interval)
    chart_bucket_s = 60           # average each 1-min bucket for chart display
    chart_max_points = 1500       # ≈ 25 h at 1-min resolution

    # Battery capacity (Ah) for derived Coulombic-SOC estimate.
    # 0 disables the estimate.
    battery_capacity_ah = 0


CONFIG = Config()


# --------------------------------------------------------------------------- #
# Sample + Phase data classes
# --------------------------------------------------------------------------- #
@dataclass
class Sample:
    ts: datetime                  # local time
    soc: Optional[float] = None
    soc_coulombic: Optional[float] = None     # derived high-precision SOC
    battery_v: Optional[float] = None
    battery_a: Optional[float] = None
    battery_power_w: Optional[float] = None
    battery_temp_c: Optional[float] = None
    soh_pct: Optional[float] = None
    ac_power_w: Optional[float] = None        # active_power_w
    meter_power_w: Optional[float] = None     # meter_active_power_w
    load_power_w: Optional[float] = None
    pv_total_power_w: Optional[float] = None
    grid_frequency: Optional[float] = None
    power_factor: Optional[float] = None
    phase: str = "idle"                       # idle / discharge / charge


@dataclass
class Phase:
    kind: str                                 # 'discharge' or 'charge'
    start: datetime
    end: Optional[datetime] = None
    dc_wh: float = 0.0                        # signed ∫ battery_power dt (Wh)
    ac_wh: float = 0.0                        # signed ∫ active_power dt (Wh)
    avg_power_w: float = 0.0                  # average |battery_power| over phase
    peak_power_w: float = 0.0                 # max |battery_power|
    soc_start: Optional[float] = None
    soc_end: Optional[float] = None

    def as_dict(self):
        return {
            "kind": self.kind,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "duration_s": (self.end - self.start).total_seconds() if self.end else
                           (datetime.now() - self.start).total_seconds(),
            "dc_wh": round(self.dc_wh, 1),
            "ac_wh": round(self.ac_wh, 1),
            "avg_power_w": round(self.avg_power_w, 1),
            "peak_power_w": round(self.peak_power_w, 1),
            "soc_start": self.soc_start,
            "soc_end": self.soc_end,
            "ongoing": self.end is None,
        }


# --------------------------------------------------------------------------- #
# Auto-phase detector
# --------------------------------------------------------------------------- #
class PhaseDetector:
    """State machine with hysteresis. Reads battery_power_w on each sample."""

    def __init__(self):
        self.state = "idle"
        self.candidate = "idle"          # what we *might* transition to
        self.candidate_since: Optional[datetime] = None
        self.phases: list[Phase] = []
        self.current_phase: Optional[Phase] = None
        self.prev_sample: Optional[Sample] = None

    def update(self, sample: Sample):
        p = sample.battery_power_w
        if p is None:
            return  # ignore samples with missing power

        # Update integrators if we have an open phase
        if self.current_phase and self.prev_sample is not None and \
           self.prev_sample.battery_power_w is not None and \
           self.prev_sample.ac_power_w is not None and \
           sample.ac_power_w is not None:
            dt_s = (sample.ts - self.prev_sample.ts).total_seconds()
            if 0 < dt_s < 60:
                # Trapezoidal integration in W·s, convert to Wh
                dc_avg = (self.prev_sample.battery_power_w + p) / 2.0
                ac_avg = (self.prev_sample.ac_power_w + sample.ac_power_w) / 2.0
                self.current_phase.dc_wh += dc_avg * dt_s / 3600.0
                self.current_phase.ac_wh += ac_avg * dt_s / 3600.0
                # Running averages / peaks
                abs_p = abs(p)
                self.current_phase.peak_power_w = max(self.current_phase.peak_power_w, abs_p)
                # Average updates incrementally
                duration = (sample.ts - self.current_phase.start).total_seconds()
                if duration > 0:
                    prev_total = self.current_phase.avg_power_w * \
                                 (duration - dt_s)
                    self.current_phase.avg_power_w = \
                        (prev_total + abs_p * dt_s) / duration
                if sample.soc is not None:
                    self.current_phase.soc_end = sample.soc

        # State transitions (with hysteresis confirmation)
        now = sample.ts

        if self.state == "idle":
            if p <= CONFIG.discharge_threshold_w:
                want = "discharge"
            elif p >= CONFIG.charge_threshold_w:
                want = "charge"
            else:
                want = "idle"
        else:  # currently discharge or charge
            if abs(p) <= CONFIG.idle_threshold_w:
                want = "idle"
            else:
                want = self.state  # stay

        if want != self.candidate:
            self.candidate = want
            self.candidate_since = now
        elif want != self.state and self.candidate_since:
            # Have we held the candidate long enough?
            held = (now - self.candidate_since).total_seconds()
            confirm = CONFIG.confirm_idle_s if want == "idle" else CONFIG.confirm_active_s
            if held >= confirm:
                self._transition_to(want, now, sample)

        # Tag the sample with current phase
        sample.phase = self.state
        self.prev_sample = sample

    def _transition_to(self, new_state: str, now: datetime, sample: Sample):
        if self.state != "idle" and self.current_phase is not None:
            # close out the current phase
            self.current_phase.end = now
            log.info("Phase ended: %s — dc=%.1f Wh ac=%.1f Wh duration=%.0fs",
                     self.current_phase.kind, self.current_phase.dc_wh,
                     self.current_phase.ac_wh,
                     (now - self.current_phase.start).total_seconds())
            self.current_phase = None

        if new_state != "idle":
            self.current_phase = Phase(
                kind=new_state,
                start=now,
                soc_start=sample.soc,
                soc_end=sample.soc,
            )
            self.phases.append(self.current_phase)
            log.info("Phase started: %s at SOC %s", new_state, sample.soc)

        self.state = new_state

    def round_trip(self):
        """Return AC and DC round-trip efficiencies from the latest discharge +
        the matching charge (which may still be in progress).

        Rules:
          - Both phases must have accumulated meaningful energy (above a
            noise-floor threshold) so tiny early blips don't pollute the math.
          - The charge phase used must have **started after** the discharge
            (so we never pair a recent discharge with an unrelated earlier
            charge that happened to "complete" with trivial energy).
          - We DON'T require the charge to be fully closed — the round-trip
            number is live as soon as the second phase has accumulated real
            energy. ``charge_complete`` in the response tells the dashboard
            whether the number is preliminary.
        """
        MIN_WH = 500.0  # noise-floor: ignore phases below 500 Wh accumulated

        discharges = [p for p in self.phases
                      if p.kind == "discharge" and abs(p.dc_wh) > MIN_WH]
        if not discharges:
            return None
        d = discharges[-1]

        # Charge has to have STARTED after this discharge began — that way we
        # always pair a discharge with the charge that follows it, not with
        # some earlier unrelated charge.
        charges = [p for p in self.phases
                   if p.kind == "charge"
                   and p.start > d.start
                   and abs(p.dc_wh) > MIN_WH]
        if not charges:
            return None
        c = charges[-1]

        # AC: discharge_ac is the AC delivered (positive), charge_ac is AC consumed.
        # active_power_w convention: + inverter exporting, − inverter importing
        # So discharge phase has +ve AC, charge phase has −ve AC.
        ac_out_wh = abs(d.ac_wh)
        ac_in_wh = abs(c.ac_wh)
        dc_out_wh = abs(d.dc_wh)
        dc_in_wh = abs(c.dc_wh)
        eff_ac = (ac_out_wh / ac_in_wh * 100.0) if ac_in_wh > 0 else None
        eff_dc = (dc_out_wh / dc_in_wh * 100.0) if dc_in_wh > 0 else None
        return {
            "ac_out_wh": round(ac_out_wh, 1),
            "ac_in_wh":  round(ac_in_wh, 1),
            "dc_out_wh": round(dc_out_wh, 1),
            "dc_in_wh":  round(dc_in_wh, 1),
            "eff_ac_pct": round(eff_ac, 2) if eff_ac is not None else None,
            "eff_dc_pct": round(eff_dc, 2) if eff_dc is not None else None,
            "charge_complete": c.end is not None,
            "discharge_complete": d.end is not None,
        }


# --------------------------------------------------------------------------- #
# CSV writer
# --------------------------------------------------------------------------- #
CSV_FIELDS = [
    "timestamp", "phase", "soc_pct", "soc_coulombic_pct",
    "battery_v", "battery_a",
    "battery_power_w", "battery_temp_c", "soh_pct",
    "ac_power_w", "meter_power_w", "load_power_w", "pv_total_power_w",
    "grid_frequency", "power_factor",
]


class CsvWriter:
    def __init__(self, dir_path):
        self.dir = dir_path
        os.makedirs(dir_path, exist_ok=True)
        self.current_date = None
        self.fh = None
        self.writer = None
        self.lock = threading.Lock()

    def _rotate_if_needed(self, ts: datetime):
        today = ts.date()
        if today != self.current_date:
            if self.fh:
                self.fh.close()
                log.info("Closed CSV for %s", self.current_date)
            path = os.path.join(self.dir, f"{today.isoformat()}.csv")
            new_file = not os.path.exists(path)
            self.fh = open(path, "a", newline="", buffering=1)
            self.writer = csv.DictWriter(self.fh, fieldnames=CSV_FIELDS)
            if new_file:
                self.writer.writeheader()
            self.current_date = today
            log.info("Opened CSV %s", path)

    def write(self, sample: Sample):
        with self.lock:
            self._rotate_if_needed(sample.ts)
            row = {
                "timestamp": sample.ts.isoformat(timespec="seconds"),
                "phase": sample.phase,
                "soc_pct": sample.soc,
                "soc_coulombic_pct": (round(sample.soc_coulombic, 3)
                                      if sample.soc_coulombic is not None else None),
                "battery_v": sample.battery_v,
                "battery_a": sample.battery_a,
                "battery_power_w": sample.battery_power_w,
                "battery_temp_c": sample.battery_temp_c,
                "soh_pct": sample.soh_pct,
                "ac_power_w": sample.ac_power_w,
                "meter_power_w": sample.meter_power_w,
                "load_power_w": sample.load_power_w,
                "pv_total_power_w": sample.pv_total_power_w,
                "grid_frequency": sample.grid_frequency,
                "power_factor": sample.power_factor,
            }
            self.writer.writerow(row)


# --------------------------------------------------------------------------- #
# Upstream poller — fetches /api/fox/data and maps to Sample
# --------------------------------------------------------------------------- #
def _f(payload, *keys):
    """Pick the first numeric value from a list of candidate keys."""
    for k in keys:
        if k in payload and payload[k] is not None:
            try:
                return float(payload[k])
            except (TypeError, ValueError):
                pass
    return None


def fetch_sample(upstream) -> Sample:
    url = f"{upstream.rstrip('/')}/api/fox/data"
    r = requests.get(url, timeout=3)
    r.raise_for_status()
    d = r.json()

    ts = datetime.now()
    return Sample(
        ts=ts,
        soc=_f(d, "system_soc", "bms1_soc", "battery_soc"),
        battery_v=_f(d, "battery_voltage", "bms1_voltage"),
        battery_a=_f(d, "battery_current", "bms1_current"),
        battery_power_w=_f(d, "battery_flow_w", "battery_power_w",
                          "battery_power_total"),
        # fox-monitor publishes bms1_max_temp (hottest cell), bms1_min_temp,
        # and bms1_ambient_temp (battery housing). Prefer max as the headline
        # value since it's the safety-relevant one; fall back to ambient and
        # then to legacy field names if a future upstream renames them.
        battery_temp_c=_f(d, "bms1_max_temp", "bms1_ambient_temp",
                          "bms1_min_temp",
                          "battery_temperature", "bms1_temp",
                          "bms1_temperature"),
        soh_pct=_f(d, "soh", "bms1_soh"),
        ac_power_w=_f(d, "active_power_w", "active_power", "inverter_active_power"),
        meter_power_w=_f(d, "meter_active_power_w", "meter_active_power"),
        load_power_w=_f(d, "load_power_w", "load_power"),
        pv_total_power_w=_f(d, "pv_total_power_w", "pv_total_power"),
        grid_frequency=_f(d, "grid_frequency", "frequency"),
        power_factor=_f(d, "power_factor"),
    )


# --------------------------------------------------------------------------- #
# Main background worker
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self):
        self.detector = PhaseDetector()
        self.csv = CsvWriter(CONFIG.csv_dir)
        self.latest: Optional[Sample] = None
        self.lock = threading.Lock()
        # 1-min downsampled buffer for chart display
        self.chart_buf = deque(maxlen=CONFIG.chart_max_points)
        self._bucket_start: Optional[datetime] = None
        self._bucket_samples: list[Sample] = []
        self._last_csv_write = 0.0
        self._stop = threading.Event()
        self._thread = None

        # Coulombic-SOC integrator state.
        # Anchor: reported integer SOC at the moment we first see valid data
        # (or via /api/anchor reset). cumulative_ah is the net Ah moved into
        # the battery since the anchor (positive = charged in, negative = out).
        # Sign convention: battery_a > 0 when charging.
        self._coulomb_anchor_pct: Optional[float] = None
        self._coulomb_anchor_ts:  Optional[datetime] = None
        self._coulomb_cumulative_ah: float = 0.0
        self._coulomb_prev_sample: Optional[Sample] = None

    def reset_coulomb_anchor(self, soc_now: Optional[float] = None):
        """Re-anchor the Coulombic integrator. Call this when the battery
        is at a known SOC (e.g. just before starting the test at 100 %)."""
        with self.lock:
            self._coulomb_cumulative_ah = 0.0
            self._coulomb_prev_sample = None
            if soc_now is not None:
                self._coulomb_anchor_pct = soc_now
            elif self.latest is not None and self.latest.soc is not None:
                self._coulomb_anchor_pct = float(self.latest.soc)
            self._coulomb_anchor_ts = datetime.now()
            log.info("Coulombic anchor reset to %s%% at %s",
                     self._coulomb_anchor_pct, self._coulomb_anchor_ts)

    def _update_coulombic_soc(self, sample: Sample):
        """Integrate battery_a over time, expressed as SOC %.
        Updates sample.soc_coulombic in place."""
        if CONFIG.battery_capacity_ah <= 0:
            sample.soc_coulombic = None
            return

        # First valid reading establishes the anchor automatically.
        if self._coulomb_anchor_pct is None:
            if sample.soc is not None:
                self._coulomb_anchor_pct = float(sample.soc)
                self._coulomb_anchor_ts = sample.ts
                self._coulomb_cumulative_ah = 0.0
                self._coulomb_prev_sample = sample
                sample.soc_coulombic = self._coulomb_anchor_pct
                log.info("Coulombic anchor auto-set to %s%% at %s",
                         self._coulomb_anchor_pct, sample.ts)
            return

        prev = self._coulomb_prev_sample
        if prev is None or prev.battery_a is None or sample.battery_a is None:
            self._coulomb_prev_sample = sample
            return

        dt_s = (sample.ts - prev.ts).total_seconds()
        if 0 < dt_s < 60:
            avg_a = (prev.battery_a + sample.battery_a) / 2.0
            self._coulomb_cumulative_ah += avg_a * dt_s / 3600.0

        soc_est = (self._coulomb_anchor_pct +
                   self._coulomb_cumulative_ah /
                   CONFIG.battery_capacity_ah * 100.0)
        # Clamp the display to a sensible range; CSV keeps the raw number
        sample.soc_coulombic = soc_est
        self._coulomb_prev_sample = sample

    def _flush_bucket(self, end_ts: datetime):
        if not self._bucket_samples:
            return
        n = len(self._bucket_samples)
        def avg(attr):
            vals = [getattr(s, attr) for s in self._bucket_samples
                    if getattr(s, attr) is not None]
            return sum(vals) / len(vals) if vals else None
        self.chart_buf.append({
            "ts": self._bucket_start.isoformat(timespec="seconds"),
            "soc": avg("soc"),
            "soc_coulombic": avg("soc_coulombic"),
            "battery_power_w": avg("battery_power_w"),
            "ac_power_w": avg("ac_power_w"),
            "battery_temp_c": avg("battery_temp_c"),
        })
        self._bucket_samples = []

    def _add_to_bucket(self, sample: Sample):
        if self._bucket_start is None:
            self._bucket_start = sample.ts.replace(microsecond=0,
                                                   second=0)
        elapsed = (sample.ts - self._bucket_start).total_seconds()
        if elapsed >= CONFIG.chart_bucket_s:
            self._flush_bucket(sample.ts)
            self._bucket_start = sample.ts.replace(microsecond=0, second=0)
        self._bucket_samples.append(sample)

    def _loop(self):
        log.info("Engine started. Upstream=%s poll=%.1fs csv=%.1fs",
                 CONFIG.upstream, CONFIG.poll_interval, CONFIG.csv_interval)
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                sample = fetch_sample(CONFIG.upstream)
                with self.lock:
                    self._update_coulombic_soc(sample)
                    self.detector.update(sample)
                    self.latest = sample
                    self._add_to_bucket(sample)
                    if t0 - self._last_csv_write >= CONFIG.csv_interval:
                        try:
                            self.csv.write(sample)
                            self._last_csv_write = t0
                        except Exception as e:
                            log.warning("CSV write failed: %s", e)
            except Exception as e:
                log.debug("poll failed: %s", e)
            # sleep the remainder of the interval
            elapsed = time.monotonic() - t0
            self._stop.wait(max(0.0, CONFIG.poll_interval - elapsed))

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def snapshot(self):
        with self.lock:
            s = self.latest
            if s is None:
                return {
                    "live": None,
                    "phase": "unknown",
                    "phases": [],
                    "round_trip": None,
                }
            return {
                "live": {
                    "ts": s.ts.isoformat(timespec="seconds"),
                    "soc": s.soc,
                    "soc_coulombic": (round(s.soc_coulombic, 3)
                                       if s.soc_coulombic is not None else None),
                    "battery_v": s.battery_v,
                    "battery_a": s.battery_a,
                    "battery_power_w": s.battery_power_w,
                    "battery_temp_c": s.battery_temp_c,
                    "soh_pct": s.soh_pct,
                    "ac_power_w": s.ac_power_w,
                    "meter_power_w": s.meter_power_w,
                    "load_power_w": s.load_power_w,
                    "pv_total_power_w": s.pv_total_power_w,
                    "grid_frequency": s.grid_frequency,
                    "power_factor": s.power_factor,
                },
                "phase": self.detector.state,
                "phase_started": (
                    self.detector.current_phase.start.isoformat(timespec="seconds")
                    if self.detector.current_phase else None
                ),
                "phases": [p.as_dict() for p in self.detector.phases],
                "round_trip": self.detector.round_trip(),
                "site_name": CONFIG.site_name,
            }

    def chart_data(self):
        with self.lock:
            return list(self.chart_buf)


ENGINE: Engine = None  # filled in by main()


# --------------------------------------------------------------------------- #
# Flask app
# --------------------------------------------------------------------------- #
app = Flask(__name__, template_folder="templates")


@app.after_request
def _no_cache_api(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/")
def index():
    return render_template("index.html", site_name=CONFIG.site_name)


@app.route("/api/state")
def api_state():
    return jsonify(ENGINE.snapshot())


@app.route("/api/history")
def api_history():
    return jsonify(ENGINE.chart_data())


@app.route("/api/anchor", methods=["POST"])
def api_anchor():
    """Reset the Coulombic-SOC integrator anchor to the current reported SOC
    (or to an explicit value via ?soc=NN). Call this when the battery has
    just reached a trusted reference SOC (e.g. fully-charged 100 %)."""
    soc = request.args.get("soc", type=float)
    ENGINE.reset_coulomb_anchor(soc_now=soc)
    return jsonify(ENGINE.snapshot())


@app.route("/csv/")
def csv_index():
    """Return a list of available CSV files."""
    files = []
    if os.path.isdir(CONFIG.csv_dir):
        for name in sorted(os.listdir(CONFIG.csv_dir)):
            if name.endswith(".csv"):
                path = os.path.join(CONFIG.csv_dir, name)
                files.append({
                    "name": name,
                    "size_bytes": os.path.getsize(path),
                    "modified": datetime.fromtimestamp(
                        os.path.getmtime(path)).isoformat(timespec="seconds"),
                })
    return jsonify(files)


@app.route("/csv/<path:filename>")
def csv_download(filename):
    return send_from_directory(CONFIG.csv_dir, filename, as_attachment=True)


@app.route("/csv/today.csv")
def csv_today():
    today = date.today().isoformat() + ".csv"
    return send_from_directory(CONFIG.csv_dir, today, as_attachment=True)


@app.route("/healthz")
def healthz():
    s = ENGINE.snapshot()
    return jsonify({"ok": s["live"] is not None}), 200


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    global ENGINE
    p = argparse.ArgumentParser(description="Fox H3 AC-AC efficiency test dashboard")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8900)
    p.add_argument("--upstream", default="http://localhost:5000",
                   help="URL of fox_remote_monitoring (default: http://localhost:5000)")
    p.add_argument("--poll-interval", type=float, default=1.0)
    p.add_argument("--csv-interval", type=float, default=5.0)
    p.add_argument("--csv-dir", default="./data")
    p.add_argument("--site-name", default="Fox H3 Efficiency Test")
    p.add_argument("--discharge-threshold-w", type=int, default=-200)
    p.add_argument("--charge-threshold-w", type=int, default=200)
    p.add_argument("--idle-threshold-w", type=int, default=50)
    p.add_argument("--confirm-active-s", type=int, default=30)
    p.add_argument("--confirm-idle-s", type=int, default=60)
    p.add_argument("--battery-capacity-ah", type=float, default=0.0,
                   help="Nominal battery capacity (Ah) for Coulombic-SOC "
                        "estimation. Zero disables the estimator.")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    CONFIG.upstream = args.upstream
    CONFIG.poll_interval = args.poll_interval
    CONFIG.csv_interval = args.csv_interval
    CONFIG.csv_dir = args.csv_dir
    CONFIG.site_name = args.site_name
    CONFIG.discharge_threshold_w = args.discharge_threshold_w
    CONFIG.charge_threshold_w = args.charge_threshold_w
    CONFIG.idle_threshold_w = args.idle_threshold_w
    CONFIG.confirm_active_s = args.confirm_active_s
    CONFIG.confirm_idle_s = args.confirm_idle_s
    CONFIG.battery_capacity_ah = args.battery_capacity_ah

    ENGINE = Engine()
    ENGINE.start()
    log.info("Listening on %s:%d (upstream=%s)", args.host, args.port, args.upstream)
    try:
        app.run(host=args.host, port=args.port,
                debug=args.debug, use_reloader=False)
    finally:
        ENGINE.stop()


if __name__ == "__main__":
    main()
