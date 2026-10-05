"""
Voltage pattern scans: one voltage per element, read from a CSV.

Elements are named the way the pinout names them, 1-indexed: L elements are E<row>_<col>
(E1_1 .. E32_32) and H elements are H<row>_<col> (H1_1 .. H64_32). "L<row>_<col>" is
accepted for L too, and an underscore after the letter is optional (H_1_1 = H1_1).
Internally rows/columns are 0-indexed, so H1_1 is (H, row 0, col 0) — the same numbering
as the mapping CSVs, the scan points, and the data file names.

Three CSV layouts are accepted:
  - Labelled: each row holds an element name and a voltage, e.g. "H1_1, 2.5". May mix E
    and H elements. Elements left out get 0 V. Rows without a name (headers) are skipped.
  - One column (or one row) of voltages, for the scanned density, taken row by row:
    the first value goes to H1_1, then H1_2 .. H1_32, H2_1, and so on.
  - A grid with the scanned density's shape: CSV row r, column c -> element row r,
    column c (so 64 x 32 for H, 32 x 32 for L).
Header lines that aren't numbers are skipped in all three.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from config import ScanGeometryConfig

LABEL_RE = re.compile(r"^([ELH])_?(\d+)_(\d+)$", re.IGNORECASE)


def density_shape(geometry: ScanGeometryConfig, density: str) -> tuple[int, int]:
    """(rows, cols) of one density: L is rows x cols, H has twice the rows."""
    rows = geometry.rows if density == "L" else geometry.rows * 2
    return rows, geometry.cols


def element_label(density: str, row: int, col: int) -> str:
    """Pinout-style 1-indexed name for a 0-indexed element, e.g. ("H", 0, 0) -> "H1_1"."""
    return f"{'E' if density == 'L' else 'H'}{row + 1}_{col + 1}"


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


@dataclass
class VoltagePattern:
    voltages: dict[tuple[str, int, int], float]  # (density, row, col), 0-indexed -> volts
    layout: str                                   # "labelled", "list" or "grid"
    source: str
    unassigned: dict[str, int] = field(default_factory=dict)  # density -> elements left at 0 V

    def voltage(self, density: str, row: int, col: int) -> float:
        return self.voltages.get((density, row, col), 0.0)

    def densities(self) -> list[str]:
        return sorted({k[0] for k in self.voltages})

    def grid(self, density: str, geometry: ScanGeometryConfig) -> np.ndarray:
        """This density's voltages as a (cols, rows) array (the controllers' convention)."""
        rows, cols = density_shape(geometry, density)
        out = np.zeros((cols, rows))
        for (d, r, c), v in self.voltages.items():
            if d == density:
                out[c, r] = v
        return out

    def describe(self) -> str:
        parts = []
        for d in self.densities():
            vals = [v for k, v in self.voltages.items() if k[0] == d]
            span = f"{min(vals):g} V" if min(vals) == max(vals) else f"{min(vals):g} to {max(vals):g} V"
            parts.append(f"{len(vals)} {d} value{'s' if len(vals) != 1 else ''}, {span}")
        text = "; ".join(parts) if parts else "no values"
        how = {"labelled": "by element name", "list": "one column, row by row from the first element",
               "grid": "grid, CSV row = element row"}[self.layout]
        text += f" ({how})"
        missing = [f"{n} {d}" for d, n in self.unassigned.items() if n]
        if missing:
            text += f"; {', '.join(missing)} not in the file (set to 0 V)"
        return text


def load_pattern(path: str | Path, geometry: ScanGeometryConfig,
                 min_v: float = 0.0, max_v: float = 10.0) -> VoltagePattern:
    """Reads a pattern CSV. Raises ValueError with a plain explanation if it can't be used."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"Pattern file not found: {path}")
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [[cell.strip() for cell in row] for row in csv.reader(f)]
    rows = [r for r in rows if any(r)]
    if not rows:
        raise ValueError(f"{path.name} is empty.")

    scanned = geometry.density_mode
    labelled = any(LABEL_RE.match(cell) for row in rows for cell in row)
    voltages: dict[tuple[str, int, int], float] = {}

    if labelled:
        layout = "labelled"
        for line_no, row in enumerate(rows, start=1):
            label = next((c for c in row if LABEL_RE.match(c)), None)
            if label is None:
                continue  # header or comment line
            values = [_number(c) for c in row if c != label and _number(c) is not None]
            if not values:
                raise ValueError(f"Line {line_no}: no voltage next to {label}.")
            letter, r, c = LABEL_RE.match(label).groups()
            density = "H" if letter.upper() == "H" else "L"
            rows_d, cols_d = density_shape(geometry, density)
            r, c = int(r) - 1, int(c) - 1
            if not (0 <= r < rows_d and 0 <= c < cols_d):
                raise ValueError(
                    f"Line {line_no}: {label} is outside the board ({density} rows run 1-{rows_d}, columns 1-{cols_d})."
                )
            key = (density, r, c)
            if key in voltages:
                raise ValueError(f"Line {line_no}: {label} appears more than once.")
            voltages[key] = values[0]
    else:
        numeric = []
        for line_no, row in enumerate(rows, start=1):
            values = [_number(c) for c in row if c != ""]
            if all(v is not None for v in values):
                numeric.append(values)
            elif numeric:
                raise ValueError(f"Line {line_no} isn't all numbers: {', '.join(row)}")
            # else: a header line before the numbers start; skip it
        if not numeric:
            raise ValueError(f"{path.name} has no voltages in it.")
        rows_d, cols_d = density_shape(geometry, scanned)
        widths = {len(r) for r in numeric}
        if widths == {1} or len(numeric) == 1:
            layout = "list"
            flat = [v for r in numeric for v in r]
            if len(flat) != rows_d * cols_d:
                raise ValueError(
                    f"{path.name} has {len(flat)} voltages, but scanning {scanned} needs {rows_d * cols_d} "
                    f"({rows_d} rows x {cols_d} columns), one per element, row by row."
                )
            for i, v in enumerate(flat):
                voltages[(scanned, i // cols_d, i % cols_d)] = v
        elif len(numeric) == rows_d and widths == {cols_d}:
            layout = "grid"
            for r, row in enumerate(numeric):
                for c, v in enumerate(row):
                    voltages[(scanned, r, c)] = v
        else:
            raise ValueError(
                f"{path.name} is a {len(numeric)} x {max(widths)} table. Scanning {scanned} needs either one "
                f"column of {rows_d * cols_d} voltages, a {rows_d} x {cols_d} grid, or rows of "
                f"'element name, voltage' (e.g. {element_label(scanned, 0, 0)}, 2.5)."
            )

    bad = [(k, v) for k, v in voltages.items() if not (min_v <= v <= max_v) or np.isnan(v)]
    if bad:
        (d, r, c), v = bad[0]
        raise ValueError(
            f"{len(bad)} voltage(s) outside the controller's {min_v:g}-{max_v:g} V range, "
            f"e.g. {element_label(d, r, c)} = {v:g} V."
        )

    unassigned = {}
    for d in sorted({k[0] for k in voltages} | {scanned}):
        rows_d, cols_d = density_shape(geometry, d)
        unassigned[d] = rows_d * cols_d - sum(k[0] == d for k in voltages)
    return VoltagePattern(voltages, layout, str(path), unassigned)
