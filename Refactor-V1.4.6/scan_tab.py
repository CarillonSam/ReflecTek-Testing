"""
Tab 2: Scan. Shows the hex grid as a live matplotlib scatter (updates as Settings-tab
geometry fields change), a progress bar, and Start/Stop controls. Starting a scan runs
AutomatedArrayScanner in a background thread (it blocks on real hardware I/O, so it
can't run on the GUI thread) and reports progress back through a queue, which the GUI
thread polls — direct cross-thread widget/plot updates are not safe in tkinter.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

from run_scan import AutomatedArrayScanner, ProgressEvent
from settings_tab import SettingsTab
from stage import HexGridPlanner, ScanPoint

BLUE = "#378ADD"
GREEN = "#639922"
GREY = "#B0B0B0"
BOX_COLOR = "#D64545"


class ScanTab(ttk.Frame):
    def __init__(self, parent, settings_tab: SettingsTab):
        super().__init__(parent)
        self.settings_tab = settings_tab

        self._queue: queue.Queue = queue.Queue()
        self._scan_thread: threading.Thread | None = None
        self._cancel_event: threading.Event | None = None
        self._polling = False

        self._points: list[ScanPoint] = []
        self._density_mode = "L"
        self._point_index: dict[tuple[str, int, int], int] = {}
        self._colors: list[str] = []
        self._scatter = None
        self._spacing_mm = 4.0  # sizes the stage-position box; kept in sync with whatever geometry is plotted
        self._stage_box: Rectangle | None = None

        self._build_progress_bar()
        self._build_plot()
        self._build_controls()

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
        self._set_points(HexGridPlanner(geometry).all_points(), geometry.spacing_mm, geometry.density_mode)

    def _set_points(self, points: list[ScanPoint], spacing_mm: float, density_mode: str) -> None:
        self._points = points
        self._density_mode = density_mode
        self._point_index = {(p.density, p.logical_row, p.logical_col): i for i, p in enumerate(points)}
        self._colors = [BLUE if p.density == density_mode else GREY for p in points]
        self._spacing_mm = spacing_mm
        self._redraw()

    def _redraw(self) -> None:
        self.ax.clear()
        self._stage_box = None  # ax.clear() drops the old patch; re-created on the next progress update
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
        self.ax.set_title(f"{active_count}/{len(self._points)} points ({self._density_mode} active)")
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

    def _build_plot(self) -> None:
        plot_frame = ttk.Frame(self, padding=10)
        plot_frame.pack(fill="both", expand=True)
        self.figure = Figure(figsize=(5, 4.5), dpi=100)
        self.ax = self.figure.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.figure, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    def _build_controls(self) -> None:
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="x")
        self.start_button = ttk.Button(frame, text="Start Scan", command=self._on_start)
        self.start_button.pack(side="left", padx=4)
        self.stop_button = ttk.Button(frame, text="Stop Scan", command=self._on_stop, state="disabled")
        self.stop_button.pack(side="left", padx=4)
        self.status_label = ttk.Label(frame, text="Idle")
        self.status_label.pack(side="left", padx=12)

    # ---------------------------------------------------------------- scan control ----

    def _on_start(self) -> None:
        if self._scan_thread is not None and self._scan_thread.is_alive():
            return
        try:
            config = self.settings_tab.get_config()
        except ValueError as e:
            messagebox.showerror("Invalid settings", f"Fix these before starting a scan:\n\n{e}")
            return

        self._set_points(HexGridPlanner(config.geometry).all_points(), config.geometry.spacing_mm, config.geometry.density_mode)
        self.progress_var.set(0.0)
        self.progress_label.configure(text="0%")
        self.voltage_label.configure(text="")
        self.status_label.configure(text="Running...")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

        self._cancel_event = threading.Event()
        cancel_event = self._cancel_event

        def worker() -> None:
            try:
                scanner = AutomatedArrayScanner(config, on_progress=self._on_progress, cancel_event=cancel_event)
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
            self.voltage_label.configure(
                text=f"Voltage {event.voltage_index + 1}/{event.voltage_count}: {event.voltage_v:.3f} V"
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
            self.status_label.configure(text="Error")
            messagebox.showerror("Scan failed", msg[1])
            self._finish()
            return True
        return False

    def _finish(self) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self._scan_thread = None
        self._cancel_event = None
