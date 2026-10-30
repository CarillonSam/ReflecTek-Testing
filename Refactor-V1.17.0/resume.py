"""
Resuming a scan that stopped before it finished (an error, a cancel, a crash).

A scan measures in a fixed order (each voltage step in turn; within a step, the active
points in planner order) and saves each measurement before taking the next, and a resume
carries on in that order. So the measurements done are always one unbroken stretch from the
start: the only thing to find is where it ends, the first measurement not yet saved.

load_resume_plan() finds that without opening the element files one by one:
  1. The folder listing gives every element file's last-modified time without opening any of
     them. The newest file is the element measured last.
  2. That one file is opened, to read which voltage slot it last saved (`iteration`). The
     measurement after it, in scan order, is the candidate resume point.
  3. The candidate is confirmed by opening the file of the measurement it names: the one
     before must be saved and this one not. Since the done measurements are one unbroken
     stretch, that can only be true at the real resume point, so the guess is either exactly
     right or rejected.
  4. If it's rejected (modified times changed by copying the folder with a tool that doesn't
     keep them, or tied on a FAT32 drive's 2-second clock), the resume point is found by
     halving instead: about log2(measurements) file opens, e.g. 13 for 6144.
This assumes the stretch really is unbroken, which the program itself always keeps true; a
file deleted or moved by hand partway through the run would not be noticed.

It returns a ResumePlan: the run's own settings (from its metadata.json), the stage
calibration it used, and how far the run got. AutomatedArrayScanner(plan.config, resume=plan)
then measures only the rest, in the original order, adding to the same files.

What the resumed part takes from where:
  - From the run (so one folder never mixes two setups): geometry, voltages, VNA sweep,
    stage calibration coefficients, voltage pattern (the copy saved in the run folder),
    board voltage limits, pinout and addressing, settle times, save options, return_home.
  - From the current Settings (they don't change what is measured, and may be exactly what
    was changed to get the scan going again): connection settings for the stage, board
    controller and VNA, and the stage's motion settings (feed, settle, tolerances, timeouts).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from config import RunConfig, config_from_dict
from data import point_filename
from pattern import element_label
from stage import HexGridPlanner, ScanPoint, StageCalibration

# Settings taken from the current Settings tab rather than from the run (see above).
CURRENT_PIXELS_FIELDS = ("port", "baud", "timeout_s", "ack_timeout_ping_s", "ack_timeout_set_s",
                         "ack_timeout_save_s", "batch_size_pixels")
CURRENT_PI_FIELDS = ("host", "username", "password", "key_filename", "port")
CURRENT_VNA_FIELDS = ("visa_resource", "visa_timeout_ms", "visa_backend")
RUN_STAGE_FIELDS = ("calibration_file",)   # every other stage setting comes from the current Settings


@dataclass
class ResumePlan:
    folder: Path
    config: RunConfig                       # the run's settings, with current connection settings
    calibration: StageCalibration           # the coefficients the run started with
    frequencies_hz: np.ndarray | None       # the run's frequency axis, from its files
    points: list[ScanPoint]                 # active points, in scan order
    voltage_count: int
    done: int                               # measurements saved = scan-order index of the next one
    total: int
    found_by: str = ""                      # "modified times" or "halving"
    files_opened: int = 0

    def __post_init__(self) -> None:
        self._position = {(p.density, p.logical_row, p.logical_col): i for i, p in enumerate(self.points)}

    def is_done(self, point: ScanPoint, voltage_index: int) -> bool:
        i = self._position[(point.density, point.logical_row, point.logical_col)]
        return voltage_index * len(self.points) + i < self.done

    def voltage_done(self, voltage_index: int) -> bool:
        return (voltage_index + 1) * len(self.points) <= self.done

    def next_measurement(self) -> tuple[int, ScanPoint] | None:
        if self.done >= self.total:
            return None
        v, i = divmod(self.done, len(self.points))
        return v, self.points[i]

    def describe(self) -> str:
        c = self.config
        kind = "Voltage pattern run" if c.scan_type == "pattern" else "Sweep run"
        text = f"{kind} {self.folder.name}: {self.done} of {self.total} measurements done, {self.total - self.done} to go."
        nxt = self.next_measurement()
        if nxt is not None:
            v, p = nxt
            where = f"{element_label(p.density, p.logical_row, p.logical_col)} (row {p.logical_row}, column {p.logical_col})"
            if c.scan_type == "pattern":
                text += f"\nResumes at {where}."
            else:
                text += f"\nResumes at voltage {v + 1}/{self.voltage_count} ({c.voltages_v[v]:g} V), {where}."
        return text

    def metadata_entry(self) -> dict:
        nxt = self.next_measurement()
        return {
            "resumed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "measurements_done_before": self.done,
            "measurements_total": self.total,
            "resume_point_found_by": self.found_by,
            "resumed_at_voltage_index": None if nxt is None else nxt[0],
            "resumed_at_element": None if nxt is None else
                element_label(nxt[1].density, nxt[1].logical_row, nxt[1].logical_col),
            "settings_from_current": {
                "stage": {k: v for k, v in vars(self.config.stage).items() if k not in RUN_STAGE_FIELDS},
                "pixels": {k: getattr(self.config.pixels, k) for k in CURRENT_PIXELS_FIELDS},
                "pi": {k: getattr(self.config.pi, k) for k in CURRENT_PI_FIELDS if k != "password"},
                "vna": {k: getattr(self.config.vna, k) for k in CURRENT_VNA_FIELDS},
            },
        }


def _merged_config(run: RunConfig, current: RunConfig, folder: Path) -> RunConfig:
    stage = replace(current.stage, **{k: getattr(run.stage, k) for k in RUN_STAGE_FIELDS})
    pixels = replace(run.pixels, **{k: getattr(current.pixels, k) for k in CURRENT_PIXELS_FIELDS})
    pi = replace(run.pi, **{k: getattr(current.pi, k) for k in CURRENT_PI_FIELDS})
    vna = replace(run.vna, **{k: getattr(current.vna, k) for k in CURRENT_VNA_FIELDS})
    # The folder may have been moved or renamed since: save into it wherever it is now.
    save = replace(run.save, output_dir=folder.parent, run_name=folder.name)
    pattern_csv = run.pattern_csv
    if run.scan_type == "pattern":
        pattern_csv = folder / "voltage_pattern_source.csv"   # the copy made when the run started
    return replace(run, stage=stage, pixels=pixels, pi=pi, vna=vna, save=save, pattern_csv=pattern_csv)


def load_resume_plan(folder: str | Path, current: RunConfig) -> ResumePlan:
    """Raises ValueError, with the reason, if the folder can't be resumed. Opens two element
    files normally, about log2(measurements) at most."""
    folder = Path(folder)
    meta_path = folder / "metadata.json"
    if not meta_path.is_file():
        raise ValueError(f"{folder} isn't a scan run folder: it has no metadata.json.")
    with open(meta_path, encoding="utf-8") as f:
        metadata = json.load(f)
    if "config" not in metadata:
        raise ValueError(f"{meta_path.name} in {folder.name} has no saved settings, so the run can't be rebuilt.")
    run = config_from_dict(RunConfig, metadata["config"])
    if run.dry_run:
        raise ValueError(f"{folder.name} was a dry run (no hardware, no real data), so there's nothing to resume.")
    if not run.save.save_individual_npz:
        raise ValueError(f"{folder.name} was run without per-element files (save_individual_npz off), "
                         "so there's no record of which points were measured.")
    if run.scan_type == "pattern" and not (folder / "voltage_pattern_source.csv").is_file():
        raise ValueError(f"{folder.name} is a voltage pattern run, but its copy of the pattern "
                         "(voltage_pattern_source.csv) is missing.")
    config = _merged_config(run, current, folder)

    points = HexGridPlanner(config.geometry).active_points()
    saved_points = metadata.get("active_points")
    if saved_points is not None:
        saved = [(p["density"], p["logical_row"], p["logical_col"]) for p in saved_points]
        if saved != [(p.density, p.logical_row, p.logical_col) for p in points]:
            raise ValueError(f"The scan order rebuilt from {folder.name}'s settings doesn't match the one it saved "
                             f"({len(points)} vs {len(saved)} points), so it can't be resumed safely.")

    voltage_count = 1 if config.scan_type == "pattern" else len(config.voltages_v)
    search = _ProgressSearch(folder, points, voltage_count)
    done, found_by = search.find()
    total = voltage_count * len(points)
    if done >= total:
        raise ValueError(f"{folder.name} is already complete: all {total} measurements are there.")
    coefficients = (metadata.get("stage_calibration") or {}).get("coefficients")
    if not coefficients:
        raise ValueError(f"{meta_path.name} in {folder.name} doesn't record the stage calibration the run used, "
                         "so the remaining points can't be placed exactly where the first part's were.")
    calibration = StageCalibration.from_dict(coefficients)
    return ResumePlan(folder, config, calibration, search.frequencies_hz, points, voltage_count, done, total,
                      found_by, search.opened)


class _ProgressSearch:
    """Finds how many measurements in scan order are saved (see the module docstring), opening
    as few element files as it can. Measurement k is point k % P at voltage slot k // P."""

    def __init__(self, folder: Path, points: list[ScanPoint], voltage_count: int) -> None:
        self.folder, self.points, self.v = folder, points, voltage_count
        self.total = voltage_count * len(points)
        self.opened = 0
        self.frequencies_hz: np.ndarray | None = None
        self._freq_bytes: int | None = None
        self._files: dict[int, tuple[np.ndarray, int] | None] = {}   # point index -> (measured, iteration)

    def _read(self, i: int) -> tuple[np.ndarray, int] | None:
        """(measured flags, last slot written) of point i's file, or None if it has no file yet."""
        if i not in self._files:
            path = self.folder / point_filename(self.points[i])
            if not path.exists():
                self._files[i] = None
                return None
            self.opened += 1
            try:
                with np.load(path) as f:
                    flags = np.asarray(f["measured"], dtype=bool)
                    iteration = int(f["iteration"])
                    size = f.zip.getinfo("frequencies_hz.npy").file_size   # its length, without reading it
                    if self.frequencies_hz is None:
                        self.frequencies_hz, self._freq_bytes = np.asarray(f["frequencies_hz"], dtype=float), size
            except Exception as e:
                raise ValueError(f"Couldn't read {path.name} in {self.folder.name}: {e}") from e
            if len(flags) != self.v:
                raise ValueError(f"{path.name} has {len(flags)} voltage slots, but the run has {self.v}.")
            if size != self._freq_bytes:
                raise ValueError(f"{path.name} has a different number of frequency points from the run's other files.")
            self._files[i] = (flags, iteration)
        return self._files[i]

    def _saved(self, k: int) -> bool:
        """Is measurement k (in scan order) saved?"""
        if k < 0:
            return True
        if k >= self.total:
            return False
        v, i = divmod(k, len(self.points))
        record = self._read(i)
        return record is not None and bool(record[0][v])

    def _is_boundary(self, k: int) -> bool:
        return self._saved(k - 1) and not self._saved(k)

    def _guess_from_modified_times(self) -> int | None:
        """The measurement after the newest element file's last save, or None if there are no files."""
        index = {point_filename(p): i for i, p in enumerate(self.points)}
        newest = None   # (modified time, scan position, point index)
        with os.scandir(self.folder) as entries:
            for entry in entries:
                i = index.get(entry.name)
                if i is not None:
                    key = (entry.stat().st_mtime_ns, i)
                    if newest is None or key > newest[:2]:
                        newest = (*key, i)
        if newest is None:
            return 0   # no element files: nothing saved yet
        i = newest[2]
        record = self._read(i)
        if record is None or not 0 <= record[1] < self.v:
            return None
        return record[1] * len(self.points) + i + 1

    def find(self) -> tuple[int, str]:
        guess = self._guess_from_modified_times()
        if guess is not None and self._is_boundary(guess):
            return guess, "modified times"
        lo, hi = 0, self.total   # halving: the first measurement not saved
        while lo < hi:
            mid = (lo + hi) // 2
            if self._saved(mid):
                lo = mid + 1
            else:
                hi = mid
        return lo, "halving"
