"""Data handler: writes metadata, per-point, and summary results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from config import SaveConfig
from stage import ScanPoint


class DataSaver:
    """Honors SaveConfig flags internally, so callers don't need to gate each call."""

    def __init__(self, config: SaveConfig) -> None:
        self.config = config
        self.run_dir = Path(config.output_dir) / config.run_name
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
        voltage_v: float,
        voltage_index: int,
        sdata: np.ndarray,
    ) -> Path | None:
        """
        Stores sdata only — amplitude/phase are derivable on load (np.abs(sdata),
        np.degrees(np.angle(sdata))) and storing them too would just be a redundant
        second copy of the same information at roughly double the disk cost.
        """
        if not self.config.save_individual_npz:
            return None
        fname = (
            f"V{voltage_index:03d}_C{point.logical_col:03d}_R{point.logical_row:03d}_"
            f"Y{point.y_loop:03d}_X{point.x_loop:03d}.npz"
        )
        path = self.run_dir / fname
        np.savez(
            path,
            voltage_v=voltage_v,
            voltage_index=voltage_index,
            logical_row=point.logical_row,
            logical_col=point.logical_col,
            y_loop=point.y_loop,
            x_loop=point.x_loop,
            stage_x_mm=point.stage_x_mm,
            stage_y_mm=point.stage_y_mm,
            skipped=point.skipped,
            sdata=sdata,
        )
        return path

    def open_summary_arrays(self, point_count: int, voltage_count: int, vna_points: int) -> np.ndarray | None:
        """
        Creates a disk-backed (memory-mapped) sdata array sized
        point_count x voltage_count x vna_points, instead of allocating it fully in
        RAM — for a large scan, the in-RAM version can run into gigabytes; a memmap
        keeps actual RAM usage small regardless of size, since the OS pages data
        to/from disk as it's touched. Stores complex sdata only (not derived
        amplitude/phase — see save_point) so this isn't a second redundant copy on
        top of itself. Returns None if save_summary_npz is off.
        """
        if not self.config.save_summary_npz:
            return None
        self._sdata = np.lib.format.open_memmap(
            self.run_dir / "summary_sdata.npy", mode="w+", dtype=np.complex128,
            shape=(point_count, voltage_count, vna_points),
        )
        return self._sdata

    def finalize_summary(self, voltages_v: np.ndarray, active_points: np.ndarray) -> Path | None:
        """
        Flushes the memmapped array from open_summary_arrays to disk and writes the
        small voltages_v/active_points arrays alongside it. Call once after the scan
        loop completes (skip on cancellation — the memmap is already on disk but
        would be incomplete). A no-op if save_summary_npz is off.
        """
        if not self.config.save_summary_npz:
            return None
        self._sdata.flush()
        path = self.run_dir / "summary_index.npz"
        np.savez(path, voltages_v=voltages_v, active_points=active_points)
        return path
