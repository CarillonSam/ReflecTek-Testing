"""
VNA handler: talks to the VNA instrument and returns amplitude/phase results.

Frequency sweeps (initialize / trigger) are what the sweep and pattern scans use. Transient
scans use CW capture instead (setup_cw / arm_cw / fire_cw / read_cw): S11 at one frequency
against time, with the same SCPI sequence as the standalone vna_cw_s11_capture.py script.
The time axis is never resampled: sample k is at k * cw_dt_s, the VNA's own reported sweep
time divided by the number of points. A capture that comes back with a different number of
points than was set up is refused, since cw_dt_s would then be wrong for it. setup_cw reads
the settings CW capture changes (CW mode, IF bandwidth, trigger source, hold function) and
close() puts them back, so the next frequency sweep isn't left waiting for an external trigger.

The arm / trigger / read order follows the proven SpecVision range script (VnaInstance):
  arm:  :TRIG:SOUR EXT first, THEN :SENS1:HOLD:FUNC SING. Setting single-sweep hold while the
        source is still internal starts a sweep straight away, before the trigger, so the
        capture wouldn't line up with the profile and the external pulse would be ignored.
  wait: ARM_SETTLE_S after arming. The MS46322B is busy for about 3 s after the arm and drops a
        trigger edge that arrives in that window: no sweep runs, and the read returns whatever
        the previous capture left in the buffer. *OPC? does not wait for this.
  read: :SENS1:HOLD:FUNC HOLD, a short pause, THEN read the buffer.
"""

from __future__ import annotations

import logging
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pyvisa

from config import TransientConfig, VNAConfig

ARM_SETTLE_S = 5.0
"""Seconds to wait after arming a USB-triggered CW capture before firing the trigger. The MS46322B
was measured busy for about 3 s after the arm (a trigger in that window is dropped); 5 is that plus
margin, the value from the proven reference script. Worth shortening once measured on this VNA:
it is added to every element's capture."""
READ_SETTLE_S = 0.1   # after :SENS1:HOLD:FUNC HOLD, before reading the buffer (as in the reference)

# Settings CW capture changes, put back by close() (query = command + "?").
CW_RESTORED_SETTINGS = (":SENS1:SWE:CW", ":SENS1:BAND", ":TRIG:SOUR", ":SENS1:HOLD:FUNC")


class UsbTrigger:
    """A pulse on a serial port, wired to the VNA's external trigger input."""

    def __init__(self, port: str) -> None:
        import serial
        self.ser = serial.Serial(port, baudrate=921600)
        self.ser.setRTS(False)
        self.ser.setDTR(False)

    def pulse(self) -> None:
        self.ser.write(b"\x00")

    def close(self) -> None:
        self.ser.close()


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
        # CW capture (setup_cw): the settings to put back, the trigger, and the capture's shape.
        self._cw_saved: dict[str, str] = {}
        self._cw: Optional[TransientConfig] = None
        self._trigger_pin: Optional[UsbTrigger] = None
        self.cw_points = 0
        self.cw_sweep_time_s = 0.0
        self.cw_dt_s = 0.0
        self.cw_dt_source = ""
        self._last_cw: Optional[np.ndarray] = None   # previous capture, to catch a stale buffer

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

    def sweep_only(self) -> None:
        """One sweep with whatever the VNA is currently set to (the Scan tab's VNA "Scan"
        button): the same trigger trigger() sends, without fetching the data. *OPC?'s reply
        is read so it isn't left in the buffer for the next read."""
        self._ensure_open()
        self.instr.write(":TRIG:SING; *OPC?")
        self.instr.read()

    # ---------------------------------------------------------------- CW capture ----

    def setup_cw(self, transient: TransientConfig) -> None:
        """Puts the VNA in CW mode for one capture long enough for the whole profile
        (len(voltages_v) * hold_s). If the VNA's own sweep time comes out shorter than that,
        the point count is raised to cover it, up to transient.max_points."""
        self._ensure_open()
        logging.info("VNA identified as: %s", self.instr.query("*IDN?").strip())
        if self._cw is None:   # remember the original settings once, before changing anything
            for cmd in CW_RESTORED_SETTINGS:
                try:
                    self._cw_saved[cmd] = self.instr.query(cmd + "?").strip()
                except Exception as e:
                    logging.warning("Couldn't read %s before CW capture, so it won't be put back: %s", cmd, e)
        self._cw = transient
        if transient.trigger == "usb" and self._trigger_pin is None:
            self._trigger_pin = UsbTrigger(transient.trigger_port)

        t = transient
        duration = t.duration_s
        points = t.estimated_points()
        for _ in range(4):
            if points > t.max_points:
                raise ValueError(
                    f"A {duration:g} s profile needs {points} points at {t.ifbw_hz:g} Hz IF bandwidth, more than the "
                    f"VNA's {t.max_points}. Use a lower IF bandwidth (fewer samples per second), shorter holds, or fewer steps.")
            self.instr.write("*CLS")
            self._write_checked(":SENS1:SWE:CW 1")                 # CW mode on CH1
            self._write_checked(f":SENS1:FREQ:CW {t.freq_hz}")
            self._write_checked(f":SENS1:BAND {t.ifbw_hz}")
            self._write_checked(f":SENS1:SWE:CW:POIN {points}")
            self._write_checked(":FORM:DATA REAL")                 # 64-bit binary
            self._write_checked(":FORM:BORD SWAP")                 # little-endian, to match read_cw's is_big_endian=False
            self._write_checked(":CALC1:PAR1:DEF S11")
            self._write_checked(":CALC1:PAR1:SEL")
            self.instr.query("*OPC?")
            try:
                sweep_time, source = float(self.instr.query(":SENS1:SWE:TIM?").strip()), "instrument"
                if not sweep_time > 0:
                    raise ValueError
            except Exception:
                sweep_time = points * (1.0 / t.ifbw_hz + t.sample_overhead_s)
                source = "estimate (the VNA didn't report its sweep time)"
            if sweep_time >= duration:
                break
            points = math.ceil(points * duration / sweep_time) + 1
        else:
            raise RuntimeError(f"Couldn't set up a CW capture covering {duration:g} s (last: {sweep_time:g} s).")
        self._last_cw = None
        self.cw_points, self.cw_sweep_time_s = points, sweep_time
        self.cw_dt_s, self.cw_dt_source = sweep_time / points, source
        logging.info("CW capture: %.4f GHz, IFBW %g Hz, %d points, one every %.4f ms, %.3f s (%s)",
                     t.freq_hz / 1e9, t.ifbw_hz, points, self.cw_dt_s * 1e3, sweep_time, source)

    def arm_cw(self) -> None:
        """Gets ready for one capture; called before every element (the single-sweep hold fires
        once and then holds, so it has to be re-armed each time).

        USB trigger: trigger source external FIRST, then single-sweep hold, then wait
        ARM_SETTLE_S so the VNA has finished arming before the pulse (see the module notes).
        Software trigger: the channel is simply held, and fire_cw sends :TRIG:SING, the same
        trigger the frequency-sweep scans use."""
        if self._cw.trigger == "usb":
            self.instr.write(":TRIG:SOUR EXT")
            self.instr.write(":SENS1:HOLD:FUNC SING")
            self.instr.query("*OPC?")
            time.sleep(ARM_SETTLE_S)
        else:
            self.instr.write(":SENS1:HOLD:FUNC HOLD")
            self.instr.query("*OPC?")

    def fire_cw(self) -> float:
        """Starts the capture. Returns time.perf_counter() taken immediately before the trigger
        was sent: the time zero every voltage change is measured from."""
        t0 = time.perf_counter()
        if self._trigger_pin is not None:
            self._trigger_pin.pulse()
        else:
            self.instr.write(":TRIG:SING")
        return t0

    def read_cw(self) -> np.ndarray:
        """Called once the capture has had time to finish. Holds the channel first, then reads,
        the order the reference script uses."""
        self.instr.write(":SENS1:HOLD:FUNC HOLD")
        time.sleep(READ_SETTLE_S)
        raw = self.instr.query_binary_values(":CALC1:DATA:SDATA?", datatype="d", is_big_endian=False,
                                             container=np.array)
        if raw.size % 2:
            raise RuntimeError(f"The VNA returned an odd number of values ({raw.size}) for complex S11.")
        s11 = raw[0::2] + 1j * raw[1::2]
        if s11.size != self.cw_points:
            raise RuntimeError(f"The VNA returned {s11.size} points instead of {self.cw_points}, so the time axis "
                               "would be wrong for this capture; not saved.")
        if self._last_cw is not None and np.array_equal(s11, self._last_cw):
            # A dropped trigger leaves the previous capture in the buffer. Real captures never
            # repeat bit for bit (noise alone sees to that), so this is the VNA not having swept.
            raise RuntimeError("The VNA returned exactly the previous capture: the trigger was missed and no "
                               "sweep ran. Not saved. If this keeps happening, raise ARM_SETTLE_S in vna.py.")
        self._last_cw = s11.copy()
        return s11

    def _restore_after_cw(self) -> None:
        for cmd, value in self._cw_saved.items():
            try:
                self.instr.write(f"{cmd} {value}")
            except Exception as e:
                logging.warning("Couldn't put %s back to %s after CW capture: %s", cmd, value, e)
        try:
            self.instr.query("*OPC?")
        except Exception:
            pass
        self._cw_saved, self._cw = {}, None

    def close(self) -> None:
        try:
            if self.instr is not None and self._cw is not None:
                self._restore_after_cw()
        except Exception:
            pass
        try:
            if self._trigger_pin is not None:
                self._trigger_pin.close()
        except Exception:
            pass
        self._trigger_pin = None
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
