"""
Main GUI entry point. Run this directly: python gui_app.py
Tab 1 (Settings & Configuration), tab 2 (Scan), tab 3 (Debug), and tab 4 (Analysis) are all wired up:
geometry edits in tab 1 live-update tab 2's preview, and switching to tab 2 always
refreshes it too.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from tkinter import ttk

from analysis_tab import AnalysisTab
from branding import APP_NAME, load_kiss_mark_image
from debug_tab import DebugTab
from scan_tab import ScanTab
from settings_tab import SettingsTab


class ScanApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1200x860")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Keep a reference on self — PhotoImage is garbage-collected otherwise.
        self.logo_image = load_kiss_mark_image()

        header = ttk.Frame(self, padding=(12, 10, 12, 4))
        header.pack(fill="x")
        ttk.Label(header, image=self.logo_image).pack(side="left", padx=(0, 8))
        ttk.Label(header, text=APP_NAME, font=("Segoe UI", 16, "bold")).pack(side="left")

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        self.settings_tab = SettingsTab(notebook)
        notebook.add(self.settings_tab, text="Settings & Configuration")

        self.scan_tab = ScanTab(notebook, self.settings_tab)
        notebook.add(self.scan_tab, text="Scan")

        self.debug_tab = DebugTab(notebook, self.settings_tab)
        notebook.add(self.debug_tab, text="Debug")
        self.settings_tab.before_calibrate = self._before_calibrate

        self.analysis_tab = AnalysisTab(notebook, self.settings_tab)
        notebook.add(self.analysis_tab, text="Analysis")

        # Live-link the two tabs: geometry edits refresh the plot immediately, and
        # switching to the Scan tab refreshes it too (covers Load Settings / Reset,
        # which change many fields at once rather than firing key-by-key events).
        self.settings_tab.on_geometry_change = self.scan_tab.refresh_preview
        notebook.bind("<<NotebookTabChanged>>", lambda e: self._on_tab_changed(notebook))

    def _before_calibrate(self) -> bool:
        """The calibration walkthrough needs the stage and VNA to itself: refuse while a scan
        runs, and release the Debug tab's connections (it keeps them open between moves)."""
        thread = getattr(self.scan_tab, "_scan_thread", None)
        if thread is not None and thread.is_alive():
            messagebox.showerror("Scan running", "Wait for the scan to finish (or cancel it) before calibrating.")
            return False
        self.debug_tab.close()
        return True

    def _on_tab_changed(self, notebook) -> None:
        self.scan_tab.refresh_preview()
        if notebook.select() == str(self.analysis_tab):
            self.analysis_tab.on_shown()

    def _on_close(self) -> None:
        self.debug_tab.close()
        self.destroy()


if __name__ == "__main__":
    ScanApp().mainloop()
