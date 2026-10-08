# Array Scan Project (Candice)

<<<<<<< HEAD
**Version 1.11.1.** Version numbers follow MAJOR.MINOR.PATCH: a change that makes old presets, data
files or calibrations invalid bumps MAJOR, a new feature bumps MINOR, and a bug fix bumps PATCH.

Candice measures a reconfigurable RF array board: a GRBL XY stage moves a near-field probe over a
hex grid of elements, a control board sets the elements' voltages, and a VNA records S11 at each
position. It runs voltage sweeps and per-element voltage patterns, calibrates the stage and the
board's surface warpage, and analyses the results.

## Installing and running

Python 3.10 or newer. On the lab PC (Windows), from Command Prompt:

```
python -m pip install --upgrade pip
python -m pip install numpy scipy matplotlib pyserial pyvisa pyvisa-py paramiko openpyxl
python -c "import numpy, scipy, matplotlib, serial, pyvisa, pyvisa_py, paramiko, openpyxl, tkinter; print('all good')"
python gui_app.py
```

tkinter comes with the python.org installer ("tcl/tk and IDLE" ticked), not from pip. If more than
one Python is installed, use `py -m pip ...` and `py gui_app.py` so both use the same one. NI-VISA is
optional: only needed if `vna.visa_backend` is cleared to use it instead of `pyvisa-py`.
=======
**Version 1.11.1.** The Scan tab's plot shows a one-element border around the scan grid (each
sub-grid N x M drawn as (N+2) x (M+2)), in amber, never scanned. 1.11.1 made the VNA Scan button
send the scans' own trigger. Version numbers follow MAJOR.MINOR.PATCH: breaking changes (old
presets, data files or calibrations no longer valid) bump MAJOR, new features bump MINOR, and bug
fixes bump PATCH.
>>>>>>> 098ed508e4ec6c3f2801e61f24ef63776a51f220

## Files

| File | What it is |
| --- | --- |
| `gui_app.py` | Entry point: `python gui_app.py` opens the app (three tabs: Settings, Scan, Analysis) |
| `settings_tab.py` | Settings tab (General and Advanced settings, presets, Calibrate...) |
| `scan_tab.py` | Scan tab: plot, Start/Cancel, and the manual-control side panel |
| `analysis_tab.py`, `analysis.py` | Analysis tab (widgets) and its processing (no GUI code) |
| `calibration_wizard.py` | The calibration walkthrough window |
| `surface_cal.py` | Surface calibration sets: site layout, plate files, solving, applying |
| `nfp_calibrate.py` | The copper-plate calibration script, unchanged; the app imports its functions |
| `config.py` | Every setting, as dataclasses; `config_io.py` reads and writes them as JSON presets |
| `run_scan.py` | `AutomatedArrayScanner`: runs a scan (also runnable on its own, see below) |
| `stage.py` | `GrblXY` stage driver, `HexGridPlanner` (scan geometry), `StageCalibration` |
| `calibrate_stage.py` | Stage-only calibration maths, and a minimal standalone calibration script |
| `controller.py` | `BoardController` interface shared by the two board controllers |
| `pixel_controller.py` | `PixelController`: the serial-protocol control board |
| `pi_controller.py` | `PiBoardController`: the Raspberry Pi + SPI DAC board, over SSH |
| `pinout.py`, `pinout_32x32.xlsx` | Element addressing, read straight from the board pinout |
| `Pixel_Map_by_ConnRow.csv` | The controller's wiring table, used to check the pinout |
| `pattern.py` | Reads voltage-pattern CSV files |
| `vna.py` | `VNAController`: VISA/SCPI driver for the VNA |
| `data.py` | `DataSaver`: one file per measured element, plus run metadata |
| `branding.py`, `kiss_mark.png` | App name and logo |

## Settings tab

**General settings** are what changes from run to run:

| Setting | Notes |
| --- | --- |
| Board controller | `pixel` (serial control board) or `pi` (Raspberry Pi) |
| RF band | Low band (17-21 GHz) or high band (26-30 GHz). Choosing one loads that band's VNA sweep (4001 points) into Advanced > VNA; a grey line shows the sweep that will run. Loading a preset keeps the preset's own sweep. |
| Scan type | Voltage sweep, or Voltage pattern (CSV) (see "Scans") |
| Voltages (V) / Pattern CSV | Comma-separated voltages for a sweep, or the pattern file. A grey line says what was read from the file, or in red why it can't be used. |
| Settle time (s) | Wait after setting voltages, before measuring (default 45 s) |
| Rows / Columns (per sub-grid) | 32 x 32 gives 3072 elements (see "Geometry") |
| Full-grid spacing (mm) | Nearest-neighbour distance between elements (default 4 mm) |
| Density to scan | L or H |
| L sub-grid | Which row the board's L elements start on (1 = the board's first row is L) |
| Mirror stagger direction | Which side the offset rows overhang |
| Output directory, Run name | Data goes in output directory / run name |
| Stage calibration file | The stage calibration (`.json`); blank = uncalibrated |
| Surface calibration folder | Where surface calibrations are saved; blank = next to the stage calibration file |

**Advanced settings** (Show advanced settings) list every other field in `config.py` under its real
name (`stage.port`, `vna.start_hz`, `pixels.addressing`...), generated from the config dataclasses, so
a new field appears automatically. Fields with fixed choices are dropdowns. Only the selected
controller's group (`pixels.*` or `pi.*`) is shown.

**Buttons:** Validate Settings (lists every problem at once), Save Settings / Load Settings (JSON
presets with every field), Reset to Defaults, and Calibrate... (the calibration walkthrough).
Presets saved by older versions still load; missing fields take their defaults and unknown ones are
ignored.

## Scan tab

**The plot** shows the whole grid in plot coordinates (stage X/Y, mm, before the stage calibration
is applied):
- the density being scanned in **blue**, turning **green** as each element is measured;
- the other density in **grey** (never scanned);
- a **border** of one extra row and column of every sub-grid on each side, in **amber**, never
  scanned (with 32 x 32 sub-grids: L drawn 34 x 34, H 68 x 34, 3468 positions in all), so the
  origin has elements above and beside it;
- the **safe area** as a dashed red outline (see below);
- a small **red box** where the stage is, following scans and manual moves.

**Start Scan** validates the settings and runs the scan in the background, with a progress bar and
the current voltage (or, for a pattern scan, the current element and its voltage). **Cancel Scan**
finishes the current point and stops cleanly; data measured so far is kept.

### Side panel

**Motor controls.** Positions are in plot coordinates (mm), with the stage calibration applied, so
the probe lands where the plot says.
- **Go to X** / **Go to Y**: move that axis to a position; the other stays where it is.
- **Move X by** / **Move Y by**: move that axis by an amount from where the stage is now.
- **Position** is where the stage reports it is (read back from GRBL); **Read** reads it again.

Each Go waits until the stage arrives. The panel's controls are greyed out while a move, a scan or a
calibration is running. The panel's connections (stage, board, VNA) stay open between uses and are
released automatically when a scan or calibration starts, and when the app closes.

**Safe area.** The box around every point a scan could visit (both densities), plus a 10 mm margin
(`SAFE_MARGIN_MM` at the top of `scan_tab.py`). With the default geometry: X -228.2 to +10,
Y -200 to +10. Before every Go, the panel reads the stage position and works out the exact target;
if it's outside the safe area, a dialog shows the target, the limits and how far past them it is,
with **Confirm move** and **Cancel**. Cancel is the default (Enter and Escape cancel too) and leaves
the stage exactly where it is. Scans don't use this check (every scan point is inside by
definition), and it isn't sent to GRBL; it only guards the panel's manual moves.

**STOP** (big red button, always enabled) sends GRBL's soft reset (Ctrl-X, 0x18) straight down the
serial line, so the stage stops at once, even mid-move or mid-scan. It reaches every stage
connection the app has open; if none is open, it opens the stage port just to send it. It also
cancels any running scan. Because GRBL can't vouch for its position after a reset during motion,
every motion control, Start Scan and Calibrate are then locked, and a pop-up says:
1. Cut power to the motors.
2. Move the motors back to the origin by hand.
3. Close and restart this software.
4. Restore power to the motors.

If the reset couldn't be delivered, the pop-up says so and to cut motor power now. A software stop
needs the PC, USB link and controller all working: it doesn't replace a physical emergency stop.

**Control board.** Each row has its own **SET** button; nothing is written until it's pressed.
- **Set all outputs to (V):** every output to one voltage.
- **Set voltage by .csv:** choose H or L, browse to a file, SET. The file is read exactly like a
  pattern-scan file (see "Pattern CSV files"). With the pixel controller the whole board is set and
  elements not in the file go to 0 V. The Pi controller can only set the chosen density.

**VNA.** **Scan** triggers one sweep with whatever the VNA is currently set to, using the same
trigger the scans use (`:TRIG:SING; *OPC?`, reading the reply). Nothing is configured, saved or
plotted.

## Scans

**Voltage sweep** (`uniform_board_mode` on, the default): for each voltage, set every element to
it, wait the settle time, then visit and measure every element of the scanned density. Options:
`uniform_board_settle_per_point` re-sends the voltage and waits before every point. With
`uniform_board_mode` off, each point is measured with only its own element at the voltage
(`single_pixel_settle_s`).

**Voltage pattern** (Scan type: Voltage pattern (CSV)): set every element to its own voltage from a
CSV, once, wait the settle time, then measure every element of the scanned density once.

The stage visits points row by row, reversing direction on each row of the scanned density
(serpentine), so no move is longer than one row step. At the end it returns to the origin
(`return_home`).

### Pattern CSV files

Element names come from the pinout, 1-indexed: L elements `E1_1`..`E32_32`, H elements
`H1_1`..`H64_32` (`L1_1` and `H_1_1` also accepted). Three layouts (`pattern.py`):
- **Labelled:** rows of `name, voltage` (e.g. `H1_1, 2.5`); may mix E and H; elements left out get 0 V.
- **One column (or row):** one value per element of the chosen density, row by row: the first value
  goes to element 1_1, then 1_2 ... 1_32, 2_1, and so on (2048 values for H, 1024 for L).
- **Grid:** the density's shape (64 x 32 for H, 32 x 32 for L); CSV row r, column c = element row r,
  column c. Text header lines are skipped; a header row of numbers would count as data.

A file that doesn't fit (wrong count or shape, a voltage outside the controller's range, an unknown
or repeated name) is refused with the reason before anything moves.

**What's recorded is what was sent.** The pixel controller keeps the last code it sent to each of
its outputs (as each batch is acknowledged); each element's saved voltage `e` is that code turned
back into volts. It therefore includes the DAC's 12-bit resolution (2.44 mV steps) and the 0 V given
to elements not in the file. The CSV's own value is saved alongside as `pattern_csv_v`. With the Pi
controller it's the value written to the Pi's grid file. No controller here reads voltages back, so
this is the commanded output, not a measurement at the element.

## Data saving

Each scan writes into `output directory / run name`, and refuses to start if that folder already
holds scan data.

**One file per element**, `<density>_R<row>_C<col>.npz` (row and column 0-indexed within the
density, e.g. `L_R005_C012.npz`). The file is created at the first visit with every array at its
final size and empty (NaN); each later visit fills its voltage's slot and rewrites the file
(atomically, via a temporary file). After a crash or a cancel, each file holds every voltage
measured there so far.

| Key | Shape | Contents |
| --- | --- | --- |
| `sdata` | (V, N) | complex S11 (magnitude and phase are `np.abs` / `np.angle` of it) |
| `e` | (V,) | voltage applied at each slot; NaN until measured |
| `iteration` | () | the slot written most recently |
| `measured`, `measured_time` | (V,) | which slots hold data, and when (Unix time) |
| `voltages_v` | (V,) | the planned voltage list |
| `frequencies_hz` | (N,) | frequency axis from the VNA's read-back sweep (linear) |
| `density`, `logical_row`, `logical_col` | () | which element |
| `stage_x_mm`, `stage_y_mm` | () | planned position (plot coordinates) |
| `physical_x_mm`, `physical_y_mm` | () | position after the stage calibration |
| `y_loop`, `x_loop` | () | full-grid row, and visit index within it |
| `scan_type`, `element`, `pattern_csv_v` | () | pattern scans only: `"pattern"`, pinout name, CSV value |

V = voltages (1 for a pattern scan), N = VNA points. At 4001 points: about 64 kB per voltage per
file, so about 330 MB for a 5-voltage L scan and 660 MB for H.

**`metadata.json`**, written once the hardware has connected: the full settings, voltages, frequency
axis, the stage calibration coefficients actually loaded, the point list in visit order, and the
start time. Pattern scans also save `voltage_pattern_source.csv` (the file as given) and
`voltage_pattern_applied.csv` (all 3072 elements: name, row, column, controller output, applied and
CSV voltage).

Optional (`save.save_summary_npz`, off by default): `summary_sdata.npy`, every trace in one array,
plus `summary_index.npz`. It duplicates the per-element files.

## Analysis tab

1. **Dataset folder:** browse to a run folder and **Load** (it starts with the current run's
   folder). Loading runs in the background; the line underneath says what was found.
2. **Correction:**
   - **Time-domain gate** (default), using the gate settings under Processing options; or
   - **Surface calibration**, with a **Calibration folder**: either a set saved by the calibration
     walkthrough (the newest is filled in by default), or a folder made by running
     `nfp_calibrate.py` by hand. A set is solved and applied in memory, exactly as the script
     would; it must have been measured with the same VNA sweep as the run. Switching correction
     doesn't reload the run from disk.
3. **Heatmap:** every element at its position, coloured by the chosen measure at a reference
   frequency. Sweep runs: phase range across voltage, phase at the last voltage, or magnitude range.
   Pattern runs: phase (gated, no reference), gated magnitude, or applied voltage. With surface
   calibration: also the surface gap (the fitted warp). All heatmaps use the same colours (viridis);
   phase spans -180 to 180 deg; applied voltage uses a logistic colour scale (below).
4. **Element plots:** click an element or type its row and column (0-indexed) and Show. Sweep runs
   show phase and magnitude vs voltage (at each reference frequency) and vs frequency (for each
   voltage); pattern runs show phase and magnitude vs frequency. Legends sit outside the plots; with
   more than 10 lines a colour bar replaces the legend. Save heatmap... / Save plot... write PNG or
   PDF.
5. **Phase reference** (sweep runs): linear trend or first voltage. Pattern runs subtract nothing.
6. **Processing options:** reference frequencies (default 18-20 GHz for low band, 27-29 GHz for high
   band), gate width and Tukey alpha (then Reprocess), and the voltage colour scale.

**Logistic voltage colour scale.** Applied voltage is coloured by
1 / (1 + exp(-(V - midpoint) / width)), so equal colour steps follow an element's S-shaped
phase-vs-voltage response rather than equal volts. Midpoint and width (default 5 V and 1.5 V) are
under Processing options; set them from the device's measured curve.

**Voltage map rotated 180 deg** (pattern runs). The machine origin is set for the tooling, not by
where element E1_1 is, so the software can't know which way round the board's numbering runs. If
the voltage map comes out rotated from the board, tick this: each element then shows the voltage
of the element at the position mirrored through the board's centre (from the run's
`voltage_pattern_applied.csv`). Display only; the data and the board are unchanged.

The Analysis tab also reads files from the original analysis script (`amplitudes`/`phases` keys,
`C#R#` names; 16-24 GHz is assumed when a file has no frequency axis).

## Calibration

**Settings > Calibrate...** opens a walkthrough: the steps are listed on the left and tick off as
they're done; each page says exactly what to do, a board map shows where the probe is, and one
button moves on. It needs the Stage calibration file set (surface calibrations are saved next to
it), and the VNA sweep set exactly as for the scans it will be used with.

1. **Before you start:** stage calibration, surface calibration, or both; the plate set (0, 1 and
   2 mm at every site, or all three at the centre and the surface plate only at the corners); and
   an optional **accuracy check** (an extra 1.5 mm plate at the centre).
2. **Origin:** nudge the probe onto the first L element (L row 1, column 1) with the arrow buttons
   (0.05-5 mm steps) or a typed nudge such as `0.2, -0.1`. It becomes (0, 0). There's no homing.
3. **Far Y** (the farthest L element straight down from the origin) and **far X** (the last element
   of the first L row): nudge onto each. The stage's scale and skew are solved and saved to the
   stage calibration file at once.
4. **Copper plates** at five sites (the centre, then the four corners, each 5% in from the outermost
   elements): the stage drives there; place the plate flat on the surface, then on the 1 mm and
   2 mm spacers, and Measure each (3 sweeps). Each raised plate is checked straight away against the
   phase change its spacer should give; more than 25 deg off asks you to check and Measure again.
5. **Solve and save:** `nfp_calibrate`'s calibration runs on the measurements. The page shows the
   surface height at each site, any problems in plain words, the accuracy check if taken (flagged
   past 3 deg or 0.5 dB), and the script's report. Nothing is saved until Save; the stage then
   returns to the origin.

Cancel at any point discards the measurements; a stage calibration already completed stays saved.

**What gets saved.** The stage calibration file (scale and skew) is overwritten by each stage
calibration. Each surface calibration is a new dated folder, `surface_cal_<YYYY-MM-DD_HHMM>`, so
earlier ones are kept; it holds one sub-folder per site with `Surface.npz`, `N1mm.npz`, `N2mm.npz`
(and `N1_5mm.npz` at the centre with the accuracy check), plus `sites.csv`, `calibration_info.json`
and `calibration_report.txt`. That's the layout `nfp_calibrate.py` reads, so it can still be run by
hand: `python nfp_calibrate.py --f-start 17 --f-stop 21 --n-points 4001 --cal-dir <set>
--data-dir <run> --out-dir <out>`.

**How precise the plates must be.** The 1 mm and 2 mm spacer thicknesses matter most: in
simulation, a 1 mm spacer that's really 1.01 mm shifts absolute phase by about 2.6 deg and distorts
phase-vs-voltage curves by about 1 deg, and the 1.5 mm check barely notices it. Measure the spacers
(aim for +-0.01 mm). A plate sitting uniformly high (e.g. debris) only shifts absolute phase (about
4.6 deg per 0.1 mm at 19 GHz). The plate material (copper or steel) makes well under 1 deg of
difference, as long as the surface is clean and uncoated.

**Stage calibration maths.** `StageCalibration` is linear:
`physical_x = ideal_x * scale_x + ideal_y * skew_x`, `physical_y = ideal_y * scale_y + ideal_x * skew_y`.
Scans and the panel move with `goto_ideal_xy`, which applies it; without a calibration file it's
the identity.

## Geometry

The board is three hex grids interleaved into one denser hex grid. One of them is **L**; the other
two together are **H**. Rows and columns are per sub-grid, so 32 x 32 gives 3072 elements: 1024 L
(32 x 32) and 2048 H (64 x 32).

For full-grid spacing `a` (default 4 mm, the nearest-neighbour distance): elements along a row are
`sqrt(3) * a` apart (6.93 mm), alternate rows are offset by half that, full-grid rows are `a / 2`
apart, and every third row is L. At 4 mm a 32 x 32 board spans about 218 x 190 mm.

- **Origin:** always the first L element, at (0, 0), for calibration, scans and the saved
  positions.
- **L sub-grid** (1, 2, 3): how many H rows sit above the board's first L row (none, one, two).
  Because the origin is on an L element, a wrong setting can't put H points on L elements; it only
  changes which edge rows of H are scanned and which H row is numbered 0.
- **Directions:** `geometry.x_direction_sign` / `y_direction_sign` (default -1, -1) set which way the
  stage moves for increasing column and row; `offset_odd_rows` picks which rows are offset; Mirror
  stagger direction flips which side they overhang.
- **Row numbers** (`logical_row`) count within each density (0-31 for L, 0-63 for H), matching the
  pinout's names (E5_9 is L row 4), and don't depend on L sub-grid.

## Control boards

**Pixel controller** (`controller_type = "pixel"`): framed serial protocol, 12-bit codes over
0-10 V, sent in batches of 512 elements, each acknowledged. Every set, uniform or not, sends each
element individually.

*Addressing.* Each element's address comes straight from `pinout_32x32.xlsx` (`pixels.pinout_file`
under Advanced to use another board's pinout). `pixels.addressing` chooses how elements are
identified on the wire:
- **`index`** (default): `CMD_SET_BY_INDEX` (0x12) with the element's controller index, 0-3071 (its
  position in the pinout read block by block); the firmware looks up the hardware address in its
  own wiring table.
- **`addr16`**: `CMD_SET_BY_ADDR16` (0x10) with the hardware address, `(DAC select << 9) | MUX word`,
  from the pinout's `FMC_A2A1A0` and `D8..D0` columns; the firmware's table isn't involved.

Each time the pinout is loaded it's checked against `Pixel_Map_by_ConnRow.csv`: all 3072 addresses
must be distinct, and each element's DAC select and MUX word must match the wiring table at its
index. That catches a different board's spreadsheet, missing or shifted pins, or edited addresses;
it can't catch two element names swapped between pins. `python pinout.py --export table.csv`
writes every element's index, MUX word, DAC select, address, connector and pin.

`pixels.save_to_flash_after_set` (off by default) asks the controller to store the voltages in
flash after uniform sets and patterns.

**Pi controller** (`controller_type = "pi"`): uploads the voltage grid as a CSV over SSH and restarts
the remote DAC script; the Pi does its own element mapping. The RF band picks which file
(`lb`/`hb`) gets the grid; the other is uploaded as zeros. It can only set one density at a time,
and is slower than the serial controller (every set re-uploads and restarts). **`pi.hb_shape` and
`pi.lb_shape` are placeholders (24, 8) and need the real sizes.** Note the letters overlap but mean
different things: `density_mode` L/H is low/high element density; the band lb/hb is the RF band.

## VNA

`vna.visa_resource` (default `TCPIP0::192.168.6.150::inst0::INSTR`) through `vna.visa_backend`
(default `@py`, pyvisa-py). Before a scan, the sweep (band start/stop/points) is set, every command
is checked against the instrument's error queue, and the settings are read back to confirm they
took effect. Each measurement triggers one sweep (`:TRIG:SING; *OPC?`) and reads the trace.

## Hardware notes

- Default ports: stage `COM10`, pixel controller `COM7`.
- Moves wait until the stage reports it's within `stage.position_tolerance_mm` (0.01 mm) of the
  target. Each move's timeout is its travel time at `stage.feed_mm_per_min` (500) x
  `move_timeout_factor` (1.5) + `move_timeout_extra_s` (10 s), so long moves aren't cut off.
- Positions are measured from where the stage was when it connected (or the last zero), not from
  GRBL's machine zero.
- A failed scan shows the error message only. The manual controls, the calibration walkthrough and
  the Analysis tab also print the full traceback to the console window.

## Running without the GUI

`python run_scan.py` runs one scan with the settings in its `if __name__ == "__main__":` block
(edit the output path, voltages and run name there first). `python calibrate_stage.py` runs a
stage-only calibration with simple dialogs.

## Adding hardware

`stage.py`, `controller.py` and `vna.py` each define a small interface (`MotorStage`,
`BoardController`, `VNAInstrument`) that the hardware classes implement. The scan builds a
`(cols, rows)` array of voltages and passes it to `set_voltage_grid`; turning grid positions into
hardware addresses is the controller's job. A new board controller needs `ping`, `set_voltage_grid`
and `close` (and, to record applied voltages in pattern scans, `element_voltage`), plus an entry in
`AutomatedArrayScanner.connect()` and the Settings tab's controller choice.

## Moving from the original code (Refactor V1.4.3)

- **Spacing means something different.** It's now the nearest-neighbour distance of the full grid;
  in the original code it was the L sub-grid's spacing. Divide an old spacing by sqrt(3) to describe
  the same board (and a row-spacing override by 3).
- **Data files:** one file per element (all voltages in it), replacing one file per measurement.
- **Pixel mapping files are gone:** addresses come from the pinout directly; presets naming mapping
  files still load (those entries are ignored).
- **The Debug tab is gone:** its controls are the Scan tab's side panel.

## Still to check on the real rig

Everything here was developed and tested against simulated hardware (stage, control board, VNA);
these haven't been confirmed on the real equipment yet:
- **STOP:** try it once with the motors moving slowly and nothing in the way.
- **Index order:** set a single element and confirm it's the expected one; or program one
  asymmetric pattern with `index`, then with `addr16`, and compare (if the firmware rejects
  `addr16`, the first batch reports an error such as `ERR_CMD`).
- **Calibration walkthrough** with the real plates: the 25 deg spacer check and the accuracy-check
  limits may need adjusting to the real probe.
- **VNA trigger** from the panel's Scan button.
- **Pi controller** shapes (`pi.hb_shape`, `pi.lb_shape`).
