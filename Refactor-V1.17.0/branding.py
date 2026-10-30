"""App branding: the name and the kiss-mark logo image."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path

APP_NAME = "Candice"
KISS_MARK_PNG = Path(__file__).parent / "kiss_mark.png"


def load_kiss_mark_image() -> tk.PhotoImage:
    """
    Loads the kiss-mark logo. Must be called after the app's Tk root window exists
    (PhotoImage needs a default root) — so call this from inside ScanApp.__init__,
    not at module import time.
    """
    return tk.PhotoImage(file=str(KISS_MARK_PNG))
