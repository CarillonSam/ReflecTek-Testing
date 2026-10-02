"""Board voltage controller interface, plus mapping utilities shared by any implementation."""

from __future__ import annotations

import csv
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

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


@dataclass
class LegacyConnRowMap:
    """Loads Pixel_Map_by_ConnRow.csv for global_index / connector / row lookups."""

    index_map: Dict[str, Dict[int, int]]
    row_map: Dict[int, List[int]]
    total: int
    errors: List[str]

    @classmethod
    def from_csv(cls, csv_path: str | Path) -> "LegacyConnRowMap":
        index_map: Dict[str, Dict[int, int]] = {}
        row_map: Dict[int, List[int]] = {r: [] for r in range(1, 9)}
        total = 0
        errors: List[str] = []

        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            for row_d in csv.DictReader(f):
                try:
                    idx = int(row_d["global_index"])
                    row = int(row_d["row"])
                    conn = row_d["connector"].strip()
                    pin = int(row_d["pin"])
                except (KeyError, ValueError) as e:
                    errors.append(str(e))
                    continue
                index_map.setdefault(conn, {})[pin] = idx
                if row in row_map:
                    row_map[row].append(idx)
                total += 1

        for r in row_map:
            row_map[r].sort()
        return cls(index_map=index_map, row_map=row_map, total=total, errors=errors)


@dataclass
class PixelMapEntry:
    element_row: int
    element_col: int
    controller_index: int  # native address for a serial-style controller (e.g. PixelController)
    band: str = ""  # e.g. "hb" / "lb" — for controllers that address by band + channel
    band_channel: Optional[int] = None  # channel number within `band`
    stage_x_mm: Optional[float] = None
    stage_y_mm: Optional[float] = None
    connector_id: str = ""
    channel_id: str = ""
    notes: str = ""


class ElementToControllerMap:
    """(element_row, element_col) -> PixelMapEntry, loaded from a mapping CSV."""

    def __init__(self, entries: List[PixelMapEntry]) -> None:
        self.entries = entries
        self._lookup = {(e.element_row, e.element_col): e for e in entries}

    @classmethod
    def from_csv(cls, csv_path: str | Path) -> "ElementToControllerMap":
        entries: List[PixelMapEntry] = []
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("element_row", "").strip() == "":
                    continue
                entries.append(
                    PixelMapEntry(
                        element_row=int(row["element_row"]),
                        element_col=int(row["element_col"]),
                        controller_index=int(row["controller_index"]),
                        band=row.get("band", "").strip(),
                        band_channel=int(row["band_channel"]) if row.get("band_channel", "").strip() else None,
                        stage_x_mm=float(row["stage_x_mm"]) if row.get("stage_x_mm") else None,
                        stage_y_mm=float(row["stage_y_mm"]) if row.get("stage_y_mm") else None,
                        connector_id=row.get("connector_id", ""),
                        channel_id=row.get("channel_id", ""),
                        notes=row.get("notes", ""),
                    )
                )
        return cls(entries)

    def get_entry(self, element_row: int, element_col: int) -> PixelMapEntry:
        return self._lookup[(element_row, element_col)]

    def get_index(self, element_row: int, element_col: int) -> int:
        """Native controller_index — only meaningful for a serial-style controller."""
        return self._lookup[(element_row, element_col)].controller_index
