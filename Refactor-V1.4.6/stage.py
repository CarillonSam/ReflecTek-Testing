"""Motor stage handler, plus the scan geometry (where the stage should go)."""

from __future__ import annotations

import json
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List

import serial

from config import ScanGeometryConfig, StageConfig


@dataclass
class StagePosition:
    x_mm: float
    y_mm: float


@dataclass
class StageCalibration:
    """
    Linear correction from ideal (planned) coordinates to actual physical stage
    coordinates, e.g. from stage skew or scale error. Identity (1.0, 0.0, 1.0, 0.0)
    means "no correction" — the default when nothing has been calibrated yet.
    """

    scale_x: float = 1.0
    skew_x: float = 0.0
    scale_y: float = 1.0
    skew_y: float = 0.0

    def transform(self, ideal_x_mm: float, ideal_y_mm: float) -> tuple[float, float]:
        return (
            ideal_x_mm * self.scale_x + ideal_y_mm * self.skew_x,
            ideal_y_mm * self.scale_y + ideal_x_mm * self.skew_y,
        )

    def to_dict(self) -> dict:
        return {"scale_x": self.scale_x, "skew_x": self.skew_x, "scale_y": self.scale_y, "skew_y": self.skew_y}

    @classmethod
    def from_dict(cls, d: dict) -> "StageCalibration":
        return cls(
            scale_x=d.get("scale_x", 1.0), skew_x=d.get("skew_x", 0.0),
            scale_y=d.get("scale_y", 1.0), skew_y=d.get("skew_y", 0.0),
        )

    def save(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path: str | Path) -> "StageCalibration":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    @classmethod
    def load_or_identity(cls, path: str | Path | None) -> "StageCalibration":
        """Loads calibration_file if it exists; otherwise returns an uncalibrated identity."""
        if path and Path(path).exists():
            return cls.load(path)
        return cls()


class MotorStage(ABC):
    """
    Interface a motor stage handler must implement, so run_scan can work with any of them.
    goto_xy / wait_until_reached take physical stage coordinates. goto_ideal_xy /
    wait_until_reached_ideal (below) take ideal/planned coordinates and apply
    `self.calibration` first — implementations get these for free by setting
    self.calibration in their own __init__ (see GrblXY).
    """

    calibration: StageCalibration

    @abstractmethod
    def goto_xy(self, x_mm: float, y_mm: float) -> None: ...

    @abstractmethod
    def wait_until_reached(self, target_x_mm: float, target_y_mm: float, timeout_s: float = 30.0) -> StagePosition: ...

    @abstractmethod
    def home_xy(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def goto_ideal_xy(self, ideal_x_mm: float, ideal_y_mm: float) -> None:
        self.goto_xy(*self.calibration.transform(ideal_x_mm, ideal_y_mm))

    def wait_until_reached_ideal(
        self, ideal_x_mm: float, ideal_y_mm: float, timeout_s: float = 30.0
    ) -> StagePosition:
        return self.wait_until_reached(*self.calibration.transform(ideal_x_mm, ideal_y_mm), timeout_s)


class GrblXY(MotorStage):
    """GRBL XY stage with software-defined coordinates and relative moves (G91)."""

    def __init__(self, config: StageConfig) -> None:
        self.config = config
        self.calibration = StageCalibration.load_or_identity(config.calibration_file)
        self.ser = serial.Serial(config.port, config.baud, timeout=1)
        try:
            time.sleep(config.startup_delay_s)
            self.ser.write(b"\r\n\r\n")
            time.sleep(0.5)
            self.ser.reset_input_buffer()

            self.x_mm = 0.0
            self.y_mm = 0.0
            for cmd in ("$X", "G21", "G91", f"F{config.feed_mm_per_min:.3f}"):
                self._send(cmd)
        except Exception:
            # Startup handshake failed after the port opened — close it explicitly rather
            # than leaving it open on a discarded, partially-constructed object.
            self.ser.close()
            raise

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass

    def _send(self, cmd: str) -> None:
        self.ser.write((cmd.strip() + "\n").encode())
        while True:
            line = self.ser.readline().decode(errors="ignore").strip()
            if not line:
                continue
            if line.lower().startswith(("error", "alarm")):
                raise RuntimeError(f"GRBL response to '{cmd}': {line}")
            if line == "ok":
                break

    def goto_xy(self, x_mm: float, y_mm: float) -> None:
        tx, ty = float(x_mm), float(y_mm)
        self._send(f"G1 X{tx - self.x_mm:.3f} Y{ty - self.y_mm:.3f}")
        self.x_mm, self.y_mm = tx, ty
        if self.config.settle_s > 0:
            time.sleep(self.config.settle_s)

    def goto_x(self, x_mm: float) -> None:
        """Move X only, leaving Y wherever it currently is."""
        self.goto_xy(x_mm, self.y_mm)

    def goto_y(self, y_mm: float) -> None:
        """Move Y only, leaving X wherever it currently is."""
        self.goto_xy(self.x_mm, y_mm)

    def home_xy(self) -> None:
        self.goto_xy(0.0, 0.0)

    def set_software_zero_here(self) -> None:
        self.x_mm = self.y_mm = 0.0

    def get_pos(self) -> StagePosition:
        self.ser.reset_input_buffer()
        self.ser.write(b"?")
        deadline = time.time() + 1.0
        while time.time() < deadline:
            line = self.ser.readline().decode("utf-8", errors="ignore").strip()
            if "MPos:" in line:
                x, y = line.split("MPos:")[1].split("|")[0].split(",")[:2]
                return StagePosition(float(x), float(y))
        raise TimeoutError("Timed out waiting for GRBL position response.")

    def wait_until_reached(
        self, target_x_mm: float, target_y_mm: float, timeout_s: float = 30.0
    ) -> StagePosition:
        tol, poll = self.config.position_tolerance_mm, self.config.readback_poll_s
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            pos = self.get_pos()
            if abs(target_x_mm - pos.x_mm) <= tol and abs(target_y_mm - pos.y_mm) <= tol:
                return pos
            time.sleep(poll)
        raise TimeoutError(f"Stage did not reach target ({target_x_mm:.3f}, {target_y_mm:.3f}) mm.")


# ============================================================ scan geometry ====

@dataclass(frozen=True)
class ScanPoint:
    y_loop: int
    x_loop: int
    logical_row: int
    logical_col: int
    stage_x_mm: float
    stage_y_mm: float
    density: str = "L"  # "L" or "H" — which of the 3 interleaved sub-lattices this point is on
    skipped: bool = False


class HexGridPlanner:
    """
    Generates the full interleaved hex grid: three hex sub-grids of `rows` x `cols`
    each, interleaved into one denser hex grid whose nearest-neighbor spacing is
    config.spacing_mm. Removing one sub-grid (L) leaves the other two (H), so the full
    grid has 3 * rows * cols points: rows * cols L and 2 * rows * cols H.

    Layout: every full-grid row is horizontal, with sub_spacing_mm (= sqrt(3) *
    spacing_mm) between points along a row and dense_row_spacing_mm (= spacing_mm / 2)
    between rows; alternate rows are offset by half of sub_spacing_mm. Every 3rd row
    belongs to L, and L on its own is a hex grid with sub_spacing_mm spacing and
    horizontal rows. The full grid is the same lattice rotated 30 degrees: its nearest
    neighbors sit at +/-30 degrees and straight up/down, not along the rows.

    l_subgrid (1/2/3) is purely about physical stage motion — it picks which of the
    3 possible row-phases *this DUT's* low-density elements are physically mounted
    at (different DUTs can differ here). It has nothing to do with controller
    addressing: ScanPoint.logical_row is each point's index *within its own density*
    (0 to rows-1 for L, 0 to 2*rows-1 for H) — the same numbering a pin mapping uses,
    completely independent of l_subgrid, since which physical pin drives "the 5th L
    element" doesn't change just because that element moved to a different phase.
    density_mode picks which of L/H is "active" (see active_points() vs all_points()).
    """

    def __init__(self, config: ScanGeometryConfig) -> None:
        self.config = config

    @property
    def spacing_mm(self) -> float:
        """Nearest-neighbor spacing of the full interleaved grid (as configured)."""
        return self.config.spacing_mm

    @property
    def sub_spacing_mm(self) -> float:
        """Nearest-neighbor spacing within one sub-grid (L, or either half of H), which
        is also the point-to-point pitch along every row: sqrt(3) * spacing_mm."""
        return self.config.spacing_mm * math.sqrt(3)

    @property
    def dense_row_spacing_mm(self) -> float:
        """Row-to-row pitch of the full grid: spacing_mm / 2 unless overridden."""
        if self.config.row_spacing_mm is not None:
            return self.config.row_spacing_mm
        return self.config.spacing_mm / 2

    @property
    def row_spacing_mm(self) -> float:
        """Row-to-row pitch of one sub-grid (every 3rd full-grid row)."""
        return self.dense_row_spacing_mm * 3

    def iter_all_points(self) -> Iterator[ScanPoint]:
        c = self.config
        sub_spacing_mm = self.sub_spacing_mm
        dense_row_spacing_mm = self.dense_row_spacing_mm
        l_phase = (c.l_subgrid - 1) % 3
        total_dense_rows = c.rows * 3
        l_row = 0  # within-density counters — advance independently of physical dense_row
        h_row = 0

        for dense_row in range(total_dense_rows):
            density = "L" if (dense_row % 3) == l_phase else "H"
            stage_y_mm = c.y_direction_sign * dense_row_spacing_mm * dense_row

            is_offset_row = (dense_row % 2 == 1) if c.offset_odd_rows else (dense_row % 2 == 0)
            row_offset_mm = c.stagger_sign * sub_spacing_mm / 2 if is_offset_row else 0.0

            col_order = range(c.cols)
            if c.serpentine and dense_row % 2 == 1:
                col_order = reversed(col_order)

            logical_row = l_row if density == "L" else h_row
            for visit_index, col in enumerate(col_order):
                yield ScanPoint(
                    y_loop=dense_row,
                    x_loop=visit_index,
                    logical_row=logical_row,
                    logical_col=col,
                    stage_x_mm=c.x_direction_sign * (col * sub_spacing_mm + row_offset_mm),
                    stage_y_mm=stage_y_mm,
                    density=density,
                )
            if density == "L":
                l_row += 1
            else:
                h_row += 1

    def all_points(self) -> List[ScanPoint]:
        """Every point in the full grid (both L and H) — for plotting."""
        return list(self.iter_all_points())

    def active_points(self) -> List[ScanPoint]:
        """Only the points matching config.density_mode — what actually gets scanned."""
        return [p for p in self.iter_all_points() if p.density == self.config.density_mode]

    def calibration_targets(self) -> "CalibrationTargets":
        """
        The three real elements the stage calibration nudges to, all in ideal
        (planned) coordinates, so each target is something the operator can physically
        line up on:
          - origin:   the planner's (0, 0) element (first row, first column of the full
                      grid). It's L when l_subgrid=1, otherwise H, but always real.
          - x_corner: last element of the first L row (far end of X).
          - y_corner: first element of the last L row (far end of Y).
        The corners are taken from the actual generated L points, so the alternate-row
        offset and the l_subgrid phase are already accounted for. They are generally
        not on the axes, which is why calibrate_stage.calibrate solves a full 2x2
        system rather than assuming axis-aligned corners.
        """
        points = self.all_points()
        # Ideal (0, 0) is where calibration zeroes the stage. With the default
        # offset_odd_rows=True an element sits exactly there; with offset_odd_rows=False
        # row 0 is shifted and nothing does, so origin is None and the operator just
        # lines up on the stage's (0, 0) reference instead.
        origin = next((p for p in points if abs(p.stage_x_mm) < 1e-9 and abs(p.stage_y_mm) < 1e-9), None)
        l_points = [p for p in points if p.density == "L"]
        last_col = self.config.cols - 1
        last_l_row = max(p.logical_row for p in l_points)
        x_corner = next(p for p in l_points if p.logical_row == 0 and p.logical_col == last_col)
        y_corner = next(p for p in l_points if p.logical_row == last_l_row and p.logical_col == 0)
        return CalibrationTargets(origin=origin, x_corner=x_corner, y_corner=y_corner)


@dataclass(frozen=True)
class CalibrationTargets:
    origin: ScanPoint | None
    x_corner: ScanPoint
    y_corner: ScanPoint

    @staticmethod
    def describe(point: ScanPoint | None) -> str:
        """Human-readable label for a dialog: density, row/col within that density, position."""
        if point is None:
            return "stage reference (0.000, 0.000) mm (no element there)"
        return (
            f"{point.density} element row {point.logical_row + 1}, col {point.logical_col + 1} "
            f"at ({point.stage_x_mm + 0.0:.3f}, {point.stage_y_mm + 0.0:.3f}) mm"
        )
