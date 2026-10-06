"""VNA handler: talks to the VNA instrument and returns amplitude/phase results."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pyvisa

from config import VNAConfig


@dataclass
class VNAResult:
    sdata: np.ndarray
    amplitude: np.ndarray
    phase_deg: np.ndarray


class VNAInstrument(ABC):
    """Interface a VNA handler must implement, so run_scan can work with any of them."""

    @abstractmethod
    def initialize(self) -> None: ...

    @abstractmethod
    def trigger(self) -> VNAResult: ...

    @abstractmethod
    def close(self) -> None: ...


class VNAController(VNAInstrument):
    """
    Driver for the lab's VNA over VISA/SCPI (formerly the standalone VNATest module).
    Connects lazily on first use, to VNAConfig.visa_resource.
    """

    def __init__(self, config: VNAConfig) -> None:
        self.config = config
        self.rm: Optional[pyvisa.ResourceManager] = None
        self.instr = None
        # Frequency axis of the sweep, set by initialize() from the instrument's read-back
        # start/stop/points (assumes a linear sweep).
        self.frequencies_hz: Optional[np.ndarray] = None

    def _ensure_open(self) -> None:
        if self.instr is None:
            self.rm = pyvisa.ResourceManager(self.config.visa_backend)
            self.instr = self.rm.open_resource(self.config.visa_resource)
            self.instr.timeout = self.config.visa_timeout_ms

    def check_response(self, label: str) -> None:
        """Debug helper: prints *OPC? and SYST:ERR? for manual troubleshooting."""
        self._ensure_open()
        print(self.instr.query("*OPC?"))
        print(f"[OK] {label}")
        print("ERRORS", self.instr.query("SYST:ERR?"))

    def _write_checked(self, cmd: str) -> None:
        """
        Sends a SCPI command and checks the instrument's error queue immediately after.
        Plain instr.write() is fire-and-forget — if the instrument rejects a command
        (wrong mode, value out of range, unsupported on this model), write() itself never
        raises, so a rejected command silently does nothing. Querying SYST:ERR? (standard
        on any SCPI instrument) after each write catches that instead of staying silent.
        This also replaces the old fixed time.sleep()s between commands: the query
        itself doesn't return until the instrument has finished processing the write, so
        it's a correct sync point rather than a guessed delay.
        """
        self.instr.write(cmd)
        error = self.instr.query("SYST:ERR?").strip()
        if not (error.startswith(("0,", "+0,")) or error.lower().startswith("no error")):
            raise RuntimeError(f"VNA rejected {cmd!r}: {error}")

    def initialize(self) -> None:
        self._ensure_open()
        c = self.config
        logging.info("VNA identified as: %s", self.instr.query("*IDN?").strip())

        self._write_checked("LSB;FMB")
        self._write_checked(f"SENS1:FREQ:START {c.start_hz}")
        self._write_checked(f"SENS1:FREQ:STOP {c.stop_hz}")
        self._write_checked(f":SENS1:SWE:POIN {c.points}")
        self._write_checked(":CALC1:PAR1:DEF S11")
        self._write_checked(":SENS:HOLD:FUNC HOLD")

        # Read back what the instrument actually thinks its settings are now, rather
        # than trusting the writes took effect just because none of them errored.
        actual_start = float(self.instr.query("SENS1:FREQ:START?"))
        actual_stop = float(self.instr.query("SENS1:FREQ:STOP?"))
        actual_points = int(float(self.instr.query("SENS1:SWE:POIN?")))
        logging.info(
            "VNA readback: start=%.0f Hz (requested %.0f), stop=%.0f Hz (requested %.0f), points=%d (requested %d)",
            actual_start, c.start_hz, actual_stop, c.stop_hz, actual_points, c.points,
        )
        self.frequencies_hz = np.linspace(actual_start, actual_stop, actual_points)
        if abs(actual_start - c.start_hz) > 1.0 or abs(actual_stop - c.stop_hz) > 1.0 or actual_points != c.points:
            raise RuntimeError(
                "VNA accepted the commands (no SCPI error) but the readback doesn't match what was "
                f"requested — requested start={c.start_hz:.0f} Hz stop={c.stop_hz:.0f} Hz points={c.points}, "
                f"instrument reports start={actual_start:.0f} Hz stop={actual_stop:.0f} Hz points={actual_points}. "
                "This usually means the SCPI command set in VNAController.initialize() doesn't match "
                "this instrument model, or a channel/trace needs selecting before these commands apply."
            )

    def trigger(self) -> VNAResult:
        self._ensure_open()
        if self.config.dwell_s > 0:
            time.sleep(self.config.dwell_s)

        self.instr.write(":TRIG:SING; *OPC?")
        self.instr.read()
        raw = self.instr.query_binary_values(
            ":CALC1:DATA:SDAT?", datatype="d", container=np.array
        ).reshape((-1, 2))
        sdata = raw[:, 0] + 1j * raw[:, 1]

        return VNAResult(
            sdata=sdata,
            amplitude=np.round(np.abs(sdata), 5),
            phase_deg=np.round(np.degrees(np.angle(sdata)), 3),
        )

    def close(self) -> None:
        try:
            if self.instr is not None:
                self.instr.close()
        except Exception:
            pass
        try:
            if self.rm is not None:
                self.rm.close()
        except Exception:
            pass
        self.instr = None
        self.rm = None
