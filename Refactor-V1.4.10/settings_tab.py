"""
Settings & Configuration tab, in two parts:

- General settings: what an operator changes run to run. That's grid size, basic
  geometry, voltages, settle time, board controller, and RF band. Choosing a band loads
  that band's default VNA sweep (config.BAND_VNA_DEFAULTS).
- Advanced settings (collapsible): every other field in config.py, labelled with its
  real variable name (stage.port, vna.start_hz, ...) and generated straight from the
  config dataclasses, so a field added to config.py shows up here automatically.

Nothing in RunConfig is hidden any more, apart from pi.active_band, which is always
derived from RunConfig.band (the General section's RF band).
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import fields, replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from config import (
    BAND_VNA_DEFAULTS, PiControllerConfig, PixelControllerConfig, RunConfig, SaveConfig,
    ScanGeometryConfig, StageConfig, VNAConfig,
)
from config_io import load_config, save_config

BAND_LABELS = {"lb": "Low band (17-21 GHz)", "hb": "High band (26-30 GHz)"}
BAND_FROM_LABEL = {v: k for k, v in BAND_LABELS.items()}

# Fields owned by the General section (or derived), so Advanced skips them.
GENERAL_RUN_FIELDS = {"voltages_v", "controller_type", "band", "uniform_board_settle_s"}
GENERAL_GEOMETRY_FIELDS = {"rows", "cols", "spacing_mm", "density_mode", "l_subgrid", "stagger_sign"}
GENERAL_STAGE_FIELDS = {"calibration_file"}
GENERAL_PIXEL_FIELDS = {"mapping_csv_l", "mapping_csv_h"}
GENERAL_SAVE_FIELDS = {"output_dir", "run_name"}
SUBCONFIG_FIELDS = {"stage", "pixels", "pi", "vna", "geometry", "save"}

# (section title, attribute prefix on RunConfig ("" = top level), dataclass, fields to skip)
ADVANCED_GROUPS = [
    ("Run", "", RunConfig, SUBCONFIG_FIELDS | GENERAL_RUN_FIELDS),
    ("Motor stage", "stage", StageConfig, GENERAL_STAGE_FIELDS),
    ("Pixel controller", "pixels", PixelControllerConfig, GENERAL_PIXEL_FIELDS),
    ("Pi controller", "pi", PiControllerConfig, {"active_band"}),
    ("VNA", "vna", VNAConfig, set()),
    ("Scan geometry", "geometry", ScanGeometryConfig, GENERAL_GEOMETRY_FIELDS),
    ("Save output", "save", SaveConfig, GENERAL_SAVE_FIELDS),
]

# Dataclass annotation (a string, thanks to `from __future__ import annotations`) -> kind.
KIND_FROM_ANNOTATION = {
    "str": "str", "int": "int", "float": "float", "bool": "bool", "Path": "path",
    "str | None": "str_opt", "float | None": "float_opt", "Path | None": "path_opt",
    "tuple[int, int]": "int_pair",
}
KIND_HINTS = {
    "str": "text", "int": "whole number", "float": "number", "bool": "",
    "path": "path", "str_opt": "text, blank = None", "float_opt": "number, blank = None",
    "path_opt": "path, blank = None", "int_pair": "rows, cols", "voltages": "list of numbers",
}
# Friendly names for General fields in validation messages (Advanced uses the variable name).
GENERAL_LABELS = {
    "voltages_v": "Voltages", "uniform_board_settle_s": "Settle time",
    "geometry.rows": "Rows", "geometry.cols": "Columns", "geometry.spacing_mm": "Full-grid spacing",
    "save.output_dir": "Output directory", "save.run_name": "Run name",
    "stage.calibration_file": "Stage calibration file",
    "pixels.mapping_csv_l": "L mapping CSV", "pixels.mapping_csv_h": "H mapping CSV",
}
# Advanced geometry fields that change the plotted points (serpentine only changes visit order).
PREVIEW_FIELDS = {"geometry.row_spacing_mm", "geometry.offset_odd_rows",
                  "geometry.x_direction_sign", "geometry.y_direction_sign"}


# Shared column widths so entries line up across General and every Advanced group.
LABEL_COL_PX = 240
BROWSE_COL_PX = 96
ENTRY_WIDTH_CHARS = 34


def _align_columns(frame, label_px: int = LABEL_COL_PX) -> None:
    frame.grid_columnconfigure(0, minsize=label_px)
    frame.grid_columnconfigure(2, minsize=BROWSE_COL_PX)


def _fmt(kind: str, value) -> str | bool:
    if kind == "bool":
        return bool(value)
    if value is None:
        return ""
    if kind == "int_pair":
        return ", ".join(str(v) for v in value)
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))  # 17000000000 rather than 17000000000.0
    return str(value)


class ScrollableFrame(ttk.Frame):
    """A vertically scrollable frame. Put child widgets in .body."""

    def __init__(self, parent):
        super().__init__(parent)
        canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.body = ttk.Frame(canvas)

        self.body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=self.body, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))


class SettingsTab(ttk.Frame):
    """
    Tab 1: settings & configuration.
    get_config() builds a full RunConfig from every widget (General + Advanced) and
    raises ValueError listing every problem at once. set_config(config) populates every
    widget. These are the integration points for the Scan tab, the Debug tab, and presets.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.vars: dict[str, tk.Variable] = {}
        self.kinds: dict[str, str] = {}
        self._template = RunConfig()

        # Called (no args) whenever a field that affects the plotted points changes —
        # the Scan tab's live preview hooks into this. None until gui_app.py wires it up.
        self.on_geometry_change: Callable[[], None] | None = None

        scroller = ScrollableFrame(self)
        scroller.pack(fill="both", expand=True)
        body = scroller.body

        self._build_general(body)

        self.advanced_button = ttk.Button(body, text="Show advanced settings", command=self._toggle_advanced)
        self.advanced_button.pack(anchor="w", padx=10, pady=(4, 0))
        self.advanced_frame = ttk.LabelFrame(body, text="Advanced settings", padding=10)
        self._build_advanced(self.advanced_frame)
        self._advanced_visible = False

        self.actions_frame = self._build_actions(body)

        self.set_config(self._template)

    # ---------------------------------------------------------------- widget helpers ----

    def _add_field(self, parent, row: int, label: str, key: str, kind: str, *,
                   hint: str | None = None, on_change: Callable[[], None] | None = None,
                   width: int = ENTRY_WIDTH_CHARS) -> list:
        """Adds one labelled row; returns its widgets (so a row can be hidden/shown later)."""
        widgets = [ttk.Label(parent, text=label)]
        widgets[0].grid(row=row, column=0, sticky="w", padx=4, pady=3)
        self.kinds[key] = kind
        if kind == "bool":
            var: tk.Variable = tk.BooleanVar(value=False)
            widgets.append(ttk.Checkbutton(parent, variable=var, command=on_change))
            widgets[-1].grid(row=row, column=1, sticky="w", padx=4, pady=3)
        else:
            var = tk.StringVar(value="")
            entry = ttk.Entry(parent, textvariable=var, width=width, show="*" if key == "pi.password" else "")
            entry.grid(row=row, column=1, sticky="w", padx=4, pady=3)
            widgets.append(entry)
            if on_change is not None:
                entry.bind("<KeyRelease>", lambda e: on_change())
            if kind in ("path", "path_opt"):
                widgets.append(ttk.Button(parent, text="Browse...", command=lambda: self._browse(key)))
                widgets[-1].grid(row=row, column=2, padx=4)
        if hint:
            widgets.append(ttk.Label(parent, text=hint, foreground="gray50"))
            widgets[-1].grid(row=row, column=3, sticky="w", padx=4)
        self.vars[key] = var
        return widgets

    def _add_choice(self, parent, row: int, label: str, var: tk.StringVar, values: list[str],
                    on_change: Callable[[], None] | None) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=3)
        combo = ttk.Combobox(parent, textvariable=var, state="readonly", values=values, width=ENTRY_WIDTH_CHARS - 2)
        combo.grid(row=row, column=1, sticky="w", padx=4, pady=3)
        if on_change is not None:
            combo.bind("<<ComboboxSelected>>", lambda e: on_change())

    def _browse(self, key: str) -> None:
        if key == "save.output_dir":
            path = filedialog.askdirectory()
        elif key == "stage.calibration_file":
            # May not exist yet (it's where calibration will be written), so a save dialog.
            path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")],
                                                confirmoverwrite=False)
        else:
            path = filedialog.askopenfilename()
        if path:
            self.vars[key].set(path)

    # ---------------------------------------------------------------- general ----

    def _build_general(self, parent) -> None:
        frame = ttk.LabelFrame(parent, text="General settings", padding=10)
        frame.pack(fill="x", padx=10, pady=6)
        # Advanced groups sit one frame deeper (~10 px more inset), so widen this label column to match.
        _align_columns(frame, LABEL_COL_PX + 10)
        notify = self._notify_geometry_change
        r = 0

        self.controller_type_var = tk.StringVar(value="pixel")
        self._add_choice(frame, r, "Board controller", self.controller_type_var, ["pixel", "pi"],
                         self._on_controller_type_changed); r += 1

        self.band_var = tk.StringVar(value=BAND_LABELS["lb"])
        self._add_choice(frame, r, "RF band", self.band_var, list(BAND_LABELS.values()), self._on_band_changed); r += 1
        self.vna_summary = ttk.Label(frame, text="", foreground="gray50")
        self.vna_summary.grid(row=r, column=1, columnspan=3, sticky="w", padx=4, pady=(0, 6)); r += 1

        self._add_field(frame, r, "Voltages (V)", "voltages_v", "voltages", hint="comma-separated"); r += 1
        self._add_field(frame, r, "Settle time (s)", "uniform_board_settle_s", "float",
                        hint="wait after setting voltages"); r += 1

        ttk.Separator(frame).grid(row=r, column=0, columnspan=4, sticky="ew", pady=8); r += 1

        self._add_field(frame, r, "Rows (per sub-grid)", "geometry.rows", "int", on_change=notify); r += 1
        self._add_field(frame, r, "Columns (per sub-grid)", "geometry.cols", "int", on_change=notify); r += 1
        self._add_field(frame, r, "Full-grid spacing (mm)", "geometry.spacing_mm", "float",
                        hint="nearest-neighbour spacing", on_change=notify); r += 1

        self.density_mode_var = tk.StringVar(value="L")
        self._add_choice(frame, r, "Density to scan", self.density_mode_var, ["L", "H"], notify); r += 1
        self.l_subgrid_var = tk.StringVar(value="1")
        self._add_choice(frame, r, "L sub-grid", self.l_subgrid_var, ["1", "2", "3"], notify); r += 1
        self._add_field(frame, r, "Mirror stagger direction", "geo_stagger_mirror", "bool", on_change=notify); r += 1

        ttk.Separator(frame).grid(row=r, column=0, columnspan=4, sticky="ew", pady=8); r += 1

        self._add_field(frame, r, "Output directory", "save.output_dir", "path"); r += 1
        self._add_field(frame, r, "Run name", "save.run_name", "str", hint="subfolder for this run"); r += 1
        self._add_field(frame, r, "Stage calibration file", "stage.calibration_file", "path_opt",
                        hint="blank = uncalibrated"); r += 1
        # Only the pixel controller uses PC-side mapping files; hidden when "pi" is selected.
        self._pixel_mapping_rows = [
            self._add_field(frame, r, "L mapping CSV", "pixels.mapping_csv_l", "path_opt", hint="pixel controller only"),
            self._add_field(frame, r + 1, "H mapping CSV", "pixels.mapping_csv_h", "path_opt", hint="pixel controller only"),
        ]
        r += 2

    def _on_band_changed(self) -> None:
        """Loads the selected band's default sweep into the Advanced VNA fields."""
        band = BAND_FROM_LABEL[self.band_var.get()]
        start_hz, stop_hz, points = BAND_VNA_DEFAULTS[band]
        self.vars["vna.start_hz"].set(_fmt("float", start_hz))
        self.vars["vna.stop_hz"].set(_fmt("float", stop_hz))
        self.vars["vna.points"].set(_fmt("int", points))
        self._update_vna_summary()

    def _update_vna_summary(self) -> None:
        try:
            start = float(self.vars["vna.start_hz"].get()) / 1e9
            stop = float(self.vars["vna.stop_hz"].get()) / 1e9
            points = int(self.vars["vna.points"].get())
            text = f"VNA sweep: {start:g}-{stop:g} GHz, {points} points (edit under Advanced > VNA)"
        except ValueError:
            text = "VNA sweep: check the values under Advanced > VNA"
        self.vna_summary.configure(text=text)

    # ---------------------------------------------------------------- advanced ----

    def _build_advanced(self, parent) -> None:
        self._group_frames: dict[str, ttk.LabelFrame] = {}
        for title, prefix, cls, skip in ADVANCED_GROUPS:
            group = ttk.LabelFrame(parent, text=title, padding=8)
            group.pack(fill="x", pady=4)
            _align_columns(group)
            self._group_frames[prefix] = group
            r = 0
            for f in fields(cls):
                if f.name in skip:
                    continue
                kind = KIND_FROM_ANNOTATION.get(f.type)
                if kind is None:
                    raise TypeError(f"settings_tab has no widget for {cls.__name__}.{f.name}: {f.type!r}")
                key = f"{prefix}.{f.name}" if prefix else f.name
                on_change = None
                if key in PREVIEW_FIELDS:
                    on_change = self._notify_geometry_change
                elif key.startswith("vna."):
                    on_change = self._update_vna_summary
                self._add_field(group, r, key, key, kind, hint=KIND_HINTS[kind], on_change=on_change)
                r += 1

    def _toggle_advanced(self) -> None:
        if self._advanced_visible:
            self.advanced_frame.pack_forget()
            self.advanced_button.configure(text="Show advanced settings")
        else:
            self.advanced_frame.pack(fill="x", padx=10, pady=6, before=self.actions_frame)
            self.advanced_button.configure(text="Hide advanced settings")
        self._advanced_visible = not self._advanced_visible

    def _on_controller_type_changed(self) -> None:
        """Only the selected controller's settings are shown (General mapping CSVs and the
        Advanced controller group)."""
        use_pi = self.controller_type_var.get() == "pi"
        for row_widgets in self._pixel_mapping_rows:
            for w in row_widgets:
                w.grid_remove() if use_pi else w.grid()
        shown, hidden = ("pi", "pixels") if use_pi else ("pixels", "pi")
        self._group_frames[hidden].pack_forget()
        self._group_frames[shown].pack(fill="x", pady=4, after=self._group_frames["stage"])

    # ---------------------------------------------------------------- actions ----

    def _build_actions(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=10)
        frame.pack(fill="x", padx=10, pady=10)
        ttk.Button(frame, text="Validate Settings", command=self._on_validate).pack(side="left", padx=4)
        ttk.Button(frame, text="Save Settings...", command=self._on_save).pack(side="left", padx=4)
        ttk.Button(frame, text="Load Settings...", command=self._on_load).pack(side="left", padx=4)
        ttk.Button(frame, text="Reset to Defaults", command=lambda: self.set_config(RunConfig())).pack(side="left", padx=4)
        ttk.Button(frame, text="Calibrate Stage...", command=self._on_calibrate).pack(side="left", padx=4)
        return frame

    def _on_validate(self) -> None:
        try:
            self.get_config()
        except ValueError as e:
            messagebox.showerror("Invalid settings", str(e))
            return
        messagebox.showinfo("Settings valid", "All settings look valid.")

    def _on_save(self) -> None:
        try:
            config = self.get_config()
        except ValueError as e:
            messagebox.showerror("Invalid settings", f"Fix these before saving:\n\n{e}")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            save_config(config, path)
        except (OSError, TypeError, ValueError) as e:
            messagebox.showerror("Save failed", str(e))
            return
        messagebox.showinfo("Saved", f"Settings saved to {path}")

    def _on_load(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            config = load_config(path)
        except (OSError, ValueError, KeyError, TypeError) as e:
            messagebox.showerror("Load failed", f"Could not load {path}:\n\n{e}")
            return
        self.set_config(config)
        messagebox.showinfo("Loaded", f"Settings loaded from {path}")

    def _on_calibrate(self) -> None:
        """
        Runs the interactive nudge-to-element calibration (calibrate_stage.calibrate)
        against real hardware, synchronously on the GUI thread — a short,
        human-in-the-loop sequence (a handful of dialogs), not a long-running scan.
        Uses the current stage settings and geometry from the form, and saves the
        result to stage.calibration_file.
        """
        try:
            config = self.get_config(require_save=False)
        except ValueError as e:
            messagebox.showerror("Invalid settings", f"Fix these before calibrating:\n\n{e}")
            return
        cal_path = config.stage.calibration_file
        if cal_path is None:
            messagebox.showerror(
                "Calibration file required",
                "Set the stage calibration file in General settings first (e.g. stage_calibration.json) "
                "so the result has somewhere to save.",
            )
            return

        from calibrate_stage import calibrate
        from stage import CalibrationTargets, GrblXY, HexGridPlanner

        try:
            targets = HexGridPlanner(config.geometry).calibration_targets()
        except (StopIteration, ValueError) as e:
            messagebox.showerror("Invalid geometry", f"Couldn't pick calibration targets from this geometry: {e}")
            return

        if not messagebox.askyesno(
            "Calibrate stage",
            "This homes the stage and walks through the nudge-to-element calibration "
            "on the real hardware, with a dialog at each step. You'll line up on:\n\n"
            f"  Origin: {CalibrationTargets.describe(targets.origin)}\n"
            f"  Far Y:  {CalibrationTargets.describe(targets.y_corner)}\n"
            f"  Far X:  {CalibrationTargets.describe(targets.x_corner)}\n\n"
            "Continue?",
        ):
            return

        try:
            stage = GrblXY(config.stage)
        except Exception as e:
            messagebox.showerror("Could not connect to stage", str(e))
            return

        try:
            calibration = calibrate(stage, targets, parent=self)
        except Exception as e:
            messagebox.showerror("Calibration failed", str(e))
            return
        finally:
            try:
                stage.goto_xy(0.0, 0.0)
            except Exception:
                pass
            stage.close()

        calibration.save(cal_path)
        messagebox.showinfo("Calibration saved", f"Saved to {cal_path}:\n\n{calibration.to_dict()}")

    # ---------------------------------------------------------------- form -> config ----

    def _parse(self, key: str, label: str, errors: list[str]):
        kind = self.kinds[key]
        var = self.vars[key]
        if kind == "bool":
            return bool(var.get())
        raw = str(var.get()).strip()
        try:
            if kind == "str":
                return raw
            if kind in ("str_opt", "float_opt", "path_opt") and raw == "":
                return None
            if kind == "str_opt":
                return raw
            if kind == "int":
                return int(raw)
            if kind in ("float", "float_opt"):
                return float(raw)
            if kind in ("path", "path_opt"):
                return Path(raw)
            if kind == "int_pair":
                parts = [p for p in raw.strip("[]() ").split(",") if p.strip()]
                if len(parts) != 2:
                    raise ValueError
                return (int(parts[0]), int(parts[1]))
            if kind == "voltages":
                values = tuple(float(p) for p in raw.split(",") if p.strip())
                if not values:
                    errors.append(f"{label}: enter at least one value")
                return values or (0.0,)
        except ValueError:
            errors.append(f"{label}: '{raw}' isn't a valid {KIND_HINTS.get(kind, 'value')}")
            return None
        raise AssertionError(f"unhandled kind {kind}")

    def _collect(self, prefix: str, cls, errors: list[str]) -> dict:
        """Parsed values for every widget under `prefix` (Advanced and General alike).
        Fields that fail to parse are left out (the error is recorded instead)."""
        out = {}
        for f in fields(cls):
            key = f"{prefix}.{f.name}" if prefix else f.name
            if key not in self.vars:
                continue
            n_errors = len(errors)
            value = self._parse(key, GENERAL_LABELS.get(key, key), errors)
            if len(errors) == n_errors:
                out[f.name] = value
        return out

    def _geometry_from_form(self, errors: list[str]) -> ScanGeometryConfig:
        values = self._collect("geometry", ScanGeometryConfig, errors)
        geometry = replace(
            self._template.geometry, **values,
            stagger_sign=-1.0 if bool(self.vars["geo_stagger_mirror"].get()) else 1.0,
            density_mode=self.density_mode_var.get(),
            l_subgrid=int(self.l_subgrid_var.get()),
        )
        if geometry.rows < 1 or geometry.cols < 1:
            errors.append("Rows and columns must be at least 1")
        if geometry.spacing_mm <= 0:
            errors.append("Full-grid spacing must be greater than 0")
        if geometry.row_spacing_mm is not None and geometry.row_spacing_mm <= 0:
            errors.append("geometry.row_spacing_mm must be greater than 0 (or blank)")
        return geometry

    def get_geometry_or_none(self) -> ScanGeometryConfig | None:
        """The form's current geometry, or None if anything in it doesn't parse yet
        (e.g. mid-keystroke) — for the Scan tab's live preview."""
        errors: list[str] = []
        geometry = self._geometry_from_form(errors)
        return None if errors else geometry

    def get_config(self, require_save: bool = True) -> RunConfig:
        """
        Builds a RunConfig from every widget. Raises ValueError listing every problem
        found, not just the first. require_save=False skips the "output directory /
        run name required" checks, for actions that don't save data (Debug tab moves,
        stage calibration), so a blank run name can't block them.
        """
        errors: list[str] = []
        t = self._template

        stage = replace(t.stage, **self._collect("stage", StageConfig, errors))
        pixels = replace(t.pixels, **self._collect("pixels", PixelControllerConfig, errors))
        pi = replace(t.pi, **self._collect("pi", PiControllerConfig, errors))
        vna = replace(t.vna, **self._collect("vna", VNAConfig, errors))
        geometry = self._geometry_from_form(errors)
        save = replace(t.save, **self._collect("save", SaveConfig, errors))
        top = self._collect("", RunConfig, errors)  # Advanced "Run" group + the General fields

        if vna.points < 2:
            errors.append("vna.points must be at least 2")
        if vna.start_hz >= vna.stop_hz:
            errors.append("vna.start_hz must be below vna.stop_hz")
        if require_save:
            if str(save.output_dir).strip() in ("", "."):
                errors.append("Output directory is required")
            if not save.run_name:
                errors.append("Run name is required")

        if errors:
            raise ValueError("\n".join(errors))

        return replace(
            t, stage=stage, pixels=pixels, pi=pi, vna=vna, geometry=geometry, save=save,
            controller_type=self.controller_type_var.get(),
            band=BAND_FROM_LABEL[self.band_var.get()],
            **top,
        )

    # ---------------------------------------------------------------- config -> form ----

    def set_config(self, config: RunConfig) -> None:
        """Populates every widget from config. Doesn't apply band defaults, so a loaded
        preset's own VNA sweep is kept exactly as saved."""
        self._template = config
        for key, kind in self.kinds.items():
            if key == "geo_stagger_mirror":
                continue
            obj = config
            for part in key.split("."):
                obj = getattr(obj, part)
            self.vars[key].set(", ".join(_fmt("float", v) for v in obj) if kind == "voltages" else _fmt(kind, obj))

        self.vars["geo_stagger_mirror"].set(config.geometry.stagger_sign < 0)
        self.controller_type_var.set(config.controller_type)
        self.band_var.set(BAND_LABELS.get(config.band, BAND_LABELS["lb"]))
        self.density_mode_var.set(config.geometry.density_mode)
        self.l_subgrid_var.set(str(config.geometry.l_subgrid))

        self._update_vna_summary()
        self._on_controller_type_changed()
        self._notify_geometry_change()

    def _notify_geometry_change(self) -> None:
        if self.on_geometry_change is not None:
            self.on_geometry_change()
