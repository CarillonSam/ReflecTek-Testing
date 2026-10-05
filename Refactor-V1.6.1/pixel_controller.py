"""Board voltage controller: framed serial protocol implementation."""

from __future__ import annotations

import struct
import threading
import time
from typing import Iterable, Optional

import numpy as np
import serial

from config import PixelControllerConfig
from controller import BoardController, ElementToControllerMap, clip_voltage, uniform_value

SOF = bytes([0x55, 0xAA])
PROTO_VER = 0x01
CMD_PING = 0x01
CMD_SET_BY_INDEX = 0x12
CMD_SAVE_TO_FLASH = 0x40

STATUS_NAMES = {
    0x00: "OK", 0x01: "ERR_CRC", 0x02: "ERR_LEN", 0x03: "ERR_CMD",
    0x04: "ERR_RANGE", 0x05: "ERR_BUSY", 0x06: "ERR_ADDR", 0x07: "ERR_STATE",
    0x08: "ERR_FLASH", 0x0A: "ERR_INTERNAL",
}

_seq = 0


def next_seq() -> int:
    global _seq
    _seq = (_seq + 1) & 0xFF
    return _seq


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def build_frame(cmd: int, seq: int, payload: bytes) -> bytes:
    hdr = bytes([PROTO_VER, cmd, seq]) + struct.pack("<H", len(payload))
    return SOF + hdr + payload + struct.pack("<H", crc16(hdr + payload))


def volt_to_code(v: float, vmin: float = 0.0, vmax: float = 10.0) -> int:
    v = clip_voltage(v, vmin, vmax)
    return round((v - vmin) / (vmax - vmin) * 4095) & 0x0FFF


class DeviceComm:
    """Framed serial request/response transport for the pixel controller."""

    def __init__(self, port: str, baud: int = 115200, timeout: float = 0.1):
        self.ser = serial.Serial(port, baud, timeout=timeout)
        self._lock = threading.Lock()

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass

    def send_recv(self, cmd: int, payload: bytes, timeout: float) -> dict:
        seq = next_seq()
        with self._lock:
            self.ser.reset_input_buffer()
            self.ser.write(build_frame(cmd, seq, payload))
            return self._recv_ack(cmd | 0x80, timeout)

    def _recv_ack(self, expected_cmd: int, timeout: float) -> dict:
        buf = bytearray()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = self.ser.read(256)
            except Exception:
                return {"ok": False, "error": "Port closed", "status": None}
            if chunk:
                buf.extend(chunk)

            while len(buf) >= 9:
                i = next((i for i in range(len(buf) - 1) if buf[i] == 0x55 and buf[i + 1] == 0xAA), None)
                if i is None:
                    break
                if i > 0:
                    buf = buf[i:]
                    continue
                if len(buf) < 7:
                    break

                ver, rcmd = buf[2], buf[3]
                rlen = struct.unpack_from("<H", buf, 5)[0]
                if ver != PROTO_VER or rlen > 2048:
                    buf = buf[1:]
                    continue

                needed = 9 + rlen
                if len(buf) < needed:
                    break

                fb, buf = bytes(buf[:needed]), buf[needed:]
                crc_r = struct.unpack_from("<H", fb, needed - 2)[0]
                if crc_r != crc16(fb[2:needed - 2]) or rcmd != expected_cmd:
                    continue

                ap = fb[7:7 + rlen]
                if len(ap) < 2:
                    return {"ok": False, "error": "short ACK", "status": None}
                st = ap[0]
                return {
                    "ok": st == 0,
                    "status": st,
                    "status_name": STATUS_NAMES.get(st, f"0x{st:02X}"),
                    "data": ap[2:],
                    "error": None,
                }
        return {"ok": False, "error": "Timeout", "status": None}


class PixelController(BoardController):
    """Board voltage controller that talks the framed serial protocol above."""

    def __init__(self, config: PixelControllerConfig, density_mode: str = "L") -> None:
        """
        density_mode ("L" or "H") picks which of mapping_csv_l/mapping_csv_h gets
        loaded — L and H elements are wired to entirely different pins, so the right
        file depends on which density this scan is actually addressing. Callers that
        never send a non-uniform grid (e.g. manual debug actions) can leave this at the
        default; it's only consulted if set_voltage_grid ever needs to look up a
        specific element.
        """
        self.config = config
        self.comm = DeviceComm(config.port, baud=config.baud, timeout=config.timeout_s)
        mapping_csv = config.mapping_csv_l if density_mode == "L" else config.mapping_csv_h
        try:
            self.pixel_map: Optional[ElementToControllerMap] = (
                ElementToControllerMap.from_csv(mapping_csv) if mapping_csv else None
            )
        except Exception:
            self.comm.close()
            raise
        self._last_grid: Optional[np.ndarray] = None
        self._density_maps: dict[str, ElementToControllerMap] = {}

    def _map_for(self, density: str) -> ElementToControllerMap:
        """The L or H element map, loaded the first time it's needed."""
        if density not in self._density_maps:
            path = self.config.mapping_csv_l if density == "L" else self.config.mapping_csv_h
            if not path:
                field_name = "mapping_csv_l" if density == "L" else "mapping_csv_h"
                raise RuntimeError(
                    f"The voltage pattern sets {density} elements, which needs the {density} mapping CSV "
                    f"(pixels.{field_name}, 'L/H mapping CSV' in General settings)."
                )
            self._density_maps[density] = ElementToControllerMap.from_csv(path)
        return self._density_maps[density]

    def apply_pattern(self, voltages: dict, default_v: float = 0.0) -> None:
        """
        Sets the whole board: every output to default_v first (one broadcast), then each
        element in `voltages` ({(density, row, col): volts}, 0-indexed) to its own value,
        addressed through that density's mapping CSV. Elements of the other density that
        aren't in `voltages` stay at default_v.
        """
        assignments = [(self._map_for(d).get_index(r, c), float(v)) for (d, r, c), v in voltages.items()]
        self._set_all_pixels(default_v)
        self._set_pixels(assignments)
        if self.config.save_to_flash_after_set:
            self._save_to_flash()
        self._last_grid = None  # the board no longer matches any single-density grid

    def close(self) -> None:
        self.comm.close()

    def ping(self) -> dict:
        return self.comm.send_recv(CMD_PING, b"", self.config.ack_timeout_ping_s)

    def _code(self, voltage_v: float) -> int:
        return volt_to_code(voltage_v, self.config.min_voltage_v, self.config.max_voltage_v)

    def set_voltage_grid(self, voltages: np.ndarray) -> None:
        v0 = uniform_value(voltages)
        if v0 is not None:
            self._set_all_pixels(v0)
            self._last_grid = np.full_like(voltages, v0)
            return

        if self.pixel_map is None:
            raise RuntimeError("Non-uniform voltage grid requires PixelControllerConfig.mapping_csv_l/mapping_csv_h.")

        assignments = [
            (self.pixel_map.get_index(row, col), float(voltages[col, row]))
            for col, row in self._changed_cells(voltages)
        ]
        self._set_pixels(assignments)
        self._last_grid = voltages.copy()

    def _changed_cells(self, voltages: np.ndarray) -> list[tuple[int, int]]:
        if self._last_grid is None or self._last_grid.shape != voltages.shape:
            cols, rows = voltages.shape
            return [(c, r) for c in range(cols) for r in range(rows)]
        return [tuple(idx) for idx in np.argwhere(voltages != self._last_grid)]

    def _set_all_pixels(self, voltage_v: float) -> None:
        code = self._code(voltage_v)
        payload = b"".join(struct.pack("<HH", i, code) for i in range(self.config.total_pixels))
        self._send_index_batched(payload, total=self.config.total_pixels)
        if self.config.save_to_flash_after_set:
            self._save_to_flash()

    def _set_pixels(self, assignments: Iterable[tuple[int, float]]) -> None:
        parts = [struct.pack("<HH", int(idx), self._code(v)) for idx, v in assignments]
        if parts:
            self._send_index_batched(b"".join(parts), total=len(parts))

    def _save_to_flash(self) -> dict:
        return self.comm.send_recv(CMD_SAVE_TO_FLASH, b"", self.config.ack_timeout_save_s)

    def _send_index_batched(self, payload: bytes, total: int) -> None:
        chunk_bytes = self.config.batch_size_pixels * 4
        batches = [payload[i:i + chunk_bytes] for i in range(0, len(payload), chunk_bytes)]
        for batch_no, chunk in enumerate(batches, start=1):
            response = self.comm.send_recv(CMD_SET_BY_INDEX, chunk, self.config.ack_timeout_set_s)
            self._raise_if_error(response, f"sending batch {batch_no}/{len(batches)}")

    @staticmethod
    def _raise_if_error(response: dict, context: str) -> None:
        if response.get("error"):
            raise RuntimeError(f"Pixel controller error while {context}: {response['error']}")
        if not response.get("ok", False):
            raise RuntimeError(
                f"Pixel controller NACK while {context}: {response.get('status_name', response.get('status'))}"
            )
