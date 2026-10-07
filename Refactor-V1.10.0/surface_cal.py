"""
Surface (warpage) calibration: the copper-plate method of nfp_calibrate.py, run from inside
the app. nfp_calibrate.py is used unchanged; this module only collects its inputs in the
layout it expects, calls its functions, and applies the result to element data.

Each calibration is saved as a new dated folder, surface_cal_<YYYY-MM-DD_HHMM>, in the same
folder as the stage calibration file (or in the Surface calibration folder, if one is set in
General settings). Nothing is overwritten, so earlier calibrations stay available; the Analysis
tab uses the newest by default. A calibration set folder holds:
    calibration_info.json      frequency grid, plate set, sites, when, stage calibration used
    sites.csv                  site,x_mm,y_mm  (physical coordinates, as in the element files)
    C/  TL/  TR/  BR/  BL/     one sub-folder per site with its plate files:
        Surface.npz            plate laid flat on the array surface   (depth 0 mm)
        N1mm.npz               plate raised on 1 mm spacers           (depth 1 mm)
        N2mm.npz               plate raised on 2 mm spacers           (depth 2 mm)
        N1_5mm.npz             centre only, optional: 1.5 mm accuracy-check plate. Not one of
                               nfp_calibrate's standards (0, 1, 2 mm), so it isn't used to solve
                               the calibration; it's corrected afterwards and compared with an
                               ideal plate, which tests the calibration independently.
    calibration_report.txt     nfp_calibrate's report, plus plain-language warnings
Each plate file holds sdata (one row per sweep; nfp_calibrate averages them) and
frequencies_hz, like the element files. "Depth" is how much closer to the probe the plate
sits than the local array surface, so the spacers' thickness is the depth.

Sites: the centre and the four corners of the array, each inset INSET_FRACTION of the way
in from the outermost elements so the plate sits fully on the board. The centre always
gets all three plates (it's the reference); the corners get all three or just the surface
plate, depending on the plate set chosen.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import nfp_calibrate as nfp
from config import ScanGeometryConfig
from stage import HexGridPlanner

INSET_FRACTION = 0.05
STANDARD_DEPTHS_MM = (0.0, 1.0, 2.0)
CHECK_DEPTH_MM = 1.5
PLATE_FILES = {0.0: "Surface.npz", 1.0: "N1mm.npz", 2.0: "N2mm.npz", CHECK_DEPTH_MM: "N1_5mm.npz"}
# Accuracy check limits: the corrected 1.5 mm plate vs an ideal one. For scale, at 19 GHz a
# 0.1 mm height error moves the phase by about 4.6 deg.
CHECK_PHASE_LIMIT_DEG = 3.0
CHECK_AMPLITUDE_LIMIT_DB = 0.5
SET_PREFIX = "surface_cal_"
PLATE_SWEEPS = 3  # sweeps per plate measurement, averaged by nfp_calibrate
PLATE_SETS = {
    "all_full": "Plates at 0, 1 and 2 mm at every site (most accurate)",
    "centre_full": "All three plates at the centre; surface plate only at the corners (faster)",
}
INFO_FILE = "calibration_info.json"


@dataclass(frozen=True)
class Site:
    name: str          # folder name, e.g. "TL"
    label: str         # for the operator, e.g. "top-left corner"
    ideal_x_mm: float  # planned stage coordinates (what the stage is told to go to)
    ideal_y_mm: float
    depths_mm: tuple[float, ...]


def site_layout(geometry: ScanGeometryConfig, plate_set: str, check: bool = False) -> list[Site]:
    """The 5 sites, in measuring order: centre first (the reference), then the corners
    going round the board. "Top"/"left" are as the board is drawn in the Scan and Analysis
    tabs (stage Y up, stage X to the right)."""
    pts = HexGridPlanner(geometry).all_points()  # both densities, so the whole board
    xs = np.array([p.stage_x_mm for p in pts])
    ys = np.array([p.stage_y_mm for p in pts])
    x_lo, x_hi = xs.min() + INSET_FRACTION * np.ptp(xs), xs.max() - INSET_FRACTION * np.ptp(xs)
    y_lo, y_hi = ys.min() + INSET_FRACTION * np.ptp(ys), ys.max() - INSET_FRACTION * np.ptp(ys)
    full = STANDARD_DEPTHS_MM
    corner = full if plate_set == "all_full" else (0.0,)
    r = lambda v: round(float(v), 3) + 0.0
    return [
        Site("C", "centre", r((xs.min() + xs.max()) / 2), r((ys.min() + ys.max()) / 2),
             full + ((CHECK_DEPTH_MM,) if check else ())),
        Site("TL", "top-left corner", r(x_lo), r(y_hi), corner),
        Site("TR", "top-right corner", r(x_hi), r(y_hi), corner),
        Site("BR", "bottom-right corner", r(x_hi), r(y_lo), corner),
        Site("BL", "bottom-left corner", r(x_lo), r(y_lo), corner),
    ]


def plate_instruction(depth_mm: float) -> str:
    if depth_mm == CHECK_DEPTH_MM:
        return (f"Accuracy check: raise the plate onto the {depth_mm:g} mm spacers, centred under the probe. "
                f"This plate isn't used to compute the calibration; it's used afterwards to test it.")
    if depth_mm == 0:
        return "Lay the copper plate flat directly on the array surface, centred under the probe."
    return (f"Raise the same plate onto the {depth_mm:g} mm spacers, still centred under the probe "
            f"(the plate should now sit {depth_mm:g} mm above the array surface).")


def expected_phase_step_deg(freq_hz: np.ndarray, depth_mm: float) -> float:
    """Phase change a plate raised depth_mm should show at the band centre, vs. the surface
    plate: the round trip shortens by 2 * depth, i.e. 2 k d. Approximate: the probe's own
    reflections distort it a little, so it's a sanity check, not a pass/fail limit."""
    f = float(freq_hz[len(freq_hz) // 2])
    return float(np.degrees(2 * nfp.wavenumber(f) * depth_mm * 1e-3))


def measured_phase_step_deg(surface: np.ndarray, raised: np.ndarray) -> float:
    """Phase of raised vs. surface plate at the band centre (sweeps averaged), wrapped to +-180."""
    i = surface.shape[-1] // 2
    a = np.mean(np.atleast_2d(surface)[:, i])
    b = np.mean(np.atleast_2d(raised)[:, i])
    return float(np.degrees(np.angle(b / a)))


# ============================================================================ where sets live


def sets_folder(stage_config) -> Path | None:
    """Where calibration sets are saved: the Surface calibration folder if one is set,
    otherwise the folder the stage calibration file is in. None if neither is set."""
    if stage_config.surface_cal_dir:
        return Path(stage_config.surface_cal_dir)
    if stage_config.calibration_file:
        return Path(stage_config.calibration_file).resolve().parent
    return None


def new_set_folder(base: str | Path, when: float | None = None) -> Path:
    """A new dated set folder name in base, e.g. surface_cal_2026-10-06_1342 (with _2, _3...
    added if one with that name already exists)."""
    stamp = time.strftime("%Y-%m-%d_%H%M", time.localtime(when))
    base = Path(base)
    path, n = base / f"{SET_PREFIX}{stamp}", 2
    while path.exists() or path.with_name(path.name + ".incomplete").exists():
        path, n = base / f"{SET_PREFIX}{stamp}_{n}", n + 1
    return path


def find_sets(base: str | Path | None) -> list[Path]:
    """Completed calibration sets in base, oldest first."""
    if base is None or not Path(base).is_dir():
        return []
    sets = [p for p in Path(base).glob(f"{SET_PREFIX}*") if p.is_dir() and is_calibration_set(p)]

    def order(p: Path):
        try:
            created = read_info(p).get("created", "")
        except (OSError, ValueError):
            created = ""
        stamp, _, suffix = p.name[len(SET_PREFIX):].partition("_")[2].partition("_")  # HHMM, then _2, _3...
        return (created, p.name[:len(SET_PREFIX) + 15], int(suffix) if suffix.isdigit() else 1)
    return sorted(sets, key=order)


def latest_set(base: str | Path | None) -> Path | None:
    sets = find_sets(base)
    return sets[-1] if sets else None


# ============================================================================ writing a set


class SetWriter:
    """Collects a calibration set into `<target>.incomplete`; finish() renames it to
    `<target>` (a new dated folder from new_set_folder()), and discard() deletes it, so a
    cancelled calibration leaves nothing behind and earlier sets are never touched."""

    def __init__(self, target: str | Path, freq_hz: np.ndarray, plate_set: str, sites: list[Site]) -> None:
        self.target = Path(target)
        self.work = self.target.with_name(self.target.name + ".incomplete")
        if self.work.exists():
            shutil.rmtree(self.work)
        self.work.mkdir(parents=True)
        self.freq_hz = np.asarray(freq_hz, dtype=float)
        self.plate_set = plate_set
        self.sites = sites
        self.physical: dict[str, tuple[float, float]] = {}

    def save_plate(self, site: Site, depth_mm: float, sweeps: np.ndarray, physical_xy: tuple[float, float]) -> Path:
        folder = self.work / site.name
        folder.mkdir(exist_ok=True)
        path = folder / PLATE_FILES[depth_mm]
        np.savez(path, sdata=np.atleast_2d(sweeps), frequencies_hz=self.freq_hz, depth_mm=depth_mm,
                 site=site.name, physical_x_mm=physical_xy[0], physical_y_mm=physical_xy[1],
                 measured_time=time.time())
        self.physical[site.name] = physical_xy
        return path

    def _write_index(self, extra: dict) -> None:
        with open(self.work / "sites.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["site", "x_mm", "y_mm"])
            for s in self.sites:
                if s.name in self.physical:
                    w.writerow([s.name, f"{self.physical[s.name][0]:.4f}", f"{self.physical[s.name][1]:.4f}"])
        info = {
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "frequency": {"start_hz": float(self.freq_hz[0]), "stop_hz": float(self.freq_hz[-1]),
                          "points": int(len(self.freq_hz))},
            "plate_set": self.plate_set,
            "sweeps_per_plate": PLATE_SWEEPS,
            "check_plate_mm": CHECK_DEPTH_MM if any(CHECK_DEPTH_MM in s.depths_mm for s in self.sites) else None,
            "sites": [{"name": s.name, "label": s.label, "ideal_x_mm": s.ideal_x_mm, "ideal_y_mm": s.ideal_y_mm,
                       "physical_x_mm": self.physical.get(s.name, (None, None))[0],
                       "physical_y_mm": self.physical.get(s.name, (None, None))[1],
                       "depths_mm": list(s.depths_mm)} for s in self.sites],
            **extra,
        }
        with open(self.work / INFO_FILE, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)

    def solve(self, extra_info: dict) -> "Solution":
        """Writes sites.csv and the info file, then solves the set (still in the work folder)."""
        self._write_index(extra_info)
        return solve_set(self.work)

    def finish(self, solution: "Solution") -> Path:
        with open(self.work / "calibration_report.txt", "w", encoding="utf-8") as f:
            f.write(solution.full_text())
        self.work.rename(self.target)
        return self.target

    def discard(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)


# ============================================================================ solving


@dataclass
class Solution:
    cal: object             # nfp_calibrate.Calibration
    report: str             # nfp_calibrate's own printed report
    warnings: list[str]     # plain-language problems for the operator
    summary: list[str]      # plain-language results

    def full_text(self) -> str:
        parts = ["Summary", *[f"  {s}" for s in self.summary]]
        if self.warnings:
            parts += ["", "Warnings", *[f"  {w}" for w in self.warnings]]
        parts += ["", "nfp_calibrate report", self.report]
        return "\n".join(parts)


def _nfp_args(cal_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(standards=list(STANDARD_DEPTHS_MM), sign=1, ref_site=None, surface="auto",
                           cal_dir=str(cal_dir))


def read_info(folder: str | Path) -> dict:
    path = Path(folder) / INFO_FILE
    if not path.is_file():
        raise ValueError(f"{folder} isn't a surface calibration set (no {INFO_FILE}).")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def frequency_grid(info: dict) -> np.ndarray:
    fr = info["frequency"]
    return np.linspace(fr["start_hz"], fr["stop_hz"], fr["points"])


def solve_set(folder: str | Path) -> Solution:
    """Runs nfp_calibrate's calibration on a set folder. Raises ValueError (with the
    script's own message) if it can't be solved."""
    folder = Path(folder)
    info = read_info(folder)
    freq = frequency_grid(info)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            sites = nfp.load_sites(str(folder), {}, [], "sdata", len(freq))
            cal = nfp.build_calibration(sites, freq, _nfp_args(folder))
            nfp.report_calibration(cal, 1)
    except SystemExit as e:  # the script reports fatal problems with sys.exit(message)
        raise ValueError(f"The surface calibration couldn't be solved: {e}") from None
    return Solution(cal, out.getvalue(), *_interpret(cal, info))


def _interpret(cal, info: dict) -> tuple[list[str], list[str]]:
    """Plain-language warnings and summary from a solved calibration."""
    warnings, summary = [], []
    labels = {s["name"]: s["label"] for s in info.get("sites", [])}
    t = cal.terms
    e11 = float(np.nanmedian(np.abs(t.e11)))
    if e11 > 1:
        warnings.append("The probe's internal reflection came out larger than 1, which isn't physical. Usually the "
                        "plates were placed at the wrong heights (check the 1 mm and 2 mm spacers) or swapped.")
    for name, fit in cal.fits.items():
        where = labels.get(name, name)
        if not np.isfinite(fit.gap_mm):
            warnings.append(f"{where}: no usable plate measurement, so it was left out of the warp map.")
        elif fit.fit_rms > 0.05:
            warnings.append(f"{where}: the plates there don't agree well with the others ({100 * fit.fit_rms:.0f}% "
                            f"misfit). Check the plate was flat and centred, then measure that site again.")
        if fit.full and fit.consistency is not None and max(fit.consistency) > 0.2:
            warnings.append(f"{where}: its own error terms differ from the pooled ones by "
                            f"{100 * max(fit.consistency):.0f}%; the probe or cabling may have moved between sites.")
    order = [s["name"] for s in info.get("sites", [])]
    summary.append("Array surface height at each site, relative to the centre (positive = closer to the probe):")
    for name, fit in sorted(cal.fits.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99):
        if np.isfinite(fit.gap_mm):
            summary.append(f"    {labels.get(name, name)}: {fit.gap_mm:+.3f} mm")
    if cal.multi_site:
        s = cal.surface
        summary.append(f"Warp map: '{s.model}' surface fitted through the {len(s.sites_xy)} sites.")
    for line, warning in _accuracy_check(cal, labels):
        summary.append(line)
        if warning:
            warnings.append(warning)
    cond = cal.fits[cal.ref].cond
    if np.nanmax(cond) > 1e4:
        warnings.append("The plate measurements barely differ from each other at some frequencies, so the "
                        "solution there is unreliable. Check the spacers really are 1 mm and 2 mm.")
    return warnings, summary


def _accuracy_check(cal, labels: dict) -> list[tuple[str, str | None]]:
    """Validation plates (the optional 1.5 mm check): corrected with the calibration and
    compared with an ideal plate at that height, exactly as nfp_calibrate's report_validation."""
    out = []
    for name, fit in cal.fits.items():
        rot = nfp.gap_rotation(cal.freq, fit.gap_mm) if np.isfinite(fit.gap_mm) else 1
        terms = cal.terms.rotated(rot)
        for depth, _file, s11 in fit.validation:
            ratio = terms.corrected(s11) / nfp.plate_gamma(cal.freq, depth, 1)
            phase = np.degrees(np.angle(ratio))
            amp = 20 * np.log10(np.abs(ratio))
            med, lo, hi = np.nanmedian(phase), np.nanmin(phase), np.nanmax(phase)
            rms = float(np.sqrt(np.nanmean(amp ** 2)))
            where = labels.get(name, name)
            line = (f"Accuracy check ({depth:g} mm plate at the {where}): phase error {med:+.1f} deg "
                    f"(from {lo:+.1f} to {hi:+.1f} across the band), amplitude error {rms:.2f} dB rms.")
            warning = None
            if abs(med) > CHECK_PHASE_LIMIT_DEG or rms > CHECK_AMPLITUDE_LIMIT_DB:
                warning = (f"The {depth:g} mm accuracy check is outside the limits ({CHECK_PHASE_LIMIT_DEG:g} deg, "
                           f"{CHECK_AMPLITUDE_LIMIT_DB:g} dB). Check that the right spacers were used for every "
                           f"plate at the {where} (about 4.6 deg per 0.1 mm at 19 GHz), then calibrate again.")
            else:
                line += " Within limits."
            out.append((line, warning))
    return out


def load_solution(folder: str | Path) -> tuple[Solution, np.ndarray]:
    """A saved set, solved again (solving takes a second or two), and its frequency grid."""
    sol = solve_set(folder)
    return sol, sol.cal.freq


# ============================================================================ applying


def correct(cal, sdata: np.ndarray, physical_xy: tuple[float, float] | None) -> tuple[np.ndarray, float]:
    """An element's corrected reflection gamma (same shape as sdata) and the warp gap used,
    exactly as nfp_calibrate.process_element computes it (no attenuator pad, full band)."""
    gap = 0.0
    if cal.multi_site:
        if physical_xy is None:
            raise ValueError("this element has no position, so the warp at it is unknown")
        gap = cal.surface.gap_at(*physical_xy)
    terms = cal.terms.rotated(nfp.gap_rotation(cal.freq, gap))
    return terms.corrected(np.asarray(sdata)), gap


def is_calibration_set(folder: str | Path) -> bool:
    return (Path(folder) / INFO_FILE).is_file()
