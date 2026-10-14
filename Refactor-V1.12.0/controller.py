"""Board voltage controller interface, plus mapping utilities shared by any implementation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import numpy as np


class BoardController(ABC):
    """
    Interface a board voltage controller must implement, so run_scan can work with any of
    them. The coordinator only ever hands over a full voltage grid — each implementation
    is responsible for knowing how to translate grid positions into its own hardware
    addressing (native index, band+channel, etc.), using whatever mapping it needs.
    To add a new controller: subclass this, implement these methods, and point
    run_scan.py at your class (see RunConfig.controller_type).
    """

    @abstractmethod
    def ping(self) -> dict: ...

    @abstractmethod
    def set_voltage_grid(self, voltages: np.ndarray) -> None:
        """
        voltages has shape (cols, rows) matching ScanGeometryConfig — voltages[col, row]
        is the voltage for that grid element. Implementations should special-case a
        uniform grid (every value equal) since that needs no per-element mapping at all.
        """
        ...

    @abstractmethod
    def close(self) -> None: ...

    def element_voltage(self, density: str, row: int, col: int) -> float:
        """The voltage this controller was last commanded to put on an element, traced from
        what it actually sent (after clipping and the DAC's resolution), or NaN if unknown.
        No controller here can read voltages back, so this is the commanded output."""
        return float("nan")


def clip_voltage(v: float, vmin: float, vmax: float) -> float:
    return max(vmin, min(vmax, float(v)))


def uniform_value(voltages: np.ndarray) -> float | None:
    """Returns the shared value if every element of voltages is equal, else None —
    the "does this grid need per-element mapping at all" check both controllers use."""
    v0 = float(voltages.flat[0])
    return v0 if np.all(voltages == v0) else None


# ---------------------------------------------------- pixel index / element mapping ----

# Legacy connector/pin -> global_index lookup (reference only; not used by the scan pipeline).
CONN_ID_MAP = {
    "J1_JA1": 0, "J2_JB1": 1, "J2_JB9": 2, "J2_JB17": 3, "J1_JA9": 4,
    "J1_JA2": 5, "J2_JB2": 6, "J2_JB10": 7, "J2_JB18": 8, "J1_JA10": 9,
    "J1_JA3": 10, "J2_JB3": 11, "J2_JB11": 12, "J2_JB19": 13, "J1_JA11": 14,
    "J1_JA4": 15, "J2_JB4": 16, "J2_JB12": 17, "J2_JB20": 18, "J1_JA12": 19,
    "J1_JA5": 20, "J2_JB5": 21, "J2_JB13": 22, "J2_JB21": 23, "J1_JA13": 24,
    "J1_JA6": 25, "J2_JB6": 26, "J2_JB14": 27, "J2_JB22": 28, "J1_JA14": 29,
    "J1_JA7": 30, "J2_JB7": 31, "J2_JB15": 32, "J2_JB23": 33, "J1_JA15": 34,
    "J1_JA8": 35, "J2_JB8": 36, "J2_JB16": 37, "J2_JB24": 38, "J1_JA16": 39,
}
