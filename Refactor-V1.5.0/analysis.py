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
import os
import re
from dataclasses import dataclass, field

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

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.density, self.row, self.col)


@dataclass
class Dataset:
    folder: str
    elements: dict[tuple[str, int, int], Element]
    skipped: list[str]

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
        if self.skipped:
            text += f"; skipped {len(self.skipped)} file(s)"
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


def load_element(path: str, settings: AnalysisSettings) -> Element:
    """Loads one file. Raises ValueError with a reason if it isn't usable."""
    name = os.path.basename(path)
    with np.load(path) as z:
        keys = z.files
        if "sdata" in keys:
            sdata = np.atleast_2d(z["sdata"])
            if "measured" in keys:
                measured = z["measured"].astype(bool)
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

        name_density, name_row, name_col = _parse_name(name)
        density = str(z["density"]) if "density" in keys else (name_density or "L")
        row = int(z["logical_row"]) if "logical_row" in keys else name_row
        col = int(z["logical_col"]) if "logical_col" in keys else name_col
        if row is None or col is None:
            raise ValueError("no row/column in the file or its name")
        position = None
        if "stage_x_mm" in keys and "stage_y_mm" in keys:
            position = (float(z["stage_x_mm"]), float(z["stage_y_mm"]))

    return Element(density, row, col, voltages, sdata, freqs_ghz, position, name)


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
    return Dataset(folder, elements, skipped)


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

    def phase_at(self, idx: int) -> np.ndarray:
        return self.phase_rel[:, idx]

    def mag_db_at(self, idx: int) -> np.ndarray:
        return self.gated_mag_db[:, idx]


def process_element(el: Element, settings: AnalysisSettings) -> Processed:
    gated = _gate(el.sdata, settings.gate_start_samples, settings.gate_stop_samples, settings.gate_alpha)
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
    return Processed(el.voltages, el.freqs_ghz, mag_db, phase_rel, list(refs), idx)


def heatmap_value(p: Processed, metric: str, ref_freq_ghz: float) -> float:
    """One number per element for the heatmap, at the frequency nearest ref_freq_ghz."""
    idx = int(np.argmin(np.abs(p.freqs_ghz - ref_freq_ghz)))
    phase, mag = p.phase_at(idx), p.mag_db_at(idx)
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
    for i, (key, el) in enumerate(ds.elements.items(), start=1):
        out[key] = process_element(el, settings)
        if progress and (i % 25 == 0 or i == total):
            progress(i, total)
    return out
