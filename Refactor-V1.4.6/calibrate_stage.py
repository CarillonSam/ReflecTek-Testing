"""
Interactive stage calibration. Walks through the nudge-to-element protocol: set an
origin, then nudge onto a far-Y element and a far-X element, computing scale/skew
coefficients from how far off "ideal" those elements were.

The targets come from HexGridPlanner.calibration_targets(): the (0, 0) element, the
last element of the first L row (far X), and the first element of the last L row
(far Y). They're real elements taken from the generated grid, so each one is
something you can physically line up on, with the alternate-row offset and the
l_subgrid phase already included. Because those corners are generally not exactly on
the axes, the coefficients come from solving a full 2x2 linear system rather than
dividing by an axis span.

Saves the result to StageConfig.calibration_file, so run_scan.py picks it up
automatically on future runs via GrblXY.calibration — no need to recalibrate every time.

Run this directly: python calibrate_stage.py
Re-run it any time the stage, mount, or board is physically disturbed.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import simpledialog

import numpy as np

from config import ScanGeometryConfig, StageConfig
from stage import CalibrationTargets, GrblXY, HexGridPlanner, StageCalibration


def _adjust(
    stage: GrblXY, target_x_mm: float, target_y_mm: float, label: str, parent=None
) -> tuple[float, float]:
    """Moves to target, then lets the user nudge (x_err,y_err) until they accept (blank input)."""
    stage.goto_xy(target_x_mm, target_y_mm)
    stage.wait_until_reached(target_x_mm, target_y_mm)

    nudge_x = nudge_y = 0.0
    x_err = y_err = 1.0
    while abs(x_err) > 0.1 or abs(y_err) > 0.1:
        answer = simpledialog.askstring(
            "Stage calibration",
            f"Line up on {label}.\n\n"
            "Enter a nudge as 'x_err,y_err' (mm), or leave blank to accept this position:",
            parent=parent,
        )
        if not answer:
            break
        x_err, y_err = map(float, answer.split(","))
        nudge_x += x_err
        nudge_y += y_err
        stage.goto_xy(target_x_mm + nudge_x, target_y_mm + nudge_y)
        stage.wait_until_reached(target_x_mm + nudge_x, target_y_mm + nudge_y)

    pos = stage.get_pos()
    return pos.x_mm, pos.y_mm


def solve_calibration(
    ideal_x_corner: tuple[float, float],
    ideal_y_corner: tuple[float, float],
    actual_x_corner: tuple[float, float],
    actual_y_corner: tuple[float, float],
) -> StageCalibration:
    """
    Finds the linear map M with M @ ideal = actual for both corners (origin fixed at
    0, 0), and returns it as StageCalibration, where
        M = [[scale_x, skew_x],
             [skew_y,  scale_y]].
    With axis-aligned corners, (X, 0) and (0, Y), this reduces to the old
    scale_x = actual_x[0] / X, skew_x = actual_y[0] / Y, and so on.
    """
    ideal = np.array([ideal_x_corner, ideal_y_corner], dtype=float).T  # columns = ideal vectors
    actual = np.array([actual_x_corner, actual_y_corner], dtype=float).T
    if abs(np.linalg.det(ideal)) < 1e-9:
        raise ValueError("Calibration targets are collinear with the origin; can't solve for scale/skew.")
    m = actual @ np.linalg.inv(ideal)
    return StageCalibration(scale_x=m[0, 0], skew_x=m[0, 1], skew_y=m[1, 0], scale_y=m[1, 1])


def calibrate(stage: GrblXY, targets: CalibrationTargets, parent=None) -> StageCalibration:
    """
    targets: from HexGridPlanner(geometry).calibration_targets().
    parent: an existing Tk widget to host the dialogs under — pass this (e.g. the GUI's
    own root) when calling from within an already-running Tk app, so this doesn't create
    a second, conflicting Tk() root. Omit only when there's no Tk app running yet (the
    standalone __main__ block below), in which case a throwaway hidden root is created
    just so the dialogs have something to attach to.
    """
    ideal_x = (targets.x_corner.stage_x_mm, targets.x_corner.stage_y_mm)
    ideal_y = (targets.y_corner.stage_x_mm, targets.y_corner.stage_y_mm)

    owns_root = parent is None
    root = None
    if owns_root:
        root = tk.Tk()
        root.withdraw()
        parent = root
    try:
        stage.home_xy()
        stage.set_software_zero_here()
        _adjust(stage, 0.0, 0.0, f"the origin: {CalibrationTargets.describe(targets.origin)}", parent)
        stage.set_software_zero_here()  # re-zero on the user-nudged true origin

        actual_y = _adjust(stage, *ideal_y, f"the far-Y element: {CalibrationTargets.describe(targets.y_corner)}", parent)
        actual_x = _adjust(stage, *ideal_x, f"the far-X element: {CalibrationTargets.describe(targets.x_corner)}", parent)
    finally:
        if owns_root:
            root.destroy()

    return solve_calibration(ideal_x, ideal_y, actual_x, actual_y)


if __name__ == "__main__":
    # Edit to match your setup. spacing_mm is the FULL-grid (all three sub-grids
    # interleaved) nearest-neighbor spacing; each sub-grid is sqrt(3) times that.
    stage_config = StageConfig(port="COM10", calibration_file="stage_calibration.json")
    geometry = ScanGeometryConfig(rows=32, cols=32, spacing_mm=6.8)

    targets = HexGridPlanner(geometry).calibration_targets()
    stage = GrblXY(stage_config)
    try:
        calibration = calibrate(stage, targets)
        calibration.save(stage_config.calibration_file)
        print("Saved calibration to", stage_config.calibration_file, "->", calibration.to_dict())
    finally:
        stage.goto_xy(0.0, 0.0)
        stage.close()
