"""
Element addressing, read straight from the pinout spreadsheet (pinout_32x32.xlsx, shipped
in the app folder). No separate mapping file: the pinout already says, for every pin,
which element it drives (E<row>_<col> for L, H<row>_<col> for H) and its MUX word.

The pixel controller's protocol addresses an element by index (CMD_SET_BY_INDEX), and the
firmware turns that index into the MUX/DAC address. The index is the element's position
in the pinout read in this order: for each of the 8 column blocks, for each of the 5 row
bands, both interleaved pin columns (odd pins, even pins) top to bottom, GND pins
skipped. That order reproduces Pixel_Map_by_ConnRow.csv (the controller's wiring table)
exactly. Every time the pinout is loaded it is checked against that table: the MUX word
at each index must match. (A MUX word alone isn't unique, since each is shared by six
elements on different DAC selects; the index is.) A mismatch stops the load, which
catches a different board's spreadsheet, a missing or extra pin, rows shifted out of
order, or an edited MUX word. It can't catch two element names swapped while their pins
stay put: the wiring table has no element names to compare against, so the names in the
pinout are trusted as written.

Rows/columns here are 0-indexed within each density: E1_1 -> ("L", 0, 0), H64_32 ->
("H", 63, 31), the same numbering as scan points, data files and pattern CSVs.

Run directly to print a summary, or export the element table for reference:
    python pinout.py                      (summary of the shipped pinout)
    python pinout.py --export table.csv   (one row per element: name, index, MUX word, pin)
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_PINOUT = Path(__file__).with_name("pinout_32x32.xlsx")
WIRING_TABLE = Path(__file__).with_name("Pixel_Map_by_ConnRow.csv")

ELEMENT_RE = re.compile(r"^([EH])(\d+)_(\d+)$")
SHEET = "PINOUT CONTROLLER"
COL_STARTS = [2, 15, 28, 41, 54, 67, 80, 93]   # the 8 column blocks
ROW_STARTS = [1, 46, 89, 132, 175]             # the 5 row bands


@dataclass(frozen=True)
class Pin:
    index: int          # controller index (what CMD_SET_BY_INDEX takes)
    label: str          # pinout name, e.g. "H1_4"
    density: str        # "L" (E-labelled) or "H"
    row: int            # 0-indexed within the density
    col: int
    muxword: int
    connector: str
    pin: int
    dac_select: int     # FMC_A2A1A0 in the pinout (0-7)

    @property
    def addr16(self) -> int:
        """The element's hardware address, as CMD_SET_BY_ADDR16 takes it: (DAC select << 9) | MUX word."""
        return (self.dac_select << 9) | self.muxword


class Pinout:
    def __init__(self, pins: list[Pin], source: Path) -> None:
        self.pins = pins
        self.source = source
        self._by_element = {(p.density, p.row, p.col): p for p in pins}
        self._by_addr = {p.addr16: p for p in pins}

    def index(self, density: str, row: int, col: int) -> int:
        try:
            return self._by_element[(density, row, col)].index
        except KeyError:
            raise KeyError(f"No {density} element at row {row}, column {col} in {self.source.name}") from None

    def pin(self, density: str, row: int, col: int) -> Pin:
        return self._by_element[(density, row, col)]

    def index_of_addr(self, addr: int) -> int:
        """The controller index of the element with this hardware address."""
        return self._by_addr[addr].index

    def count(self, density: str) -> int:
        return sum(p.density == density for p in self.pins)


def _read_sheet(path: Path) -> list[tuple]:
    """(connector, pin, label, muxword, dac_select) for every non-GND pin, in controller-index
    order. Each block's columns are: pin, element, MUX word (D8..D0), DAC select (FMC_A2A1A0)
    for the odd pins, and the same again 6 columns to the right for the even pins."""
    import openpyxl  # only needed here, so the rest of the app doesn't depend on it

    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    if SHEET not in wb.sheetnames:
        raise ValueError(f"{path.name} has no '{SHEET}' sheet.")
    ws = wb[SHEET]
    entries = []
    for col_start in COL_STARTS:
        for row_start in ROW_STARTS:
            conn_name = ws.cell(row=row_start, column=col_start).value
            r = row_start + 3
            while True:
                pin_a = ws.cell(row=r, column=col_start).value
                pin_b = ws.cell(row=r, column=col_start + 6).value
                if pin_a is None and pin_b is None:
                    break
                if pin_a is not None:
                    entries.append((conn_name, pin_a, ws.cell(row=r, column=col_start + 1).value,
                                    ws.cell(row=r, column=col_start + 2).value,
                                    ws.cell(row=r, column=col_start + 3).value))
                if pin_b is not None:
                    entries.append((conn_name, pin_b, ws.cell(row=r, column=col_start + 7).value,
                                    ws.cell(row=r, column=col_start + 8).value,
                                    ws.cell(row=r, column=col_start + 9).value))
                r += 1
    return [e for e in entries if e[2] != "GND"]


def _parse_binary(value, label, what: str) -> int:
    """The pinout writes MUX words and DAC selects as binary digits ('101110000', '001'). If
    Excel has turned one into a number, its digits are still the binary digits: the number
    101 means binary 101 (= 5), not decimal 101. (A lost leading zero doesn't matter.)"""
    if value is None or str(value).strip() == "":
        raise ValueError(
            f"No {what} for {label}. If the spreadsheet was re-saved by a program that doesn't "
            f"keep formula results, open it in Excel and save it again."
        )
    text = str(int(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value).strip()
    if not set(text) <= {"0", "1"}:
        raise ValueError(f"The {what} for {label} isn't binary: {value!r}")
    return int(text, 2)


def _check_against_wiring(pins: list[Pin], wiring_path: Path, source: Path) -> None:
    # The full hardware address at each index must match: MUX word and DAC select (the wiring
    # table's dac_sel_int is the pinout's FMC_A2A1A0 read as binary).
    with open(wiring_path, newline="", encoding="utf-8-sig") as f:
        rows = {int(r["global_index"]): (int(r["dac_sel_int"]) << 9) | int(r["muxword_hex"], 16)
                for r in csv.DictReader(f)}
    if len(rows) != len(pins):
        raise ValueError(f"{source.name} has {len(pins)} element pins but {wiring_path.name} has {len(rows)} entries.")
    bad = [p for p in pins if rows.get(p.index) != p.addr16]
    if bad:
        p = bad[0]
        want = rows.get(p.index, 0)
        raise ValueError(
            f"{source.name} doesn't match the controller wiring table ({wiring_path.name}) at {len(bad)} index(es), "
            f"e.g. index {p.index} ({p.label}): MUX word {p.muxword:#x}, DAC select {p.dac_select} in the pinout; "
            f"MUX word {want & 0x1FF:#x}, DAC select {want >> 9} in the wiring table. "
            f"Voltages would go to the wrong elements."
        )


@lru_cache(maxsize=4)
def _load_cached(path: str, mtime: float, wiring: str | None) -> Pinout:
    source = Path(path)
    pins = []
    for index, (conn, pin_no, label, mux, dac) in enumerate(_read_sheet(source)):
        m = ELEMENT_RE.match(str(label).strip())
        if not m:
            raise ValueError(f"{source.name}: unrecognised element name {label!r} at index {index} ({conn} pin {pin_no}).")
        letter, r, c = m.groups()
        pins.append(Pin(index, f"{letter}{r}_{c}", "L" if letter == "E" else "H", int(r) - 1, int(c) - 1,
                        _parse_binary(mux, label, "MUX word"), str(conn), int(pin_no),
                        _parse_binary(dac, label, "DAC select")))
    seen = {}
    for p in pins:
        key = (p.density, p.row, p.col)
        if key in seen:
            raise ValueError(f"{source.name}: {p.label} appears twice (indexes {seen[key]} and {p.index}).")
        seen[key] = p.index
    addresses = {}
    for p in pins:
        if p.addr16 in addresses:
            raise ValueError(f"{source.name}: {p.label} and {addresses[p.addr16]} have the same hardware address "
                             f"(DAC select {p.dac_select}, MUX word {p.muxword:#x}).")
        addresses[p.addr16] = p.label
    if wiring is not None:
        _check_against_wiring(pins, Path(wiring), source)
    return Pinout(pins, source)


def load_pinout(path: str | Path | None = None) -> Pinout:
    """The pinout at `path` (default: the one shipped with the app), checked against the
    wiring table if that's present. Cached, and reloaded automatically if the file changes."""
    source = Path(path) if path else DEFAULT_PINOUT
    if not source.is_file():
        raise FileNotFoundError(f"Pinout spreadsheet not found: {source}")
    wiring = str(WIRING_TABLE) if WIRING_TABLE.is_file() else None
    return _load_cached(str(source.resolve()), os.path.getmtime(source), wiring)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pinout", nargs="?", default=None, help="pinout spreadsheet (default: the shipped one)")
    parser.add_argument("--export", metavar="CSV", help="write one row per element for reference")
    args = parser.parse_args()
    po = load_pinout(args.pinout)
    checked = "checked against " + WIRING_TABLE.name if WIRING_TABLE.is_file() else "no wiring table to check against"
    print(f"{po.source.name}: {po.count('L')} L (E) elements, {po.count('H')} H elements, indexes 0-{len(po.pins) - 1}; {checked}")
    if args.export:
        with open(args.export, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["element", "density", "row", "col", "controller_index", "muxword_hex", "dac_select",
                        "addr16_hex", "connector", "pin"])
            for p in po.pins:
                w.writerow([p.label, p.density, p.row, p.col, p.index, f"{p.muxword:#05x}", p.dac_select,
                            f"{p.addr16:#05x}", p.connector, p.pin])
        print("wrote", args.export)
