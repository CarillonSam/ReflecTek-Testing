"""
Tab 2: Scan. Shows the hex grid as a live matplotlib scatter (updates as Settings-tab
geometry fields change), a progress bar, Start/Cancel controls, and a side panel of manual
controls (replacing the old Debug tab):

  Motor controls  - go to an X or Y position, or move X or Y by an amount, in the plot's
                    coordinates (the stage calibration is applied, so the probe lands where
                    the plot says); the stage's reported position; and an emergency STOP.
  Control board   - set every output to one voltage, or set voltages from a .csv (H or L).
  VNA             - one sweep with the VNA's current settings (nothing saved).

STOP sends GRBL's soft reset (stops at once), cancels any scan, and locks every motion
control until the software is restarted, since GRBL can no longer vouch for the position.

Scans and manual moves run on background threads (they block on hardware I/O) and report
back through queues the GUI thread polls; direct cross-thread widget updates aren't safe.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

from run_scan import AutomatedArrayScanner, ProgressEvent
from settings_tab import SettingsTab
from pi_controller import PiBoardController
from pixel_controller import PixelController
from stage import GrblXY, HexGridPlanner, ScanPoint, emergency_reset_port

# Manual moves outside the box around every scan point, plus this margin, need confirming.
SAFE_MARGIN_MM = 9.0
ESTOP_STATUS = "EMERGENCY STOPPED. Restart the software before moving the stage."
EMERGENCY_INSTRUCTIONS = (
    "The stage has been stopped with an emergency reset. It can no longer be trusted to know "
    "where it is, so every motion control is locked.\n\n"
    "Before using the stage again:\n"
    "  1. Cut power to the motors.\n"
    "  2. Move the motors back to the origin by hand.\n"
    "  3. Close and restart this software.\n"
    "  4. Restore power to the motors."
)

BLUE = "#378ADD"
GREEN = "#639922"
GREY = "#B0B0B0"
BOX_COLOR = "#D64545"
BORDER_COLOR = "#B00B00"   # border elements around the scan grid: drawn, never scanned


class ScanTab(ttk.Frame):
    def __init__(self, parent, settings_tab: SettingsTab):
        super().__init__(parent)
        self.settings_tab = settings_tab

        self._queue: queue.Queue = queue.Queue()
        self._scan_thread: threading.Thread | None = None
        self._scan_type = "sweep"
        self._cancel_event: threading.Event | None = None
        self._polling = False
        self._scanner: AutomatedArrayScanner | None = None   # the running scan's, so STOP can reach its stage

        # Manual controls (side panel). Hardware is connected on first use and released
        # before a scan or calibration starts (they open their own connections).
        self.stage_factory = GrblXY          # replaceable in tests
        self._manual_stage = None
        self._manual_stage_config = None
        self._manual_controller = None
        self._manual_controller_key = None
        self._manual_vna = None
        self._manual_queue: queue.Queue = queue.Queue()
        self._manual_busy = False
        self.emergency_stopped = False

        self._points: list[ScanPoint] = []
        self._border: list[tuple[float, float, str]] = []
        self._density_mode = "L"
        self._point_index: dict[tuple[str, int, int], int] = {}
        self._colors: list[str] = []
        self._scatter = None
        self._spacing_mm = 4.0  # sizes the stage-position box; kept in sync with whatever geometry is plotted
        self._stage_box: Rectangle | None = None
        self._stage_pos: tuple[float, float] | None = None  # last known stage position (plot coords), kept across redraws

        self._build_progress_bar()
        self._build_controls()          # packed at the bottom before the body, so it's never squeezed out
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self._build_side_panel(body)
        self._build_plot(body)

        self.refresh_preview()

    # ---------------------------------------------------------------- live preview ----

    def refresh_preview(self) -> None:
        """Rebuilds the plotted points from the Settings tab's current geometry. Shows
        the WHOLE dense lattice (both L and H) — only the active density (per
        geometry.density_mode) is colored/scannable, the other stays grey and static.
        A no-op if the geometry fields don't currently parse (e.g. mid-keystroke) — the
        plot just keeps showing the last valid geometry rather than erroring."""
        geometry = self.settings_tab.get_geometry_or_none()
        if geometry is None:
            return
        planner = HexGridPlanner(geometry)
        self._set_points(planner.all_points(), geometry.spacing_mm, geometry.density_mode, planner.border_points())

    def _set_points(self, points: list[ScanPoint], spacing_mm: float, density_mode: str,
                    border: list[tuple[float, float, str]] | None = None) -> None:
        """border: positions one sub-grid row/column outside the scan grid (HexGridPlanner.
        border_points()), drawn in BORDER_COLOR as inactive elements. Display only: they're
        not scan points, and the safe area is still based on the scan points alone."""
        self._points = points
        self._border = border or []
        self._density_mode = density_mode
        self._point_index = {(p.density, p.logical_row, p.logical_col): i for i, p in enumerate(points)}
        self._colors = [BLUE if p.density == density_mode else GREY for p in points]
        self._spacing_mm = spacing_mm
        self._redraw()

    def _redraw(self) -> None:
        self.ax.clear()
        self._stage_box = None  # ax.clear() drops the old patch; re-created on the next progress update
        if self._border:
            self.ax.scatter([b[0] for b in self._border], [b[1] for b in self._border], c=BORDER_COLOR,
                            s=24, zorder=2, label="border (not scanned)")
        if self._points:
            xs = [p.stage_x_mm for p in self._points]
            ys = [p.stage_y_mm for p in self._points]
            self._scatter = self.ax.scatter(xs, ys, c=self._colors, s=24, zorder=2)
        else:
            self._scatter = None
        active_count = sum(1 for p in self._points if p.density == self._density_mode)
        self.ax.set_xlabel("Stage X (mm)")
        self.ax.set_ylabel("Stage Y (mm)")
        self.ax.set_aspect("equal", adjustable="datalim")
        border_note = f" + {len(self._border)} border" if self._border else ""
        self.ax.set_title(f"{active_count}/{len(self._points)} points ({self._density_mode} active){border_note}")
        if self._points:
            xs = [p.stage_x_mm for p in self._points]
            ys = [p.stage_y_mm for p in self._points]
            m = SAFE_MARGIN_MM
            self.ax.add_patch(Rectangle((min(xs) - m, min(ys) - m), max(xs) - min(xs) + 2 * m, max(ys) - min(ys) + 2 * m,
                                        linewidth=1.0, edgecolor="#B3261E", facecolor="none", linestyle="--",
                                        alpha=0.6, zorder=1, label="safe area"))
            self.ax.autoscale_view()
        if self._stage_pos is not None:
            self._update_stage_box(*self._stage_pos)   # ax.clear() removed it; put it back
        self.canvas.draw_idle()

    def _mark_scanned(self, point: ScanPoint) -> None:
        idx = self._point_index.get((point.density, point.logical_row, point.logical_col))
        if idx is None or self._scatter is None:
            return
        self._colors[idx] = GREEN
        self._scatter.set_facecolor(self._colors)
        self.canvas.draw_idle()

    def _update_stage_box(self, x_mm: float, y_mm: float) -> None:
        """Draws/moves a box representing where the stage currently is. Sized relative
        to point spacing so it stays legible whether the grid is coarse or fine."""
        self._stage_pos = (x_mm, y_mm)
        size = self._spacing_mm * 0.6
        if self._stage_box is None:
            self._stage_box = Rectangle(
                (x_mm - size / 2, y_mm - size / 2), size, size,
                linewidth=1.8, edgecolor=BOX_COLOR, facecolor="none", zorder=5,
            )
            self.ax.add_patch(self._stage_box)
        else:
            self._stage_box.set_xy((x_mm - size / 2, y_mm - size / 2))
            self._stage_box.set_width(size)
            self._stage_box.set_height(size)
        self.canvas.draw_idle()

    # ---------------------------------------------------------------- widgets ----

    def _build_progress_bar(self) -> None:
        frame = ttk.Frame(self, padding=(10, 10, 10, 4))
        frame.pack(fill="x")
        self.progress_var = tk.DoubleVar(value=0.0)
        ttk.Progressbar(frame, variable=self.progress_var, maximum=100).pack(side="left", fill="x", expand=True)
        self.progress_label = ttk.Label(frame, text="0%", width=6)
        self.progress_label.pack(side="left", padx=(8, 0))
        self.voltage_label = ttk.Label(frame, text="", width=22)
        self.voltage_label.pack(side="left", padx=(12, 0))

    def _build_plot(self, parent) -> None:
        plot_frame = ttk.Frame(parent, padding=10)
        plot_frame.pack(side="left", fill="both", expand=True)
        self.figure = Figure(figsize=(5, 4.5), dpi=100)
        self.ax = self.figure.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.figure, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    def _build_controls(self) -> None:
        frame = ttk.Frame(self, padding=10)
        frame.pack(side="bottom", fill="x")
        self.start_button = ttk.Button(frame, text="Start Scan", command=self._on_start)
        self.start_button.pack(side="left", padx=4)
        self.stop_button = ttk.Button(frame, text="Cancel Scan", command=self._on_stop, state="disabled")
        self.stop_button.pack(side="left", padx=4)
        self.status_label = ttk.Label(frame, text="Idle")
        self.status_label.pack(side="left", padx=12)

    # ---------------------------------------------------------------- side panel ----

    def _build_side_panel(self, parent) -> None:
        panel = ttk.Frame(parent, padding=(0, 10, 10, 10))
        panel.pack(side="right", fill="y")

        motors = ttk.LabelFrame(panel, text="Motor controls", padding=(10, 6))
        motors.pack(fill="x")
        ttk.Label(motors, text="Plot coordinates, mm (stage calibration applied)",
                  foreground="gray40").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self._motor_widgets = []
        self.goto_x_var, self.goto_y_var = tk.StringVar(), tk.StringVar()
        self.move_x_var, self.move_y_var = tk.StringVar(), tk.StringVar()
        for row, (label, var, axis, relative) in enumerate((
                ("Go to X", self.goto_x_var, "x", False),
                ("Go to Y", self.goto_y_var, "y", False),
                ("Move X by", self.move_x_var, "x", True),
                ("Move Y by", self.move_y_var, "y", True)), start=1):
            ttk.Label(motors, text=label).grid(row=row, column=0, sticky="w", pady=2)
            entry = ttk.Entry(motors, textvariable=var, width=10)
            entry.grid(row=row, column=1, padx=6, pady=2)
            go = ttk.Button(motors, text="Go", width=5,
                            command=lambda v=var, a=axis, r=relative: self._on_move(v, a, r))
            go.grid(row=row, column=2, pady=2)
            entry.bind("<Return>", lambda e, v=var, a=axis, r=relative: self._on_move(v, a, r))
            self._motor_widgets += [entry, go]
        self.position_label = ttk.Label(motors, text="Position: not read yet", foreground="gray30")
        self.position_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        read = ttk.Button(motors, text="Read", width=5, command=self._on_read_position)
        read.grid(row=5, column=2, pady=(6, 0))
        self._motor_widgets.append(read)
        # A plain tk.Button so it can be big and red on every platform. Always enabled.
        self.estop_button = tk.Button(motors, text="STOP", command=self.emergency_stop, bg="#C62828", fg="white",
                                      activebackground="#8E0000", activeforeground="white",
                                      font=("TkDefaultFont", 16, "bold"), height=1, relief="raised", bd=3)
        self.estop_button.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(10, 2), ipady=6)
        ttk.Label(motors, text="Stops the stage at once (GRBL reset); restart after.",
                  foreground="gray40", justify="left").grid(row=7, column=0, columnspan=3, sticky="w")
        self.motor_status = ttk.Label(motors, text="", wraplength=250, justify="left")
        self.motor_status.grid(row=8, column=0, columnspan=3, sticky="w")

        # ---- Control board: one voltage everywhere, or per element from a CSV
        board = ttk.LabelFrame(panel, text="Control board", padding=(10, 6))
        board.pack(fill="x", pady=(6, 0))
        ttk.Label(board, text="Set all outputs to (V)").grid(row=0, column=0, columnspan=2, sticky="w")
        self.voltage_var = tk.StringVar()
        ventry = ttk.Entry(board, textvariable=self.voltage_var, width=8)
        ventry.grid(row=0, column=2, sticky="w", padx=6)
        ventry.bind("<Return>", lambda e: self._on_set_voltage())
        vset = ttk.Button(board, text="SET", width=5, command=self._on_set_voltage)
        vset.grid(row=0, column=3)
        ttk.Separator(board).grid(row=1, column=0, columnspan=4, sticky="ew", pady=5)
        ttk.Label(board, text="Set voltage by .csv").grid(row=2, column=0, sticky="w")
        self.csv_density_var = tk.StringVar(value="H")
        dens = ttk.Frame(board)
        dens.grid(row=2, column=1, columnspan=3, sticky="w", padx=(6, 0))
        radios = [ttk.Radiobutton(dens, text=d, value=d, variable=self.csv_density_var) for d in ("H", "L")]
        for r in radios:
            r.pack(side="left", padx=(0, 6))
        self.csv_path_var = tk.StringVar()
        centry = ttk.Entry(board, textvariable=self.csv_path_var, width=18)
        centry.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        browse = ttk.Button(board, text="Browse...", command=self._browse_csv)
        browse.grid(row=3, column=2, sticky="w", padx=6, pady=(4, 0))
        cset = ttk.Button(board, text="SET", width=5, command=self._on_set_csv)
        cset.grid(row=3, column=3, pady=(4, 0))
        self.board_status = ttk.Label(board, text="", wraplength=250, justify="left")
        self.board_status.grid(row=4, column=0, columnspan=4, sticky="w")
        self._motor_widgets += [ventry, vset, *radios, centry, browse, cset]

        # ---- VNA: one sweep with whatever the VNA is currently set to
        vna = ttk.LabelFrame(panel, text="VNA", padding=(10, 6))
        vna.pack(fill="x", pady=(6, 0))
        vbutton = ttk.Button(vna, text="Scan", width=10, command=self._on_vna_scan)
        vbutton.grid(row=0, column=0, sticky="w")
        ttk.Label(vna, text="One sweep, current VNA settings;\nnothing is saved.", foreground="gray40",
                  justify="left").grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.vna_status = ttk.Label(vna, text="", wraplength=250, justify="left")
        self.vna_status.grid(row=1, column=0, columnspan=2, sticky="w")
        self._motor_widgets.append(vbutton)

    def _set_manual_enabled(self, enabled: bool) -> None:
        """Manual controls are disabled during scans, manual moves, and for good after an
        emergency stop. The STOP button is never disabled."""
        if self.emergency_stopped:
            enabled = False
        for w in self._motor_widgets:
            w.configure(state="normal" if enabled else "disabled")

    # ---------------------------------------------------------------- manual hardware ----

    def _manual_config(self):
        return self.settings_tab.get_config(require_save=False)

    def _ensure_stage(self, stage_config):
        if self._manual_stage is not None and self._manual_stage_config != stage_config:
            self._manual_stage.close()   # stage settings edited since connecting: reconnect
            self._manual_stage = None
        if self._manual_stage is None:
            self._manual_stage = self.stage_factory(stage_config)
            self._manual_stage_config = stage_config
        return self._manual_stage

    def _ensure_controller(self, config):
        key = (config.controller_type, config.pi if config.controller_type == "pi" else config.pixels,
               config.geometry.density_mode)
        if self._manual_controller is not None and self._manual_controller_key != key:
            self._manual_controller.close()
            self._manual_controller = None
        if self._manual_controller is None:
            self._manual_controller = (PiBoardController(config.pi) if config.controller_type == "pi"
                                       else PixelController(config.pixels, config.geometry.density_mode))
            self._manual_controller_key = key
        return self._manual_controller

    @property
    def manual_busy(self) -> bool:
        return self._manual_busy

    def release_manual_hardware(self) -> None:
        """Closes the panel's own stage/controller connections (before a scan or calibration
        opens its own, and on exit)."""
        for dev in (self._manual_stage, self._manual_controller, self._manual_vna):
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    logging.exception("Closing manual-control hardware failed")
        self._manual_stage = self._manual_controller = self._manual_vna = None

    def _plot_position(self, stage) -> tuple[float, float]:
        """Where the stage reports it is, in plot (ideal) coordinates."""
        pos = stage.get_pos()
        return stage.calibration.inverse(pos.x_mm, pos.y_mm)

    # ---------------------------------------------------------------- manual actions ----

    def _on_move(self, var: tk.StringVar, axis: str, relative: bool) -> None:
        """Go to / Move by. Reads where the stage is first (so the exact target is known),
        checks the target against the safe area, asks before leaving it, then moves."""
        if self._manual_busy or self.emergency_stopped:
            return
        raw = var.get().strip()
        try:
            value = float(raw)
        except ValueError:
            messagebox.showerror("Invalid value", f"'{raw}' isn't a number (mm).")
            return
        try:
            config = self._manual_config()
        except ValueError as e:
            messagebox.showerror("Invalid settings", f"Fix these first:\n\n{e}")
            return
        what = f"{'Move' if relative else 'Go to'} {axis.upper()} {'by' if relative else ''} {value:g} mm".replace("  ", " ")

        def plan(current):
            x, y = current
            if axis == "x":
                x = x + value if relative else value
            else:
                y = y + value if relative else value
            self._show_position(current, "")
            outside = self._outside_safe_area(config.geometry, x, y)
            if outside and not self.confirm_outside(self._outside_message(what, x, y, config.geometry, outside)):
                self.motor_status.configure(text=f"{what}: cancelled, the stage didn't move.", foreground="")
                return
            self._run_manual(f"{what}...", lambda: self._move_to(config.stage, x, y),
                             lambda pos: self._show_position(pos, f"{what}: done."))
        self._run_manual("Reading position...", lambda: self._plot_position(self._ensure_stage(config.stage)), plan)

    def _move_to(self, stage_config, x: float, y: float):
        stage = self._ensure_stage(stage_config)
        stage.goto_ideal_xy(x, y)
        stage.wait_until_reached_ideal(x, y)
        return self._plot_position(stage)

    # ---------------------------------------------------------------- safe area ----

    @staticmethod
    def safe_area(geometry) -> tuple[float, float, float, float]:
        """(x_min, x_max, y_min, y_max) in plot coordinates: every point a scan could visit
        (both densities), plus SAFE_MARGIN_MM on every side."""
        pts = HexGridPlanner(geometry).all_points()
        xs = [p.stage_x_mm for p in pts]
        ys = [p.stage_y_mm for p in pts]
        m = SAFE_MARGIN_MM
        return min(xs) - m, max(xs) + m, min(ys) - m, max(ys) + m

    def _outside_safe_area(self, geometry, x: float, y: float) -> list[str]:
        """How the target leaves the safe area, e.g. ["Y is 1.000 mm below -200.000"]; [] if inside."""
        x0, x1, y0, y1 = self.safe_area(geometry)
        out = []
        if x < x0: out.append(f"X is {x0 - x:.3f} mm below the limit of {x0:.3f}")
        if x > x1: out.append(f"X is {x - x1:.3f} mm above the limit of {x1:.3f}")
        if y < y0: out.append(f"Y is {y0 - y:.3f} mm below the limit of {y0:.3f}")
        if y > y1: out.append(f"Y is {y - y1:.3f} mm above the limit of {y1:.3f}")
        return out

    def _outside_message(self, what: str, x: float, y: float, geometry, outside: list[str]) -> str:
        x0, x1, y0, y1 = self.safe_area(geometry)
        return (f"{what} would take the probe to X {x:.3f}, Y {y:.3f} mm, outside the safe area "
                f"({SAFE_MARGIN_MM:g} mm beyond the furthest scan points):\n\n"
                f"    X {x0:.3f} to {x1:.3f}\n    Y {y0:.3f} to {y1:.3f}\n\n"
                + "\n".join(outside) + ".\n\n"
                "Make sure nothing is in the way. Confirm to move there anyway, or Cancel to leave the "
                "stage where it is.")

    def confirm_outside(self, message: str) -> bool:
        """Modal Confirm / Cancel dialog. Cancel is the default (Enter and Escape both cancel),
        so a move outside the safe area always takes a deliberate click."""
        dialog = tk.Toplevel(self)
        dialog.title("Outside the safe area")
        dialog.transient(self.winfo_toplevel())
        dialog.resizable(False, False)
        result = {"ok": False}
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Outside the safe area", font=("TkDefaultFont", 12, "bold"),
                  foreground="#B3261E").pack(anchor="w")
        ttk.Label(body, text=message, wraplength=440, justify="left").pack(anchor="w", pady=(8, 14))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")

        def close(ok: bool):
            result["ok"] = ok
            dialog.destroy()
        cancel = ttk.Button(buttons, text="Cancel", command=lambda: close(False))
        cancel.pack(side="right")
        ttk.Button(buttons, text="Confirm move", command=lambda: close(True)).pack(side="right", padx=(0, 8))
        dialog.bind("<Return>", lambda e: close(False))
        dialog.bind("<Escape>", lambda e: close(False))
        dialog.protocol("WM_DELETE_WINDOW", lambda: close(False))
        self._confirm_dialog = dialog          # for tests
        dialog.grab_set()
        cancel.focus_set()
        self.wait_window(dialog)
        return result["ok"]

    def _on_read_position(self) -> None:
        if self._manual_busy or self.emergency_stopped:
            return
        try:
            config = self._manual_config()
        except ValueError as e:
            messagebox.showerror("Invalid settings", f"Fix these first:\n\n{e}")
            return
        self._run_manual("Reading position...", lambda: self._plot_position(self._ensure_stage(config.stage)),
                         lambda pos: self._show_position(pos, ""))

    def _show_position(self, pos, message: str) -> None:
        self.position_label.configure(text=f"Position: X {pos[0]:.3f}, Y {pos[1]:.3f}")
        self._update_stage_box(*pos)
        self.motor_status.configure(text=message, foreground="")

    def _on_set_voltage(self) -> None:
        if self._manual_busy or self.emergency_stopped:
            return
        raw = self.voltage_var.get().strip()
        try:
            value = float(raw)
            config = self._manual_config()
        except ValueError as e:
            messagebox.showerror("Invalid value", f"'{raw}' isn't a number (V)." if "could not convert" in str(e) else str(e))
            return
        self._run_manual(f"Setting all outputs to {value:g} V...",
                         lambda: self._ensure_controller(config).set_voltage_grid(np.array([[value]])),
                         lambda _: self.board_status.configure(text=f"All outputs set to {value:g} V.", foreground=""),
                         status_label=self.board_status)

    def _browse_csv(self) -> None:
        from tkinter import filedialog
        path = filedialog.askopenfilename(filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if path:
            self.csv_path_var.set(path)

    def _on_set_csv(self) -> None:
        """Set voltage by .csv: reads the file the same way pattern scans do (element names,
        one column in row order, or a rows x columns grid of the chosen density) and writes it
        to the board. With the pixel controller, every element not in the file is set to 0 V."""
        if self._manual_busy or self.emergency_stopped:
            return
        path = self.csv_path_var.get().strip()
        density = self.csv_density_var.get()
        if not path:
            messagebox.showerror("No file", "Choose a .csv file first.")
            return
        try:
            config = self._manual_config()
        except ValueError as e:
            messagebox.showerror("Invalid settings", f"Fix these first:\n\n{e}")
            return
        from dataclasses import replace
        from pattern import element_label, load_pattern
        geometry = replace(config.geometry, density_mode=density)
        try:
            pattern = load_pattern(path, geometry, config.pixels.min_voltage_v, config.pixels.max_voltage_v)
        except ValueError as e:
            messagebox.showerror("Can't use that file", str(e))
            return
        if config.controller_type == "pi":
            others = [k for k, v in pattern.voltages.items() if k[0] != density and v != 0]
            if others:
                messagebox.showerror("Can't use that file", f"The file sets {len(others)} element(s) outside {density} "
                                     f"(e.g. {element_label(*others[0])}); the Pi controller can only set one density.")
                return

        def work():
            controller = self._ensure_controller(config)
            if isinstance(controller, PixelController):
                controller.apply_pattern(pattern.voltages)          # whole board; others 0 V
            else:
                controller.set_voltage_grid(pattern.grid(density, geometry))
        summary = pattern.describe()
        if config.controller_type != "pi":
            summary += "; every element not in the file set to 0 V"
        self._run_manual(f"Writing {path.replace(chr(92), '/').split('/')[-1]} to the board...", work,
                         lambda _: self.board_status.configure(text=f"Set from file: {summary}.", foreground=""),
                         status_label=self.board_status)

    def _on_vna_scan(self) -> None:
        """One VNA sweep with its current settings (VNAController.sweep_only)."""
        if self._manual_busy or self.emergency_stopped:
            return
        try:
            config = self._manual_config()
        except ValueError as e:
            messagebox.showerror("Invalid settings", f"Fix these first:\n\n{e}")
            return

        def work():
            if self._manual_vna is not None and self._manual_vna.config != config.vna:
                self._manual_vna.close()        # VNA settings edited since connecting: reconnect
                self._manual_vna = None
            if self._manual_vna is None:
                from vna import VNAController
                self._manual_vna = VNAController(config.vna)
            self._manual_vna.sweep_only()
        self._run_manual("Triggering a VNA sweep...", work,
                         lambda _: self.vna_status.configure(text="Sweep done.", foreground=""),
                         status_label=self.vna_status)

    def _run_manual(self, message: str, work, done, status_label=None) -> None:
        status_label = status_label or self.motor_status
        self._manual_busy = True
        self._set_manual_enabled(False)
        self.start_button.configure(state="disabled")
        status_label.configure(text=message, foreground="")

        def worker():
            try:
                self._manual_queue.put(("ok", work(), done, status_label))
            except Exception as e:
                logging.exception("Manual control failed")
                self._manual_queue.put(("error", f"{type(e).__name__}: {e}", done, status_label))
        threading.Thread(target=worker, daemon=True).start()
        self.after(50, self._poll_manual)

    def _poll_manual(self) -> None:
        try:
            kind, payload, done, status_label = self._manual_queue.get_nowait()
        except queue.Empty:
            self.after(50, self._poll_manual)
            return
        self._manual_busy = False
        if kind == "ok":
            done(payload)
        elif self.emergency_stopped:   # the interrupted action's own error; keep the STOP message
            status_label.configure(text=ESTOP_STATUS, foreground="#B3261E")
        else:
            status_label.configure(text=payload, foreground="#B3261E")
        if self._manual_busy:      # done() chained another action (e.g. the move after reading the position)
            return
        self._set_manual_enabled(True)
        if not self.emergency_stopped and (self._scan_thread is None or not self._scan_thread.is_alive()):
            self.start_button.configure(state="normal")

    # ---------------------------------------------------------------- emergency stop ----

    def emergency_stop(self) -> None:
        """STOP: GRBL soft reset on every stage connection this program has open (the running
        scan's and/or the panel's); if none is open, opens the port and sends it there. Then
        cancels any scan, locks the motion controls and tells the operator what to do."""
        self.emergency_stopped = True
        reached, problems = [], []
        if self._cancel_event is not None:
            self._cancel_event.set()
        stages = []
        scan_stage = getattr(self._scanner, "stage", None) if self._scanner is not None else None
        if scan_stage is not None:
            stages.append(("the scan's stage connection", scan_stage))
        if self._manual_stage is not None:
            stages.append(("the manual-control connection", self._manual_stage))
        for name, stage in stages:
            try:
                stage.emergency_stop()
                reached.append(name)
            except Exception as e:
                problems.append(f"{name}: {e}")
        if not reached:
            try:
                cfg = self._manual_config().stage
            except Exception:
                cfg = self.settings_tab._template.stage
            try:
                emergency_reset_port(cfg.port, cfg.baud)
                reached.append(f"the stage port {cfg.port} (opened just to send the reset)")
            except Exception as e:
                problems.append(f"port {cfg.port}: {e}")
        logging.warning("EMERGENCY STOP sent to: %s; problems: %s", reached, problems)

        self._set_manual_enabled(False)
        self.start_button.configure(state="disabled")
        self.motor_status.configure(text=ESTOP_STATUS, foreground="#B3261E")
        self.status_label.configure(text="Emergency stopped: restart the software")
        detail = ("Reset sent to " + " and ".join(reached) + ".") if reached else ""
        if problems:
            detail += ("\n\nCOULD NOT REACH: " + "; ".join(problems) +
                       "\nThe stage may still be moving: cut motor power now.")
        messagebox.showwarning("EMERGENCY STOP", f"{EMERGENCY_INSTRUCTIONS}\n\n{detail}".strip())

    # ---------------------------------------------------------------- scan control ----

    def _on_start(self) -> None:
        if self._scan_thread is not None and self._scan_thread.is_alive():
            return
        if self.emergency_stopped:
            messagebox.showerror("Emergency stop", EMERGENCY_INSTRUCTIONS)
            return
        if self._manual_busy:
            messagebox.showerror("Stage busy", "Wait for the manual move to finish before starting a scan.")
            return
        try:
            config = self.settings_tab.get_config()
        except ValueError as e:
            messagebox.showerror("Invalid settings", f"Fix these before starting a scan:\n\n{e}")
            return

        planner = HexGridPlanner(config.geometry)
        self._set_points(planner.all_points(), config.geometry.spacing_mm, config.geometry.density_mode,
                         planner.border_points())
        self._scan_type = config.scan_type
        self.progress_var.set(0.0)
        self.progress_label.configure(text="0%")
        self.voltage_label.configure(text="")
        self.status_label.configure(text="Running...")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.release_manual_hardware()        # the scan opens its own connections
        self._set_manual_enabled(False)

        self._cancel_event = threading.Event()
        cancel_event = self._cancel_event

        def worker() -> None:
            try:
                scanner = AutomatedArrayScanner(config, on_progress=self._on_progress, cancel_event=cancel_event)
                self._scanner = scanner
                scanner.run()
                self._queue.put(("cancelled",) if cancel_event.is_set() else ("done",))
            except Exception as e:
                self._queue.put(("error", str(e)))

        self._scan_thread = threading.Thread(target=worker, daemon=True)
        self._scan_thread.start()
        if not self._polling:
            self._polling = True
            self.after(100, self._poll_queue)

    def _on_stop(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        self.status_label.configure(text="Stopping...")

    def _on_progress(self, event: ProgressEvent) -> None:
        # Runs on the worker thread — must only touch the thread-safe queue here,
        # never widgets or the plot directly.
        self._queue.put(("progress", event))

    def _poll_queue(self) -> None:
        finished = False
        try:
            while True:
                finished = self._handle_message(self._queue.get_nowait()) or finished
        except queue.Empty:
            pass
        if finished:
            self._polling = False
        else:
            self.after(100, self._poll_queue)

    def _handle_message(self, msg: tuple) -> bool:
        kind = msg[0]
        if kind == "progress":
            event: ProgressEvent = msg[1]
            pct = 100.0 * event.step / event.total_steps if event.total_steps else 0.0
            self.progress_var.set(pct)
            self.progress_label.configure(text=f"{pct:.0f}%")
            from pattern import element_label
            text = (f"Voltage {event.voltage_index + 1}/{event.voltage_count}: {event.voltage_v:.3f} V"
                    if self._scan_type == "sweep" else
                    f"{element_label(event.point.density, event.point.logical_row, event.point.logical_col)}: "
                    f"{event.voltage_v:.3f} V")
            self.voltage_label.configure(
                text=text
            )
            self._mark_scanned(event.point)
            self._update_stage_box(event.point.stage_x_mm, event.point.stage_y_mm)
            return False
        if kind == "done":
            self.status_label.configure(text="Complete")
            self._finish()
            return True
        if kind == "cancelled":
            self.status_label.configure(text="Cancelled")
            self._finish()
            return True
        if kind == "error":
            if self.emergency_stopped:   # the scan failing is expected after an emergency stop
                self.status_label.configure(text="Emergency stopped")
            else:
                self.status_label.configure(text="Error")
                messagebox.showerror("Scan failed", msg[1])
            self._finish()
            return True
        return False

    def _finish(self) -> None:
        self.start_button.configure(state="disabled" if self.emergency_stopped else "normal")
        self.stop_button.configure(state="disabled")
        self._scan_thread = None
        self._cancel_event = None
        self._scanner = None
        self._set_manual_enabled(not self.emergency_stopped)
        if self.emergency_stopped:
            self.status_label.configure(text="Emergency stopped: restart the software")
