#!/usr/bin/env python3
"""
nfp_calibrate.py - copper-plate calibration for near-field reflectarray unit-cell
measurements, with an optional gap map for arrays that bow or warp.

Usage
    python nfp_calibrate.py --f-start 26 --f-stop 30 --n-points 161 [options]

    The frequency grid (GHz, inclusive, linearly spaced) applies to every plate and
    element file, so the last axis of each sdata array must have --n-points values.

Folder layout
    cal/   *.npz                  plates for a flat array (one site), or
    cal/   sites.csv + <site>/    one sub-folder of plate .npz files per site (site,x_mm,y_mm)
    data/  *.npz                  element scans: sdata, optional voltages_v, and
                                  physical_x_mm / physical_y_mm (or stage_*) for multi-site

    Plate files hold raw S11 under sdata (same key as the elements). If a plate's
    sdata has several rows (repeated sweeps), they are averaged.

Plate depth is read from the file name ("Surface" -> 0, "N1mm" -> 1, "N1_52mm" -> 1.52)
and means how much closer to the probe the plate sits than the local array surface.
Site coordinates come from sites.csv, a folder name like "x69.1_y-64.1", or --site-xy.
Plate files directly in cal/ form a site named "all".

Method
    Error model per frequency: S = e00 + T*G / (1 - e11*G), with a plate at depth d
    giving G = -exp(+j*2*k*d). Three or more plates solve e00, e11, T.
    A site whose surface sits delta closer to the probe has e11 and T rotated by
    exp(+j*2*k*delta). Each site's delta is fitted, a smooth delta(x, y) is fitted
    through the sites, and each element is corrected at its own delta.
    --cal-pad-db rescales T for an attenuator present in only one session (approximate).

Output (in --out-dir): <element>_cal.npz with the original arrays plus gamma, gap_mm,
frequencies_hz, cal_e00, cal_e11, cal_T, cal_depths_mm; cal_terms.npz; PNGs with --plot.
"""

import argparse
import csv
import glob
import os
import re
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

C0 = 299_792_458.0
OUTPUT_KEYS = ("gamma", "gap_mm", "frequencies_hz", "cal_depths_mm", "cal_e00", "cal_e11", "cal_T")
FREQ_TOL_HZ = 1.0


# --------------------------------------------------------------------------- #
#  Physics
# --------------------------------------------------------------------------- #
def wavenumber(freq_hz):
    return 2 * np.pi * freq_hz / C0


def gap_rotation(freq_hz, delta_mm):
    """Round-trip phase factor for a reference plane moved delta_mm toward the probe."""
    return np.exp(2j * wavenumber(freq_hz) * delta_mm * 1e-3)


def plate_gamma(freq_hz, depth_mm, sign=+1):
    return -np.exp(sign * 2j * wavenumber(freq_hz) * depth_mm * 1e-3)


def pad_factor(freq_hz, pad_db, delay_ps):
    return 10 ** (2 * pad_db / 20) * np.exp(2j * np.pi * freq_hz * 2 * delay_ps * 1e-12)


@dataclass
class ErrorTerms:
    e00: np.ndarray
    e11: np.ndarray
    T: np.ndarray

    def measured(self, gamma):
        return self.e00 + self.T * gamma / (1 - self.e11 * gamma)

    def corrected(self, s11):
        y = s11 - self.e00
        return y / (self.T + self.e11 * y)

    def rotated(self, rot):
        return ErrorTerms(self.e00, self.e11 * rot, self.T * rot)


def solve_error_terms(S_meas, G_ideal):
    """Solve S = A + B*G + C*G*S per frequency (A = e00, C = e11, B = T - e00*e11).

    One SVD per frequency gives both the least-squares solution and the condition number.
    """
    n_plates, n_freq = S_meas.shape
    M = np.stack([np.ones_like(G_ideal), G_ideal, G_ideal * S_meas], axis=-1).transpose(1, 0, 2)
    rhs = S_meas.T[..., None]

    ok = np.all(np.isfinite(M), axis=(1, 2))
    U, s, Vh = np.linalg.svd(M[ok], full_matrices=False)
    x = Vh.conj().swapaxes(-1, -2) @ ((U.conj().swapaxes(-1, -2) @ rhs[ok]) / s[..., None])

    solution = np.full((n_freq, 3), np.nan + 0j)
    cond = np.full(n_freq, np.nan)
    solution[ok] = x[..., 0]
    cond[ok] = s[:, 0] / s[:, -1]

    A, B, C = solution.T
    return ErrorTerms(e00=A, e11=C, T=B + A * C), cond


def fit_gap_offset(freq, depths, S_meas, terms, sign, search_mm=3.0):
    """Offset (mm) at which the reference terms best reproduce this site's plates.

    Coarse 0.05 mm grid over +-search_mm, then a 0.002 mm grid around the best point.
    Returns (delta_mm, rms misfit relative to the plate signal level).
    """
    def misfit(deltas):
        d = np.asarray(deltas)[:, None]
        return sum(np.nanmean(abs(S - terms.measured(plate_gamma(freq, depth + d, sign))) ** 2, axis=1)
                   for depth, S in zip(depths, S_meas))

    coarse = np.arange(-search_mm, search_mm + 1e-9, 0.05)
    best = coarse[np.argmin(misfit(coarse))]
    fine = np.arange(best - 0.06, best + 0.06 + 1e-9, 0.002)
    fine_cost = misfit(fine)

    signal = np.sqrt(np.nanmean([np.nanmean(abs(S) ** 2) for S in S_meas]))
    rms = np.sqrt(fine_cost.min() / len(depths)) / signal
    return fine[np.argmin(fine_cost)], rms


def relative_difference(a, b):
    return np.sqrt(np.nanmean(abs(a - b) ** 2) / np.nanmean(abs(b) ** 2))


# --------------------------------------------------------------------------- #
#  Plates and sites
# --------------------------------------------------------------------------- #
DEPTH_PATTERN = re.compile(r"N?(\d+(?:[._]\d+)?)\s*mm", re.IGNORECASE)
XY_PATTERN = re.compile(r"x\s*(-?\d+(?:\.\d+)?)[_, ]+y\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)


@dataclass
class Plate:
    depth_mm: float
    name: str
    s11: np.ndarray


@dataclass
class Site:
    name: str
    plates: List[Plate]
    xy: Optional[Tuple[float, float]] = None


def read_plate(path, key, n_points):
    """Return the plate's S11 as one sweep of n_points; repeated sweeps are averaged."""
    with np.load(path, allow_pickle=False) as npz:
        if key not in npz.files:
            sys.exit(f"{path}: no {key!r} array (has {npz.files})")
        s11 = np.asarray(npz[key])
    if s11.shape[-1] != n_points:
        sys.exit(f"{path}: last axis of {key!r} has {s11.shape[-1]} points, --n-points is {n_points}")
    return s11.reshape(-1, n_points).mean(axis=0)


def depth_from_name(name):
    base = os.path.splitext(os.path.basename(name))[0]
    match = DEPTH_PATTERN.search(base)
    if match:
        return float(match.group(1).replace("_", "."))
    if "surface" in base.lower():
        return 0.0
    return None


def find_plate_files(folder):
    return sorted(glob.glob(os.path.join(folder, "*.npz")))


def load_plates(folder, depth_overrides, key, n_points):
    plates = []
    for path in find_plate_files(folder):
        name = os.path.basename(path)
        depth = depth_overrides.get(name, depth_from_name(name))
        if depth is None:
            print(f"  skip {name}: cannot read a depth from the name (use --depth '{name}=<mm>')")
            continue
        plates.append(Plate(depth, name, read_plate(path, key, n_points)))
    return plates


def load_sites(cal_dir, depth_overrides, xy_overrides, key, n_points):
    candidates = [("all", cal_dir)] + [(sub, os.path.join(cal_dir, sub)) for sub in sorted(os.listdir(cal_dir))]
    sites = {}
    for name, folder in candidates:
        if os.path.isdir(folder) and find_plate_files(folder):
            sites[name] = Site(name, load_plates(folder, depth_overrides, key, n_points))
    if not sites:
        sys.exit(f"No plate .npz files found in {cal_dir!r} or its sub-folders")
    assign_coordinates(sites, cal_dir, xy_overrides)
    return sites


def assign_coordinates(sites, cal_dir, xy_overrides):
    """Priority: --site-xy, then sites.csv, then an 'x.._y..' folder name."""
    csv_path = os.path.join(cal_dir, "sites.csv")
    if os.path.exists(csv_path):
        with open(csv_path, newline="") as fh:
            for row in csv.DictReader(fh):
                row = {k.strip().lower(): v.strip() for k, v in row.items() if k}
                if row.get("site") in sites:
                    sites[row["site"]].xy = (float(row["x_mm"]), float(row["y_mm"]))

    for site in sites.values():
        match = XY_PATTERN.search(site.name)
        if site.xy is None and match:
            site.xy = (float(match.group(1)), float(match.group(2)))

    for item in xy_overrides:
        name, xy = item.split("=", 1)
        if name not in sites:
            sys.exit(f"--site-xy: unknown site {name!r}")
        x, y = (float(v) for v in xy.split(","))
        sites[name].xy = (x, y)

    missing = [s.name for s in sites.values() if s.xy is None]
    if len(sites) > 1 and missing:
        sys.exit(f"Several sites but no coordinates for {missing}. Add them to {csv_path} (site,x_mm,y_mm).")


# --------------------------------------------------------------------------- #
#  Gap surface
# --------------------------------------------------------------------------- #
SURFACE_MODELS = {
    "const":    (1, lambda x, y: [np.ones_like(x)]),
    "plane":    (3, lambda x, y: [np.ones_like(x), x, y]),
    "bilinear": (4, lambda x, y: [np.ones_like(x), x, y, x * y]),
    "bowl":     (4, lambda x, y: [np.ones_like(x), x, y, x * x + y * y]),
    "quad":     (6, lambda x, y: [np.ones_like(x), x, y, x * x, x * y, y * y]),
}


def auto_surface_model(n_sites):
    if n_sites < 3:
        return "const"
    if n_sites < 5:
        return "plane"
    return "bowl" if n_sites == 5 else "quad"


@dataclass
class GapSurface:
    model: str
    coef: np.ndarray
    centre: np.ndarray
    scale: float
    sites_xy: np.ndarray
    residuals_mm: np.ndarray
    dof: int

    def design(self, xy):
        xy = np.atleast_2d(xy)
        x = (xy[:, 0] - self.centre[0]) / self.scale
        y = (xy[:, 1] - self.centre[1]) / self.scale
        return np.stack(SURFACE_MODELS[self.model][1](x, y), -1)

    def gap_at(self, x, y):
        return float((self.design([[x, y]]) @ self.coef)[0])

    def is_outside(self, x, y, margin_mm):
        lo, hi = self.sites_xy.min(0) - margin_mm, self.sites_xy.max(0) + margin_mm
        return not (lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1])


def flat_surface():
    return GapSurface("const", np.zeros(1), np.zeros(2), 1.0, np.zeros((0, 2)), np.zeros(1), 0)


def fit_gap_surface(xy, gaps_mm, requested_model):
    xy, gaps_mm = np.asarray(xy, float), np.asarray(gaps_mm, float)
    n_sites = len(xy)

    model = auto_surface_model(n_sites) if requested_model == "auto" else requested_model
    n_coef = SURFACE_MODELS[model][0]
    if model != "const" and n_sites < n_coef:
        print(f"  WARNING: surface '{model}' needs {n_coef} sites, have {n_sites}; falling back to 'const'.")
        model, n_coef = "const", 1

    scale = max(np.ptp(xy[:, 0]), np.ptp(xy[:, 1]), 1.0)
    surface = GapSurface(model, None, xy.mean(0), scale, xy, None, n_sites - n_coef)
    design = surface.design(xy)
    surface.coef, *_ = np.linalg.lstsq(design, gaps_mm, rcond=None)
    surface.residuals_mm = design @ surface.coef - gaps_mm
    return surface


# --------------------------------------------------------------------------- #
#  Calibration for one frequency grid
# --------------------------------------------------------------------------- #
@dataclass
class SiteCal:
    depths: np.ndarray
    S: np.ndarray
    validation: list
    notes: List[str]
    terms: Optional[ErrorTerms] = None
    cond: Optional[np.ndarray] = None
    gap_mm: float = np.nan
    fit_rms: float = np.nan
    consistency: Optional[tuple] = None

    @property
    def full(self):
        return len(self.depths) >= 3


@dataclass
class Calibration:
    freq: np.ndarray
    sites: Dict[str, Site]
    fits: Dict[str, SiteCal]
    full: List[str]
    ref: str
    terms: ErrorTerms
    surface: GapSurface

    @property
    def multi_site(self):
        return len(self.sites) > 1


def split_standards(site, n_points, standards, tol=1e-6):
    """Separate a site's standard plates from its validation plates, averaging duplicate depths."""
    depths, S, notes = [], [], []
    for depth in sorted(set(round(d, 6) for d in standards)):
        matches = [p.s11 for p in site.plates if abs(p.depth_mm - depth) < tol]
        if not matches:
            continue
        if len(matches) > 1:
            notes.append(f"depth {depth:g} mm: averaging {len(matches)} files")
        depths.append(depth)
        S.append(np.mean(matches, axis=0))

    validation = [(p.depth_mm, p.name, p.s11) for p in site.plates
                  if not any(abs(p.depth_mm - d) < tol for d in depths)]
    S = np.array(S) if S else np.zeros((0, n_points))
    return SiteCal(np.array(depths), S, validation, notes)


def choose_reference(sites, full, requested):
    if requested is not None:
        ref = requested
    elif len(full) == 1 or all(sites[n].xy is None for n in full):
        ref = full[0]
    else:
        centre = np.mean([s.xy for s in sites.values()], axis=0)
        ref = min(full, key=lambda n: np.hypot(*(np.array(sites[n].xy) - centre)))
    if ref not in full:
        sys.exit(f"Reference site {ref!r} has no full plate set")
    return ref


def fit_gaps(fits, names, freq, terms, sign):
    for name in names:
        fit = fits[name]
        if len(fits) == 1:
            fit.gap_mm, fit.fit_rms = 0.0, 0.0
        elif len(fit.depths) == 0:
            fit.gap_mm, fit.fit_rms = np.nan, np.nan
        else:
            fit.gap_mm, fit.fit_rms = fit_gap_offset(freq, fit.depths, fit.S, terms, sign)


def pool_full_sites(fits, full, freq, ref, sign):
    """Average the full sites' terms at the reference plane, then refit every gap against them."""
    back = {n: gap_rotation(freq, -fits[n].gap_mm) for n in full}
    pooled = ErrorTerms(
        e00=np.mean([fits[n].terms.e00 for n in full], 0),
        e11=np.mean([fits[n].terms.e11 * back[n] for n in full], 0),
        T=np.mean([fits[n].terms.T * back[n] for n in full], 0),
    )
    fit_gaps(fits, fits, freq, pooled, sign)

    ref_gap = fits[ref].gap_mm
    for fit in fits.values():
        fit.gap_mm -= ref_gap
    pooled = pooled.rotated(gap_rotation(freq, ref_gap))

    for n in full:
        own = fits[n].terms.rotated(gap_rotation(freq, -fits[n].gap_mm))
        fits[n].consistency = (relative_difference(own.e00, pooled.e00),
                               relative_difference(own.e11, pooled.e11),
                               relative_difference(own.T, pooled.T))
    return pooled


def build_calibration(sites, freq, args):
    fits = {name: split_standards(site, freq.size, args.standards) for name, site in sites.items()}
    full = [name for name, fit in fits.items() if fit.full]
    if not full:
        sys.exit("No site has three plates at the standard depths "
                 f"{args.standards} mm; at least one site needs a full set.")

    for name in full:
        ideal = np.array([plate_gamma(freq, d, args.sign) for d in fits[name].depths])
        fits[name].terms, fits[name].cond = solve_error_terms(fits[name].S, ideal)

    ref = choose_reference(sites, full, args.ref_site)
    terms = fits[ref].terms
    if len(full) > 1:
        # Only the full sites' gaps are needed for pooling; every site is refitted afterwards.
        fit_gaps(fits, full, freq, terms, args.sign)
        terms = pool_full_sites(fits, full, freq, ref, args.sign)
    else:
        fit_gaps(fits, fits, freq, terms, args.sign)

    usable = [n for n in sites if sites[n].xy is not None and np.isfinite(fits[n].gap_mm)]
    if len(sites) == 1 or not usable:
        surface = flat_surface()
    else:
        surface = fit_gap_surface([sites[n].xy for n in usable], [fits[n].gap_mm for n in usable],
                                  args.surface)
    return Calibration(freq, sites, fits, full, ref, terms, surface)


# --------------------------------------------------------------------------- #
#  Report
# --------------------------------------------------------------------------- #
def report_calibration(cal, sign):
    freq, fits, ref = cal.freq, cal.fits, cal.ref
    f_centre = freq[len(freq) // 2]
    lam_mm = C0 / f_centre * 1e3
    deg_per_mm = 2 * 360 / lam_mm

    print(f"\nCentre {f_centre/1e9:.2f} GHz, lambda = {lam_mm:.2f} mm.  "
          f"Phase per 0.1 mm of gap: {0.1*deg_per_mm:.1f} deg.")
    depths = fits[ref].depths
    print(f"Reference site: {ref!r}  standard depths (mm): {', '.join(f'{d:g}' for d in depths)}")
    if len(depths) > 1:
        steps = np.diff(depths) * deg_per_mm
        print("  plate-to-plate phase advance 2kd at centre: " + ", ".join(f"{s:.0f} deg" for s in steps))
    cond = fits[ref].cond[np.isfinite(fits[ref].cond)]
    print(f"  condition number of the solve: median {np.median(cond):.1f}, max {cond.max():.1f}")

    t = cal.terms
    print(f"  median |e11| = {np.nanmedian(abs(t.e11)):.2f}   |e00| = {np.nanmedian(abs(t.e00)):.2f}"
          f"   |T| = {np.nanmedian(abs(t.T)):.2f}")
    if np.nanmedian(abs(t.e11)) > 1:
        print("  WARNING: |e11| > 1. The plate depths or sign convention are probably wrong (try --sign -1).")

    report_sites(cal)
    report_validation(cal, sign)


def report_sites(cal):
    show_consistency = len(cal.full) > 1
    print("\nSites:")
    header = f"  {'site':<10}{'x_mm':>8}{'y_mm':>8}{'plates':>8}{'type':>7}{'gap mm':>9}{'fit rms':>9}"
    if show_consistency:
        header += f"{'terms differ from pooled (e00/e11/T)':>40}"
    print(header)

    for name, site in cal.sites.items():
        fit = cal.fits[name]
        x, y = site.xy if site.xy else (np.nan, np.nan)
        row = (f"  {name:<10}{x:>8.1f}{y:>8.1f}{len(site.plates):>8}{'full' if fit.full else 'light':>7}"
               f"{fit.gap_mm:>+9.3f}{100*fit.fit_rms:>8.1f}%")
        if show_consistency and fit.consistency is not None:
            row += "      " + " / ".join(f"{100*v:.0f}%" for v in fit.consistency)
        print(row)
        for note in fit.notes:
            print(f"      {note}")

    if cal.multi_site:
        s = cal.surface
        print(f"Gap surface: '{s.model}' fitted to {len(s.sites_xy)} sites ({s.dof} degrees of freedom left); "
              f"residual at sites: " + ", ".join(f"{r:+.3f}" for r in s.residuals_mm) + " mm")
        if s.dof <= 0:
            print("  note: the surface passes through every site exactly, so its residuals say nothing about quality.")


def report_validation(cal, sign):
    for name, fit in cal.fits.items():
        rot = gap_rotation(cal.freq, fit.gap_mm) if np.isfinite(fit.gap_mm) else 1
        terms = cal.terms.rotated(rot)
        for depth, file_name, s11 in fit.validation:
            ratio = terms.corrected(s11) / plate_gamma(cal.freq, depth, sign)
            phase = np.degrees(np.angle(ratio))
            amp_db = 20 * np.log10(abs(ratio))
            print(f"  validation [{name}] {file_name} ({depth:g} mm): phase error median "
                  f"{np.nanmedian(phase):+.1f} deg (band {np.nanmin(phase):+.1f}..{np.nanmax(phase):+.1f}), "
                  f"amplitude error rms {np.sqrt(np.nanmean(amp_db**2)):.2f} dB")


# --------------------------------------------------------------------------- #
#  Element files
# --------------------------------------------------------------------------- #
@dataclass
class ElementResult:
    file: str
    out: str
    freq: np.ndarray
    S: np.ndarray
    G: np.ndarray
    volt: Optional[np.ndarray]
    gap: float
    outside: bool
    max_mag: float
    over: float


def element_xy(data):
    for kx, ky in (("physical_x_mm", "physical_y_mm"), ("stage_x_mm", "stage_y_mm")):
        if kx in data and ky in data:
            return float(np.ravel(data[kx])[0]), float(np.ravel(data[ky])[0])
    return None


def process_element(path, cal, args):
    file_name = os.path.basename(path)
    freq = cal.freq
    with np.load(path, allow_pickle=False) as npz:
        data = dict(npz)

    if args.key not in data:
        print(f"  skip {file_name}: no {args.key!r} array (has {list(data)})")
        return None
    S = np.asarray(data[args.key])
    if S.shape[-1] != freq.size:
        print(f"  skip {file_name}: last axis of {args.key!r} has {S.shape[-1]} points, "
              f"--n-points is {freq.size}")
        return None
    if "frequencies_hz" in data:
        f_file = np.asarray(data["frequencies_hz"], dtype=float)
        if f_file.shape != freq.shape or not np.allclose(f_file, freq, rtol=0, atol=FREQ_TOL_HZ):
            print(f"  WARNING {file_name}: its frequencies_hz does not match --f-start/--f-stop/--n-points; "
                  "using the command-line grid")

    gap, outside = 0.0, False
    if cal.multi_site:
        xy = element_xy(data)
        if xy is None:
            print(f"  skip {file_name}: several calibration sites but no physical_x_mm/physical_y_mm in the file")
            return None
        gap = cal.surface.gap_at(*xy)
        outside = cal.surface.is_outside(*xy, args.extrap_mm)

    terms = cal.terms.rotated(gap_rotation(freq, gap))
    terms.T = terms.T * pad_factor(freq, args.cal_pad_db, args.cal_pad_delay_ps)
    G = terms.corrected(S)

    in_band = np.ones(freq.size, bool)
    if args.band:
        in_band = (freq >= args.band[0] * 1e9) & (freq <= args.band[1] * 1e9)
        G = np.where(in_band, G, np.nan)

    for key in OUTPUT_KEYS:
        if key in data and key != "frequencies_hz":
            print(f"  note: overwriting existing key {key!r} in output")
    out = dict(data, gamma=G, gap_mm=np.array(gap), frequencies_hz=freq, cal_depths_mm=cal.fits[cal.ref].depths,
               cal_e00=terms.e00, cal_e11=terms.e11, cal_T=terms.T)
    out_name = os.path.splitext(file_name)[0] + "_cal.npz"
    np.savez_compressed(os.path.join(args.out_dir, out_name), **out)

    mag = abs(G)
    over = np.nanmean(mag[..., in_band] > 1.0) if np.isfinite(mag).any() else np.nan
    return ElementResult(file=file_name, out=out_name, freq=freq, S=S, G=G, volt=data.get("voltages_v"),
                         gap=gap, outside=outside, max_mag=np.nanmax(mag), over=over)


def plot_result(res, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    G, f_ghz = np.atleast_2d(res.G), res.freq / 1e9
    labels = res.volt if res.volt is not None else np.arange(G.shape[0])
    fig, (ax_mag, ax_phase) = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
    for i, v in enumerate(labels):
        ax_mag.plot(f_ghz, 20 * np.log10(abs(G[i])), lw=1, label=f"{v:g} V")
        ax_phase.plot(f_ghz, np.degrees(np.angle(G[i] / G[0])), lw=1)
    ax_mag.set_ylabel("|Gamma| (dB)")
    ax_phase.set_ylabel("phase relative to first bias state (deg)")
    ax_phase.set_xlabel("Frequency (GHz)")
    ax_mag.legend(ncol=3, fontsize=8)
    ax_mag.set_title(f"{res.file}   gap offset {res.gap:+.3f} mm")
    for ax in (ax_mag, ax_phase):
        ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, os.path.splitext(res.out)[0] + ".png"), dpi=110)
    plt.close(fig)


# --------------------------------------------------------------------------- #
#  Command line
# --------------------------------------------------------------------------- #
def parse_args():
    ap = argparse.ArgumentParser(description="Copper-plate calibration for near-field reflectarray "
                                             "unit-cell measurements (see the file header for details).",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--f-start", type=float, required=True, metavar="GHZ", help="first frequency of the sweep")
    ap.add_argument("--f-stop", type=float, required=True, metavar="GHZ", help="last frequency of the sweep")
    ap.add_argument("--n-points", type=int, required=True, help="number of frequency points")
    ap.add_argument("--cal-dir", default="cal", help="folder with plate .npz files (or one sub-folder per site)")
    ap.add_argument("--data-dir", default="data", help="folder with the element .npz files")
    ap.add_argument("--out-dir", default="corrected", help="where corrected files are written")
    ap.add_argument("--standards", type=float, nargs="+", default=[0.0, 1.0, 2.0],
                    help="plate depths (mm) used to solve the error terms; other plates are validation only")
    ap.add_argument("--depth", action="append", default=[], metavar="FILE=MM",
                    help="override the depth parsed from a plate file name (repeatable)")
    ap.add_argument("--sign", type=int, choices=(1, -1), default=1,
                    help="+1: plate toward the probe advances the phase, G = -exp(+j2kd)")
    ap.add_argument("--ref-site", default=None, help="site whose full plate set defines the error terms "
                                                      "(default: full site nearest the centroid)")
    ap.add_argument("--site-xy", action="append", default=[], metavar="SITE=X,Y",
                    help="site coordinates in mm, overriding sites.csv (repeatable)")
    ap.add_argument("--surface", choices=("auto",) + tuple(SURFACE_MODELS), default="auto",
                    help="gap-surface model fitted through the sites. auto: plane for 3-4 sites, bowl "
                         "(plane + r^2) for 5, quadratic for 6 or more")
    ap.add_argument("--extrap-mm", type=float, default=5.0,
                    help="warn when an element lies this far outside the area spanned by the sites")
    ap.add_argument("--key", default="sdata", help="npz key holding the raw S11 in plate and element files "
                                                  "(last axis = frequency)")
    ap.add_argument("--band", type=float, nargs=2, metavar=("FMIN_GHZ", "FMAX_GHZ"),
                    help="set the output to NaN outside this band")
    ap.add_argument("--cal-pad-db", type=float, default=0.0, metavar="DB",
                    help="attenuation present during the plate measurements but absent from the element "
                         "scans (negative for the opposite case); rescales T by 2*DB")
    ap.add_argument("--cal-pad-delay-ps", type=float, default=0.0, metavar="PS",
                    help="one-way electrical delay of that attenuator; only changes the absolute phase "
                         "slope, not bias-to-bias differences")
    ap.add_argument("--plot", action="store_true", help="write a PNG next to each corrected file")
    return ap.parse_args()


def print_sites(sites):
    for name, site in sites.items():
        where = f" at ({site.xy[0]:.1f}, {site.xy[1]:.1f}) mm" if site.xy else ""
        print(f"  site {name!r}{where}")
        for p in sorted(site.plates, key=lambda p: p.depth_mm):
            print(f"    {p.depth_mm:6.2f} mm  {p.name}")


def save_cal_terms(cal, out_dir):
    t = cal.terms
    np.savez_compressed(
        os.path.join(out_dir, "cal_terms.npz"), frequencies_hz=cal.freq, e00=t.e00, e11=t.e11, T=t.T, ref_site=cal.ref,
        site_names=np.array(list(cal.sites)),
        site_xy=np.array([s.xy if s.xy else (np.nan, np.nan) for s in cal.sites.values()]),
        site_gap_mm=np.array([cal.fits[n].gap_mm for n in cal.sites]))


def print_summary(results, n_files, out_dir, multi_site):
    print(f"\nProcessed {len(results)} of {n_files} files -> {out_dir!r}")
    print(f"{'file':<28}{'shape':<14}" + (f"{'gap mm':>8}" if multi_site else "")
          + f"{'max |G|':>9}{'frac |G|>1':>12}")
    for r in results:
        print(f"{r.file:<28}{str(r.S.shape):<14}" + (f"{r.gap:>+8.3f}" if multi_site else "")
              + f"{r.max_mag:>9.2f}{100*r.over:>11.1f}%" + ("  (outside sites)" if r.outside else ""))

    if any(r.outside for r in results):
        print("\nNOTE: elements marked 'outside sites' use an extrapolated gap. Add a calibration site nearer to them.")
    bad = [r.file for r in results if r.over > 0.02]
    if bad:
        print("\nWARNING: more than 2% of points have |G| > 1 in: " + ", ".join(bad))
        print("A passive element should not exceed 1. Check that these files were measured at the same probe\n"
              "standoff, orientation, cabling, attenuator and VNA setup as the calibration plates.")


def main():
    args = parse_args()
    if args.n_points < 2 or args.f_stop <= args.f_start:
        sys.exit("Need --n-points >= 2 and --f-stop > --f-start")
    freq = np.linspace(args.f_start * 1e9, args.f_stop * 1e9, args.n_points)
    print(f"Frequency grid: {args.f_start:g}-{args.f_stop:g} GHz, {args.n_points} points "
          f"({(freq[1] - freq[0]) / 1e6:g} MHz step)")

    depth_overrides = {}
    for item in args.depth:
        name, mm = item.rsplit("=", 1)
        depth_overrides[name] = float(mm)

    if args.cal_pad_db:
        print(f"NOTE: --cal-pad-db {args.cal_pad_db:g} rescales T by {2*args.cal_pad_db:g} dB only. "
              "e00 and e11 are not adjusted, so fringes can remain.")

    print(f"Reading calibration plates from {args.cal_dir!r}")
    sites = load_sites(args.cal_dir, depth_overrides, args.site_xy, args.key, args.n_points)
    print_sites(sites)

    files = sorted(glob.glob(os.path.join(args.data_dir, "*.npz")))
    if not files:
        sys.exit(f"No .npz files found in {args.data_dir!r}")
    os.makedirs(args.out_dir, exist_ok=True)

    cal = build_calibration(sites, freq, args)
    report_calibration(cal, args.sign)
    save_cal_terms(cal, args.out_dir)

    results = []
    for path in files:
        result = process_element(path, cal, args)
        if result:
            results.append(result)
            if args.plot:
                plot_result(result, args.out_dir)
    print_summary(results, len(files), args.out_dir, multi_site=len(sites) > 1)


if __name__ == "__main__":
    main()
