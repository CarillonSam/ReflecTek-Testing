"""
Element-to-element phase-vs-voltage analysis (adapted from ElementToElementPTV.py).

No GUI code here: analysis_tab.py draws the plots, this module loads a dataset folder,
time-gates each element's S11, and computes the curves and heatmap values.

Reads the files this app writes (one <density>_R<row>_C<col>.npz per coordinate, with
complex `sdata`, `e`/`measured` voltage slots and `frequencies_hz`), and also the older
legacy files (separate amplitude/phase arrays, C<col>R<row> file names, no frequency
axis), using the same key-name fallbacks as the original script.

Changes from the original script:
  - Works from the complex sdata directly (no magnitude/phase round trip).
  - Uses each file's own frequency axis; the fixed 16-24 GHz axis is only a fallback for
    legacy files that don't record one.
  - Elements are keyed by (density, row, col): L and H row numbers overlap.
  - Only measured voltage slots are used, so a cancelled or partial scan still loads.
  - Reference frequencies default per band (the original list's "19.75 GHz" and
    "20.0 GHz" entries both pointed at 19.5 GHz; those copy-paste slips are fixed).
  - Gating keeps full precision (the original rounded magnitude/phase mid-calculation).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
from dataclasses import dataclass, field

from pathlib import Path

import numpy as np
import scipy.fft as sfft
from scipy.signal.windows import tukey

# Legacy key names, checked in order (same lists as the original script).
AMPLITUDE_KEYS = ["amplitudes", "amp", "mag"]
PHASE_KEYS = ["phases_deg", "phases", "phase_deg"]
VOLTAGE_ARRAY_KEYS = ["e", "voltages"]

HEATMAP_METRICS = {
    "Phase range across voltage (deg)": "phase_range",
    "Phase at last voltage (deg)": "phase_last",
    "Magnitude range across voltage (dB)": "mag_range",
}
# Voltage pattern runs (one measurement per element, no voltage axis): time-gated, but with
# no reference subtracted.
RAW_HEATMAP_METRICS = {
    "Phase, no reference (deg)": "raw_phase",
    "Gated magnitude (dB)": "raw_mag",
    "Applied voltage (V)": "applied_v",
}
# Offered in addition when a surface calibration is applied: the warp map it fitted.
CAL_HEATMAP_METRICS = {"Surface gap (mm)": "surface_gap"}
CORRECTIONS = {"Time-domain gate": "gate", "Surface calibration": "surface_cal"}
PHASE_REFERENCES = {
    "Linear trend": "linear_trend",
    "First voltage": "first_voltage",
}


@dataclass
class AnalysisSettings:
    gate_start_samples: int = 15   # samples before the time-domain peak to keep
    gate_stop_samples: int = 20    # samples after the peak to keep
    gate_alpha: float = 0.5        # Tukey window shape
    phase_reference: str = "linear_trend"  # or "first_voltage"
    ref_freqs_ghz: list[float] = field(default_factory=list)  # empty = band default
    # How each element's response is cleaned up before analysis:
    #   "gate":        time-domain gate (the settings above), as the original script
    #   "surface_cal": use `gamma` from the copper-plate surface calibration's output folder
    #                  (cal_folder; one <element>_cal.npz per element), with no gating
    correction: str = "gate"
    cal_folder: str = ""
    # Legacy files only (no recorded frequency axis or voltages):
    legacy_freq_start_ghz: float = 16.0
    legacy_freq_stop_ghz: float = 24.0
    legacy_v_min: float = 0.0
    legacy_v_max: float = 10.0


def default_ref_freqs(freqs_ghz: np.ndarray) -> list[float]:
    """Nine reference frequencies, 0.25 GHz apart, for whichever band the sweep covers:
    18-20 GHz for low band (the original script's list), 27-29 GHz for high band,
    otherwise nine points across the middle half of the sweep."""
    lo, hi = float(freqs_ghz[0]), float(freqs_ghz[-1])
    for start in (18.0, 27.0):
        if lo <= start and start + 2.0 <= hi:
            return [round(start + 0.25 * i, 3) for i in range(9)]
    span = hi - lo
    return [round(v, 3) for v in np.linspace(lo + span / 4, hi - span / 4, 9)]


# ============================================================================ loading


@dataclass
class Element:
    density: str
    row: int
    col: int
    voltages: np.ndarray          # (n_measured,)
    sdata: np.ndarray             # (n_measured, n_freq) complex
    freqs_ghz: np.ndarray         # (n_freq,)
    position_mm: tuple[float, float] | None
    file_name: str
    scan_type: str = "sweep"   # "pattern" for voltage pattern runs
    correction: str = "gate"   # "surface_cal" if sdata holds calibrated gamma
    gap_mm: float | None = None  # surface calibration's gap at this element (warp), if any
    physical_mm: tuple[float, float] | None = None  # calibrated position (what the warp map uses)

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.density, self.row, self.col)


@dataclass
class Dataset:
    folder: str
    elements: dict[tuple[str, int, int], Element]
    skipped: list[str]
    scan_type: str = "sweep"   # "pattern" if this folder is a voltage pattern run
    correction: str = "gate"   # "surface_cal" once apply_surface_calibration() has been applied
    notes: list[str] = field(default_factory=list)

    @property
    def densities(self) -> list[str]:
        return sorted({k[0] for k in self.elements})

    def describe(self) -> str:
        if not self.elements:
            return "No usable element files found."
        counts = ", ".join(f"{sum(k[0] == d for k in self.elements)} {d}" for d in self.densities)
        any_el = next(iter(self.elements.values()))
        f = any_el.freqs_ghz
        nv = sorted({len(e.voltages) for e in self.elements.values()})
        nv_text = f"{nv[0]}" if len(nv) == 1 else f"{nv[0]}-{nv[-1]}"
        text = f"{counts} elements, {nv_text} voltages, {f[0]:g}-{f[-1]:g} GHz ({len(f)} points)"
        if self.scan_type == "pattern":
            text = f"Voltage pattern run: {counts} elements, one measurement each, {f[0]:g}-{f[-1]:g} GHz ({len(f)} points)"
        if self.skipped:
            text += f"; skipped {len(self.skipped)} file(s)"
        for note in self.notes:
            text += f"; {note}"
        return text


def _find_key(available, candidates):
    return next((name for name in candidates if name in available), None)


def _parse_name(file_name: str):
    """(density, row, col) from 'L_R005_C012.npz' (this app), or (None, row, col) from
    legacy names like '...C2R0.npz' / '...C15_R56_.npz'."""
    m = re.search(r"([LH])_R(\d+)_C(\d+)", file_name)
    if m:
        return m.group(1), int(m.group(2)), int(m.group(3))
    m = re.search(r"C(\d+)_?R(\d+)", file_name)
    if m:
        return None, int(m.group(2)), int(m.group(1))
    return None, None, None


def load_element(path: str, settings: AnalysisSettings, data_key: str = "sdata") -> Element:
    """Loads one file. Raises ValueError with a reason if it isn't usable. data_key="gamma"
    reads a surface-calibration output file's corrected reflection instead of sdata."""
    name = os.path.basename(path)
    with np.load(path) as z:
        keys = z.files
        if data_key == "gamma" and "gamma" not in keys:
            raise ValueError("no 'gamma' array (not a surface-calibration output file)")
        if data_key in keys:
            sdata = np.atleast_2d(z[data_key])
            if "measured" in keys and z["measured"].shape == (sdata.shape[0],):
                measured = z["measured"].astype(bool)
            elif data_key == "gamma":
                measured = ~np.isnan(sdata).all(axis=1)  # gamma is NaN outside the --band, so test whole rows
            else:
                measured = ~np.isnan(sdata).any(axis=1)
            if "e" in keys and z["e"].shape == (sdata.shape[0],):
                voltages = z["e"]
            elif "voltages_v" in keys and z["voltages_v"].shape == (sdata.shape[0],):
                voltages = z["voltages_v"]
            else:
                voltages = np.linspace(settings.legacy_v_min, settings.legacy_v_max, sdata.shape[0])
            sdata, voltages = sdata[measured], np.asarray(voltages, dtype=float)[measured]
        else:
            amp_key, phase_key = _find_key(keys, AMPLITUDE_KEYS), _find_key(keys, PHASE_KEYS)
            if amp_key is None or phase_key is None:
                raise ValueError(f"no sdata or amplitude/phase arrays (has {list(keys)})")
            amp, phase = np.atleast_2d(z[amp_key]), np.atleast_2d(z[phase_key])
            sdata = amp * np.exp(1j * np.deg2rad(phase))
            volt_key = _find_key(keys, VOLTAGE_ARRAY_KEYS)
            if volt_key is not None and z[volt_key].shape == (sdata.shape[0],):
                voltages = np.asarray(z[volt_key], dtype=float)
            else:
                voltages = np.linspace(settings.legacy_v_min, settings.legacy_v_max, sdata.shape[0])
            keep = ~np.isnan(voltages) & ~np.isnan(sdata).any(axis=1)
            sdata, voltages = sdata[keep], voltages[keep]

        if len(voltages) == 0:
            raise ValueError("no measured voltages yet")
        if "frequencies_hz" in keys and len(z["frequencies_hz"]) == sdata.shape[1]:
            freqs_ghz = np.asarray(z["frequencies_hz"], dtype=float) / 1e9
        else:
            freqs_ghz = np.linspace(settings.legacy_freq_start_ghz, settings.legacy_freq_stop_ghz, sdata.shape[1])
        if data_key == "gamma":
            # The calibration sets gamma to NaN outside its --band: keep only frequencies
            # that are finite for every measured voltage.
            finite = np.isfinite(sdata).all(axis=0)
            if not finite.any():
                raise ValueError("gamma is NaN at every frequency")
            sdata, freqs_ghz = sdata[:, finite], freqs_ghz[finite]
        gap_mm = float(np.ravel(z["gap_mm"])[0]) if "gap_mm" in keys else None
        physical = None
        for kx, ky in (("physical_x_mm", "physical_y_mm"), ("stage_x_mm", "stage_y_mm")):  # as nfp_calibrate
            if kx in keys and ky in keys:
                physical = (float(np.ravel(z[kx])[0]), float(np.ravel(z[ky])[0]))
                break

        name_density, name_row, name_col = _parse_name(name)
        density = str(z["density"]) if "density" in keys else (name_density or "L")
        row = int(z["logical_row"]) if "logical_row" in keys else name_row
        col = int(z["logical_col"]) if "logical_col" in keys else name_col
        if row is None or col is None:
            raise ValueError("no row/column in the file or its name")
        position = None
        if "stage_x_mm" in keys and "stage_y_mm" in keys:
            position = (float(z["stage_x_mm"]), float(z["stage_y_mm"]))
        scan_type = str(z["scan_type"]) if "scan_type" in keys else "sweep"

    return Element(density, row, col, voltages, sdata, freqs_ghz, position, name, scan_type,
                   correction="surface_cal" if data_key == "gamma" else "gate", gap_mm=gap_mm,
                   physical_mm=physical)


def load_dataset(folder: str, settings: AnalysisSettings) -> Dataset:
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Folder not found: {folder}")
    elements, skipped = {}, []
    for path in sorted(glob.glob(os.path.join(folder, "*.npz"))):
        if os.path.basename(path).startswith("summary_"):
            continue  # the optional whole-run summary isn't a per-element file
        try:
            el = load_element(path, settings)
        except Exception as e:
            skipped.append(f"{os.path.basename(path)}: {e}")
            continue
        elements[el.key] = el
    scan_type = "sweep"
    meta_path = os.path.join(folder, "metadata.json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                if json.load(f).get("scan_mode") == "voltage_pattern":
                    scan_type = "pattern"
        except (OSError, ValueError):
            pass
    if any(el.scan_type == "pattern" for el in elements.values()):
        scan_type = "pattern"
    return Dataset(folder, elements, skipped, scan_type)


def apply_surface_calibration(ds: Dataset, cal_folder: str, settings: AnalysisSettings, progress=None) -> Dataset:
    """
    Matches each element of `ds` to its file in the surface calibration's output folder
    (<element>_cal.npz, written by the copper-plate calibration script) and returns a new
    Dataset whose elements hold that file's corrected `gamma` instead of raw sdata.

    A calibration file must come from the same run as the element: same voltages, and its
    frequencies must be the dataset's own frequency points (it may cover only part of the
    band if the calibration was run with --band). Elements without a matching file are
    left out and counted in the dataset's notes.
    """
    if not os.path.isdir(cal_folder):
        raise FileNotFoundError(f"Calibration folder not found: {cal_folder}")
    import surface_cal
    if surface_cal.is_calibration_set(cal_folder):
        return _apply_calibration_set(ds, cal_folder)
    paths = sorted(p for p in glob.glob(os.path.join(cal_folder, "*_cal.npz")))
    if not paths:
        raise ValueError(f"No <element>_cal.npz files in {cal_folder}. Choose the surface "
                         f"calibration's output folder (its --out-dir).")
    elements, mismatched, unreadable, extra = {}, [], [], 0
    for i, path in enumerate(paths, start=1):
        try:
            cal = load_element(path, settings, data_key="gamma")
        except Exception as e:
            unreadable.append(f"{os.path.basename(path)}: {e}")
            continue
        raw = ds.elements.get(cal.key)
        if raw is None:
            extra += 1
            continue
        same_v = len(cal.voltages) == len(raw.voltages) and np.allclose(cal.voltages, raw.voltages)
        idx = np.searchsorted(raw.freqs_ghz, cal.freqs_ghz)
        same_f = (idx.max(initial=0) < len(raw.freqs_ghz)
                  and np.allclose(raw.freqs_ghz[np.minimum(idx, len(raw.freqs_ghz) - 1)], cal.freqs_ghz, atol=1e-9))
        if not (same_v and same_f):
            mismatched.append(cal.file_name)
            continue
        cal.position_mm = raw.position_mm  # keep the run's own coordinates for plotting
        cal.scan_type = raw.scan_type
        elements[cal.key] = cal
        if progress and i % 50 == 0:
            progress(i, len(paths))
    if not elements:
        reasons = []
        if mismatched:
            reasons.append(f"{len(mismatched)} file(s) have different voltages or frequencies (e.g. {mismatched[0]})")
        if extra:
            reasons.append(f"{extra} file(s) are for elements not in this dataset")
        if unreadable:
            reasons.append(f"{len(unreadable)} file(s) couldn't be read (e.g. {unreadable[0]})")
        raise ValueError(f"None of the calibration files in {cal_folder} match this dataset: " + "; ".join(reasons))

    gaps = [el.gap_mm for el in elements.values() if el.gap_mm is not None]
    note = f"surface-calibrated: {len(elements)} of {len(ds.elements)} elements"
    if gaps:
        note += f", gap {min(gaps):+.3f} to {max(gaps):+.3f} mm"
    notes = [note]
    missing = len(ds.elements) - len(elements) - len(mismatched)
    if missing > 0:
        notes.append(f"{missing} element(s) have no calibration file (left out)")
    if mismatched:
        notes.append(f"{len(mismatched)} calibration file(s) don't match this run (left out)")
    for line in unreadable:
        logging.warning("Surface calibration: skipped %s", line)
    return Dataset(ds.folder, elements, ds.skipped, ds.scan_type, correction="surface_cal", notes=notes)


def _apply_calibration_set(ds: Dataset, set_folder: str) -> Dataset:
    """A calibration set from the app's calibration walkthrough: solve it (nfp_calibrate)
    and correct every element in memory, exactly as nfp_calibrate would write it."""
    import surface_cal
    from dataclasses import replace as dc_replace
    solution = surface_cal.solve_set(set_folder)
    freq_ghz = solution.cal.freq / 1e9
    elements, unplaced = {}, 0
    for key, el in ds.elements.items():
        if len(el.freqs_ghz) != len(freq_ghz) or not np.allclose(el.freqs_ghz, freq_ghz, rtol=0, atol=1e-9):
            raise ValueError(
                f"This calibration set was measured over {freq_ghz[0]:g}-{freq_ghz[-1]:g} GHz with {len(freq_ghz)} "
                f"points, but this run is {el.freqs_ghz[0]:g}-{el.freqs_ghz[-1]:g} GHz with {len(el.freqs_ghz)} points. "
                f"Calibrate with the same VNA sweep as the scan."
            )
        try:
            gamma, gap = surface_cal.correct(solution.cal, el.sdata, el.physical_mm)
        except ValueError:
            unplaced += 1
            continue
        elements[key] = dc_replace(el, sdata=gamma, correction="surface_cal", gap_mm=gap)
    if not elements:
        raise ValueError("None of this run's elements have a position, so the warp map can't be applied.")
    gaps = [el.gap_mm for el in elements.values()]
    info = surface_cal.read_info(set_folder)
    notes = [f"surface-calibrated with {Path(set_folder).name} ({info.get('created', '?')}): {len(elements)} of "
             f"{len(ds.elements)} elements, gap {min(gaps):+.3f} to {max(gaps):+.3f} mm"]
    if unplaced:
        notes.append(f"{unplaced} element(s) have no position (left out)")
    if solution.warnings:
        notes.append(f"{len(solution.warnings)} calibration warning(s), see calibration_report.txt")
    return Dataset(ds.folder, elements, ds.skipped, ds.scan_type, correction="surface_cal", notes=notes)


# ============================================================================ processing


_TUKEY_CACHE: dict[tuple[int, float], np.ndarray] = {}


def _tukey(n: int, alpha: float) -> np.ndarray:
    key = (n, alpha)
    if key not in _TUKEY_CACHE:
        _TUKEY_CACHE[key] = tukey(n, alpha=alpha)
    return _TUKEY_CACHE[key]


def _gate(sdata: np.ndarray, start: int, stop: int, alpha: float) -> np.ndarray:
    """Time-gates each row around its own time-domain peak (as the original script),
    all rows in one FFT. Returns the gated complex response, same shape."""
    td = sfft.ifft(sdata, axis=1)
    peaks = np.argmax(np.abs(td), axis=1)
    windows = np.zeros(td.shape)
    n = td.shape[1]
    for i, peak in enumerate(peaks):
        lo, hi = max(int(peak) - start, 0), min(int(peak) + stop, n)
        windows[i, lo:hi] = _tukey(hi - lo, alpha)
    return sfft.fft(windows * td, axis=1)


@dataclass
class Processed:
    voltages: np.ndarray       # (n_v,)
    freqs_ghz: np.ndarray      # (n_f,)
    gated_mag_db: np.ndarray   # (n_v, n_f)
    phase_rel: np.ndarray      # (n_v, n_f), unwrapped, relative to the chosen reference
    ref_freqs_ghz: list[float]
    ref_indices: list[int]     # nearest frequency index for each reference frequency
    raw: bool = False          # True for pattern runs: one measurement, no reference subtracted
    correction: str = "gate"   # how the response was cleaned up: "gate" or "surface_cal"
    gap_mm: float | None = None  # surface calibration's gap (warp) at this element

    def phase_at(self, idx: int) -> np.ndarray:
        return self.phase_rel[:, idx]

    def mag_db_at(self, idx: int) -> np.ndarray:
        return self.gated_mag_db[:, idx]


def _cleaned(el: Element, settings: AnalysisSettings) -> np.ndarray:
    """The element's response after correction: calibrated gamma as-is, otherwise time-gated."""
    if el.correction == "surface_cal":
        return el.sdata
    return _gate(el.sdata, settings.gate_start_samples, settings.gate_stop_samples, settings.gate_alpha)


def process_element(el: Element, settings: AnalysisSettings) -> Processed:
    gated = _cleaned(el, settings)
    with np.errstate(divide="ignore"):
        mag_db = 20 * np.log10(np.abs(gated))
    # Unwrap along frequency, then along voltage (same two passes as the original).
    phase = np.rad2deg(np.unwrap(np.angle(gated), axis=1))
    if phase.shape[0] > 1:
        phase = np.rad2deg(np.unwrap(np.deg2rad(phase), axis=0))
    if settings.phase_reference == "linear_trend":
        slope, intercept = np.polyfit(el.freqs_ghz, phase.mean(axis=0), deg=1)
        phase_rel = phase - (slope * el.freqs_ghz + intercept)[np.newaxis, :]
    else:
        phase_rel = phase - phase[0:1, :]
    refs = settings.ref_freqs_ghz or default_ref_freqs(el.freqs_ghz)
    idx = [int(np.argmin(np.abs(el.freqs_ghz - f))) for f in refs]
    return Processed(el.voltages, el.freqs_ghz, mag_db, phase_rel, list(refs), idx,
                     correction=el.correction, gap_mm=el.gap_mm)


def process_raw(el: Element, settings: AnalysisSettings) -> Processed:
    """Pattern runs: time-gated exactly as the sweep analysis (same gate settings), but with
    no reference subtracted. Phase is the gated phase in degrees, wrapped to -180..180."""
    gated = _cleaned(el, settings)
    with np.errstate(divide="ignore"):
        mag_db = 20 * np.log10(np.abs(gated))
    phase = np.degrees(np.angle(gated))
    refs = settings.ref_freqs_ghz or default_ref_freqs(el.freqs_ghz)
    idx = [int(np.argmin(np.abs(el.freqs_ghz - f))) for f in refs]
    return Processed(el.voltages, el.freqs_ghz, mag_db, phase, list(refs), idx, raw=True,
                     correction=el.correction, gap_mm=el.gap_mm)


def heatmap_value(p: Processed, metric: str, ref_freq_ghz: float) -> float:
    """One number per element for the heatmap, at the frequency nearest ref_freq_ghz."""
    idx = int(np.argmin(np.abs(p.freqs_ghz - ref_freq_ghz)))
    phase, mag = p.phase_at(idx), p.mag_db_at(idx)
    if metric == "surface_gap":
        return float(p.gap_mm) if p.gap_mm is not None else float("nan")
    if metric == "raw_phase":
        return float(phase[0])
    if metric == "raw_mag":
        return float(mag[0])
    if metric == "applied_v":
        return float(p.voltages[0])
    if metric == "phase_last":
        return float(phase[-1])
    if len(phase) < 2:
        return float("nan")  # a range needs at least two measured voltages
    if metric == "mag_range":
        return float(np.nanmax(mag) - np.nanmin(mag))
    return float(np.nanmax(phase) - np.nanmin(phase))


def process_dataset(ds: Dataset, settings: AnalysisSettings, progress=None) -> dict:
    """Processes every element; returns {key: Processed}. progress(done, total) is called
    as it goes, if given."""
    out, total = {}, len(ds.elements)
    process = process_raw if ds.scan_type == "pattern" else process_element
    for i, (key, el) in enumerate(ds.elements.items(), start=1):
        out[key] = process(el, settings)
        if progress and (i % 25 == 0 or i == total):
            progress(i, total)
    return out
