# Array Scan Project

## Files

- `config.py` — all experiment settings (dataclasses)
- `stage.py` — motor stage handler (`MotorStage` interface + `GrblXY` implementation),
  the scan geometry it moves through (`HexGridPlanner`), and stage calibration (`StageCalibration`)
- `calibrate_stage.py` — one-time interactive calibration utility (run standalone, not part of a scan)
- `controller.py` — `BoardController` interface, plus the legacy CSV mapping helpers
  shared by any controller implementation
- `pixel_controller.py` — `PixelController`: the framed-serial-protocol board controller
- `pi_controller.py` — `PiBoardController`: SSH/file-upload board controller for the
  Raspberry Pi + SPI DAC setup (wraps `PiController`, the paramiko transport layer)
- `vna.py` — VNA handler: `VNAInstrument` interface + `VNAController`, the concrete
  VISA/SCPI driver for the lab's VNA (this used to be the separate `VNATest.py`)
- `data.py` — data handler: `DataSaver`
- `run_scan.py` — coordinator (`AutomatedArrayScanner`) that wires the four handlers
  together and runs the scan; also has the example run config in `__main__`
- `merge_mapping_workspace.py` — one-off utility to build a mapping CSV workspace (see below)
- `build_pixel_mapping.py` — generates the real `PixelController` mapping CSV directly
  from the pinout spreadsheet (needs `openpyxl`, not a dependency of anything else here)
- `config_io.py` — RunConfig <-> JSON (for saving/loading GUI presets)
- `branding.py` — app name ("Candice") and the kiss-mark logo loader
- `kiss_mark.png` — the logo image (40x35, transparent background)
- `settings_tab.py` — GUI tab 1: Settings & Configuration (`SettingsTab`)
- `scan_tab.py` — GUI tab 2: Scan (`ScanTab`) — live geometry preview, progress bar, Start/Stop
- `debug_tab.py` — GUI tab 3: Debug (`DebugTab`) — manual stage jog + board-wide voltage set
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

`python gui_app.py` opens **Candice**, a three-tab window (Settings, Scan, Debug) (needs tkinter, which ships with
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
  (`stage.calibration_file`), and the L/H mapping CSVs (`pixels.mapping_csv_l` / `_h`, shown only
  when the pixel controller is selected). The Pi controller's `pi.local_file_*`/`remote_file_*`
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
both controllers already have (see "Large scans and memory" — any single-value grid
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

**Calibration targets are real elements (changed in V1.4.4).** `HexGridPlanner.calibration_targets()`
picks three elements straight from the generated grid:
- **Origin:** the planner's (0, 0) element (first row, first column of the full grid). It's an
  L element when `l_subgrid=1` and an H element otherwise, but it is always a real element.
- **Far X:** the last element of the first L row.
- **Far Y:** the first element of the last L row.

Before V1.4.4 the corners were computed as `(sign * (cols-1) * spacing, 0)` and
`(0, sign * (rows-1) * row_spacing)`. The Y one didn't land on any element: the last L row is an
offset row, so the nearest element was half a sub-grid pitch away in X. An operator lining up on the
element they could see would record that offset as stage skew. With `l_subgrid` 2 or 3 the origin and
X targets also landed on H elements rather than L. Taking targets from the generated points fixes all
of that, because the alternate-row offset and the `l_subgrid` phase are already in their coordinates.
The signs still matter and are still handled, since the targets carry whatever
`x_direction_sign`/`y_direction_sign` the planner applied.

Because those corners are generally not on the axes, `calibrate_stage.solve_calibration()` solves the
full 2x2 system `M @ ideal = actual` for both corners (origin fixed at 0, 0) instead of dividing by an
axis span. For axis-aligned corners it reduces exactly to the old formulas. Verified by simulating a
stage with a known scale and skew and an operator who nudges onto the true element: the coefficients
come back exact (to floating-point precision) for all three `l_subgrid` values and both stagger
directions.

**To calibrate:** set the stage calibration file in General settings, then click **Calibrate Stage...** at the bottom of the Settings tab
(needs a real stage connected). The confirmation dialog lists the three elements you'll line up on,
by density, row/column, and ideal position, and each nudge dialog repeats which one it's waiting for.
It connects using the current hidden stage config (port, baud, etc.) plus whatever path is in the
Calibration file field, takes its targets from the current Scan Geometry fields, walks through home,
origin, far Y, and far X, then saves `scale_x`/`skew_x`/`scale_y`/`skew_y` to that file. You can also
run `calibrate_stage.py` directly as a standalone script (same `calibrate()` function, with a
hardcoded config at the bottom of the file to edit). Either way, once saved, every future run using
that same `calibration_file` path picks the correction up automatically. You only need to recalibrate
if the stage, mount, or board gets physically disturbed, or you're switching to a different DUT that
needs its own calibration file.

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

**`ScanPoint.logical_row` is each point's index *within its own density*** — 0 to
`rows-1` for L, 0 to `2*rows-1` for H — matching pin-mapping row numbers directly (see
"`build_pixel_mapping.py`" below), and deliberately *independent* of `l_subgrid`: which
physical pin drives "the 5th L element" doesn't change just because that element's
physical position moved to a different lattice phase. (`l_subgrid` only affects the
internal `dense_row` used to compute `stage_x_mm`/`stage_y_mm` — it never reaches
`logical_row`.) L and H `logical_row` ranges legitimately overlap (both start at 0),
which is why the Scan tab's point lookup and the mapping CSVs are both keyed with
density alongside row/col, not row/col alone.
**`mapping_template_32x32.csv` is stale** — it predates the density split entirely and
isn't in the right shape for either one. `build_pixel_mapping.py` replaces the workflow
it was for, so no need to regenerate it as a template.

## Current scan order

In `uniform_board_mode=True` the scan runs in this order:

1. Set all 3072 pixels to one voltage
2. Scan every active point (whichever density `density_mode` selects — L or H, not both)
3. Collect VNA at each stage point
4. Move to next voltage and repeat

## Large scans and memory

Two separate problems here, fixed together.

**Redundant storage (the bigger one).** Every per-point file used to store `amplitudes`
and `phases_deg` *alongside* the raw `sdata` they were computed from — but
`amplitude = abs(sdata)` and `phase = degrees(angle(sdata))` are entirely derivable from
`sdata`, so storing all three wrote roughly double what was needed, and the summary
arrays duplicated that same derived data a second time on top of it. For a 32x32 grid,
11 voltages, 5000 VNA points: the actual data of interest (`sdata` alone, complex128) is
~0.9 GB, but the old code wrote **~2.7 GB** — about 4x. Fixed: `save_point()` and
`open_summary_arrays()` now store `sdata` only, everywhere. Recomputing amplitude/phase
on load is one line (`np.abs(sdata)`, `np.degrees(np.angle(sdata))`) and costs nothing
worth avoiding. This alone gets the same scenario down to ~1.8 GB total — the remaining
2x over the "essential" 0.9 GB is the per-point files and the summary array each holding
a full copy of `sdata`, which is real but avoidable: if you only need one of the two
forms, turning off `save_individual_npz` or `save_summary_npz` removes that copy entirely.

**RAM usage for the summary array.** `run_scan.py` used to preallocate the summary
arrays entirely in RAM (`np.empty`) — fine for small scans, but for a full-board scan
this can run into gigabytes, which can fail outright on a memory-constrained PC.
`DataSaver.open_summary_arrays()` now creates this as a disk-backed memory-mapped array
(`np.lib.format.open_memmap`, mode `"w+"`) instead, and the scan loop writes into it
incrementally as each point is measured. **The real benefit isn't lower peak memory in
an idle system** — if you write straight through the whole array with nothing else
competing for RAM, actual resident memory ends up similar either way, since the OS still
has to hold touched pages in RAM until they're written back. The benefit is what happens
when memory *is* tight: an anonymous array that doesn't fit in RAM+swap raises
`MemoryError` immediately (verified directly: an 8.2 GB anonymous array failed hard on a
3.9 GB/no-swap test machine), while the memmapped version succeeded at the same size,
because its backing store is the file on disk, not RAM+swap.

**File layout as of both fixes:** each per-point `.npz` now has `sdata` (complex128) plus
the small scalar fields (voltage, indices, coordinates) — no `amplitudes`/`phases_deg`
keys anymore. The summary is `summary_sdata.npy` (one complex128 array, directly
`np.load`-able) plus `summary_index.npz` (just `voltages_v` and `active_points`, which
stay tiny regardless of scan size). This replaces the very first version's single
`summary_arrays.npz` with `amplitudes`/`phases_deg`/`voltages_v`/`active_points` inside
it — any existing analysis script needs updating to match, both for the file split and
for computing amplitude/phase from `sdata` itself rather than reading them directly.

`save_summary_npz=False` skips allocating *any* array (memmap or otherwise) — the
original code always allocated the full in-RAM arrays regardless of the flag, only the
final write was skipped.

**On a cancelled scan:** `summary_sdata.npy` exists (created up front) but is only
partially populated — the rest is whatever `open_memmap`'s initial fill left there.
`summary_index.npz` is *not* written on cancellation, since there's no reliable way to
tell which rows are real data from the array alone — the per-point `.npz` files remain
the source of truth for a cancelled run.



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

- If you're using `PixelController`, set `uniform_board_mode=False` and provide
  `RunConfig.pixels.mapping_csv_l` / `mapping_csv_h` (see `build_pixel_mapping.py`).
  `PiBoardController` needs no mapping at all — see "Using the Pi controller" above.
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

## DAC mapping — how a board element becomes a controller address

This is now **controller-specific**, not a shared concept — the two controllers address
hardware too differently for one explanation to cover both.

**`PixelController`** still needs a mapping CSV — now two of them,
`PixelControllerConfig.mapping_csv_l` and `mapping_csv_h`, since L and H elements are
wired to completely different pins (see "`build_pixel_mapping.py`" below for why one
shared file doesn't work). `PixelController.__init__` takes a `density_mode` argument
and loads whichever file matches — `run_scan.py` passes
`RunConfig.geometry.density_mode` automatically. `ElementToControllerMap` (in
`controller.py`) reads whichever file got loaded and gives you, per
`(element_row, element_col)`, a `controller_index` — the native serial-protocol address.
`set_voltage_grid` looks this up per grid cell and batches serial `set pixel` commands.
No mapping CSV is needed for a **uniform** grid (every value equal) — it detects that
case and takes a fast broadcast path instead; mapping only matters once a grid varies
element-to-element (mapped-element mode).

**`PiBoardController` needs no mapping at all.** The Pi has its own internal
element-to-DAC map, so the array `set_voltage_grid` receives gets uploaded directly —
`csv[row][col]` is the voltage for the element at that position, and the Pi's own
firmware handles the rest. See "Using the Pi controller" above for the details
(`active_band`, `hb_shape`/`lb_shape`).

**Still open, and this is real hardware truth I don't have:**
1. Is the `PixelController` board the same physical array of elements the Pi drives, or
   a different array entirely? Doesn't block either controller working independently,
   but matters if you're trying to reconcile results between them.
2. `PixelController`'s `controller_index == global_index` (from `Pixel_Map_by_ConnRow.csv`)
   is still an unverified assumption — worth a real bench check (single low-voltage
   element, confirm it's physically the element you expected) before trusting
   mapped-element mode.
3. `PiControllerConfig.hb_shape`/`lb_shape` are placeholder `(24, 8)` — need real numbers.

## Legacy mapping CSV

`Pixel_Map_by_ConnRow.csv` is included for reference. It's useful for global_index lookup,
connector/pin lookup, and row-based grouping used by the original GUI. It does **not** by
itself define the final 32x32 stage-element-to-controller map — for that you still need the
element `(row, col)` mapping you are building.

## `build_pixel_mapping.py` — generating the real mapping CSVs

Builds `PixelController`'s mapping CSVs directly from the pinout spreadsheet's "PINOUT
CONTROLLER" sheet — no manual cross-referencing, which is what
`merge_mapping_workspace.py` (below) existed for before element identity was available.

```
python build_pixel_mapping.py pinout_32x32.xlsx --output pixel_mapping
  -> writes pixel_mapping_L.csv (1024 rows) and pixel_mapping_H.csv (2048 rows)
```

**Which controller pin drives which element is a fixed wiring fact, independent of
`l_subgrid`.** `l_subgrid` is purely a stage-motion parameter (which of the 3 lattice
phases *this DUT's* low-density elements are physically mounted at — different DUTs can
differ here) and has nothing to do with addressing. So `element_row` here is just each
label's own row number directly — `E12_4` → row 11, `H37_9` → row 36 — no phase
decoding at all. L and H elements are wired to entirely different pins and their row
numbers overlap (both start at 0), which is why this writes two separate files rather
than one: load `pixel_mapping_L.csv` as `PixelControllerConfig.mapping_csv_l` and
`pixel_mapping_H.csv` as `mapping_csv_h`; `PixelController` picks whichever one matches
the active `density_mode` automatically.

(This replaced an earlier, wrong version of this script that *did* fold `l_subgrid` into
the decode — that conflated "where the stage physically goes" with "which pin gets
written," which aren't the same thing. Fixed once that was pointed out.)

**What's actually verified, not assumed:** the script's row/column traversal order (8
connector-blocks arranged 5 row-bands deep, each block holding 2 interleaved pin
sub-columns for odd/even pins) was cross-checked directly against
`Pixel_Map_by_ConnRow.csv` — the resulting pin/mux-word sequence matches that file
exactly, 0 mismatches across all 3072 entries. That confirms `controller_index`
(assigned as each element's position in this traversal, 0 to 3071) correctly lines up
with the right `H{row}_{col}`/`E{row}_{col}` label for every single element — this
isn't sampled or spot-checked, it's a complete match. Also directly confirmed through the
real scanner: for the same logical element, `controller_index` is identical across all 3
`l_subgrid` values (only the physical stage position and scan visit order differ) —
exactly the independence this design depends on.

**What's still an assumption:** `controller_index == global_index` itself (the same
"gut feeling" flagged earlier) — a real bench check (single low-voltage element,
confirm it's physically the one you expected) is still worth doing before trusting
mapped-element mode.

Needs `openpyxl` (`pip install openpyxl`) — not a dependency of the GUI or scan pipeline
itself, only this one script.

## What `merge_mapping_workspace.py` does

**Superseded by `build_pixel_mapping.py`** now that the pinout spreadsheet has element
identity in it — this section is kept for reference, not as the recommended path.

You need a CSV that maps each physical board element `(element_row, element_col)` to the
`controller_index` the pixel controller uses to address it — that's what
`mapping_csv_l`/`mapping_csv_h` on `PixelControllerConfig` point to, and what
`ElementToControllerMap` reads. Before the pinout spreadsheet was available,
`merge_mapping_workspace.py` was how you'd get started building that file by hand:

1. Reads `mapping_template_32x32.csv` — a blank 1024-row grid, one row per `(element_row,
   element_col)`, with empty `controller_index`/`band`/`band_channel` columns waiting to be filled in.
2. Reads `Pixel_Map_by_ConnRow.csv` — the legacy 3072-row wiring table, which tells you,
   for each `global_index`, which physical connector/pin/DAC channel it's wired to.
3. Writes `mapping_workspace.csv`: the blank grid with extra empty columns
   (`legacy_row`, `legacy_connector`, `legacy_pin`, `legacy_dac_sel_int`, `legacy_dac_ch`,
   `assignment_notes`) for you to fill in by hand as you work out which connector goes
   with which board element.
4. Also writes `legacy_connrow_reference.csv` — just the useful columns of the legacy
   table, trimmed down for easier side-by-side lookup while you do that.

It doesn't compute or infer any mapping itself — it's a one-time helper that saves you
from manually re-typing 1024 rows of template and gives you a reference sheet next to it.
Once you've filled in `controller_index` for each element and saved that as your real
mapping CSV, point `PixelControllerConfig.mapping_csv_l` (or `_h`) at it and set
`uniform_board_mode=False`. Again — if you have a pinout spreadsheet with element
identity in it like this project does, `build_pixel_mapping.py` does this step for you
directly; this manual workflow is the fallback for when you don't.
