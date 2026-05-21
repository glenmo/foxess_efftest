#!/usr/bin/env python3
"""
SMA Sunny WebBox Modbus TCP probe.

This is intentionally self-contained: it uses Flask for the dashboard because
the repo already depends on Flask, but the Modbus TCP client is implemented
with the Python standard library. It reads only. No Modbus write functions are
implemented.
"""

import argparse
import itertools
import json
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, jsonify, render_template, request


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sma_webbox_probe")


SMA_NAN_U16 = {0xFFFF, 0x8000}
SMA_NAN_U32 = {0xFFFFFFFF, 0x80000000, 0x7FFFFFFF}
SMA_NAN_S32 = {-2147483648, 2147483647}


@dataclass(frozen=True)
class RegisterPoint:
    address: int
    words: int
    dtype: str
    name: str
    unit: str = ""
    scale: float = 1.0
    group: str = "SMA candidates"
    note: str = ""
    function: int = 3


# The WebBox manual uses SMA's 1-based register numbers. The Modbus PDU uses
# zero-based offsets, so the client subtracts 1 before sending requests.
#
# This list is deliberately conservative. It probes common SMA plant/device
# registers and likely live electrical/weather values; the adaptive scanner
# below is what finds model-specific SensorBox and MeteoStation channels.
REGISTER_POINTS = [
    RegisterPoint(30001, 2, "u32", "SMA device class", group="Identification"),
    RegisterPoint(30003, 2, "u32", "SMA device type", group="Identification"),
    RegisterPoint(30005, 2, "u32", "SMA device status", group="Identification"),
    RegisterPoint(30057, 2, "u32", "Serial number", group="Identification"),
    RegisterPoint(30194, 2, "timestamp", "WebBox live timestamp", group="Live data"),
    RegisterPoint(40002, 2, "timestamp", "WebBox live timestamp mirror", group="Live data"),
    RegisterPoint(30513, 4, "u64", "Total feed-in energy", "Wh", group="Energy"),
    RegisterPoint(30517, 4, "u64", "Total grid-supplied energy", "Wh", group="Energy"),
    RegisterPoint(30769, 2, "s32", "DC voltage candidate", "V", 0.01, "Live power"),
    RegisterPoint(30771, 2, "s32", "DC current candidate", "A", 0.001, "Live power"),
    RegisterPoint(30773, 2, "s32", "DC power candidate", "W", 1.0, "Live power"),
    RegisterPoint(30775, 2, "s32", "Active power candidate", "W", 1.0, "Live power"),
    RegisterPoint(30783, 2, "s32", "AC active power candidate", "W", 1.0, "Live power"),
    RegisterPoint(30803, 2, "u32", "Grid frequency candidate", "Hz", 0.01, "Grid"),
    RegisterPoint(30813, 2, "u32", "Voltage L1 candidate", "V", 0.01, "Grid"),
    RegisterPoint(30815, 2, "u32", "Voltage L2 candidate", "V", 0.01, "Grid"),
    RegisterPoint(30817, 2, "u32", "Voltage L3 candidate", "V", 0.01, "Grid"),
    RegisterPoint(30953, 2, "u32", "Operating status candidate", group="Status"),
    RegisterPoint(30957, 2, "u32", "Condition candidate", group="Status"),
    RegisterPoint(34609, 2, "s32", "Ambient temperature candidate", "degC", 0.01, "Weather"),
    RegisterPoint(34611, 2, "s32", "Module temperature candidate", "degC", 0.01, "Weather"),
    RegisterPoint(34613, 2, "u32", "Irradiance candidate", "W/m2", 1.0, "Weather"),
    RegisterPoint(34615, 2, "u32", "Wind speed candidate", "m/s", 0.1, "Weather"),
]


DEFAULT_SCAN_RANGES = [
    (30001, 30100),
    (30191, 30240),
    (30501, 30540),
    (30769, 30840),
    (30951, 30980),
    (34601, 34640),
]


class ModbusError(Exception):
    pass


class ModbusTcpClient:
    def __init__(self, host: str, port: int = 502, timeout: float = 2.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._tx = itertools.count(1)

    def _request(self, unit_id: int, function: int, start: int, count: int) -> bytes:
        transaction_id = next(self._tx) & 0xFFFF
        protocol_id = 0
        pdu = struct.pack(">BHH", function, start, count)
        mbap = struct.pack(">HHHB", transaction_id, protocol_id, len(pdu) + 1, unit_id)
        frame = mbap + pdu

        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            sock.sendall(frame)
            header = self._recv_exact(sock, 7)
            rx_tx, rx_proto, rx_len, rx_unit = struct.unpack(">HHHB", header)
            if rx_tx != transaction_id or rx_proto != 0 or rx_unit != unit_id:
                raise ModbusError("Modbus transaction/header mismatch")
            body = self._recv_exact(sock, rx_len - 1)

        if not body:
            raise ModbusError("Empty Modbus response")
        rx_function = body[0]
        if rx_function & 0x80:
            code = body[1] if len(body) > 1 else None
            raise ModbusError(f"Modbus exception {code}")
        if rx_function != function:
            raise ModbusError(f"Unexpected Modbus function {rx_function}")
        return body[1:]

    @staticmethod
    def _recv_exact(sock: socket.socket, length: int) -> bytes:
        chunks = []
        remaining = length
        while remaining:
            chunk = sock.recv(remaining)
            if not chunk:
                raise ModbusError("Socket closed while reading response")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read_registers(self, unit_id: int, address: int, count: int, function: int = 3) -> list[int]:
        if count < 1 or count > 125:
            raise ValueError("Modbus register count must be 1..125")
        if function not in (3, 4):
            raise ValueError("Only read holding/input registers are supported")
        start = address - 1
        payload = self._request(unit_id, function, start, count)
        if not payload:
            raise ModbusError("Missing byte-count in Modbus response")
        byte_count = payload[0]
        data = payload[1:]
        if byte_count != count * 2 or len(data) != byte_count:
            raise ModbusError("Unexpected Modbus byte count")
        return list(struct.unpack(">" + "H" * count, data))


def parse_unit_ids(text: str) -> list[int]:
    ids: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            first, last = part.split("-", 1)
            ids.update(range(int(first), int(last) + 1))
        else:
            ids.add(int(part))
    return [i for i in sorted(ids) if 0 <= i <= 255]


def parse_ranges(text: str) -> list[tuple[int, int]]:
    if not text.strip():
        return DEFAULT_SCAN_RANGES
    ranges = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" not in part:
            addr = int(part)
            ranges.append((addr, addr))
            continue
        first, last = part.split("-", 1)
        a, b = int(first), int(last)
        ranges.append((min(a, b), max(a, b)))
    return ranges


def decode_words(words: list[int], dtype: str):
    if dtype == "raw":
        return " ".join(f"{w:04X}" for w in words)
    if dtype == "u16":
        return None if words[0] in SMA_NAN_U16 else words[0]
    if dtype == "s16":
        raw = words[0]
        return None if raw in SMA_NAN_U16 else struct.unpack(">h", struct.pack(">H", raw))[0]
    if dtype in ("u32", "s32", "float32"):
        if len(words) < 2:
            return None
        raw = (words[0] << 16) | words[1]
        if dtype == "u32":
            return None if raw in SMA_NAN_U32 else raw
        if dtype == "s32":
            val = struct.unpack(">i", struct.pack(">I", raw))[0]
            return None if val in SMA_NAN_S32 else val
        return struct.unpack(">f", struct.pack(">I", raw))[0]
    if dtype == "timestamp":
        if len(words) < 2:
            return None
        raw = decode_words(words[:2], "u32")
        if raw is None or raw <= 0:
            return None
        # Keep this focused on live WebBox timestamps, not sentinel pairs that
        # happen to be valid Unix epochs such as 0xFFFF0000 or 0x0000FFFF.
        if raw < 1577836800 or raw > 1893456000:  # 2020-01-01 .. 2030-01-01 UTC
            return None
        try:
            return datetime.fromtimestamp(raw, timezone.utc).astimezone().isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    if dtype == "u64":
        if len(words) < 4:
            return None
        if any(word in SMA_NAN_U16 for word in words[:4]):
            return None
        raw = 0
        for word in words[:4]:
            raw = (raw << 16) | word
        return None if raw in (0xFFFFFFFFFFFFFFFF, 0x8000000000000000) else raw
    if dtype == "string":
        data = b"".join(struct.pack(">H", w) for w in words)
        return data.rstrip(b"\x00").decode("latin-1", errors="replace").strip()
    raise ValueError(f"Unsupported dtype {dtype}")


def scaled(value, scale: float):
    if value is None:
        return None
    if isinstance(value, (int, float)) and scale != 1.0:
        return round(value * scale, 4)
    return value


def is_interesting_words(words: list[int]) -> bool:
    if not words:
        return False
    if all(w == 0 for w in words):
        return False
    if all(w in SMA_NAN_U16 for w in words):
        return False
    return True


def interpret_block(address: int, words: list[int]) -> list[dict]:
    interpretations = []
    for offset, word in enumerate(words):
        if word not in (0, 0xFFFF, 0x8000):
            val_s16 = decode_words([word], "s16")
            interpretations.append({
                "address": address + offset,
                "dtype": "u16/s16",
                "value": f"{word} / {val_s16}",
            })
    for offset in range(0, max(0, len(words) - 1)):
        pair = words[offset:offset + 2]
        u32 = decode_words(pair, "u32")
        s32 = decode_words(pair, "s32")
        ts = decode_words(pair, "timestamp")
        if u32 is None and s32 is None:
            continue
        if ts:
            interpretations.append({
                "address": address + offset,
                "dtype": "timestamp",
                "value": ts,
            })
        if u32 not in (0, None) or s32 not in (0, None):
            interpretations.append({
                "address": address + offset,
                "dtype": "u32/s32",
                "value": f"{u32} / {s32}",
            })
    return interpretations[:16]


def read_point(client: ModbusTcpClient, unit_id: int, point: RegisterPoint) -> dict:
    words = client.read_registers(unit_id, point.address, point.words, point.function)
    raw_value = decode_words(words, point.dtype)
    value = scaled(raw_value, point.scale)
    return {
        "unit_id": unit_id,
        "address": point.address,
        "function": point.function,
        "words": point.words,
        "dtype": point.dtype,
        "name": point.name,
        "group": point.group,
        "unit": point.unit,
        "scale": point.scale,
        "note": point.note,
        "raw_words": [f"{w:04X}" for w in words],
        "value": value,
        "raw_value": raw_value,
        "ok": value is not None,
    }


def scan_span(
    client: ModbusTcpClient,
    unit_id: int,
    start: int,
    end: int,
    function: int,
    max_block: int,
    found: list[dict],
    errors: list[dict],
):
    count = end - start + 1
    if count <= 0:
        return
    count = min(count, max_block)
    try:
        words = client.read_registers(unit_id, start, count, function)
        if is_interesting_words(words):
            found.append({
                "unit_id": unit_id,
                "function": function,
                "address": start,
                "words": count,
                "raw_words": [f"{w:04X}" for w in words],
                "interpretations": interpret_block(start, words),
            })
        next_start = start + count
        if next_start <= end:
            scan_span(client, unit_id, next_start, end, function, max_block, found, errors)
    except Exception as exc:
        errors.append({
            "unit_id": unit_id,
            "function": function,
            "address": start,
            "words": count,
            "error": str(exc),
        })
        next_start = start + count
        if next_start <= end:
            scan_span(client, unit_id, next_start, end, function, max_block, found, errors)


def probe_webbox(
    host: str,
    port: int,
    unit_ids: list[int],
    ranges: list[tuple[int, int]],
    timeout: float,
    max_block: int,
    include_input_registers: bool,
) -> dict:
    started = datetime.now()
    client = ModbusTcpClient(host, port, timeout)
    points = []
    point_errors = []
    blocks = []
    scan_errors = []

    for unit_id in unit_ids:
        for point in REGISTER_POINTS:
            try:
                result = read_point(client, unit_id, point)
                if result["ok"]:
                    points.append(result)
            except Exception as exc:
                point_errors.append({
                    "unit_id": unit_id,
                    "address": point.address,
                    "name": point.name,
                    "error": str(exc),
                })

        functions = [3, 4] if include_input_registers else [3]
        for function in functions:
            for first, last in ranges:
                scan_span(
                    client=client,
                    unit_id=unit_id,
                    start=first,
                    end=last,
                    function=function,
                    max_block=max(1, min(max_block, 60)),
                    found=blocks,
                    errors=scan_errors,
                )

    finished = datetime.now()
    active_unit_ids = sorted({
        item["unit_id"] for item in points
    } | {
        item["unit_id"] for item in blocks
    })
    return {
        "host": host,
        "port": port,
        "unit_ids": unit_ids,
        "active_unit_ids": active_unit_ids,
        "ranges": ranges,
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "duration_s": round((finished - started).total_seconds(), 3),
        "points": points,
        "blocks": blocks,
        "point_errors": point_errors[:100],
        "scan_errors": scan_errors[:200],
        "error_counts": {
            "point_errors": len(point_errors),
            "scan_errors": len(scan_errors),
        },
    }


class ProbeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.latest: Optional[dict] = None
        self.error: Optional[str] = None
        self.stop_event = threading.Event()

    def start(self, args: dict):
        with self.lock:
            if self.running:
                raise RuntimeError("Probe already running")
            self.running = True
            self.latest = None
            self.error = None
            self.stop_event.clear()
        thread = threading.Thread(target=self._run, args=(args,), daemon=True)
        thread.start()

    def _run(self, args: dict):
        try:
            poll_interval = args.pop("poll_interval")
            while not self.stop_event.is_set():
                result = probe_webbox(**args)
                with self.lock:
                    self.latest = result
                    self.error = None
                if poll_interval <= 0:
                    break
                self.stop_event.wait(poll_interval)
        except Exception as exc:
            log.exception("Probe failed")
            with self.lock:
                self.error = str(exc)
        finally:
            with self.lock:
                self.running = False

    def stop(self):
        self.stop_event.set()

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "latest": self.latest,
                "error": self.error,
            }


STATE = ProbeState()
app = Flask(__name__, template_folder="templates")


@app.after_request
def no_cache_api(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/")
def index():
    return render_template("sma_webbox.html")


@app.route("/api/probe", methods=["POST"])
def api_probe():
    payload = request.get_json(force=True) or {}
    host = str(payload.get("host") or "").strip()
    if not host:
        return jsonify({"error": "host is required"}), 400
    try:
        args = {
            "host": host,
            "port": int(payload.get("port") or 502),
            "unit_ids": parse_unit_ids(str(payload.get("unit_ids") or "1-10,126,255")),
            "ranges": parse_ranges(str(payload.get("ranges") or "")),
            "timeout": float(payload.get("timeout") or 2.0),
            "max_block": int(payload.get("max_block") or 20),
            "include_input_registers": bool(payload.get("include_input_registers")),
            "poll_interval": float(payload.get("poll_interval") or 5.0),
        }
        STATE.start(args)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(STATE.snapshot())


@app.route("/api/stop", methods=["POST"])
def api_stop():
    STATE.stop()
    return jsonify(STATE.snapshot())


@app.route("/api/state")
def api_state():
    return jsonify(STATE.snapshot())


@app.route("/api/registers")
def api_registers():
    return jsonify([point.__dict__ for point in REGISTER_POINTS])


def main():
    parser = argparse.ArgumentParser(description="Probe SMA Sunny WebBox Modbus TCP data")
    parser.add_argument("--host", help="Sunny WebBox IP/hostname. If set with --once, run CLI probe.")
    parser.add_argument("--web-host", default="0.0.0.0")
    parser.add_argument("--web-port", type=int, default=8910)
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--unit-ids", default="1-10,126,255")
    parser.add_argument("--ranges", default="")
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--max-block", type=int, default=20)
    parser.add_argument("--include-input-registers", action="store_true")
    parser.add_argument("--once", action="store_true", help="Run one probe and print JSON instead of starting the web app")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.once:
        if not args.host:
            parser.error("--once requires --host")
        result = probe_webbox(
            host=args.host,
            port=args.port,
            unit_ids=parse_unit_ids(args.unit_ids),
            ranges=parse_ranges(args.ranges),
            timeout=args.timeout,
            max_block=args.max_block,
            include_input_registers=args.include_input_registers,
        )
        print(json.dumps(result, indent=2))
        return

    log.info("Listening on %s:%d", args.web_host, args.web_port)
    app.run(host=args.web_host, port=args.web_port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
