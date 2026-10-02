from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Sequence


@dataclass
class StageConfig:
    port: str = "COM10"
    baud: int = 115200
    feed_mm_per_min: float = 500.0
    settle_s: float = 0.1
    position_tolerance_mm: float = 0.01
    readback_poll_s: float = 0.1
    startup_delay_s: float = 2.0

    # JSON file with saved scale/skew calibration coefficients (see calibrate_stage.py).
    # If unset or the file doesn't exist yet, moves are uncalibrated (identity transform).
    calibration_file: Path | None = None


@dataclass
class PixelControllerConfig:
    port: str = "COM7"
    baud: int = 115200
    timeout_s: float = 0.1
    ack_timeout_ping_s: float = 2.0
    ack_timeout_set_s: float = 3.0
    ack_timeout_save_s: float = 5.0
    batch_size_pixels: int = 512
    total_pixels: int = 3072
    min_voltage_v: float = 0.0
    max_voltage_v: float = 10.0
    save_to_flash_after_set: bool = False

    # (element_row, element_col) -> controller_index, for non-uniform voltage grids.
    # Separate files per density: L and H elements are wired to entirely different pins
    # (see build_pixel_mapping.py), and element_row means "row within that density"
    # (0 to rows-1 for L, 0 to 2*rows-1 for H) -- not a physical/dense-lattice row, so
    # one shared file can't disambiguate the two. Whichever one is loaded is picked by
    # RunConfig.geometry.density_mode at connect time. Not needed when every grid sent
    # is uniform (e.g. plain uniform-board-mode scans).
    mapping_csv_l: Path | None = None
    mapping_csv_h: Path | None = None


# VNA sweep defaults for each RF band. Picking a band in the GUI's General section loads
# these into vna.start_hz / vna.stop_hz / vna.points (still editable under Advanced).
BAND_VNA_DEFAULTS: dict[str, tuple[float, float, int]] = {
    "hb": (26e9, 30e9, 4001),  # high band
    "lb": (17e9, 21e9, 4001),  # low band
}


@dataclass
class VNAConfig:
    # Defaults match BAND_VNA_DEFAULTS["lb"], since RunConfig.band defaults to "lb".
    start_hz: float = 17e9
    stop_hz: float = 21e9
    points: int = 4001
    dwell_s: float = 0.0
    visa_resource: str = "TCPIP0::192.168.6.150::inst0::INSTR"
    visa_timeout_ms: float = 100000
    # '@py' = pyvisa-py, the pure-Python backend (no NI-VISA/vendor driver install needed).
    # Set to '' for pyvisa's default (NI-VISA or another installed vendor backend).
    visa_backend: str = "@py"


@dataclass
class ScanGeometryConfig:
    """Defines a hex-packed (triangular lattice) grid of scan points."""

    # rows/cols are the size of ONE sub-grid (L), so the full interleaved grid has
    # 3 * rows * cols points (e.g. 32 x 32 -> 3072: 1024 L + 2048 H).
    rows: int = 32
    cols: int = 32

    # Nearest-neighbor spacing of the FULL interleaved grid (all three sub-grids
    # together) -- the smallest point-to-point distance on the board. Each individual
    # sub-grid (L, or either half of H) is then a hex grid with sqrt(3) * spacing_mm
    # between its own neighbors; HexGridPlanner derives that automatically.
    spacing_mm: float = 4.0

    # Row-to-row pitch of the FULL grid. None (default) = true hex packing,
    # spacing_mm / 2, so every point is equidistant from its 6 nearest neighbors.
    # Set a number to override (stretches the grid vertically).
    row_spacing_mm: float | None = None

    offset_odd_rows: bool = True  # alternates which full-grid rows get the half-pitch horizontal offset
    stagger_sign: float = 1.0  # +1.0 (default) offsets rows to the right; -1.0 mirrors it to the left
    x_direction_sign: float = -1.0
    y_direction_sign: float = -1.0
    serpentine: bool = True  # alternate scan direction each row, to minimize stage travel

    # The physical lattice is 3 interleaved sub-lattices (a dense hex lattice 3-colored
    # by row): one sub-lattice is "L" (low density — rows/cols above describe exactly
    # this sub-lattice, same as before), the other two combined are "H" (high density,
    # 2x the point count). density_mode picks which one actually gets scanned/plotted
    # as active; l_subgrid (1, 2, or 3) picks which of the 3 possible row-phases is L.
    density_mode: str = "L"
    l_subgrid: int = 1


@dataclass
class PiControllerConfig:
    host: str = "raspberrypi.local"
    username: str = "pi"
    password: str | None = None
    key_filename: str | None = None
    port: int = 22

    local_file_hb: str = "hb_voltages.csv"
    local_file_lb: str = "lb_voltages.csv"
    remote_file_hb: str = "/home/pi/hb_voltages.csv"
    remote_file_lb: str = "/home/pi/lb_voltages.csv"
    remote_command: str = "python3 /home/pi/dac_driver.py"
    stop_file: str = "/home/pi/stop.txt"
    stop_settle_s: float = 1.0

    # The Pi has its own internal element -> DAC channel map, so no PC-side mapping is
    # needed: set_voltage_grid's array is uploaded directly as a row-per-line CSV,
    # where csv[row][col] is the voltage for the element at that (row, col) position.
    # active_band picks which file ("lb" or "hb") gets that real scan data; the other
    # file is still uploaded (in case the remote script expects both present) but
    # filled with zeros at its own configured shape. Always overwritten from
    # RunConfig.band (see RunConfig.__post_init__), so set the band there, not here.
    active_band: str = "lb"
    hb_shape: tuple[int, int] = (24, 8)  # (rows, cols) for DataHB.csv when it's the inactive band
    lb_shape: tuple[int, int] = (24, 8)  # (rows, cols) for DataLB.csv when it's the inactive band

    min_voltage_v: float = 0.0
    max_voltage_v: float = 10.0


@dataclass
class SaveConfig:
    output_dir: Path = Path(r"C:\Data\ArrayScans")
    run_name: str = "scan_run"
    save_individual_npz: bool = True
    save_summary_npz: bool = True
    save_metadata_json: bool = True


@dataclass
class RunConfig:
    stage: StageConfig = field(default_factory=StageConfig)
    pixels: PixelControllerConfig = field(default_factory=PixelControllerConfig)
    pi: PiControllerConfig = field(default_factory=PiControllerConfig)
    vna: VNAConfig = field(default_factory=VNAConfig)
    geometry: ScanGeometryConfig = field(default_factory=ScanGeometryConfig)
    save: SaveConfig = field(default_factory=SaveConfig)

    voltages_v: Sequence[float] = (0.0,)
    return_home: bool = True
    uniform_board_mode: bool = True
    dry_run: bool = False

    # Which BoardController implementation to use: "pixel" (PixelController, serial
    # protocol) or "pi" (PiBoardController, SSH file-upload to a Raspberry Pi).
    controller_type: str = "pixel"

    # RF band for this run: "lb" (low band) or "hb" (high band). The single source of
    # truth for band: pi.active_band is synced from it automatically, and the GUI loads
    # BAND_VNA_DEFAULTS[band] into the VNA sweep when the band is changed.
    band: str = "lb"

    uniform_board_settle_s: float = 45.0
    single_pixel_settle_s: float = 0.2

    # Only relevant when uniform_board_mode=True. False (default): send the grid once
    # per voltage step, delay once, then scan every point. True: re-send the grid and
    # delay again before every single point (slower, but re-affirms voltage at each
    # measurement).
    uniform_board_settle_per_point: bool = False

    def __post_init__(self) -> None:
        if self.pi.active_band != self.band:
            self.pi = replace(self.pi, active_band=self.band)
