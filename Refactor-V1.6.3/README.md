# Array Scan Project

**Version 1.6.3.** The pixel controller reads each element's address straight from the pinout
spreadsheet; the separate mapping CSVs, their settings, and the scripts that generated them are gone.
1.6.2 shipped generated mapping files; 1.6.1 made pattern-run analysis time-gated; 1.6.0 added
voltage pattern scans. Version numbers follow MAJOR.MINOR.PATCH: breaking changes (old presets, data
files or calibrations no longer valid) bump MAJOR, new features bump MINOR, and bug fixes bump PATCH.

## Files

- `config.py` — all experiment settings (dataclasses)
- `stage.py` — motor stage handler (`MotorStage` interface + `GrblXY` implementation),
  the scan geometry it moves through (`HexGridPlanner`), and stage calibration (`StageCalibration`)
- `calibrate_stage.py` — one-time interactive calibration utility (run standalone, not part of a scan)
- `controller.py` — `BoardController` interface, plus small helpers shared by controllers
- `pixel_controller.py` — `PixelController`: the framed-serial-protocol board controller
- `pi_controller.py` — `PiBoardController`: SSH/file-upload board controller for the
  Raspberry Pi + SPI DAC setup (wraps `PiController`, the paramiko transport layer)
- `vna.py` — VNA handler: `VNAInstrument` interface + `VNAController`, the concrete
  VISA/SCPI driver for the lab's VNA (this used to be the separate `VNATest.py`)
- `data.py` — data handler: `DataSaver`
- `run_scan.py` — coordinator (`AutomatedArrayScanner`) that wires the four handlers
  together and runs the scan; also has the example run config in `__main__`
- `pinout.py` — reads each element's controller index straight from `pinout_32x32.xlsx` (needs
  `openpyxl`), checked against `Pixel_Map_by_ConnRow.csv`; see "Element addressing" below
- `pinout_32x32.xlsx` — the board pinout: element names, pins and MUX words
- `Pixel_Map_by_ConnRow.csv` — the controller's wiring table (index, connector, pin, MUX word, DAC select)
- `config_io.py` — RunConfig <-> JSON (for saving/loading GUI presets)
- `branding.py` — app name ("Candice") and the kiss-mark logo loader
- `kiss_mark.png` — the logo image (40x35, transparent background)
- `settings_tab.py` — GUI tab 1: Settings & Configuration (`SettingsTab`)
- `scan_tab.py` — GUI tab 2: Scan (`ScanTab`) — live geometry preview, progress bar, Start/Stop
- `debug_tab.py` — GUI tab 3: Debug (`DebugTab`) — manual stage jog + board-wide voltage set
- `pattern.py` — reads voltage pattern CSVs (one voltage per element, by pinout name, list or grid)
- `analysis.py` — dataset loading, time gating and phase/magnitude processing for the Analysis tab (adapted from `ElementToElementPTV.py`; no GUI code)
- `analysis_tab.py` — GUI tab 4: Analysis (`AnalysisTab`) — board heatmap and per-element 2x2 plots
- `gui_app.py` — GUI entry point; run this directly (`python gui_app.py`)

## Adding a new controller, stage, or VNA

`stage.py`, `controller.py`, and `vna.py` each define a small interface (`MotorStage`,
`BoardController`, `VNAInstrument`) that the real hardware classes implement:

```python
class BoardController(ABC):
    def ping(self) -> dict: ...
    def set_voltage_grid(self, voltages: np.ndarray) -> None: ...  # voltages[col, row]
    def close(self) -> None: ...
```

`run_scan.py` never touches per-pixel addressing at all — it builds a `(cols, rows)`
numpy array of the voltage each grid element should get and hands the whole thing to
`set_voltage_grid`. Translating a grid position into physical hardware addressing
(serial index, band+channel, whatever) is entirely the controller's job.

For `BoardController`, `RunConfig.controller_type` picks which implementation
`AutomatedArrayScanner.connect()` constructs — `"pixel"` (default, `PixelController`)
or `"pi"` (`PiBoardController`). Nothing else in `run_scan.py` needs to change, since it
only calls methods on the interface.

## Using the Pi controller

Set `controller_type="pi"` and fill in `RunConfig.pi` (a `PiControllerConfig`) with your
SSH host/credentials and file paths. **No PC-side element mapping at all** — the Pi has
its own internal element-to-DAC map, so `set_voltage_grid`'s array is uploaded directly:
`csv[row][col]` in the uploaded file is the voltage for the element at that position,
full stop. Concretely, `voltages` (shape `(cols, rows)`, this project's convention) gets
transposed to `(rows, cols)` and written as one line per row, comma-separated columns —
verified directly against a worked example (a 3x3 grid, checked cell by cell) before
this was wired in.

`PiControllerConfig.active_band` ("lb" or "hb") picks which file gets that real,
scan-derived grid — the *other* band's file is still uploaded (some remote scripts
expect both present) filled with zeros, sized by its own `hb_shape`/`lb_shape`
(`(rows, cols)`, independent of the active band's shape, since the two bands can be
physically different sizes). **`hb_shape`/`lb_shape` are placeholder values (`(24, 8)`)
and need real numbers before trusting this.**

**Naming collision worth being careful about:** this project's `ScanGeometryConfig.density_mode`
uses `"L"`/`"H"` for a completely different concept (low/high density — which of the 3
interleaved sub-lattices is being scanned), while `PiControllerConfig.active_band` uses
`"lb"`/`"hb"` for RF frequency band. They're unrelated axes that happen to share letters —
a low-density (`density_mode="L"`) scan could target either the low-band or high-band RF
path (`active_band="lb"` or `"hb"`), and vice versa. Flagging this now since it's an easy
mix-up, not renaming anything without being asked.

**Because this controller pushes over SSH and restarts a remote process on every write**,
it's inherently slower than the serial controller — there's no more "uniform grid" fast
path to skip work, either, since every `set_voltage_grid` call now does the exact same
amount of work (write 2 files, one push) regardless of whether the grid is uniform or not.

## GUI

`python gui_app.py` opens **Candice**, a four-tab window (Settings, Scan, Debug, Analysis) (needs tkinter, which ships with
standard Python installs on Windows) with a lipstick-kiss logo in the header
(`kiss_mark.png`, loaded via `branding.load_kiss_mark_image()`). This is a real desktop
app, not a web page — it needs direct access to COM ports, local files, and SSH, none of
which a browser page could reach, so tkinter (stdlib, matches what your existing scripts
already use) is the natural fit here.

What the GUI covers end to end:
- **Change experiment-specific settings** — Settings tab (General, plus Advanced for everything else).
- **View experiment progress and the points to be scanned** — Scan tab (live matplotlib
  preview, progress bar + voltage label, current-position box).
- **Calibrate the stage for the particular DUT** — Settings tab, **Calibrate Stage...** button.
- **Begin a scan** — Scan tab's **Start Scan** button (and **Stop Scan** to cancel one
  cleanly mid-run).
- **Manual hardware debugging** — Debug tab (see below).

**Settings & Configuration tab** (`settings_tab.py`, `SettingsTab`), reorganised in V1.4.5 into two parts.

*General settings* holds what an operator changes run to run:
- Board controller (`pixel` / `pi`)
- RF band: low band (17-21 GHz) or high band (26-30 GHz). Choosing a band loads that band's default
  VNA sweep (`config.BAND_VNA_DEFAULTS`: LB 17-21 GHz, HB 26-30 GHz, 4001 points each) into the
  Advanced VNA fields, and a grey line under the selector shows the sweep that will actually run.
  Loading a preset does *not* apply band defaults, so a preset's own sweep is kept exactly as saved.
- Voltages (comma-separated) and settle time (`uniform_board_settle_s`, default 45 s)
- Grid size (rows and columns per sub-grid), full-grid spacing, density to scan, L sub-grid, and
  mirror stagger direction
- File paths (moved here in V1.4.6): output directory and run name (`save.output_dir`,
  `save.run_name`; data lands in output directory / run name), stage calibration file
  (`stage.calibration_file`). The Pi controller's `pi.local_file_*`/`remote_file_*`
  paths stay under Advanced, since they're fixed wiring for the Pi setup rather than per-run files.

*Advanced settings* (collapsed by default; **Show advanced settings**) holds every other field in
`config.py`, each labelled with its real variable name (`stage.port`, `vna.start_hz`,
`geometry.x_direction_sign`, ...) and a grey hint for its type. It's generated from the config
dataclasses themselves, so a field added to `config.py` appears here automatically (and an
annotation type the tab doesn't know how to edit fails loudly at startup, rather than silently
vanishing). Only the selected controller's group (`pixels.*` or `pi.*`) is shown. The one field not
shown is `pi.active_band`, which is always derived from the RF band (`RunConfig.band`, synced in
`RunConfig.__post_init__`), so there's one source of truth for band.

This replaces the old design where hardware/connection fields were hidden and only settable through
`config.py` or a preset. They're still tucked away under Advanced, but editable.

Buttons at the bottom: **Validate Settings**, **Save Settings... / Load Settings...** (JSON presets via
`config_io.py`, now including every field), **Reset to Defaults**, and **Calibrate Stage...** (uses the stage
calibration file from General and the stage settings under Advanced).

Validation collects every problem in the form at once and shows them together, using the General
labels or the variable name. `get_config()` requires `save.output_dir` and `save.run_name`;
`get_config(require_save=False)` skips just those two checks, which is what the Debug tab and stage
calibration use so a blank run name can't block a manual jog.

Presets from older versions still load: missing fields fall back to their defaults, and a preset with
no `band` takes it from `pi.active_band`. (The V1.4.4 note about spacing meaning still applies to
presets older than that.) Saving a preset with a mapping CSV path set used to crash with
`TypeError: PosixPath is not JSON serializable`; fixed.

Verified with real tkinter under a virtual display (not a fake this time): defaults and a set of
custom values in every section round-trip through every widget exactly, the band switch loads the
right sweep and syncs `pi.active_band`, validation reports all problems together, the live preview
hook fires for geometry fields in both sections, presets save and reload, and a dry-run scan started
from the Scan tab's Start button runs to 100%.

## Scan tab

`scan_tab.py`, `ScanTab`. A matplotlib scatter of every active point (`stage_x_mm`,
`stage_y_mm`), a progress bar, and Start Scan / Stop Scan buttons.

**Live geometry preview.** The plot rebuilds from `SettingsTab.get_geometry_or_none()`
(a non-raising variant of the geometry portion of `get_config()` — returns `None` on
invalid/incomplete input instead of erroring, so a half-typed number doesn't break
anything). It's called two ways: `SettingsTab.on_geometry_change` fires on every
`<KeyRelease>` in the four geometry fields (rows, cols, spacing, row spacing override),
and switching to the Scan tab calls it again via `<<NotebookTabChanged>>` — that second
path is what catches Load Settings / Reset to Defaults, which change many fields at once
without firing individual key events.

**Running a scan.** Start Scan calls `SettingsTab.get_config()` (full validation, same as
the Settings tab's own Validate button) and, if valid, launches `AutomatedArrayScanner` in
a background thread — a real scan blocks on serial/SSH/VISA I/O, so it can't run on the
GUI thread without freezing the window. The scanner now takes two optional callbacks:

```python
AutomatedArrayScanner(config, on_progress=callback, cancel_event=threading.Event())
```

`on_progress(event: ProgressEvent)` fires after every point is measured, where
`ProgressEvent` bundles `step`, `total_steps`, `point`, `voltage_index`, `voltage_count`,
and `voltage_v` — `run()` computes `total_steps = voltage_count * point_count` up front, so
`event.step / event.total_steps` is a direct progress fraction over the *whole* run, and
the voltage fields are what drives the "Voltage 2/5: 1.000 V" label next to the progress
bar. `cancel_event` is checked before each point; if set, the scan stops cleanly after the
point in progress (hardware still gets homed/closed via the usual `finally: self.close()`)
and skips writing the final summary `.npz` (per-point files already written up to that
point are kept).

Because `on_progress` runs on the worker thread, `ScanTab` never touches widgets or the
plot from inside it — it only pushes onto a `queue.Queue`, which the GUI thread drains via
`self.after(100, self._poll_queue)`. That's the standard, correct pattern for tkinter:
direct cross-thread widget mutation isn't safe. Each drained progress message updates the
progress bar and recolors that point's marker via `scatter.set_facecolor(...)`.

**Three colors, not two.** The plot always shows the *whole* dense lattice (both L and H —
`HexGridPlanner.all_points()`), not just what's being scanned. Whichever density
`ScanGeometryConfig.density_mode` selects starts **blue**, then turns **green** the first
time each point is measured (later voltage steps revisiting it are idempotent — it
doesn't turn "more green"). The *other* density is **grey** from the start and never
changes, since it's never scanned at all — `on_progress` only ever fires for active
points, so inactive ones simply never receive an event to react to. Switching
`density_mode` (or `l_subgrid`) in Settings immediately swaps which set is blue/green vs
grey, live, via the same geometry-change hook everything else in this tab already uses.

A small red box (`matplotlib.patches.Rectangle`) also moves to each measured point's
position — a "the stage is currently here" indicator, distinct from the blue/green/grey
history dots. It's sized relative to that run's point spacing (`0.6 × spacing_mm`) so it
stays legible on coarse or fine grids, and it tracks in the same ideal/planned coordinate
space as the rest of the plot (not the post-calibration physical position), consistent with
everything else drawn there. It only appears once a scan is actually running — starting a
new scan or editing geometry (which redraws the whole plot) clears it until the next
progress update places it again.

I verified all of this except the actual widget rendering, which needs a real display:
the progress-callback math and cooperative cancellation directly against `run_scan.py`
(exact step/total counts, and that cancelling mid-run skips the summary but keeps
per-point data already collected); the matplotlib scatter-and-recolor calls directly
against a real (non-Tk) `Figure`/`Axes`, confirming `set_facecolor` correctly updates
only the intended point; and the full GUI wiring — app construction, live geometry
linking, tab-switch refresh, and a complete scan from Start through 100% through Stop —
by extending the same fake-tkinter harness from before with a stub for matplotlib's
Tk-specific canvas (the real `Figure`/`Axes` still does the actual plotting inside it) and
actually running the real background thread to completion.

## Voltage pattern scans (V1.6.0)

Settings > General > **Scan type: Voltage pattern (CSV)** sets each element to its own voltage from
a CSV, once, then measures every element of the scanned density (L or H) a single time. There is no
voltage sweep and no reference: each element's file holds one raw measurement.

**Element names** are the pinout's, 1-indexed: L elements are `E1_1` .. `E32_32`, H elements are
`H1_1` .. `H64_32` (`L1_1` and `H_1_1` spellings also work). `H1_1` is H row 0, column 0 in the
0-indexed numbering used by file names and the Analysis tab.

**CSV layouts** (`pattern.py`):
- **Labelled:** rows of `element name, voltage`, e.g. `H1_1, 2.5`. May mix E and H elements. Any
  element left out is set to 0 V.
- **One column (or one row):** one voltage per element of the scanned density, row by row: the first
  value goes to H1_1, then H1_2 .. H1_32, H2_1, and so on (2048 values for H, 1024 for L).
- **Grid:** rows x columns of the scanned density (64 x 32 for H, 32 x 32 for L); CSV row r,
  column c is element row r, column c.

Header lines are skipped. The grey line under the field says what was read (e.g. "2048 H values, 0
to 7.5 V (by element name)") or, in red, why the file can't be used (wrong count, a voltage outside
the controller's range, a duplicate or unknown name). Validate and Start refuse an unusable file
before any hardware moves.

**Hardware:** with the pixel controller, every output is first set to 0 V, then each element in the
pattern is set by its index from the pinout (see "Element addressing" below), so nothing needs
selecting. Elements of the other density not in the file stay at 0 V.
The Pi controller takes one grid for the scanned density only, so a pattern that sets the other
density's elements to anything but 0 V is refused. After programming, the scan waits the settle time.

**Saved data:** one file per element as usual, with one measurement (`sdata` shape (1, N)), `e` =
that element's pattern voltage, plus `scan_type = "pattern"` and `element` (its pinout name). The
run folder also gets `voltage_pattern_source.csv` (the file as given) and
`voltage_pattern_applied.csv` (all 3072 elements, name, row, column, and the voltage actually
set), and `metadata.json` records `scan_mode: "voltage_pattern"`.

**Analysis:** the Analysis tab recognises a pattern run. Each element is time-gated with the same
gate settings as sweep runs (Processing options), but nothing is subtracted: no linear trend, no
first-voltage reference. The heatmap offers **Phase, no reference** (the gated phase, -180 to 180
deg, drawn with a cyclic colour scale since -180 and +180 are the same phase), **Gated magnitude**,
and **Applied voltage** (the pattern itself, to check it went where intended). Selecting an element
shows its gated phase and magnitude vs frequency with the heatmap frequency marked. The phase
reference option is greyed out.

Verified: pattern files built from the real pinout labels load in all three layouts, and bad files
are rejected with the reasons above; addressed from the pinout, all 3072 controller
outputs end at exactly their element's voltage; a pattern scan run from the GUI completes and saves
the files above; and on a synthetic pattern run with a known phase for every element plus a late reflection,
the gated-phase heatmap recovers each element's direct-path phase (the reflection gated out) and
the applied-voltage heatmap matches the pattern exactly.

## Element addressing: straight from the pinout (V1.6.3)

The pixel controller's protocol sets an element by index (`CMD_SET_BY_INDEX`); the firmware turns
the index into the MUX/DAC address. `pinout.py` reads every element's index straight from
`pinout_32x32.xlsx` (sheet "PINOUT CONTROLLER"): an element's index is its position when the sheet
is read block by block (8 column blocks, 5 row bands each, odd- and even-pin columns top to bottom,
GND pins skipped). There is no separate mapping file and nothing to select: the pinout is the only
source. A different board's pinout can be used by setting `pixels.pinout_file` under Advanced.

Every time the pinout is loaded (once per connection, about half a second) it is checked against
`Pixel_Map_by_ConnRow.csv`, the controller's wiring table: the MUX word at each index must match.
A MUX word alone isn't unique (each is shared by six elements on different DAC selects); the index
is. A mismatch stops the scan before anything is sent, which catches a different board's
spreadsheet, a missing or extra pin, rows out of order, or an edited MUX word (all tested on
altered copies). It can't catch two element names swapped while their pins stay put, since the
wiring table has no element names to compare against; the pinout's names are trusted as written.

`python pinout.py` prints a summary; `python pinout.py --export table.csv` writes one row per
element (name, index, MUX word, connector, pin) for reference. That the firmware's index order
matches the wiring table is still worth one bench check: set a single element and confirm it's the
expected one.

1.6.2 shipped `pixel_mapping_L.csv`/`pixel_mapping_H.csv` generated from the pinout; earlier versions
required selecting those files by hand. Both are gone, along with `build_pixel_mapping.py`,
`merge_mapping_workspace.py` and `mapping_template_32x32.csv`. Presets that still name mapping files
load fine; those entries are ignored.

## Analysis tab

`analysis_tab.py` (widgets and drawing) and `analysis.py` (processing), adapted from
`ElementToElementPTV.py`.

1. **Dataset folder:** type or browse to a run folder and click **Load** (or press Enter). The box
   starts with the current run's folder (output directory / run name from Settings). Loading and
   processing run in the background with a progress bar: about 7 s for an L dataset and 14 s for H.
   The line under the folder shows what was found, e.g. "1024 L elements, 6 voltages, 17-21 GHz".
2. **Heatmap:** every element at its physical position, coloured by phase range across voltage,
   phase at the last voltage (what the original script plotted), or magnitude range, at one of the
   reference frequencies. Elements without enough measured voltages show grey.
3. **Element plots:** click an element on the heatmap, or enter its row and column (0-indexed, as
   in the file names; plus L/H if the folder has both) and press **Show**. The right side shows the
   original script's 2x2 figure: phase and magnitude vs voltage at each reference frequency, and vs
   frequency for each voltage. Each plot's legend sits outside it, to the right; when a plot has more
than 10 lines (more than 10 reference frequencies, or more than 10 voltages), the lines are coloured
by their actual value and a colour bar replaces the legend. **Save heatmap...** and **Save plot...**
write PNG or PDF.
4. **Processing options** (collapsed): reference frequencies, gate width (samples before/after the
   time-domain peak) and Tukey alpha, then **Reprocess**. **Phase reference** (linear trend or first
   voltage) reprocesses immediately.

Differences from the original script: it works from the files' complex `sdata` (no magnitude/phase
round trip); it uses each file's own frequency axis (16-24 GHz is only assumed for legacy files that
don't record one); elements are keyed by density as well as row/column, since L and H row numbers
overlap; only measured voltages are used, so a cancelled scan still loads; reference frequencies
default per band (18-20 GHz for low band, 27-29 GHz for high band, 0.25 GHz apart; the original
list's "19.75 GHz" and "20.0 GHz" entries both pointed at 19.5 GHz); and gating isn't rounded
mid-calculation. Legacy files (`amplitudes`/`phases` keys, `C#R#` names) still load.

Verified against the original script's own functions on the same element (phase within 0.001 deg,
magnitude within 0.0001 dB, the difference being the original's rounding), and on synthetic
datasets with a known phase shift per element plus a reflection the gate must remove: the heatmap
recovers every element's phase range to 0.0003 deg. Tested end to end with real tkinter: L and H
datasets, high band, a cancelled scan, legacy files, clicking and typed selection, invalid input,
reprocessing, and saving.

Figures are re-fitted to their widget's pixel size before each draw. Matplotlib can raise a figure's
DPI after the window appears (to match display scaling), keeping its size in inches, which renders
it wider than its widget and clips the right edge; this showed up as clipped plots during testing.

## Debug tab

`debug_tab.py`, `DebugTab`. Three independent fields, each with its own Go button — X
move, Y move, set-every-output voltage. Typing changes nothing; only pressing that
field's Go button acts, and each field only touches what it's labeled for (moving X
never changes Y, and vice versa — this is `GrblXY.goto_x()`/`goto_y()`, which move only
their own axis via `goto_xy(x, self.y_mm)` / `goto_xy(self.x_mm, y)`).

These moves bypass calibration deliberately — `goto_x`/`goto_y` are the raw stage
primitives, not `goto_ideal_xy`, since manual hardware debugging wants direct physical
control, not the ideal-to-physical correction a scan applies. The voltage field calls
`set_voltage_grid(np.array([[v]]))` — a 1x1 array still trips the uniform-grid fast path
the pixel controller has (see "How a board element becomes a controller address" — any single-value grid
takes the broadcast-to-everything path, regardless of its logical shape), so it correctly
sets every physical output with no need to reconstruct the real scan geometry here.

Hardware connects lazily — first Go press for the stage or the controller opens that
connection and keeps it open for reuse by later presses, rather than reconnecting every
time. `ScanApp` closes both on window close (`protocol("WM_DELETE_WINDOW", ...)`) so a
serial port doesn't leak past app exit. Uses `SettingsTab.get_config(require_save=False)` for connection details, so a blank run
name can't block a manual stage jog. If you edit the stage or controller settings after the
Debug tab has connected, the next Go press closes the old connection and reconnects with the
new settings.

**Known limitation, not fixed:** nothing stops you from using the Debug tab while a scan
is running in the background thread — same as Calibrate Stage, it fails with a clear
"port already in use"-style error rather than corrupting anything, but there's no
explicit cross-tab lock preventing the attempt.

## Stage calibration

The hex grid geometry computes *ideal* coordinates; the physical stage rarely matches
that exactly (skew, scale error from mounting/belts/etc). `StageCalibration` (in
`stage.py`) is a linear correction: `scale_x`, `skew_x`, `scale_y`, `skew_y`, applied as

```
physical_x = ideal_x * scale_x + ideal_y * skew_x
physical_y = ideal_y * scale_y + ideal_x * skew_y
```

`GrblXY` loads this from `StageConfig.calibration_file` (a JSON file) on construction —
if the file doesn't exist or isn't set, it defaults to an identity transform (no
correction), so nothing breaks if you haven't calibrated yet. `run_scan.py` always moves
to points via `stage.goto_ideal_xy(...)` / `stage.wait_until_reached_ideal(...)`, which
apply this transform automatically — the scan loop itself never has to think about
calibration at all.

**Calibration protocol: three L elements, no homing.** Calibration starts wherever the stage is,
and every target is an L element:
1. **Origin: the first L element (L row 1, col 1).** Nudge the stage onto it; that spot becomes
   (0, 0). The planner always puts this element at (0, 0), whatever the L sub-grid setting.
2. **Far Y: the farthest L element straight down from the origin (x = 0).** Only every other L row
   has an element on that line (the rows between are offset by half a pitch), so with 32 L rows
   it's L row 31. At 4 mm spacing: (0, -180) mm.
3. **Far X: the farthest L element straight across from the origin (y = 0)**, the last element of
   the first L row. At 4 mm spacing: (-214.774, 0) mm.

Both corners are exactly on the axes, so each measures one axis's scale and the other axis's
skew directly (`calibrate_stage.solve_calibration()` still solves the general 2x2 system, which
reduces to that). At the end the stage is sent back to the first L element, so a scan started next
begins at the same origin. Verified on the simulated stage, including starting 25 mm away from the
first L element: the coefficients come back to about 2e-6.

**To calibrate:** set the stage calibration file in General settings, move the stage roughly over
the first L element (the Debug tab works for this), then click **Calibrate Stage...** at the bottom
of the Settings tab (needs a real stage connected). The confirmation dialog lists the three L
elements you'll line up on, and each nudge dialog repeats which one it's waiting for. It uses the
stage settings under Advanced, takes its targets from the current Scan Geometry fields, then saves
`scale_x`/`skew_x`/`scale_y`/`skew_y` to the calibration file. You can also run `calibrate_stage.py`
directly as a standalone script (same `calibrate()` function, with a hardcoded config at the bottom
of the file to edit). Once saved, every future run using that same calibration file picks the
correction up automatically. Recalibrate if the stage, mount, or board gets physically disturbed,
or you're switching to a different DUT that needs its own calibration file.

**Move timeouts and stage coordinates (fixed in V1.4.8).** Two bugs made calibration fail with
"Stage did not reach target" while the stage was still moving:

1. `wait_until_reached` allowed a fixed 30 s for every move. At the default 500 mm/min (8.3 mm/s)
   that cuts off any move longer than 250 mm. On a 4 mm, 32 x 32 grid, origin to far Y is about
   186 mm (22 s, fine), but far Y to far X is a 281 mm diagonal (34 s), so calibration always died
   on the second extent. The timeout is now sized to each move:
   travel time at `stage.feed_mm_per_min` x `stage.move_timeout_factor` (1.5) +
   `stage.move_timeout_extra_s` (10 s). Both are under Advanced > Motor stage. A timeout now reports
   the last position read, the move length, and the feed rate.
2. Position readback compared software targets against GRBL's raw machine position (MPos), which
   only works if MPos happened to be 0 at the software zero. Re-zeroing at the nudged origin during
   calibration didn't reset MPos, so after any origin nudge every later check was off by the nudge
   (and the saved calibration absorbed that error); connecting with the stage away from MPos 0 failed
   immediately. `GrblXY` now records MPos at connect and at every `set_software_zero_here()`, and
   `get_pos()` reports position relative to it, so readback is always in the commanded frame.

Verified against a simulated GRBL stage (moves at the real feed rate on a virtual clock, reports
interpolated MPos): the old code reproduces all three failures (no nudge: times out on far X; origin
nudge: times out on far Y; stage not at MPos 0: times out on the origin). The fixed code completes
all three and recovers a known stage scale/skew to about 2e-6, the limit of GRBL's 3-decimal
moves. Full L and H scans (two voltage steps each, including the jump back to the first point)
also run cleanly on the simulator.

## Stagger direction

`ScanGeometryConfig.stagger_sign` (`+1.0` default, or `-1.0`) mirrors which side the
offset rows overhang. This is a different knob from `offset_odd_rows`: that one picks
*which* rows get the half-spacing offset (odd vs even), while `stagger_sign` flips the
*direction* of that offset. For an infinite lattice these would be equivalent, but the
grid is finite, so they change different things — `offset_odd_rows` shifts the whole
checkerboard pattern by half a spacing, while `stagger_sign` mirrors which edge of the
array the offset rows stick out past.

Exposed in the Settings tab as a checkbox, **"Mirror stagger direction"**, in the Scan
Geometry section — unchecked is `+1.0`, checked is `-1.0`. Unlike `offset_odd_rows` /
`x_direction_sign` / `y_direction_sign` (still config-only, since those are rig-mount
constants), this one's visible and live-updates the Scan tab's preview the moment you
toggle it, on the theory that "does the plot look right" is exactly when you'd want to
flip it.

## Scan geometry — L/H interleaved density

The board is three hex grids interleaved into one denser hex grid. Remove one of them (**L**) and the
other two are **H**. `rows` and `cols` are the size of *one* sub-grid, so 32 x 32 gives
3 x 32 x 32 = 3072 points: 1024 L and 2048 H.

**`spacing_mm` is the nearest-neighbor spacing of the full interleaved grid (changed in V1.4.4).** It's
the smallest point-to-point distance on the board. Each sub-grid on its own is a hex grid with
`sqrt(3) * spacing_mm` between neighbors, which `HexGridPlanner.sub_spacing_mm` derives. Concretely,
for spacing `a`:
- points along a row are `sqrt(3) * a` apart (`sub_spacing_mm`), with alternate rows offset by half that;
- full-grid rows are `a / 2` apart (`dense_row_spacing_mm`, overridable via `row_spacing_mm`);
- every 3rd row is L, so L rows are `1.5 * a` apart (`row_spacing_mm` property).

Rows are horizontal. That makes L a normal flat-row hex grid, and makes the full grid the same lattice
rotated 30 degrees: its nearest neighbors are at +/-30 degrees and straight up/down, not along a row.
Example: 6.8 mm full-grid spacing gives 11.78 mm sub-grid spacing and a 32 x 32 array spanning about
365 x 323 mm. Before V1.4.4 `spacing_mm` meant the L sub-grid's spacing, so **presets saved with
older versions need their spacing divided by sqrt(3)** to describe the same physical board (and a
`row_spacing_mm` override, which used to mean L row pitch, needs dividing by 3).

Two new fields control this:
- **`l_subgrid`** (1, 2, or 3) — which of the 3 possible row-phases within each dense
  3-row group is L. Doesn't change point *count*, just which physical rows count as L
  vs H.
- **`density_mode`** ("L" or "H") — which one is actually active: scanned (moved to,
  measured) and shown as blue→green progress in the Scan tab. The other density is
  plotted too (for context) but stays grey and is never touched by the stage or the
  controller.

Both are in the Settings tab's Scan Geometry section, live-updating the Scan tab's
preview the same way rows/cols/spacing already did.

**Why this replaced the old skip-pattern mechanism:** the very first version of this
scan (your original `LegacySerpentinePlanner`) approximated something like this with
mod/remainder skip logic on a single lattice. Since then the pipeline moved to a clean
single hex lattice with no skipping at all. This reintroduces the ability to scan a
subset — but properly, as two well-defined interleaved sub-lattices with known geometric
relationships (verified: pulling out L's points alone and checking nearest-neighbor
distances gives a perfectly uniform hex lattice, and H is always exactly 2x L's count),
rather than an arbitrary skip pattern.

**Serpentine order alternates per row of the scanned density (fixed in V1.4.7).** It used to
alternate on the full-grid row number, but L and H rows interleave, so some consecutive H rows ran the
same direction and the stage flew back across the whole board between them (about 1.5x the necessary
travel for an H scan). It now alternates on `logical_row`, so every move in an L or H scan is at most
one row step. `x_loop` (visit index within a row, used in per-point filenames) changes accordingly for
H rows that used to run the other way.

**`ScanPoint.logical_row` is each point's index *within its own density*** — 0 to
`rows-1` for L, 0 to `2*rows-1` for H — matching the pinout's own row numbers (E5_9 is L row 4;
see "Element addressing" below), and deliberately *independent* of `l_subgrid`: which
physical pin drives "the 5th L element" doesn't change just because that element's
physical position moved to a different lattice phase. (`l_subgrid` only affects the
internal `dense_row` used to compute `stage_x_mm`/`stage_y_mm` — it never reaches
`logical_row`.) L and H `logical_row` ranges legitimately overlap (both start at 0),
which is why the Scan tab's point lookup and the pinout lookup are both keyed with
density alongside row/col, not row/col alone.

## Current scan order

In `uniform_board_mode=True` the scan runs in this order:

1. Set all 3072 pixels to one voltage
2. Scan every active point (whichever density `density_mode` selects — L or H, not both)
3. Collect VNA at each stage point
4. Move to next voltage and repeat

## Data saving (V1.4.9)

Each scan writes into `output_directory/run_name/`, and refuses to start if that folder already
holds scan data (pick a new run name rather than silently overwriting a previous run).

**One file per visited coordinate**, named `<density>_R<row>_C<col>.npz` (row/col 0-indexed within
that density, e.g. `L_R005_C012.npz`). This follows the legacy protocol: the first time a coordinate
is visited, its file is created with every array already at its final size and empty (NaN); each
later visit, at the next voltage, rewrites the file with everything saved so far plus the slot just
measured. So at any moment, including after a crash or a cancelled scan, each file holds every
voltage measured there so far, and `measured` says which slots are real. Writes go to a temporary
file that is then renamed over the old one, so a failure mid-write can't corrupt a file that already
holds earlier voltages.

| Key | Shape | Contents |
| --- | --- | --- |
| `e` | (V,) | voltage applied at each slot, NaN until measured (legacy key) |
| `iteration` | () | index of the voltage slot written most recently (legacy key) |
| `sdata` | (V, N) | raw complex S11, unrounded |
| `measured`, `measured_time` | (V,) | which slots hold data, and when (Unix time) |
| `voltages_v` | (V,) | the full planned voltage list |
| `frequencies_hz` | (N,) | frequency axis from the VNA's read-back start/stop/points (linear sweep) |
| `density`, `logical_row`, `logical_col` | () | which element this is |
| `stage_x_mm`, `stage_y_mm` | () | ideal (planned) position |
| `physical_x_mm`, `physical_y_mm` | () | position after the stage calibration correction |
| `y_loop`, `x_loop` | () | full-grid row, and visit index within that row |

V = number of voltages, N = VNA points. Magnitude and phase aren't stored (V1.4.10), since they
come straight from `sdata`: `np.abs(sdata)` and `np.degrees(np.angle(sdata))`. At 4001 points
that's about 64 kB per voltage per file, so 0.32 MB per coordinate for 5 voltages, and roughly
330 MB for a 5-voltage L scan (1024 files) or 660 MB for H.

**`metadata.json`** is written once the hardware has connected, so it records what was actually
used: the full config, the voltage list, the frequency axis (start/stop/points from the VNA's
read-back), the stage calibration coefficients actually loaded (not just the file path, which could
be overwritten by a later recalibration), the active-point list in visit order, and the start time.

**Optional whole-run summary** (`save.save_summary_npz`, now off by default): `summary_sdata.npy`,
one disk-backed complex array of shape (points, voltages, N) in active-point order, plus
`summary_index.npz` (`voltages_v`, `active_points`, `frequencies_hz`) when the scan completes. It
duplicates the per-coordinate files, doubling disk use, so turn it on only if an analysis wants
everything in one array.

The V1.4.3-V1.4.8 layout (one file per measurement, `V###_C###_R###_Y###_X###.npz`, with `sdata`
only) is gone; per-coordinate files replace it.

Verified with dry-run scans fed recognisable fake traces: one file per coordinate, every slot holds
exactly its own (point, voltage) trace, calibrated coordinates and
the frequency axis are correct, a cancelled scan leaves earlier voltages intact with the rest NaN,
a reused run name is refused, and a simulated disk error mid-write leaves the existing file loadable
with its earlier data.

## The origin and the L sub-grid

**The origin is always the first L element** (L row 1, col 1), for calibration, scans, the Debug
tab, and the saved `stage_x_mm`/`stage_y_mm`. The planner shifts the whole grid so that element is
at (0, 0). With L sub-grid 1 nothing changes from V1.4.10; with 2 or 3 every position shifts by that
element's old offset, and H rows above the first L row get positive coordinates.

**L sub-grid** now only says how many H rows sit above the board's first L row: 1 = none (the
board's first row is L), 2 = one, 3 = two. Because the origin is always on an L element, a wrong
setting can no longer put H points on L elements (verified for every setting/board combination);
it can only shift which H rows at the edges get scanned, and which H row is numbered 0.

## Notes

- Stage defaults to `COM10`
- Pixel controller defaults to `COM7`
- `vna.py` connects to `VNAConfig.visa_resource` (default `TCPIP0::192.168.6.150::inst0::INSTR`)
  via `VNAConfig.visa_backend` (default `'@py'`, the pure-Python `pyvisa-py` backend — no
  NI-VISA/vendor driver install needed; set to `''` to use pyvisa's default instead)
- `VNAController.initialize()` checks the instrument's error queue (`SYST:ERR?`, standard
  on any SCPI instrument) after every command it sends, and reads back
  sweep-type/start/stop/points afterward to confirm they actually took effect — plain
  `instr.write()` never raises on its own even if the instrument rejects a command, so
  without this a bad command (wrong value, wrong mode, unsupported on this model) fails
  completely silently. If the VNA isn't ending up with the sweep settings you configured,
  this will now raise `RuntimeError` naming either the specific rejected command and the
  instrument's own error text, or the exact requested-vs-actual mismatch if the
  instrument accepted the command but didn't apply it (often a channel/trace selection
  issue). This also replaces the old fixed `time.sleep(1)` between each command with the
  query itself as the sync point, which is instrument-paced rather than guessed.

- Per-element (mapped) sweeps: set `uniform_board_mode=False`. `PixelController` addresses each
  element from the pinout automatically; `PiBoardController` needs no addressing on the PC side —
  see "Using the Pi controller" above.
- `RunConfig.uniform_board_settle_s` (default 45 s, "Settle time" in General) adds a delay between updating all pixels and
  starting the X-Y-VNA sweep; `single_pixel_settle_s` is for mapped-element mode
- `RunConfig.uniform_board_settle_per_point` (uniform board mode only):
  `False` (default) sends the grid once per voltage step, delays once, then scans every
  point; `True` re-sends the grid and delays again before every single point

## First thing to edit

Open `run_scan.py`, scroll to the bottom `if __name__ == "__main__":` block, and change:
- output path
- voltage list
- run name

Then run:

```
python run_scan.py
```

## How a board element becomes a controller address

**`PixelController`:** a uniform grid (every value equal) takes a broadcast path that sets every
output, with no addressing needed. A non-uniform grid or a voltage pattern sets each element by its
index from the pinout (see "Element addressing" above).

**`PiBoardController`** needs no PC-side addressing: the Pi has its own internal element-to-DAC
map. See "Using the Pi controller" above.
