"""
Tab 3: Debug. Manual, explicit hardware control — move the stage along one axis, or set
every board output to one voltage. Typing in a field changes nothing by itself; only the
matching Go button acts. Hardware connections are made lazily (first Go press) and kept
open for reuse across subsequent presses, not reconnected every time.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np

from pi_controller import PiBoardController
from pixel_controller import PixelController
from settings_tab import SettingsTab
from stage import GrblXY


class DebugTab(ttk.Frame):
    def __init__(self, parent, settings_tab: SettingsTab):
        super().__init__(parent)
        self.settings_tab = settings_tab
        self._stage: GrblXY | None = None
        self._controller = None  # PixelController | PiBoardController, connected lazily
        self._stage_config = None  # settings each connection was opened with, to spot edits
        self._controller_key = None

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="Manual hardware control. Nothing moves or changes until you press Go.",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 16))

        self.x_var = tk.StringVar(value="")
        self.y_var = tk.StringVar(value="")
        self.v_var = tk.StringVar(value="")
        self._field(frame, 1, "Move X to (mm)", self.x_var, self._on_go_x)
        self._field(frame, 2, "Move Y to (mm)", self.y_var, self._on_go_y)
        self._field(frame, 3, "Set all outputs to (V)", self.v_var, self._on_go_voltage)

        self.status_label = ttk.Label(frame, text="Idle")
        self.status_label.grid(row=4, column=0, columnspan=3, sticky="w", pady=(16, 0))

    def _field(self, parent, row: int, label: str, var: tk.StringVar, command) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=6)
        ttk.Entry(parent, textvariable=var, width=14).grid(row=row, column=1, sticky="w", padx=4, pady=6)
        ttk.Button(parent, text="Go", command=command).grid(row=row, column=2, sticky="w", padx=4, pady=6)

    # ---------------------------------------------------------------- hardware (lazy) ----

    def _current_config(self):
        """The form's current settings. Skips the save-related checks, so a blank run name
        can't block a manual jog. Raises ValueError if any setting doesn't parse."""
        return self.settings_tab.get_config(require_save=False)

    def _ensure_stage(self) -> GrblXY:
        stage_config = self._current_config().stage
        if self._stage is not None and self._stage_config != stage_config:
            self._stage.close()  # stage settings were edited since connecting: reconnect
            self._stage = None
        if self._stage is None:
            self._stage = GrblXY(stage_config)
            self._stage_config = stage_config
        return self._stage

    def _ensure_controller(self):
        config = self._current_config()
        key = (config.controller_type, config.pi if config.controller_type == "pi" else config.pixels,
               config.geometry.density_mode)
        if self._controller is not None and self._controller_key != key:
            self._controller.close()  # controller settings were edited since connecting: reconnect
            self._controller = None
        if self._controller is None:
            self._controller = (
                PiBoardController(config.pi) if config.controller_type == "pi"
                else PixelController(config.pixels, config.geometry.density_mode)
            )
            self._controller_key = key
        return self._controller

    def close(self) -> None:
        """Releases any hardware this tab opened — call on app shutdown."""
        for device in (self._stage, self._controller):
            if device is not None:
                try:
                    device.close()
                except Exception:
                    pass
        self._stage = None
        self._controller = None

    # ---------------------------------------------------------------- actions ----

    def _on_go_x(self) -> None:
        self._run_action(self.x_var, "Move X", lambda v: self._ensure_stage().goto_x(v), "Moved X to {:.3f} mm")

    def _on_go_y(self) -> None:
        self._run_action(self.y_var, "Move Y", lambda v: self._ensure_stage().goto_y(v), "Moved Y to {:.3f} mm")

    def _on_go_voltage(self) -> None:
        self._run_action(
            self.v_var, "Set voltage",
            lambda v: self._ensure_controller().set_voltage_grid(np.array([[v]])),
            "Set all outputs to {:.3f} V",
        )

    def _run_action(self, var: tk.StringVar, title: str, action, success_text: str) -> None:
        raw = var.get().strip()
        try:
            value = float(raw)
        except ValueError:
            messagebox.showerror("Invalid value", f"'{raw}' is not a number.")
            return
        try:
            action(value)
        except Exception as e:
            messagebox.showerror(f"{title} failed", str(e))
            return
        self.status_label.configure(text=success_text.format(value))
