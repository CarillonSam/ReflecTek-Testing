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
from dataclasses import replace as dc_replace

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
    "Raw phase at voltage (deg)": "phase_at_v",   # pick the voltage in the Analysis tab
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
# Pattern runs with a reference run subtracted: replaces "Phase, no reference".
REF_HEATMAP_METRICS = {"Phase minus reference (deg)": "ref_phase"}
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
    # Voltage pattern runs only: subtract each element's phase in another pattern run (the
    # reference folder, processed with these same settings) from its phase in this one.
    use_reference: bool = False
    reference_folder: str = ""
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
    pattern_csv_v: float | None = None  # pattern runs: what the CSV asked for (voltages = what was applied)

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
    profile_v: np.ndarray | None = None   # transient runs: the voltage profile, in order

    @property
    def densities(self) -> list[str]:
        return sorted({k[0] for k in self.elements})

    def describe(self) -> str:
        if not self.elements:
            return "No usable element files found."
        if self.scan_type == "transient":
            return _describe_transient(self)
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
        pattern_csv_v = float(z["pattern_csv_v"]) if "pattern_csv_v" in keys else None

    return Element(density, row, col, voltages, sdata, freqs_ghz, position, name, scan_type,
                   correction="surface_cal" if data_key == "gamma" else "gate", gap_mm=gap_mm,
                   physical_mm=physical, pattern_csv_v=pattern_csv_v)


def load_dataset(folder: str, settings: AnalysisSettings, progress=None) -> Dataset:
    """progress(done, total) is called as files load, if given (transient runs only: their
    files are large, so loading is most of the work)."""
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Folder not found: {folder}")
    if _is_transient_folder(folder):
        return load_transient_dataset(folder, progress)
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
    # Pattern runs with a reference run: "subtracted" (phase_rel is this run minus the
    # reference), or "missing" (the reference run has no such element: phase_rel is NaN).
    ref_status: str | None = None
    # Sweeps: the corrected response's phase with no reference subtracted, wrapped to
    # -180..180 (same as a pattern run's "Phase, no reference"). (n_v, n_f)
    raw_phase: np.ndarray | None = None

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
                     correction=el.correction, gap_mm=el.gap_mm, raw_phase=np.degrees(np.angle(gated)))


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


def subtract_reference_phase(ds: Dataset, processed: dict, ref_ds: Dataset,
                             ref_processed: dict) -> tuple[Dataset, dict]:
    """
    Voltage pattern runs: each element's phase minus the same element's phase in a reference
    pattern run (matched by density, row and column), wrapped to -180..180. That is exactly the
    phase of S / S_ref, so there are no wrap-around jumps. Both runs must be processed with the
    same settings and measured at the same frequencies. Magnitude is left as it is.

    Elements the reference run doesn't have keep their place with NaN phase (grey on the
    heatmap). Returns the dataset (with a note added) and the new {key: Processed}.
    """
    if ref_ds.scan_type != "pattern":
        raise ValueError(f"The reference folder isn't a voltage pattern run: {ref_ds.folder}")
    if not ref_processed:
        raise ValueError(f"No usable element files in the reference folder: {ref_ds.folder}")
    out, missing = {}, 0
    for key, p in processed.items():
        r = ref_processed.get(key)
        if r is None:
            out[key] = dc_replace(p, phase_rel=np.full_like(p.phase_rel, np.nan), ref_status="missing")
            missing += 1
            continue
        if len(r.freqs_ghz) != len(p.freqs_ghz) or not np.allclose(r.freqs_ghz, p.freqs_ghz, rtol=0, atol=1e-9):
            raise ValueError(
                f"The reference run's frequencies don't match this run's at {key[0]} row {key[1]}, column "
                f"{key[2]}: {r.freqs_ghz[0]:g}-{r.freqs_ghz[-1]:g} GHz ({len(r.freqs_ghz)} points) vs "
                f"{p.freqs_ghz[0]:g}-{p.freqs_ghz[-1]:g} GHz ({len(p.freqs_ghz)} points). "
                f"Use a reference measured with the same VNA sweep.")
        diff = np.mod(p.phase_rel - r.phase_rel[:1] + 180.0, 360.0) - 180.0
        out[key] = dc_replace(p, phase_rel=diff, ref_status="subtracted")
    note = f"phase minus reference run {Path(ref_ds.folder).name}: {len(out) - missing} of {len(out)} elements"
    if missing:
        note += f" ({missing} not in the reference, shown grey)"
    return dc_replace(ds, notes=[*ds.notes, note]), out


def rotated_voltages(ds: Dataset) -> dict[tuple[str, int, int], float]:
    """
    For a voltage pattern run: the voltage that reached each element's position if the board's
    element numbering is rotated 180 deg relative to the stage. The machine origin is set for
    the tooling, independently of where pinout element E1_1 is, so the software can't know the
    orientation; this is the user's "Voltage map rotated 180 deg" option. For each element at
    stage position P, it returns the applied voltage of the element at the position mirrored
    through the board's centre (2C - P).

    Uses the run's voltage_pattern_applied.csv (every element of both densities) and the scan
    geometry in metadata.json, since a rotated position may belong to the other density. Runs
    without those fall back to the scanned elements themselves. NaN where nothing is found.
    """
    import csv as _csv
    from config import ScanGeometryConfig
    from stage import HexGridPlanner

    applied_path = os.path.join(ds.folder, "voltage_pattern_applied.csv")
    meta_path = os.path.join(ds.folder, "metadata.json")
    positions: dict[tuple[str, int, int], tuple[float, float]] = {}
    voltages: dict[tuple[str, int, int], float] = {}
    if os.path.isfile(applied_path) and os.path.isfile(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            g = json.load(f).get("config", {}).get("geometry", {})
        fields = {k: g[k] for k in ScanGeometryConfig.__dataclass_fields__ if k in g}
        for p in HexGridPlanner(ScanGeometryConfig(**fields)).all_points():
            positions[(p.density, p.logical_row, p.logical_col)] = (p.stage_x_mm, p.stage_y_mm)
        with open(applied_path, newline="", encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                value = row.get("applied_voltage_v") or row.get("voltage_v") or ""
                if value != "":
                    voltages[(row["density"], int(row["row"]), int(row["col"]))] = float(value)
    if not positions:  # older run: only what was scanned
        for key, el in ds.elements.items():
            if el.position_mm is not None:
                positions[key] = el.position_mm
                voltages[key] = float(el.voltages[0])
    if not positions:
        return {key: float("nan") for key in ds.elements}
    xy = np.array(list(positions.values()))
    centre = (xy.min(axis=0) + xy.max(axis=0)) / 2
    at = {(round(x, 3), round(y, 3)): key for key, (x, y) in positions.items()}
    out = {}
    for key, el in ds.elements.items():
        pos = positions.get(key, el.position_mm)
        if pos is None:
            out[key] = float("nan")
            continue
        other = at.get((round(2 * centre[0] - pos[0], 3), round(2 * centre[1] - pos[1], 3)))
        out[key] = voltages.get(other, float("nan")) if other is not None else float("nan")
    return out


VOLTAGE_MATCH_V = 5e-4   # a measured voltage counts as the chosen one within this


def dataset_voltages(processed: dict) -> list[float]:
    """Every voltage measured on any element, ascending. Readings within VOLTAGE_MATCH_V of
    the lowest in their group count as one voltage (the same setpoint read back slightly
    differently in different files), listed as that lowest reading."""
    out: list[float] = []
    for v in sorted(float(v) for p in processed.values() for v in p.voltages):
        if not out or v - out[-1] > VOLTAGE_MATCH_V:
            out.append(v)
    return out


def heatmap_value(p: Processed, metric: str, ref_freq_ghz: float, voltage: float | None = None) -> float:
    """One number per element for the heatmap, at the frequency nearest ref_freq_ghz.
    "phase_at_v" also needs voltage: NaN (grey) where this element wasn't measured at it."""
    idx = int(np.argmin(np.abs(p.freqs_ghz - ref_freq_ghz)))
    phase, mag = p.phase_at(idx), p.mag_db_at(idx)
    if metric == "surface_gap":
        return float(p.gap_mm) if p.gap_mm is not None else float("nan")
    if metric in ("raw_phase", "ref_phase"):
        return float(phase[0])
    if metric == "raw_mag":
        return float(mag[0])
    if metric == "applied_v":
        return float(p.voltages[0])
    if metric == "phase_at_v":
        if p.raw_phase is None or voltage is None:
            return float("nan")
        hits = np.flatnonzero(np.abs(np.asarray(p.voltages, dtype=float) - voltage) <= VOLTAGE_MATCH_V)
        return float(p.raw_phase[hits[-1], idx]) if hits.size else float("nan")
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


# ============================================================================ transient runs
#
# A transient run holds, per element, one continuous CW capture of S11 against time while the
# board steps through a voltage profile (see data.py). There is one frequency, so nothing is
# time-gated or calibrated: this is the raw S11 phase and magnitude at the CW frequency.
#
# Loading cleans up two things the capture can't be trusted on:
#   * Unmeasured tail. If the buffer is read before the sweep has finished, the points not yet
#     measured come back as exactly 1+0j (or 0). That run of filler at the end is dropped.
#   * The time axis. Each change of voltage is found in the trace itself (the board changing is
#     an abrupt step in S11). When the file's sample interval is only an estimate (dt_source not
#     "instrument"), the real interval is recovered from the detected changes: they are known to
#     be (step_done_s[i+1] - step_done_s[i]) apart on the PC clock, so samples between them /
#     seconds between them gives dt. One interval is used for the whole run (it's a property of
#     the VNA settings), the median over its elements. The PC-clock times themselves are not
#     used to place anything: the Pi applies a change about a second after it was sent, and
#     that lag varies.
#
# Holds run from one detected change to the next (the first from the start of the capture, the
# last to the end of the measured data). The board can already move before a change appears:
# the Pi stops its DAC program about a second before the new voltage takes effect (on the sample
# run, the 10 V hold drops about 1 deg 1.5-1.75 s before the 10 -> 0 V change). So each hold's
# quiet part ends at its "update start": before the next change by the controller's update time
# (step_done_s - step_sent_s) plus UPDATE_MARGIN_S for the VNA's trigger latency (at most half
# the hold), or by guard_pct of the hold, whichever is longer.
#
# Transition time, for the change from step i to step i + 1 of the profile:
#   1. Settled phase of each step: the mean phase over the last `settle_pct` of that step's
#      hold, ending at its update start (above).
#   2. Tolerance: `tolerance_pct` of this transition's own phase change, the difference
#      between the two settled phases (e.g. 20 -> 200 deg: 10% of 180 = +-18 deg; 2 -> 20 deg:
#      +-1.8 deg). The same tolerance is used for both bands.
#   3. Start band = settled phase of step i +- tolerance; end band = that of step i + 1.
#   4. Arrival: the moment the phase enters the end band for the last time, so it stays in the
#      band until the end of step i + 1's settled window (overshoot or ringing that leaves the
#      band again restarts the clock).
#   5. Departure: the first moment, from the detected change on, that the phase leaves the start
#      band, through either edge (wrapping is handled by unwrapping the phase in time first).
#      Counting from the change means earlier noise can't start the clock, and a response that
#      swings away and later passes back through the start band (as 10 -> 0 V falls do) is
#      timed from when it first moved, not from the later pass. If the controller's update has
#      already pushed the phase out of the start band by the time the change arrives (the Pi's
#      dip, above), the clock starts at the change itself: it times the element's response to
#      the new voltage, not the controller's update. How far off it already was is recorded
#      (offset_at_change_deg) and shown in the element view.
#   6. Transition time = arrival - departure. Both crossings are interpolated between samples.

TRANSIENT_HEATMAP_METRICS = {"Transition time (ms)": "transition_time"}
TRANSIENT_DIRECTIONS = ("Rise", "Fall")
MIN_SETTLE_SAMPLES = 3
CHANGE_LAG_S = 0.05        # change detection compares samples this far apart (at least 1 sample)
CHANGE_THRESHOLD = 10.0    # ...and calls it a change above median + this many robust sigmas
CHANGE_FLOOR = 0.001       # ...or above this fraction of the change's own jump (matters only on noise-free data)
CHANGE_SEARCH = 0.25       # search +- this fraction of a hold around where the change is expected
DT_MISMATCH_NOTE = 0.01    # an instrument-reported interval this far off the changes gets a note
UPDATE_MARGIN_S = 1.0      # added to the controller's update time when ending a hold's quiet part


@dataclass
class TransientSettings:
    tolerance_pct: float = 10.0   # band half-width, % of this transition's phase change (< 50, so bands never overlap)
    settle_pct: float = 10.0      # settled phase = mean over the last this-% of each hold...
    guard_pct: float = 2.0        # ...ending at least this % of the hold before the next change (see update start)


@dataclass
class TransientElement:
    density: str
    row: int
    col: int
    file_name: str
    position_mm: tuple[float, float] | None
    dt_s: float                 # sample interval used (recovered from the changes when the file's was estimated)
    phase_deg: np.ndarray       # (n,) raw S11 phase, unwrapped in time (measured samples only)
    mag_db: np.ndarray          # (n,) raw |S11| in dB
    voltages_v: np.ndarray      # (S,) the profile
    hold_s: float
    step_sent_s: np.ndarray     # (S,) when each change was sent, from the trigger (PC clock)
    step_done_s: np.ndarray     # (S,) when the controller returned
    applied_v: np.ndarray | None
    freq_hz: float
    ifbw_hz: float | None = None
    dt_source: str = ""
    dt_file_s: float = float("nan")   # the interval the file was saved with
    n_file: int = 0                   # points in the file, including any unmeasured tail
    changes: np.ndarray | None = None  # (S - 1,) sample index where each change starts; -1 = not found
    scan_type: str = "transient"

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.density, self.row, self.col)

    @property
    def n(self) -> int:
        return len(self.phase_deg)

    @property
    def time_s(self) -> np.ndarray:
        return np.arange(self.n) * self.dt_s   # nothing resampled: sample k is at k * dt

    @property
    def change_s(self) -> np.ndarray:
        """When each change starts in the trace (s), NaN where it wasn't found."""
        c = np.asarray(self.changes if self.changes is not None else [], dtype=float)
        return np.where(c >= 0, c * self.dt_s, np.nan)


@dataclass
class Transition:
    index: int        # from step `index` to step `index + 1` (0-indexed)
    v_from: float
    v_to: float
    direction: str    # "Rise" or "Fall"

    @property
    def label(self) -> str:
        return f"{self.v_from:g} \u2192 {self.v_to:g} V  (step {self.index + 1}\u2192{self.index + 2})"


@dataclass
class TransitionResult:
    status: str                  # "ok", or why there's no time (see TRANSITION_REASONS)
    time_s: float = float("nan")
    depart_s: float = float("nan")
    arrive_s: float = float("nan")
    phase_from: float = float("nan")   # settled phases (unwrapped deg)
    phase_to: float = float("nan")
    tol_deg: float = float("nan")
    window_from: tuple[float, float] | None = None   # the settled windows used (s)
    window_to: tuple[float, float] | None = None
    change_s: float = float("nan")    # when the change starts in the trace
    offset_at_change_deg: float = float("nan")   # phase - start phase at the change, if already outside the start band

    @property
    def reason(self) -> str:
        return TRANSITION_REASONS.get(self.status, self.status)


TRANSITION_REASONS = {
    "ok": "",
    "small_step": "no phase change between the two settled phases",
    "not_settled": "never stays inside the end band",
    "no_start": "the change wasn't found in the trace",
    "short_hold": "hold too short to find a settled phase",
    "no_step": "this element's capture doesn't include this step",
}


def transitions(profile_v) -> list[Transition]:
    """Every change in the profile, in order (repeats included, told apart by step number).
    Steps to the same voltage aren't changes and are left out."""
    out = []
    v = [float(x) for x in np.asarray(profile_v, dtype=float).ravel()]
    for i in range(len(v) - 1):
        if v[i + 1] != v[i]:
            out.append(Transition(i, v[i], v[i + 1], "Rise" if v[i + 1] > v[i] else "Fall"))
    return out


def _is_transient_folder(folder: str) -> bool:
    try:
        with open(os.path.join(folder, "metadata.json"), encoding="utf-8") as f:
            if json.load(f).get("scan_mode") == "transient":
                return True
    except (OSError, ValueError):
        pass
    for path in sorted(glob.glob(os.path.join(folder, "*.npz")))[:3]:   # no metadata: look at the files
        try:
            with np.load(path) as z:
                if "s11" in z.files and "dt_s" in z.files:
                    return True
        except Exception:
            continue
    return False


def _measured_length(s11: np.ndarray) -> int:
    """Samples before the unmeasured tail: a buffer read before the sweep finished returns the
    points not yet measured as exactly 1+0j (or 0)."""
    filler = (s11 == 1) | (s11 == 0) | ~np.isfinite(s11)
    real = np.flatnonzero(~filler)
    return int(real[-1]) + 1 if real.size else 0


def _step_times(sent: np.ndarray, done: np.ndarray) -> np.ndarray:
    """PC-clock time of each change, for spacing only: when the controller returned, which on
    the six-element sample run tracks the change seen in the trace more closely than when it
    was sent."""
    return done if done.shape == sent.shape and np.isfinite(done).all() else sent


def _find_changes(s11: np.ndarray, dt: float, hold_s: float, times: np.ndarray) -> np.ndarray:
    """Sample index where each change (step 1 onwards) starts, -1 if not found. Each is looked
    for within CHANGE_SEARCH of a hold around where it's expected, from the previous change and
    the PC-clock spacing, using the interval measured so far once two have been found. The
    change is the biggest jump |s11[k] - s11[k - lag]| there, walked back to where that jump
    first rose above the noise before it (a backward difference, so it rises at the change
    itself)."""
    n = s11.size
    lag = max(1, int(round(CHANGE_LAG_S / dt)))
    jump = np.zeros(n)
    if n > lag:
        jump[lag:] = np.abs(s11[lag:] - s11[:-lag])
    out = np.full(len(times) - 1, -1, dtype=np.int64)
    found_k, found_t = [], []
    prev_k, prev_t, dt_cur = 0.0, 0.0, dt
    for i in range(1, len(times)):
        hold_n = max(hold_s, times[i] - times[i - 1]) / dt_cur
        expected = prev_k + (times[i] - prev_t) / dt_cur
        lo = int(max(prev_k + CHANGE_SEARCH * hold_n if found_k else lag, expected - CHANGE_SEARCH * hold_n))
        hi = int(min(n, expected + CHANGE_SEARCH * hold_n))
        if hi - lo < 10:
            continue
        w = jump[lo:hi]
        peak = int(np.argmax(w))
        quiet = w[:peak - 2 * lag] if peak - 2 * lag >= 20 else w   # noise from before the change
        med = float(np.median(quiet))
        sigma = 1.4826 * float(np.median(np.abs(quiet - med)))
        if w[peak] <= med + CHANGE_THRESHOLD * sigma:
            continue
        thr = med + max(CHANGE_THRESHOLD * sigma, CHANGE_FLOOR * (w[peak] - med))
        below = np.flatnonzero(w[:peak] < thr)
        k = lo + (int(below[-1]) + 1 if below.size else 0)
        out[i - 1] = k
        found_k.append(k)
        found_t.append(times[i])
        prev_k, prev_t = float(k), float(times[i])
        if len(found_k) >= 2:
            dt_cur = float(np.polyfit(found_k, found_t, 1)[0])
    return out


def _interval_from_changes(el: TransientElement) -> float:
    """This element's sample interval from its detected changes and their PC-clock spacing
    (NaN with fewer than two)."""
    times = _step_times(el.step_sent_s, el.step_done_s)[1:]
    ok = el.changes >= 0
    if ok.sum() < 2:
        return float("nan")
    slope = float(np.polyfit(el.changes[ok].astype(float), times[ok], 1)[0])
    return slope if slope > 0 else float("nan")


def load_transient_element(path: str) -> TransientElement:
    name = os.path.basename(path)
    with np.load(path) as z:
        keys = z.files
        missing = [k for k in ("s11", "dt_s", "voltages_v", "step_sent_s") if k not in keys]
        if missing:
            raise ValueError(f"not a transient capture (no {', '.join(missing)})")
        raw = np.asarray(z["s11"]).ravel()
        n = _measured_length(raw)
        if n < 10:
            raise ValueError("capture has no measured samples")
        s11 = raw[:n]
        if not np.isfinite(s11).all():
            raise ValueError("capture has non-finite samples")
        phase = np.degrees(np.unwrap(np.angle(s11)))
        with np.errstate(divide="ignore"):
            mag = 20 * np.log10(np.abs(s11))
        name_density, name_row, name_col = _parse_name(name)
        density = str(z["density"]) if "density" in keys else (name_density or "L")
        row = int(z["logical_row"]) if "logical_row" in keys else name_row
        col = int(z["logical_col"]) if "logical_col" in keys else name_col
        if row is None or col is None:
            raise ValueError("no row/column in the file or its name")
        position = None
        if "stage_x_mm" in keys and "stage_y_mm" in keys:
            position = (float(z["stage_x_mm"]), float(z["stage_y_mm"]))
        voltages = np.asarray(z["voltages_v"], dtype=float).ravel()
        sent = np.asarray(z["step_sent_s"], dtype=float).ravel()
        done = np.asarray(z["step_done_s"], dtype=float).ravel() if "step_done_s" in keys else sent.copy()
        hold = float(z["hold_s"]) if "hold_s" in keys else (float(np.median(np.diff(sent))) if sent.size > 1 else 0.0)
        dt = float(z["dt_s"])
        return TransientElement(
            density, row, col, name, position, dt,
            phase.astype(np.float32), mag.astype(np.float32), voltages, hold, sent, done,
            np.asarray(z["applied_v"], dtype=float).ravel() if "applied_v" in keys else None,
            float(z["freq_hz"]) if "freq_hz" in keys else float("nan"),
            float(z["ifbw_hz"]) if "ifbw_hz" in keys else None,
            str(z["dt_source"]) if "dt_source" in keys else "",
            dt_file_s=dt, n_file=int(raw.size),
            changes=_find_changes(s11, dt, hold, _step_times(sent, done)),
        )


def load_transient_dataset(folder: str, progress=None) -> Dataset:
    paths = [p for p in sorted(glob.glob(os.path.join(folder, "*.npz")))
             if not os.path.basename(p).startswith("summary_")]
    elements, skipped = {}, []
    for i, path in enumerate(paths, start=1):
        try:
            el = load_transient_element(path)
        except Exception as e:
            skipped.append(f"{os.path.basename(path)}: {e}")
        else:
            elements[el.key] = el
        if progress and (i % 25 == 0 or i == len(paths)):
            progress(i, len(paths))
    profile = None
    try:
        with open(os.path.join(folder, "metadata.json"), encoding="utf-8") as f:
            meta = json.load(f)
        for source in (meta.get("transient") or {}, meta):
            for k in ("voltages_v", "voltages", "voltage_profile"):
                if isinstance(source.get(k), list) and len(source[k]) > 1:
                    profile = np.asarray(source[k], dtype=float)
                    break
            if profile is not None:
                break
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    notes = []
    if elements:
        # The profile the elements themselves record wins (it's what was actually run);
        # metadata is only a fallback. Elements with a different profile are noted.
        profiles = [tuple(np.round(el.voltages_v, 6)) for el in elements.values()]
        common = max(set(profiles), key=profiles.count)
        profile = np.asarray(common, dtype=float)
        odd = sum(p != common for p in profiles)
        if odd:
            notes.append(f"{odd} element(s) recorded a different profile (shown grey)")
        notes += _fix_time_axis(elements)
    return Dataset(folder, elements, skipped, "transient", notes=notes, profile_v=profile)


def _fix_time_axis(elements: dict) -> list[str]:
    """Sets every element's sample interval (see the notes at the top of this section) and fills
    in changes that weren't detected. Returns notes for the run description."""
    els = list(elements.values())
    notes = []
    file_dt = float(np.median([el.dt_file_s for el in els]))
    estimated = any(el.dt_source != "instrument" for el in els)
    measured = np.array([_interval_from_changes(el) for el in els])
    measured = measured[np.isfinite(measured)]
    if measured.size:
        dt_run = float(np.median(measured))
        if estimated:
            for el in els:
                el.dt_s = dt_run
            notes.append(f"sample interval {dt_run * 1e3:.4g} ms, recovered from the voltage changes in "
                         f"{measured.size} element(s) (the file's {file_dt * 1e3:.4g} ms was an estimate)")
        elif abs(dt_run / file_dt - 1) > DT_MISMATCH_NOTE:
            notes.append(f"the VNA's sample interval ({file_dt * 1e3:.4g} ms) disagrees with the voltage changes "
                         f"({dt_run * 1e3:.4g} ms); the VNA's is used")
    elif estimated:
        notes.append(f"sample interval {file_dt * 1e3:.4g} ms is the file's estimate; it couldn't be checked "
                     "(needs two or more detected voltage changes), so times may be scaled wrong")
    # Changes not found in an element: placed by the run's usual lag after the PC-clock time.
    lags = [c * el.dt_s - t for el in els
            for c, t in zip(el.changes, _step_times(el.step_sent_s, el.step_done_s)[1:]) if c >= 0]
    lag = float(np.median(lags)) if lags else 0.0
    missing = 0
    for el in els:
        times = _step_times(el.step_sent_s, el.step_done_s)[1:]
        for j in np.flatnonzero(el.changes < 0):
            k = int(round((times[j] + lag) / el.dt_s))
            if 0 < k < el.n:
                el.changes[j] = k
            missing += 1
    if missing:
        notes.append(f"{missing} voltage change(s) not seen in the trace, placed from the step times")
    cut = [el for el in els if el.n < el.n_file]
    if cut:
        lost = max(el.n_file - el.n for el in cut)
        notes.append(f"{len(cut)} capture(s) end early (up to {lost} of {cut[0].n_file} points, "
                     f"{lost * cut[0].dt_s:.3g} s, never measured: read before the sweep finished)")
    return notes


def _describe_transient(ds: Dataset) -> str:
    counts = ", ".join(f"{sum(k[0] == d for k in ds.elements)} {d}" for d in ds.densities)
    el = next(iter(ds.elements.values()))
    prof = ", ".join(f"{v:g}" for v in (ds.profile_v if ds.profile_v is not None else el.voltages_v))
    text = (f"Transient run: {counts} elements, profile {prof} V, hold {el.hold_s:g} s, "
            f"CW {el.freq_hz / 1e9:g} GHz, {el.n} samples every {el.dt_s * 1e3:.4g} ms")
    if ds.skipped:
        text += f"; skipped {len(ds.skipped)} file(s)"
    for note in ds.notes:
        text += f"; {note}"
    return text


def _hold_bounds(el: TransientElement) -> list[tuple[int, int]]:
    """Sample range [start, stop) of each step: from its detected change to the next one."""
    edges = [0] + [int(c) if c >= 0 else -1 for c in (el.changes if el.changes is not None else [])] + [el.n]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def _settled_windows(el: TransientElement, ts: TransientSettings) -> list[tuple[int, int] | None]:
    """Sample range [a, b) averaged for each step's settled phase, or None if the hold is too
    short (or its change wasn't found). b is the step's update start (see the notes above); the
    last step has no update after it, so only guard_pct applies."""
    update_s = np.clip(el.step_done_s - el.step_sent_s, 0, None) if el.step_done_s.shape == el.step_sent_s.shape \
        else np.zeros(len(el.voltages_v))
    out = []
    for i, (start, stop) in enumerate(_hold_bounds(el)):
        length = stop - start
        if start < 0 or stop < 0 or length < MIN_SETTLE_SAMPLES:
            out.append(None)
            continue
        guard = ts.guard_pct / 100 * length
        if stop < el.n and i + 1 < len(update_s):   # a change follows: keep clear of its update
            guard = max(guard, min((float(update_s[i + 1]) + UPDATE_MARGIN_S) / el.dt_s, 0.5 * length))
        width = max(MIN_SETTLE_SAMPLES, int(round(ts.settle_pct / 100 * length)))
        b = stop - int(round(guard))
        a = max(start, b - width)
        out.append((a, b) if b - a >= MIN_SETTLE_SAMPLES else None)
    return out


def settled_phases(el: TransientElement, ts: TransientSettings) -> tuple[np.ndarray, list]:
    """Each step's settled phase (unwrapped deg; NaN where it can't be found), and the windows."""
    wins = _settled_windows(el, ts)
    phases = np.full(len(wins), np.nan)
    for i, w in enumerate(wins):
        if w is not None:
            phases[i] = np.mean(el.phase_deg[w[0]:w[1]], dtype=np.float64)
    return phases, wins


def _crossing(x: np.ndarray, k: int, centre: float, tol: float) -> float:
    """Fractional sample index between k and k + 1 where x crosses the edge of centre +- tol
    (the edge on the side of whichever of the two samples is outside the band)."""
    outside = x[k + 1] if abs(x[k + 1] - centre) > tol else x[k]
    edge = centre + tol * np.sign(outside - centre)
    dx = x[k + 1] - x[k]
    frac = 0.5 if dx == 0 else float(np.clip((edge - x[k]) / dx, 0.0, 1.0))
    return k + frac


def transition_time(el: TransientElement, index: int, ts: TransientSettings) -> TransitionResult:
    """Transition time for the change from step `index` to `index + 1` (see the notes above)."""
    if index + 1 >= len(el.voltages_v) or el.changes is None or index >= len(el.changes):
        return TransitionResult("no_step")
    phases, wins = settled_phases(el, ts)
    dt = el.dt_s
    change = float(el.changes[index] * dt) if el.changes[index] >= 0 else float("nan")
    w_from, w_to = wins[index], wins[index + 1]
    if w_from is None or w_to is None:
        return TransitionResult("short_hold", change_s=change)
    pa, pb = float(phases[index]), float(phases[index + 1])
    tol = ts.tolerance_pct / 100 * abs(pb - pa)
    common = dict(phase_from=pa, phase_to=pb, tol_deg=tol, change_s=change,
                  window_from=(w_from[0] * dt, w_from[1] * dt), window_to=(w_to[0] * dt, w_to[1] * dt))
    if abs(pb - pa) <= 2 * tol:   # only when the change is zero, since tolerance_pct < 50
        return TransitionResult("small_step", **common)
    lo, hi = w_from[0], w_to[1]   # from the start band's settled window to the end band's
    x = el.phase_deg[lo:hi].astype(np.float64)
    outside_end = np.flatnonzero(np.abs(x - pb) > tol)
    arrive = int(outside_end[-1]) + 1 if outside_end.size else 0
    if arrive >= len(x):
        return TransitionResult("not_settled", **common)
    if el.changes[index] < 0:
        return TransitionResult("no_start", **common)
    from_k = int(el.changes[index]) - lo   # the detected change
    if not 0 <= from_k < arrive:
        return TransitionResult("no_start", **common)
    if abs(x[from_k] - pa) > tol:   # already out of the start band (controller update): start at the change
        common["offset_at_change_deg"] = float(x[from_k] - pa)
        t_dep = (lo + from_k) * dt
    else:
        outside_start = np.flatnonzero(np.abs(x[from_k:arrive] - pa) > tol)
        depart = from_k + int(outside_start[0]) - 1   # last sample inside; crossing is between it and the next
        t_dep = (lo + _crossing(x, depart, pa, tol)) * dt
    t_arr = (lo + _crossing(x, arrive - 1, pb, tol)) * dt
    return TransitionResult("ok", time_s=max(t_arr - t_dep, 0.0), depart_s=t_dep, arrive_s=t_arr, **common)


def transition_map(ds: Dataset, transition: Transition, ts: TransientSettings) -> dict:
    """{key: TransitionResult} for every element. Elements whose recorded profile differs from
    the dataset's get "no_step" (grey)."""
    out = {}
    for key, el in ds.elements.items():
        same = (ds.profile_v is None or (len(el.voltages_v) == len(ds.profile_v)
                                         and np.allclose(el.voltages_v, ds.profile_v, atol=1e-6)))
        out[key] = transition_time(el, transition.index, ts) if same else TransitionResult("no_step")
    return out
