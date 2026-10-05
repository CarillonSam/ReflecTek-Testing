"""
Coordinator: wires the motor stage, board voltage controller, VNA, and data
handlers together and runs the scan. Run this file directly to start a scan.
"""

from __future__ import annotations

import csv
import logging
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from config import PixelControllerConfig, RunConfig, SaveConfig, StageConfig
from controller import BoardController
from data import DataSaver
from pattern import VoltagePattern, element_label, load_pattern
from pi_controller import PiBoardController
from pixel_controller import PixelController
from stage import HexGridPlanner, MotorStage, ScanPoint, GrblXY, StageCalibration
from vna import VNAController, VNAResult

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


@dataclass(frozen=True)
class ProgressEvent:
    step: int
    total_steps: int
    point: ScanPoint
    voltage_index: int
    voltage_count: int
    voltage_v: float


class AutomatedArrayScanner:
    """
    Coordinates four handlers to run a scan:
      - stage: MotorStage       (currently GrblXY)
      - array: BoardController  (currently PixelController or PiBoardController)
      - vna:   VNAController
      - saver: DataSaver
    All this class does with voltages is build a (cols, rows) grid matching the hex
    grid geometry and hand the whole thing to the controller — it has no idea how any
    controller addresses hardware. To use different hardware, construct a different
    MotorStage / BoardController implementation in connect() below.
    """

    def __init__(
        self,
        config: RunConfig,
        on_progress: Optional[Callable[[ProgressEvent], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        """
        on_progress(event) is called after each point is measured — event.step /
        event.total_steps covers the whole run (all voltages x all points), so it's a
        direct progress fraction. cancel_event, if set mid-run, stops the scan cleanly
        after the current point (hardware still gets homed/closed via close()).
        """
        self.config = config
        self.on_progress = on_progress
        self.cancel_event = cancel_event
        self.scan_plan = HexGridPlanner(config.geometry)
        self.active_points = self.scan_plan.active_points()
        row_count = config.geometry.rows if config.geometry.density_mode == "L" else config.geometry.rows * 2
        self.grid_shape = (config.geometry.cols, row_count)
        # Pattern scans: read and check the CSV now, before any hardware is touched.
        self.pattern: Optional[VoltagePattern] = None
        if config.scan_type == "pattern":
            if config.pattern_csv is None:
                raise ValueError("Voltage pattern scan selected, but no pattern CSV is set.")
            self.pattern = load_pattern(config.pattern_csv, config.geometry,
                                        config.pixels.min_voltage_v, config.pixels.max_voltage_v)
        self.stage: Optional[MotorStage] = None
        self.array: Optional[BoardController] = None
        self.vna: Optional[VNAController] = None
        self.saver = DataSaver(config.save)

    def connect(self) -> None:
        if self.config.dry_run:
            logging.info("Dry-run mode: hardware connections skipped.")
            return

        self.stage = GrblXY(self.config.stage)
        self.array = (
            PiBoardController(self.config.pi) if self.config.controller_type == "pi"
            else PixelController(self.config.pixels, self.config.geometry.density_mode)
        )
        self.vna = VNAController(self.config.vna)

        ping = self.array.ping()
        if ping.get("error"):
            raise RuntimeError(f"Board controller ping failed: {ping['error']}")
        if not ping.get("ok", False):
            raise RuntimeError(f"Board controller ping NACK: {ping.get('status_name')}")

        self.vna.initialize()

    def close(self) -> None:
        if self.config.dry_run:
            return
        try:
            if self.config.return_home and self.stage is not None:
                self.stage.home_xy()
        finally:
            for device in (self.vna, self.array, self.stage):
                if device is not None:
                    try:
                        device.close()
                    except Exception:
                        pass

    def _uniform_grid(self, voltage_v: float) -> np.ndarray:
        return np.full(self.grid_shape, voltage_v, dtype=float)

    def _single_point_grid(self, point: ScanPoint, voltage_v: float) -> np.ndarray:
        """Every element at 0 V except this point's — the controller's mapping decides
        where (logical_col, logical_row) physically lands."""
        grid = np.zeros(self.grid_shape, dtype=float)
        grid[point.logical_col, point.logical_row] = voltage_v
        return grid

    def program_voltage_for_scan(self, voltage_v: float) -> None:
        if self.config.dry_run:
            logging.info("[DRY RUN] set_voltage_grid: uniform %.3f V over %s", voltage_v, self.grid_shape)
            return

        assert self.array is not None
        self.array.set_voltage_grid(self._uniform_grid(voltage_v))
        time.sleep(self.config.uniform_board_settle_s)

    def program_voltage_for_point(self, point: ScanPoint, voltage_v: float) -> None:
        if self.config.dry_run:
            logging.info(
                "[DRY RUN] set_voltage_grid: (row=%d col=%d) -> %.3f V", point.logical_row, point.logical_col, voltage_v
            )
            return

        assert self.array is not None
        self.array.set_voltage_grid(self._single_point_grid(point, voltage_v))
        time.sleep(self.config.single_pixel_settle_s)

    def program_pattern(self) -> None:
        """Sets every element to its pattern voltage, once, then waits the settle time."""
        assert self.pattern is not None
        if self.config.dry_run:
            logging.info("[DRY RUN] voltage pattern: %s", self.pattern.describe())
            return
        assert self.array is not None
        if isinstance(self.array, PixelController):
            self.array.apply_pattern(self.pattern.voltages)  # whole board, through the L/H maps
        else:
            # The Pi controller takes one grid for the scanned density; it can't address
            # the other density's elements separately.
            density = self.config.geometry.density_mode
            others = [k for k, v in self.pattern.voltages.items() if k[0] != density and v != 0]
            if others:
                raise RuntimeError(
                    f"The pattern sets {len(others)} element(s) outside the scanned density ({density}), "
                    f"e.g. {element_label(*others[0])}; the Pi controller can only set the scanned density."
                )
            self.array.set_voltage_grid(self.pattern.grid(density, self.config.geometry))
        time.sleep(self.config.uniform_board_settle_s)

    def measure_point(self, point: ScanPoint) -> VNAResult:
        if self.config.dry_run:
            zeros = np.zeros(self.config.vna.points)
            return VNAResult(sdata=zeros.astype(np.complex128), amplitude=zeros, phase_deg=zeros)

        assert self.stage is not None and self.vna is not None
        self.stage.goto_ideal_xy(point.stage_x_mm, point.stage_y_mm)
        self.stage.wait_until_reached_ideal(point.stage_x_mm, point.stage_y_mm)
        return self.vna.trigger()

    def _frequencies_hz(self) -> np.ndarray:
        """The VNA's actual sweep (read back in initialize()), or the configured one in a dry run."""
        if self.vna is not None and self.vna.frequencies_hz is not None:
            return self.vna.frequencies_hz
        c = self.config.vna
        return np.linspace(c.start_hz, c.stop_hz, c.points)

    def _calibration(self) -> StageCalibration:
        """The correction actually applied to moves (identity if uncalibrated)."""
        if self.stage is not None:
            return self.stage.calibration
        return StageCalibration.load_or_identity(self.config.stage.calibration_file)

    def _save_pattern_files(self) -> None:
        """Copies the pattern CSV into the run folder, plus a resolved list (every element of
        the board, its pinout name and the voltage it was actually set to)."""
        run_dir = self.saver.run_dir
        shutil.copy(self.config.pattern_csv, run_dir / "voltage_pattern_source.csv")
        with open(run_dir / "voltage_pattern_applied.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["element", "density", "row", "col", "voltage_v"])
            g = self.config.geometry
            for density in ("L", "H"):
                rows = g.rows if density == "L" else g.rows * 2
                for r in range(rows):
                    for c in range(g.cols):
                        writer.writerow([element_label(density, r, c), density, r, c,
                                         self.pattern.voltage(density, r, c)])

    def _run_pattern(self, frequencies_hz: np.ndarray, calibration: StageCalibration) -> bool:
        """Pattern scan body: program once, measure each point once. Returns True if completed."""
        self._save_pattern_files()
        self.program_pattern()
        total = len(self.active_points)
        for step, point in enumerate(self.active_points, start=1):
            if self.cancel_event is not None and self.cancel_event.is_set():
                logging.info("Scan cancelled.")
                return False
            v = self.pattern.voltage(point.density, point.logical_row, point.logical_col)
            logging.info("Point %d/%d | %s (%.3f V) | x=%.3f mm y=%.3f mm", step, total,
                         element_label(point.density, point.logical_row, point.logical_col), v,
                         point.stage_x_mm, point.stage_y_mm)
            result = self.measure_point(point)
            self.saver.save_point(
                point=point, voltages_v=np.array([v]), voltage_index=0, sdata=result.sdata,
                frequencies_hz=frequencies_hz,
                physical_xy=calibration.transform(point.stage_x_mm, point.stage_y_mm),
                extra={"scan_type": np.str_("pattern"),
                       "element": np.str_(element_label(point.density, point.logical_row, point.logical_col))},
            )
            if self.on_progress:
                self.on_progress(ProgressEvent(step=step, total_steps=total, point=point,
                                               voltage_index=0, voltage_count=1, voltage_v=v))
        return True

    def run(self) -> None:
        if self.pattern is not None:
            return self._run_pattern_scan()
        voltages_v = np.asarray(self.config.voltages_v, dtype=float)
        voltage_count = len(voltages_v)
        point_count = len(self.active_points)
        total_steps = voltage_count * point_count
        step = 0
        cancelled = False

        try:
            self.connect()
            # Saved after connecting, so the frequency axis is what the VNA actually
            # confirmed and the calibration is the one the stage actually loaded.
            frequencies_hz = self._frequencies_hz()
            calibration = self._calibration()
            self.saver.save_metadata({
                "config": asdict(self.config),
                "scan_mode": "uniform_board" if self.config.uniform_board_mode else "mapped_element",
                "density_mode": self.config.geometry.density_mode,
                "voltages_v": voltages_v.tolist(),
                "frequencies_hz": {"start": float(frequencies_hz[0]), "stop": float(frequencies_hz[-1]),
                                   "points": int(len(frequencies_hz)), "spacing": "linear"},
                "stage_calibration": {"source_file": self.config.stage.calibration_file,
                                      "coefficients": calibration.to_dict()},
                "file_layout": "one <density>_R<row>_C<col>.npz per coordinate; see data.py",
                "active_point_count": point_count,
                "active_points": [point.__dict__ for point in self.active_points],
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            sdata_summary = self.saver.open_summary_arrays(point_count, voltage_count, len(frequencies_hz))

            for v_idx, voltage_v in enumerate(self.config.voltages_v):
                if cancelled:
                    break
                logging.info("Voltage step %d/%d: %.3f V", v_idx + 1, voltage_count, voltage_v)

                settle_per_point = self.config.uniform_board_mode and self.config.uniform_board_settle_per_point
                if self.config.uniform_board_mode and not settle_per_point:
                    self.program_voltage_for_scan(voltage_v)  # once for this whole voltage step

                for p_idx, point in enumerate(self.active_points):
                    if self.cancel_event is not None and self.cancel_event.is_set():
                        logging.info("Scan cancelled.")
                        cancelled = True
                        break

                    logging.info(
                        "Point %d/%d | %s row=%d col=%d | x=%.3f mm y=%.3f mm",
                        p_idx + 1, point_count, point.density, point.logical_row, point.logical_col,
                        point.stage_x_mm, point.stage_y_mm,
                    )

                    if settle_per_point:
                        self.program_voltage_for_scan(voltage_v)  # re-send + delay before this point
                    elif not self.config.uniform_board_mode:
                        self.program_voltage_for_point(point, voltage_v)

                    result = self.measure_point(point)
                    if sdata_summary is not None:
                        sdata_summary[p_idx, v_idx, :] = result.sdata

                    self.saver.save_point(
                        point=point, voltages_v=voltages_v, voltage_index=v_idx,
                        sdata=result.sdata,
                        frequencies_hz=frequencies_hz,
                        physical_xy=calibration.transform(point.stage_x_mm, point.stage_y_mm),
                    )

                    step += 1
                    if self.on_progress:
                        self.on_progress(ProgressEvent(
                            step=step, total_steps=total_steps, point=point,
                            voltage_index=v_idx, voltage_count=voltage_count, voltage_v=voltage_v,
                        ))

            if not cancelled:
                active_points = np.array(
                    [[p.logical_row, p.logical_col, p.stage_x_mm, p.stage_y_mm, p.y_loop, p.x_loop]
                     for p in self.active_points],
                    dtype=float,
                )
                self.saver.finalize_summary(voltages_v=voltages_v, active_points=active_points,
                                            frequencies_hz=frequencies_hz)
        finally:
            self.close()


    def _run_pattern_scan(self) -> None:
        try:
            self.connect()
            frequencies_hz = self._frequencies_hz()
            calibration = self._calibration()
            self.saver.save_metadata({
                "config": asdict(self.config),
                "scan_mode": "voltage_pattern",
                "density_mode": self.config.geometry.density_mode,
                "pattern": {"source_file": str(self.config.pattern_csv), "layout": self.pattern.layout,
                            "summary": self.pattern.describe(),
                            "copied_to": ["voltage_pattern_source.csv", "voltage_pattern_applied.csv"]},
                "frequencies_hz": {"start": float(frequencies_hz[0]), "stop": float(frequencies_hz[-1]),
                                   "points": int(len(frequencies_hz)), "spacing": "linear"},
                "stage_calibration": {"source_file": self.config.stage.calibration_file,
                                      "coefficients": calibration.to_dict()},
                "file_layout": "one <density>_R<row>_C<col>.npz per coordinate, one measurement each "
                               "(e = that element's pattern voltage); see data.py",
                "active_point_count": len(self.active_points),
                "active_points": [point.__dict__ for point in self.active_points],
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            self._run_pattern(frequencies_hz, calibration)
        finally:
            self.close()


if __name__ == "__main__":
    # First thing to edit: output path, voltage list, run name.
    config = RunConfig(
        voltages_v=(0.0, 0.5, 1.0, 1.5, 2.0),
        stage=StageConfig(port="COM10", calibration_file="stage_calibration.json"),  # run calibrate_stage.py once first
        pixels=PixelControllerConfig(port="COM7"),
        save=SaveConfig(
            output_dir=Path(r"C:\Users\labuser\Gen3WGSDualPatchGraphs\AutomationTesting"),
            run_name="2026-03-14_uniform_board_scan",
        ),
        uniform_board_mode=True,
        single_pixel_settle_s=0.2,
        dry_run=False,
    )
    AutomatedArrayScanner(config).run()
