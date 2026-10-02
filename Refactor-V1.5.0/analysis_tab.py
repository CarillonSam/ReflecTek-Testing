"""
Tab 4: Analysis. Pick a dataset folder, see a heatmap of the board, and click an element
(or enter its row and column) to see its 2x2 summary: phase and magnitude vs voltage at
the reference frequencies, and vs frequency for every voltage. The processing lives in
analysis.py; this file only handles widgets and drawing.

Loading and processing run on a background thread (a full H dataset takes several
seconds), reporting back through a queue the GUI thread polls, same as the Scan tab.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import tkinter as tk
from dataclasses import replace
from tkinter import filedialog, messagebox, ttk

import matplotlib
import matplotlib.cm
import matplotlib.colors
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import analysis
from analysis import HEATMAP_METRICS, PHASE_REFERENCES, AnalysisSettings

SELECT_COLOR = "#E24B4A"
MISSING_COLOR = "#D3D1C7"
HEATMAP_CMAP = "viridis"
VOLTAGE_CMAP = "cool"       # lines on the two vs-voltage plots (one per reference frequency)
FREQUENCY_CMAP = "viridis"  # lines on the two vs-frequency plots (one per voltage)


class AnalysisTab(ttk.Frame):
    def __init__(self, parent, settings_tab=None):
        super().__init__(parent)
        self.settings_tab = settings_tab
        self.dataset: analysis.Dataset | None = None
        self.processed: dict = {}
        self.settings = AnalysisSettings()
        self.selected: tuple[str, int, int] | None = None
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._auto_ref_text = ""  # last band-default reference list filled in automatically
        self._plot_keys: list = []
        self._plot_xy = np.zeros((0, 2))

        self._build_controls()
        self._build_plots()
        self._draw_empty()

    # ---------------------------------------------------------------- widgets ----

    def _build_controls(self) -> None:
        top = ttk.Frame(self, padding=(10, 8, 10, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Dataset folder").grid(row=0, column=0, sticky="w")
        self.folder_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.folder_var, width=50)
        entry.grid(row=0, column=1, sticky="ew", padx=6)
        entry.bind("<Return>", lambda e: self.load())
        ttk.Button(top, text="Browse...", command=self._browse).grid(row=0, column=2, padx=2)
        ttk.Button(top, text="Load", command=self.load).grid(row=0, column=3, padx=2)
        top.grid_columnconfigure(1, weight=1)

        status = ttk.Frame(self, padding=(10, 4, 10, 0))
        status.pack(fill="x")
        self.status_label = ttk.Label(status, text="Choose a dataset folder (a run folder from a scan) and click Load.",
                                      foreground="gray40")
        self.status_label.pack(side="left")
        self.progress = ttk.Progressbar(status, length=160, maximum=100)

        opts = ttk.Frame(self, padding=(10, 6, 10, 0))
        opts.pack(fill="x")
        ttk.Label(opts, text="Heatmap colour").pack(side="left")
        self.metric_var = tk.StringVar(value=next(iter(HEATMAP_METRICS)))
        metric = ttk.Combobox(opts, textvariable=self.metric_var, values=list(HEATMAP_METRICS), state="readonly", width=29)
        metric.pack(side="left", padx=(4, 8))
        metric.bind("<<ComboboxSelected>>", lambda e: self._draw_heatmap())
        ttk.Label(opts, text="at").pack(side="left")
        self.heat_freq_var = tk.StringVar()
        self.heat_freq_combo = ttk.Combobox(opts, textvariable=self.heat_freq_var, state="readonly", width=8)
        self.heat_freq_combo.pack(side="left", padx=4)
        self.heat_freq_combo.bind("<<ComboboxSelected>>", lambda e: self._draw_heatmap())
        ttk.Label(opts, text="GHz").pack(side="left", padx=(0, 12))
        ttk.Label(opts, text="Phase reference").pack(side="left")
        self.ref_var = tk.StringVar(value="Linear trend")
        ref = ttk.Combobox(opts, textvariable=self.ref_var, values=list(PHASE_REFERENCES), state="readonly", width=11)
        ref.pack(side="left", padx=4)
        ref.bind("<<ComboboxSelected>>", lambda e: self.reprocess())
        self.proc_button = ttk.Button(opts, text="Processing options", command=self._toggle_processing)
        self.proc_button.pack(side="right")

        # Processing options (collapsed by default): reference frequencies and gate settings.
        self.proc_frame = proc = ttk.Frame(self, padding=(10, 6, 10, 0))
        row1, row2 = ttk.Frame(proc), ttk.Frame(proc)
        row1.pack(fill="x"); row2.pack(fill="x", pady=(4, 0))
        proc = row1
        ttk.Label(proc, text="Reference frequencies (GHz)").pack(side="left")
        self.ref_freqs_var = tk.StringVar()
        ttk.Entry(proc, textvariable=self.ref_freqs_var, width=52).pack(side="left", padx=(4, 12))
        proc = row2
        ttk.Label(proc, text="Gate: samples before peak").pack(side="left")
        self.gate_start_var = tk.StringVar(value=str(self.settings.gate_start_samples))
        ttk.Entry(proc, textvariable=self.gate_start_var, width=4).pack(side="left", padx=(4, 6))
        ttk.Label(proc, text="after").pack(side="left")
        self.gate_stop_var = tk.StringVar(value=str(self.settings.gate_stop_samples))
        ttk.Entry(proc, textvariable=self.gate_stop_var, width=4).pack(side="left", padx=(4, 6))
        ttk.Label(proc, text="Tukey alpha").pack(side="left")
        self.gate_alpha_var = tk.StringVar(value=str(self.settings.gate_alpha))
        ttk.Entry(proc, textvariable=self.gate_alpha_var, width=5).pack(side="left", padx=(4, 8))
        ttk.Button(proc, text="Reprocess", command=self.reprocess).pack(side="left")

        self.sel_frame = sel = ttk.Frame(self, padding=(10, 6, 10, 4))
        sel.pack(fill="x")
        ttk.Label(sel, text="Element").pack(side="left")
        self.density_var = tk.StringVar(value="L")
        self.density_combo = ttk.Combobox(sel, textvariable=self.density_var, values=["L"], state="readonly", width=3)
        self.density_combo.pack(side="left", padx=(6, 8))
        ttk.Label(sel, text="Row").pack(side="left")
        self.row_var = tk.StringVar()
        row_entry = ttk.Entry(sel, textvariable=self.row_var, width=5)
        row_entry.pack(side="left", padx=(4, 8))
        ttk.Label(sel, text="Column").pack(side="left")
        self.col_var = tk.StringVar()
        col_entry = ttk.Entry(sel, textvariable=self.col_var, width=5)
        col_entry.pack(side="left", padx=(4, 8))
        for w in (row_entry, col_entry):
            w.bind("<Return>", lambda e: self._show_entered())
        ttk.Button(sel, text="Show", command=self._show_entered).pack(side="left")
        ttk.Label(sel, text="or click the heatmap (from 0)", foreground="gray40").pack(side="left", padx=8)
        ttk.Button(sel, text="Save plot...", command=lambda: self._save(self.summary_fig, "element")).pack(side="right")
        ttk.Button(sel, text="Save heatmap...", command=lambda: self._save(self.heat_fig, "heatmap")).pack(side="right", padx=4)
        self._proc_visible = False

    def _toggle_processing(self) -> None:
        if self._proc_visible:
            self.proc_frame.pack_forget()
            self.proc_button.configure(text="Processing options")
        else:
            self.proc_frame.pack(fill="x", before=self.sel_frame)
            self.proc_button.configure(text="Hide processing")
        self._proc_visible = not self._proc_visible

    def _build_plots(self) -> None:
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        left, right = ttk.Frame(panes), ttk.Frame(panes)
        panes.add(left, weight=2)
        panes.add(right, weight=3)

        # Small requested sizes: the canvases expand to whatever space the window has.
        self.heat_fig = Figure(figsize=(3, 3), dpi=100, layout="constrained")
        self.heat_ax = self.heat_fig.add_subplot(111)
        self.heat_canvas = FigureCanvasTkAgg(self.heat_fig, master=left)
        self.heat_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.heat_canvas.mpl_connect("button_press_event", self._on_heat_click)
        self.heat_canvas.get_tk_widget().bind("<Configure>", lambda e: self._draw_heatmap(), add="+")
        self._colorbar = None

        self.summary_fig = Figure(figsize=(4, 3), dpi=100, layout="constrained")
        self.summary_canvas = FigureCanvasTkAgg(self.summary_fig, master=right)
        self.summary_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.summary_canvas.get_tk_widget().bind(
            "<Configure>", lambda e: self._show(self.selected) if self.selected in self.processed else None, add="+")

    # ---------------------------------------------------------------- dataset ----

    def on_shown(self) -> None:
        """Called when the tab is selected: suggests the current run's folder if the
        folder box is still empty."""
        if self.folder_var.get().strip() or self.settings_tab is None:
            return
        try:
            save = self.settings_tab.get_config(require_save=False).save
            self.folder_var.set(str(save.output_dir / save.run_name))
        except Exception:
            pass

    def _browse(self) -> None:
        start = self.folder_var.get().strip()
        path = filedialog.askdirectory(initialdir=start if os.path.isdir(start) else None)
        if path:
            self.folder_var.set(path)
            self.load()

    def _settings_from_form(self) -> AnalysisSettings | None:
        try:
            ref_text = self.ref_freqs_var.get().strip()
            refs = [float(v) for v in ref_text.replace(";", ",").split(",") if v.strip()]
            return replace(
                self.settings,
                gate_start_samples=int(self.gate_start_var.get()),
                gate_stop_samples=int(self.gate_stop_var.get()),
                gate_alpha=float(self.gate_alpha_var.get()),
                phase_reference=PHASE_REFERENCES[self.ref_var.get()],
                ref_freqs_ghz=[] if ref_text == self._auto_ref_text else refs,
            )
        except ValueError:
            messagebox.showerror("Invalid analysis settings",
                                 "Reference frequencies must be numbers separated by commas, gate samples whole "
                                 "numbers, and alpha a number between 0 and 1.")
            return None

    def load(self) -> None:
        folder = self.folder_var.get().strip()
        if not folder:
            messagebox.showerror("No folder", "Enter or browse to a dataset folder first.")
            return
        settings = self._settings_from_form()
        if settings is not None:
            self._start(lambda progress: self._work_load(folder, settings, progress), f"Loading {folder}...")

    def reprocess(self) -> None:
        if self.dataset is None:
            return
        settings = self._settings_from_form()
        if settings is not None:
            ds = self.dataset
            self._start(lambda progress: (ds, analysis.process_dataset(ds, settings, progress), settings),
                        "Reprocessing...")

    def _work_load(self, folder, settings, progress):
        ds = analysis.load_dataset(folder, settings)
        for line in ds.skipped:
            logging.warning("Analysis: skipped %s", line)
        return ds, analysis.process_dataset(ds, settings, progress), settings

    def _start(self, work, message: str) -> None:
        if self._busy:
            return
        self._busy = True
        self.status_label.configure(text=message)
        self.progress.configure(value=0)
        self.progress.pack(side="left", padx=10)

        def progress(done, total):
            self._queue.put(("progress", 100.0 * done / max(total, 1)))

        def worker():
            try:
                self._queue.put(("done", work(progress)))
            except Exception as e:
                logging.exception("Analysis failed")
                self._queue.put(("error", f"{type(e).__name__}: {e}"))

        threading.Thread(target=worker, daemon=True).start()
        self.after(100, self._poll)

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "progress":
                    self.progress.configure(value=payload)
                    continue
                self._busy = False
                self.progress.pack_forget()
                if kind == "error":
                    self.status_label.configure(text="Load failed.")
                    messagebox.showerror("Analysis failed", payload)
                else:
                    self._loaded(*payload)
                return
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _loaded(self, ds: analysis.Dataset, processed: dict, settings: AnalysisSettings) -> None:
        new_dataset = ds is not self.dataset
        self.dataset, self.processed, self.settings = ds, processed, settings
        self.status_label.configure(text=ds.describe())
        if not processed:
            self._draw_empty("No usable element files in this folder.")
            return
        refs = next(iter(processed.values())).ref_freqs_ghz
        ref_text = ", ".join(f"{f:g}" for f in refs)
        if not settings.ref_freqs_ghz:  # band default in use: show it, and remember it's automatic
            self.ref_freqs_var.set(ref_text)
            self._auto_ref_text = ref_text
        choices = [f"{f:g}" for f in refs]
        self.heat_freq_combo.configure(values=choices)
        if self.heat_freq_var.get() not in choices:
            self.heat_freq_var.set("18.5" if "18.5" in choices else choices[len(choices) // 2])
        self.density_combo.configure(values=ds.densities)
        if new_dataset or self.selected not in processed:
            self.density_var.set(ds.densities[0])
            self.selected = min(k for k in processed if k[0] == ds.densities[0])
        self._draw_heatmap()
        self._show(self.selected)

    # ---------------------------------------------------------------- heatmap ----

    def _draw_empty(self, message: str = "No dataset loaded") -> None:
        for fig in (self.heat_fig, self.summary_fig):
            fig.clear()
            ax = fig.add_subplot(111)
            ax.axis("off")
            ax.text(0.5, 0.5, message, ha="center", va="center", color="gray")
        self.heat_ax = self.heat_fig.axes[0]
        self._colorbar = None
        self.heat_canvas.draw_idle()
        self.summary_canvas.draw_idle()

    def _positions(self) -> tuple[list, np.ndarray, bool]:
        keys = sorted(self.processed)
        els = self.dataset.elements
        if all(els[k].position_mm is not None for k in keys):
            return keys, np.array([els[k].position_mm for k in keys]), True
        # Legacy files without stage positions: fall back to a column/row grid.
        return keys, np.array([(k[2], -k[1]) for k in keys], dtype=float), False

    def _draw_heatmap(self) -> None:
        if not self.processed or not self.heat_freq_var.get():
            return
        keys, xy, physical = self._positions()
        metric = HEATMAP_METRICS[self.metric_var.get()]
        freq = float(self.heat_freq_var.get())
        values = np.array([analysis.heatmap_value(self.processed[k], metric, freq) for k in keys])
        self._plot_keys, self._plot_xy = keys, xy

        _fit(self.heat_canvas)
        self.heat_fig.clear()
        ax = self.heat_ax = self.heat_fig.add_subplot(111)
        spacing = _spacing(xy)
        pad = spacing
        ax.set_xlim(xy[:, 0].min() - pad, xy[:, 0].max() + pad)
        ax.set_ylim(xy[:, 1].min() - pad, xy[:, 1].max() + pad)
        ax.set_aspect("equal")
        good = ~np.isnan(values)
        scatters = []
        if (~good).any():
            scatters.append(ax.scatter(xy[~good, 0], xy[~good, 1], c=MISSING_COLOR, marker="H", linewidths=0))
        sc = ax.scatter(xy[good, 0], xy[good, 1], c=values[good], cmap=HEATMAP_CMAP, marker="H", linewidths=0)
        scatters.append(sc)
        self._colorbar = self.heat_fig.colorbar(sc, ax=ax, orientation="horizontal", shrink=0.9, aspect=30)
        self._colorbar.ax.tick_params(labelsize=8)
        ax.set_title(f"{self.metric_var.get()}\nat {freq:g} GHz", fontsize=9)
        ax.set_xlabel("Stage X (mm)" if physical else "Column", fontsize=8)
        ax.set_ylabel("Stage Y (mm)" if physical else "Row (negated)", fontsize=8)
        ax.tick_params(labelsize=8)
        ring = None
        if self.selected in self.processed:
            i = keys.index(self.selected)
            ring = ax.scatter([xy[i, 0]], [xy[i, 1]], facecolors="none", edgecolors=SELECT_COLOR, linewidths=1.8)
        # Hexagon size from the real element spacing, measured once the layout is final,
        # so they roughly tile at any window size.
        self.heat_fig.canvas.draw()
        px = abs(ax.transData.transform((spacing, 0))[0] - ax.transData.transform((0, 0))[0])
        size = max(px / self.heat_fig.dpi * 72 * 0.95, 1.0) ** 2
        for s in scatters:
            s.set_sizes([size])
        if ring is not None:
            ring.set_sizes([size * 2.2])
        self.heat_canvas.draw_idle()

    def _on_heat_click(self, event) -> None:
        if event.inaxes is not self.heat_ax or event.xdata is None or not self._plot_keys:
            return
        d = np.hypot(self._plot_xy[:, 0] - event.xdata, self._plot_xy[:, 1] - event.ydata)
        i = int(np.argmin(d))
        if d[i] <= _spacing(self._plot_xy) * 0.75:
            self._select(self._plot_keys[i])

    # ---------------------------------------------------------------- element ----

    def _show_entered(self) -> None:
        if not self.processed:
            messagebox.showerror("No dataset", "Load a dataset first.")
            return
        try:
            key = (self.density_var.get(), int(self.row_var.get()), int(self.col_var.get()))
        except ValueError:
            messagebox.showerror("Invalid element", "Row and column must be whole numbers.")
            return
        if key not in self.processed:
            rows = sorted({k[1] for k in self.processed if k[0] == key[0]})
            cols = sorted({k[2] for k in self.processed if k[0] == key[0]})
            messagebox.showerror(
                "Element not found",
                f"No {key[0]} element at row {key[1]}, column {key[2]} in this dataset.\n\n"
                f"{key[0]} rows here run {rows[0]}-{rows[-1]} and columns {cols[0]}-{cols[-1]}." if rows else
                f"This dataset has no {key[0]} elements.",
            )
            return
        self._select(key)

    def _select(self, key) -> None:
        self.selected = key
        self.density_var.set(key[0])
        self.row_var.set(str(key[1]))
        self.col_var.set(str(key[2]))
        self._draw_heatmap()
        self._show(key)

    def _show(self, key) -> None:
        p = self.processed[key]
        el = self.dataset.elements[key]
        fig = self.summary_fig
        _fit(self.summary_canvas)
        fig.clear()
        axes = fig.subplots(2, 2)
        _draw_vs_voltage(axes[0, 0], p, phase=True, ylabel=_phase_label(self.settings))
        _draw_vs_voltage(axes[0, 1], p, phase=False, ylabel="Magnitude (dB)")
        _draw_vs_frequency(axes[1, 0], p, p.phase_rel, _phase_label(self.settings), "Phase vs. frequency")
        _draw_vs_frequency(axes[1, 1], p, p.gated_mag_db, "Gated magnitude (dB)", "Magnitude vs. frequency")
        axes[1, 1].set_ylim(-50, 1)  # as the original script
        fig.suptitle(f"{key[0]} element, row {key[1]}, column {key[2]}  ({el.file_name})", fontsize=10)
        self.summary_canvas.draw_idle()

    def _save(self, fig, kind: str) -> None:
        if not self.processed:
            return
        default = f"{kind}.png" if kind == "heatmap" or self.selected is None else \
            f"{self.selected[0]}_R{self.selected[1]:03d}_C{self.selected[2]:03d}_summary.png"
        path = filedialog.asksaveasfilename(defaultextension=".png", initialfile=default,
                                            filetypes=[("PNG image", "*.png"), ("PDF", "*.pdf")])
        if path:
            fig.savefig(path, dpi=200, bbox_inches="tight")


# ---------------------------------------------------------------- drawing helpers ----

def _fit(canvas) -> None:
    """Sizes the figure to its widget's actual pixel size. Matplotlib can change the
    figure's DPI after the window appears (to match display scaling, e.g. 125% on a
    Windows laptop) while keeping its size in inches, which makes the figure render
    wider than its widget and clips the right-hand edge."""
    w, h = canvas.get_tk_widget().winfo_width(), canvas.get_tk_widget().winfo_height()
    if w > 1 and h > 1:
        fig = canvas.figure
        fig.set_size_inches(w / fig.dpi, h / fig.dpi, forward=False)


def _spacing(xy: np.ndarray) -> float:
    """Typical nearest-neighbour distance between plotted elements."""
    if len(xy) < 2:
        return 1.0
    sample = xy[:: max(1, len(xy) // 200)]
    d = np.hypot(sample[:, None, 0] - xy[None, :, 0], sample[:, None, 1] - xy[None, :, 1])
    d[d < 1e-9] = np.inf
    return float(np.median(d.min(axis=1)))


def _phase_label(settings: AnalysisSettings) -> str:
    if settings.phase_reference == "linear_trend":
        return "Phase (deg, rel. to linear trend)"
    return "Phase (deg, rel. to first voltage)"


MAX_LEGEND_ENTRIES = 10  # more lines than this get a colour bar instead of a legend


def _line_colors(values, cmap_name: str):
    """Colours for one line per value. Up to MAX_LEGEND_ENTRIES lines: spread evenly through
    the colormap (most distinct, as the original script). More than that: by each line's
    actual value, so a colour bar can show what the colours mean."""
    values = np.asarray(values, dtype=float)
    cmap = matplotlib.colormaps[cmap_name]
    if len(values) <= MAX_LEGEND_ENTRIES:
        return cmap(np.linspace(0, 1, len(values))), None
    norm = matplotlib.colors.Normalize(vmin=values.min(), vmax=values.max())
    return cmap(norm(values)), matplotlib.cm.ScalarMappable(norm=norm, cmap=cmap)


def _key(ax, mappable, title: str) -> None:
    """Legend outside the plot (to its right), or a colour bar if there are too many lines."""
    if mappable is None:
        ax.legend(title=title, fontsize=6, title_fontsize=7, loc="upper left",
                  bbox_to_anchor=(1.02, 1.0), borderaxespad=0.0, frameon=False)
    else:
        # Attached to the plot itself (an inset just right of it), so it sits next to its
        # own plot rather than lining up with the legends in the other row.
        bar = ax.figure.colorbar(mappable, cax=ax.inset_axes([1.03, 0.0, 0.04, 1.0]))
        bar.set_label(title, fontsize=7)
        bar.ax.tick_params(labelsize=6)


def _draw_vs_voltage(ax, p, phase: bool, ylabel: str) -> None:
    colors, mappable = _line_colors(p.ref_freqs_ghz, VOLTAGE_CMAP)
    for color, f, idx in zip(colors, p.ref_freqs_ghz, p.ref_indices):
        y = p.phase_at(idx) if phase else p.mag_db_at(idx)
        ax.plot(p.voltages, y, marker="o", ms=3, lw=1.2, color=color, label=f"{f:g} GHz")
    ax.set_xlabel("Voltage (V)", fontsize=8)
    ax.set_ylabel(ylabel, fontsize=8)
    ax.set_title("Phase vs. voltage" if phase else "Magnitude vs. voltage", fontsize=9)
    _key(ax, mappable, "Frequency (GHz)" if mappable is not None else "Frequency")
    _style(ax)


def _draw_vs_frequency(ax, p, data, ylabel: str, title: str) -> None:
    colors, mappable = _line_colors(p.voltages, FREQUENCY_CMAP)
    for color, v, row in zip(colors, p.voltages, data):
        ax.plot(p.freqs_ghz, row, lw=1.0, color=color, label=f"{v:g} V")
    ax.set_xlim(p.freqs_ghz[0], p.freqs_ghz[-1])
    ax.set_xlabel("Frequency (GHz)", fontsize=8)
    ax.set_ylabel(ylabel, fontsize=8)
    ax.set_title(title, fontsize=9)
    _key(ax, mappable, "Voltage (V)" if mappable is not None else "Voltage")
    _style(ax)


def _style(ax) -> None:
    ax.ticklabel_format(axis="y", useOffset=False)  # show real values, not "+6.02" offsets
    ax.tick_params(labelsize=7)
    ax.minorticks_on()
    ax.grid(True, which="major", alpha=0.5)
    ax.grid(True, which="minor", alpha=0.15)
