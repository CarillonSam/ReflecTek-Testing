"""
Resuming a scan that stopped before it finished (an error, a cancel, a crash).

The per-coordinate files are the record of what was measured: each holds a `measured` flag
per voltage slot and is written atomically (see data.py), so every slot marked measured is
complete. load_resume_plan() reads a run folder and returns a ResumePlan: the run's own
settings (from its metadata.json), the stage calibration it used, and which measurements are
already done. AutomatedArrayScanner(plan.config, resume=plan) then measures only the rest,
in the original order, adding to the same files.

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
    measured: dict[tuple[str, int, int], np.ndarray]   # element -> measured flag per voltage slot
    voltage_count: int
    done: int
    total: int

    def is_done(self, point: ScanPoint, voltage_index: int) -> bool:
        m = self.measured.get((point.density, point.logical_row, point.logical_col))
        return m is not None and bool(m[voltage_index])

    def voltage_done(self, voltage_index: int) -> bool:
        return all(self.is_done(p, voltage_index) for p in self.points)

    def next_measurement(self) -> tuple[int, ScanPoint] | None:
        for v in range(self.voltage_count):
            for p in self.points:
                if not self.is_done(p, v):
                    return v, p
        return None

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
    """Raises ValueError, with the reason, if the folder can't be resumed."""
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
    measured: dict[tuple[str, int, int], np.ndarray] = {}
    frequencies_hz = None
    for p in points:
        path = folder / point_filename(p)
        if not path.exists():
            continue
        try:
            with np.load(path) as f:
                flags = np.asarray(f["measured"], dtype=bool)
                freqs = np.asarray(f["frequencies_hz"], dtype=float)
        except Exception as e:
            raise ValueError(f"Couldn't read {path.name} in {folder.name}: {e}") from e
        if len(flags) != voltage_count:
            raise ValueError(f"{path.name} has {len(flags)} voltage slots, but the run has {voltage_count}.")
        if frequencies_hz is None:
            frequencies_hz = freqs
        elif len(freqs) != len(frequencies_hz) or not np.allclose(freqs, frequencies_hz, rtol=1e-9, atol=0):
            raise ValueError(f"{path.name} has a different frequency axis from the run's other files.")
        measured[(p.density, p.logical_row, p.logical_col)] = flags

    total = voltage_count * len(points)
    done = int(sum(int(m.sum()) for m in measured.values()))
    if done >= total:
        raise ValueError(f"{folder.name} is already complete: all {total} measurements are there.")
    calibration = StageCalibration.from_dict(metadata.get("stage_calibration", {}).get("coefficients", {}))
    return ResumePlan(folder, config, calibration, frequencies_hz, points, measured, voltage_count, done, total)
