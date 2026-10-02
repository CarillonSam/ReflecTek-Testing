"""
Data handler. One .npz file per visited coordinate, plus run-level metadata.

Per-coordinate files (the legacy protocol): the first time a coordinate is visited its
file is created with every array already at its final size, (voltage_count, vna_points),
and filled with NaN ("empty"). Every later visit at another voltage rewrites the whole
file: everything already saved plus the slot just measured. So at any moment, including
after a crash or a cancelled scan, each file holds every voltage measured there so far,
and `measured` says which slots are real.

Keys in each file:
  e              (V,)    voltage applied at each slot; NaN until measured (legacy key)
  iteration      ()      index of the voltage slot written most recently (legacy key)
  sdata          (V, N)  raw complex S11, NaN until measured. Magnitude and phase aren't
                         stored, since they're derivable: np.abs(sdata) and
                         np.degrees(np.angle(sdata)).
  measured       (V,)    True where a slot holds real data
  measured_time  (V,)    Unix time each slot was measured; NaN until measured
  voltages_v     (V,)    the full planned voltage list (so slot i means voltages_v[i])
  frequencies_hz (N,)    frequency axis, from the VNA's read-back sweep settings
  density, logical_row, logical_col   which element this is (row/col are 0-indexed,
                                      within that density)
  stage_x_mm, stage_y_mm              ideal (planned) position
  physical_x_mm, physical_y_mm        position after the stage calibration correction
  y_loop, x_loop                      full-grid row and visit index within that row

Files are written to a temporary name and then renamed over the old one, so a crash
mid-write can't corrupt a file that already holds earlier voltages.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import numpy as np

from config import SaveConfig
from stage import ScanPoint


def point_filename(point: ScanPoint) -> str:
    """e.g. L_R005_C012.npz: density, then row and column within that density (0-indexed)."""
    return f"{point.density}_R{point.logical_row:03d}_C{point.logical_col:03d}.npz"


class DataSaver:
    """Honors SaveConfig flags internally, so callers don't need to gate each call."""

    def __init__(self, config: SaveConfig) -> None:
        self.config = config
        self.run_dir = Path(config.output_dir) / config.run_name
        if self.run_dir.exists() and any(
            p.suffix in (".npz", ".npy") or p.name == "metadata.json" for p in self.run_dir.iterdir()
        ):
            raise FileExistsError(
                f"Run folder {self.run_dir} already contains scan data. Choose a new run name "
                "(or move the old data) so it isn't overwritten."
            )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._sdata: np.ndarray | None = None

    def save_metadata(self, metadata: dict[str, Any]) -> Path | None:
        if not self.config.save_metadata_json:
            return None
        path = self.run_dir / "metadata.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, default=str)
        return path

    def save_point(
        self,
        point: ScanPoint,
        voltages_v: np.ndarray,
        voltage_index: int,
        sdata: np.ndarray,
        frequencies_hz: np.ndarray,
        physical_xy: tuple[float, float],
    ) -> Path | None:
        """Fills slot voltage_index of this coordinate's file and rewrites the file."""
        if not self.config.save_individual_npz:
            return None
        path = self.run_dir / point_filename(point)
        arrays = self._load_or_create(path, point, voltages_v, frequencies_hz, physical_xy)

        arrays["e"][voltage_index] = voltages_v[voltage_index]
        arrays["sdata"][voltage_index] = sdata
        arrays["measured"][voltage_index] = True
        arrays["measured_time"][voltage_index] = time.time()
        arrays["iteration"] = np.int64(voltage_index)

        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as f:  # a file object, so numpy doesn't append ".npz" to the name
            np.savez(f, **arrays)
        os.replace(tmp, path)  # atomic: the old file stays intact until the new one is complete
        return path

    @staticmethod
    def _load_or_create(path: Path, point: ScanPoint, voltages_v: np.ndarray,
                        frequencies_hz: np.ndarray, physical_xy: tuple[float, float]) -> dict:
        if path.exists():
            with np.load(path) as existing:
                return {k: existing[k].copy() for k in existing.files}
        v, n = len(voltages_v), len(frequencies_hz)
        return {
            # data slots, all "empty" until measured
            "e": np.full(v, np.nan),
            "sdata": np.full((v, n), np.nan + 1j * np.nan, dtype=np.complex128),
            "measured": np.zeros(v, dtype=bool),
            "measured_time": np.full(v, np.nan),
            "iteration": np.int64(-1),
            # fixed for the whole run
            "voltages_v": np.asarray(voltages_v, dtype=float),
            "frequencies_hz": np.asarray(frequencies_hz, dtype=float),
            "density": np.str_(point.density),
            "logical_row": np.int64(point.logical_row),
            "logical_col": np.int64(point.logical_col),
            "y_loop": np.int64(point.y_loop),
            "x_loop": np.int64(point.x_loop),
            "stage_x_mm": np.float64(point.stage_x_mm),
            "stage_y_mm": np.float64(point.stage_y_mm),
            "physical_x_mm": np.float64(physical_xy[0]),
            "physical_y_mm": np.float64(physical_xy[1]),
        }

    # ------------------------------------------------ optional whole-run summary ----

    def open_summary_arrays(self, point_count: int, voltage_count: int, vna_points: int) -> np.ndarray | None:
        """
        Optional (save_summary_npz, off by default): one disk-backed array of every trace,
        shape (point_count, voltage_count, vna_points), in active-point order. It duplicates
        the per-coordinate files, so it doubles disk use; turn it on only if an analysis
        wants everything in one array.
        """
        if not self.config.save_summary_npz:
            return None
        self._sdata = np.lib.format.open_memmap(
            self.run_dir / "summary_sdata.npy", mode="w+", dtype=np.complex128,
            shape=(point_count, voltage_count, vna_points),
        )
        return self._sdata

    def finalize_summary(self, voltages_v: np.ndarray, active_points: np.ndarray,
                         frequencies_hz: np.ndarray) -> Path | None:
        if not self.config.save_summary_npz:
            return None
        self._sdata.flush()
        path = self.run_dir / "summary_index.npz"
        np.savez(path, voltages_v=voltages_v, active_points=active_points, frequencies_hz=frequencies_hz)
        return path
