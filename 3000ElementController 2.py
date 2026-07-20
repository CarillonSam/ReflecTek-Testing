# -*- coding: utf-8 -*-
"""
Created on Mon Mar  9 13:17:42 2026

@author: SchollJamesAC3CARILL
"""

import pandas as pd
import re
import serial
import serial.tools.list_ports
import struct
import time
import numpy as np


DACMAP_FILENAME = r"C:\Users\SchollJamesAC3CARILL\OneDrive - Carillon Technologies\Documents\2026-01-21 32X32 PINOUT-Fei Controller.xlsx"
#VOLTAGE_FILENAME
PORT = "COM4"
MAX_V = 10
MIN_V = 0
# ─────────────────────────────────────────────
# Protocol
# ─────────────────────────────────────────────
SOF            = bytes([0x55, 0xAA])
PROTO_VER      = 0x01
FW_EXPECTED    = 0x0128

CMD_PING           = 0x01
CMD_SET_BY_ADDR16  = 0x10
CMD_SET_BY_INDEX   = 0x12
CMD_SET_BY_PIXCODE = 0x14
CMD_SAVE_TO_FLASH  = 0x40

STATUS_NAMES = {
    0x00: "OK",       0x01: "ERR_CRC",   0x02: "ERR_LEN",
    0x03: "ERR_CMD",  0x04: "ERR_RANGE", 0x05: "ERR_BUSY",
    0x06: "ERR_ADDR", 0x07: "ERR_STATE", 0x08: "ERR_FLASH",
    0x0A: "ERR_INTERNAL",
}

TIMEOUT_PING = 2.0
TIMEOUT_SET  = 3.0
TIMEOUT_SAVE = 5.0
# Max pixels per USB packet: USBP_MAX_PAYLOAD=2048 bytes / 4 bytes per pixel = 512
BATCH_SIZE   = 512

# ─────────────────────────────────────────────
# CRC + Frame
# ─────────────────────────────────────────────
_seq = 0
def next_seq():
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

def build_frame(cmd, seq, payload):
    hdr = bytes([PROTO_VER, cmd, seq]) + struct.pack("<H", len(payload))
    return SOF + hdr + payload + struct.pack("<H", crc16(hdr + payload))

def volt_to_code(v):
    return (np.round(np.clip(v, MIN_V, MAX_V) / 10.0 * 4095).astype(np.uint16)) & 0x0FFF

# ─────────────────────────────────────────────
# Pixel map loader
#
# 
# ─────────────────────────────────────────────
def get_dacmap_lb(filename):
    """
    Parses master pinout file for 32x32 controller low band elements located in CTMC sharepoint. 
    Returns address in proper format to send to controller for each element.
    r"\Carillon Technologies\CTMC Sharepoint - Documents\CTMC - Operations\
        2 - ReflecTek\Design\Design D - 3000 Element Controller\2026-01-21 32X32 PINOUT-Fei Controller.xlsx"
    Parameters
    ----------
    filename : string
        Excel file

    Returns
    -------
    dacmap : ndarray
        2d array of addresses corresponding to elements on 32x32 antenna. Shape of array matches physical locations of elements

    """
    df = pd.read_excel(filename, header=None)

    dacmap = np.zeros((32,32))

    for _, row in df.iterrows():
        row = row.tolist()

        for i, cell in enumerate(row):

            if isinstance(cell, str) and re.match(r"E\d+_\d+", cell):

                parts = cell[1:].split('_')
                row_idx = int(parts[0]) - 1
                col_idx = int(parts[1]) - 1

                bit_9 = str(row[i+1])
                bit_3 = str(row[i+2])
                addr = (int(bit_3,2) << 9) | int(bit_9,2)

                dacmap[row_idx][col_idx] = addr
    return dacmap

def get_dacmap_hb(filename):
    """
    Parses master pinout file for 64x32 controller high band elements located in CTMC sharepoint. 
    Returns address in proper format to send to controller for each element.
    r"\Carillon Technologies\CTMC Sharepoint - Documents\CTMC - Operations\
        2 - ReflecTek\Design\Design D - 3000 Element Controller\2026-01-21 32X32 PINOUT-Fei Controller.xlsx"
    Parameters
    ----------
    filename : string
        Excel file

    Returns
    -------
    dacmap : ndarray
        2d array of addresses corresponding to elements on 64x32 antenna. Shape of array matches physical locations of elements

    """
    df = pd.read_excel(filename, header=None)

    dacmap = np.zeros((64,32))

    for _, row in df.iterrows():
        row = row.tolist()

        for i, cell in enumerate(row):

            if isinstance(cell, str) and re.match(r"H\d+_\d+", cell):

                parts = cell[1:].split('_')
                row_idx = int(parts[0]) - 1
                col_idx = int(parts[1]) - 1

                bit_9 = str(row[i+1])
                bit_3 = str(row[i+2])
                addr = (int(bit_3,2) << 9) | int(bit_9,2)

                dacmap[row_idx][col_idx] = addr
    return dacmap


# ─────────────────────────────────────────────
# Comm
# ─────────────────────────────────────────────
class DeviceComm:
    def __init__(self, port):
        self.ser = serial.Serial(port, 115200, timeout=0.1)

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def send_recv(self, cmd, payload, timeout):
        seq = next_seq()
        self.ser.reset_input_buffer()
        self.ser.write(build_frame(cmd, seq, payload))
        return self._recv_ack(cmd | 0x80, timeout)

    def _recv_ack(self, expected_cmd, timeout):
        buf = bytearray()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = self.ser.read(256)
            except Exception:
                # Serial port closed (e.g. on_close called) — exit immediately
                return {"ok": False, "error": "Port closed", "status": None}
            if chunk:
                buf.extend(chunk)
            while len(buf) >= 9:
                i = 0
                while i < len(buf) - 1:
                    if buf[i] == 0x55 and buf[i + 1] == 0xAA:
                        break
                    i += 1
                if i > 0:
                    buf = buf[i:]
                    continue
                if len(buf) < 7:
                    break
                ver  = buf[2]
                rcmd = buf[3]
                rlen = struct.unpack_from("<H", buf, 5)[0]
                if ver != PROTO_VER or rlen > 2048:
                    buf = buf[1:]
                    continue
                needed = 9 + rlen
                if len(buf) < needed:
                    break
                fb    = bytes(buf[:needed])
                buf   = buf[needed:]
                crc_r = struct.unpack_from("<H", fb, needed - 2)[0]
                if crc_r != crc16(fb[2:needed - 2]):
                    continue
                if rcmd != expected_cmd:
                    continue
                ap = fb[7:7 + rlen]
                if len(ap) < 2:
                    return {"ok": False, "error": "short ACK"}
                st = ap[0]
                return {
                    "ok":          st == 0,
                    "status":      st,
                    "status_name": STATUS_NAMES.get(st, f"0x{st:02X}"),
                    "data":        ap[2:],
                    "error":       None,
                }
        return {"ok": False, "error": "Timeout", "status": None}
    
# ─────────────────────────────────────────────
# Main App
# ─────────────────────────────────────────────

class App():
    def __init__(self, port = PORT, dacmap_filename = DACMAP_FILENAME):

        self.comm: DeviceComm = None
        self.connected = False
        self.port = port

        # Load pixel map from CSV
        self.pixel_map_lb = get_dacmap_lb(dacmap_filename)
        self.pixel_map_hb = get_dacmap_hb(dacmap_filename)
        
        self._connect()
        
    def _connect(self):
        try:
            self.comm = DeviceComm(self.port)
        except Exception as e:
            print(f"[ERROR] Open port failed: {e}")
            return
        self._do_ping()


    def _disconnect(self):
        if self.comm:
            self.comm.close()
            self.comm = None
        self.connected = False
    def _do_ping(self):
        print("[PING] → 0x01")
        r = self.comm.send_recv(CMD_PING, b"", TIMEOUT_PING)
        if r["error"] or not r["ok"]:
            msg = r["error"] or r["status_name"]
            print(f"[PING] ✗ {msg}")
            return
        data = r["data"]
        if len(data) >= 2:
            fw     = struct.unpack_from("<H", data, 0)[0]
            fw_str = f"V{(fw >> 8) & 0xFF}.{(fw & 0xF0) >> 4}.{fw & 0x0F}"
            ok_ver = fw == FW_EXPECTED
            print(f"[PING] ✓ FW={fw_str} {'✓' if ok_ver else '⚠ mismatch'}")
            
    def _send_all_pixels(self, volt, pixel_map):
        code = volt_to_code(volt)
        payload = b"".join(struct.pack("<HH", int(addr), int(code)) for addr, code in zip(pixel_map.ravel(), code.ravel()))
        self._do_set_index_batched(payload, len(volt.ravel()))

    def _do_set_index_batched(self, payload, total):
        """
        Send payload in BATCH_SIZE-pixel chunks.
        Each chunk = BATCH_SIZE × 4 bytes = 2048 bytes = USBP_MAX_PAYLOAD.
        """
        chunk_bytes = BATCH_SIZE * 4
        batches     = [payload[i:i + chunk_bytes]
                       for i in range(0, len(payload), chunk_bytes)]
        done = 0
        for i, b in enumerate(batches):
            n = len(b) // 4
            r = self.comm.send_recv(CMD_SET_BY_ADDR16, b, TIMEOUT_SET)
            if r["error"]:
                print(f"[BATCH {i+1}/{len(batches)}] ✗ {r['error']}")
                break
            done += n
            print(f"BATCH {i+1}/{len(batches)}", r, n)
            if not r["ok"]:
                break
    def send_voltages(self, voltages, lb_or_hb):
        if lb_or_hb == "lb":
            hb_arr = np.zeros((64,32))
            self._send_all_pixels(voltages, self.pixel_map_lb)
            self._send_all_pixels(hb_arr, self.pixel_map_hb)
        elif lb_or_hb == "hb":
            lb_arr = np.zeros((32,32))
            self._send_all_pixels(voltages, self.pixel_map_hb)
            self._send_all_pixels(lb_arr, self.pixel_map_lb)
        else:
            print("Select lb or hb")
            
    # ── Utils ─────────────────────────────────
    def _check_conn(self):
        if not self.connected or not self.comm:
            print("Not Connected", "Connect to device first.")
            return False
        return True
         
if __name__ == "__main__":  
    app = App()
    voltages = np.full((64,32), 8)
    app.send_voltages(voltages, "hb")
    app._disconnect()
    
    