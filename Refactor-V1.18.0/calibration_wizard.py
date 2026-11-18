"""
Calibration walkthrough: stage calibration and surface (warpage) calibration in one guided
window, opened from Settings > Calibrate...

Steps (each one page, with what to do in plain words):
  1. Before you start: choose stage and/or surface calibration and the plate set; lists
     what's needed.
  2. First L element (origin): nudge the probe onto it; that spot becomes (0, 0).
     Always done, since every move after it is measured from there.
  3. Far Y and far X elements (stage calibration only): nudge onto each; the stage's scale
     and skew are solved and saved to the stage calibration file.
  4. Copper plates (surface calibration only): the stage drives to each of the 5 sites
     (centre first, then the corners); the operator places the plate as instructed and
     clicks Measure. Each measurement is saved and sanity-checked straight away.
  5. Solve: nfp_calibrate.py's calibration runs on what was collected; the summary and any
     warnings are shown in plain words before anything is saved.

Hardware runs on a background thread so the window stays responsive. Cancelling at any
point keeps the previous surface calibration set untouched (see surface_cal.SetWriter).
The stage and VNA classes are parameters so the walkthrough can be tested with simulated
hardware.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from dataclasses import dataclass, replace
from tkinter import messagebox, ttk
from typing import Callable

import numpy as np

import surface_cal
from calibrate_stage import solve_calibration
from config import RunConfig
from stage import GrblXY, HexGridPlanner
from vna import VNAController

NUDGE_STEPS_MM = ("0.05", "0.1", "0.5", "1", "5")
PHASE_CHECK_TOLERANCE_DEG = 25.0


@dataclass
class Step:
    kind: str                 # "nudge" | "plate" | "solve"
    title: str
    target: tuple[float, float] = (0.0, 0.0)   # ideal coordinates (nudge: raw stage coordinates)
    role: str = ""            # nudge: "origin" | "far_y" | "far_x"
    site: surface_cal.Site | None = None
    depth_mm: float = 0.0
    done: bool = False
    measured: tuple[float, float] | None = None  # nudge: where the stage actually ended up


class CalibrationWizard(tk.Toplevel):
    def __init__(self, parent, config: RunConfig,
                 stage_factory: Callable = GrblXY, vna_factory: Callable = VNAController,
                 on_finished: Callable[[str], None] | None = None) -> None:
        super().__init__(parent)
        self.title("Calibration")
        self.geometry("1120x660")
        self.minsize(1000, 580)
        self.transient(parent)
        self.config = config
        self.stage_factory, self.vna_factory = stage_factory, vna_factory
        self.on_finished = on_finished
        self.stage = self.vna = self.writer = self.solution = None
        self.steps: list[Step] = []
        self.index = -1
        self.ideal_targets = HexGridPlanner(config.geometry).calibration_targets()
        self.measured: dict[tuple[str, float], np.ndarray] = {}
        self.stage_result = ""
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._nudge = np.zeros(2)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._build()
        self._show_intro()

    # ------------------------------------------------------------------ layout ----

    def _build(self) -> None:
        left = ttk.Frame(self, padding=(12, 12, 6, 12))
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Steps", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.step_list = tk.Listbox(left, width=31, activestyle="none", exportselection=False,
                                    highlightthickness=0, borderwidth=1)
        self.step_list.pack(fill="y", expand=True, pady=(4, 0))
        self.step_list.bind("<<ListboxSelect>>", lambda e: self._highlight())

        right = ttk.Frame(self, padding=(6, 12, 12, 12))
        right.pack(side="left", fill="both", expand=True)
        self.heading = ttk.Label(right, text="", font=("TkDefaultFont", 13, "bold"))
        self.heading.pack(anchor="w")
        body = ttk.Frame(right)
        body.pack(fill="both", expand=True, pady=(8, 0))
        self.text_col = ttk.Frame(body)
        self.text_col.pack(side="left", fill="both", expand=True)
        self.instructions = ttk.Label(self.text_col, text="", wraplength=470, justify="left")
        self.instructions.pack(anchor="nw", fill="x")
        self.options = ttk.Frame(self.text_col)          # intro choices
        self.nudge_frame = ttk.Frame(self.text_col)      # nudge controls
        self._build_nudge(self.nudge_frame)
        self.result = ttk.Label(self.text_col, text="", wraplength=470, justify="left")
        self.result.pack(anchor="nw", fill="x", pady=(10, 0))
        self.report_frame = ttk.Frame(self.text_col)
        # Fixed, modest width (it scrolls) so the report never squeezes the board map.
        self.report = tk.Text(self.report_frame, height=12, width=60, wrap="none", font=("TkFixedFont", 8))
        ttk.Label(self.report_frame, text="Full report from nfp_calibrate (also saved with the set):",
                  foreground="gray40").grid(row=0, column=0, sticky="w", pady=(0, 2))
        ys = ttk.Scrollbar(self.report_frame, orient="vertical", command=self.report.yview)
        xs = ttk.Scrollbar(self.report_frame, orient="horizontal", command=self.report.xview)
        self.report.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.report.grid(row=1, column=0, sticky="nsew"); ys.grid(row=1, column=1, sticky="ns"); xs.grid(row=2, column=0, sticky="ew")
        self.report_frame.grid_rowconfigure(1, weight=1); self.report_frame.grid_columnconfigure(0, weight=1)

        map_col = ttk.Frame(body)
        map_col.pack(side="left", fill="y", padx=(12, 0))
        self.map = tk.Canvas(map_col, width=250, height=225, background="white", highlightthickness=1,
                             highlightbackground="#B4B2A9")
        self.map.pack()
        ttk.Label(map_col, text="Board as drawn in the Scan tab", foreground="gray40").pack(pady=(4, 0))

        bottom = ttk.Frame(right)
        bottom.pack(fill="x", side="bottom", pady=(10, 0))
        self.status = ttk.Label(bottom, text="", foreground="gray40")
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=120)
        self.primary = ttk.Button(bottom, text="Start", command=self._primary)
        self.primary.pack(side="right")
        self.secondary = ttk.Button(bottom, text="", command=self._secondary)
        ttk.Button(bottom, text="Cancel", command=self._cancel).pack(side="right", padx=(0, 8))

    def _build_nudge(self, frame) -> None:
        ttk.Label(frame, text="Nudge the stage (mm):").grid(row=0, column=0, columnspan=4, sticky="w", pady=(10, 4))
        self.step_var = tk.StringVar(value="0.1")
        ttk.Label(frame, text="step").grid(row=1, column=0, sticky="e")
        ttk.Combobox(frame, textvariable=self.step_var, values=NUDGE_STEPS_MM, width=5).grid(row=1, column=1, sticky="w", padx=4)
        pad = ttk.Frame(frame)
        pad.grid(row=1, column=2, rowspan=3, padx=(16, 0))
        for text, dx, dy, r, c in (("Y +", 0, 1, 0, 1), ("X -", -1, 0, 1, 0), ("X +", 1, 0, 1, 2), ("Y -", 0, -1, 2, 1)):
            ttk.Button(pad, text=text, width=5, command=lambda dx=dx, dy=dy: self._nudge_by(dx, dy)).grid(row=r, column=c, padx=2, pady=2)
        ttk.Label(frame, text="or type x, y").grid(row=4, column=0, sticky="e", pady=(8, 0))
        self.typed_var = tk.StringVar()
        typed = ttk.Entry(frame, textvariable=self.typed_var, width=12)
        typed.grid(row=4, column=1, sticky="w", padx=4, pady=(8, 0))
        typed.bind("<Return>", lambda e: self._nudge_typed())
        ttk.Button(frame, text="Nudge", command=self._nudge_typed).grid(row=4, column=2, sticky="w", padx=(16, 0), pady=(8, 0))
        self.nudge_label = ttk.Label(frame, text="", foreground="gray40")
        self.nudge_label.grid(row=5, column=0, columnspan=4, sticky="w", pady=(8, 0))

    # ------------------------------------------------------------------ intro ----

    def _show_intro(self) -> None:
        self.heading.configure(text="Before you start")
        c = self.config
        self.do_stage = tk.BooleanVar(value=True)
        self.do_surface = tk.BooleanVar(value=True)
        self.do_check = tk.BooleanVar(value=False)
        self.plate_set = tk.StringVar(value=c.stage.surface_plates if c.stage.surface_plates in surface_cal.PLATE_SETS else "all_full")
        for w in self.options.winfo_children():
            w.destroy()
        self.options.pack(anchor="nw", fill="x", pady=(10, 0))
        ttk.Checkbutton(self.options, text="Stage calibration (first L element, far Y, far X)",
                        variable=self.do_stage, command=self._refresh_intro).pack(anchor="w")
        ttk.Checkbutton(self.options, text="Surface calibration (copper plate at the centre and four corners)",
                        variable=self.do_surface, command=self._refresh_intro).pack(anchor="w", pady=(4, 0))
        ttk.Label(self.options, text="Plates:").pack(anchor="w", pady=(10, 0))
        self.plate_radios = []
        for key, text in surface_cal.PLATE_SETS.items():
            rb = ttk.Radiobutton(self.options, text=text, value=key, variable=self.plate_set, command=self._refresh_intro)
            rb.pack(anchor="w", padx=(16, 0))
            self.plate_radios.append(rb)
        self.check_box = ttk.Checkbutton(
            self.options, text=f"Accuracy check (optional): one extra {surface_cal.CHECK_DEPTH_MM:g} mm plate at the centre",
            variable=self.do_check, command=self._refresh_intro)
        self.check_box.pack(anchor="w", pady=(8, 0))
        self._refresh_intro()
        self._set_buttons("Start", None)
        self._draw_map()

    def _refresh_intro(self) -> None:
        c = self.config
        stage, surface = self.do_stage.get(), self.do_surface.get()
        for rb in [*self.plate_radios, self.check_box]:
            rb.configure(state="normal" if surface else "disabled")
        check = surface and self.do_check.get()
        n_plates = sum(len(s.depths_mm) for s in surface_cal.site_layout(c.geometry, self.plate_set.get(), check))
        lines = ["This walks you through calibrating the scanner. Each step says exactly what to do; the stage "
                 "moves on its own between steps.", ""]
        if surface:
            lines += ["You will need:",
                      "  - the copper calibration plate",
                      "  - the 1 mm and 2 mm spacers" + (f", and the {surface_cal.CHECK_DEPTH_MM:g} mm spacers "
                                                          "for the accuracy check" if check else ""),
                      f"  - {n_plates} plate measurements at 5 sites", "",
                      "Keep the VNA settings exactly as they'll be for your scans (same band and points): the "
                      "calibration only applies to scans with the same sweep.", ""]
        lines += ["Results are saved to:"]
        if stage:
            lines.append(f"  - stage calibration: {c.stage.calibration_file or '(not set: set it in General settings)'}")
        if surface:
            base = surface_cal.sets_folder(c.stage)
            lines.append(f"  - surface calibration: a new dated folder (e.g. {surface_cal.SET_PREFIX}"
                         f"{__import__('time').strftime('%Y-%m-%d_%H%M')}) in "
                         f"{base or '(not set: set the stage calibration file in General settings)'}")
        if not stage and surface:
            lines += ["", "Without stage calibration, the saved stage calibration file is used to find the sites "
                          f"({c.stage.calibration_file or 'none set, so moves are uncorrected'})."]
        self.instructions.configure(text="\n".join(lines))
        # Preview of the steps these choices will give.
        self.steps = self._plan_steps(stage, surface, self.plate_set.get(), check)
        self._fill_step_list()
        self.steps = []
        self._draw_map()

    def _plan_steps(self, stage: bool, surface: bool, plate_set: str, check: bool = False) -> list[Step]:
        t = self.ideal_targets
        steps = [Step("nudge", "First L element (origin)", (0.0, 0.0), role="origin")]
        if stage:
            steps += [Step("nudge", "Far Y element", (t.y_corner.stage_x_mm, t.y_corner.stage_y_mm), role="far_y"),
                      Step("nudge", "Far X element", (t.x_corner.stage_x_mm, t.x_corner.stage_y_mm), role="far_x")]
        if surface:
            self.sites = surface_cal.site_layout(self.config.geometry, plate_set, check)
            for i, site in enumerate(self.sites, start=1):
                for d in site.depths_mm:
                    place = site.label.replace(" corner", "")
                    kind = "check plate" if d == surface_cal.CHECK_DEPTH_MM else "plate"
                    steps.append(Step("plate", f"Site {i} ({place}): {d:g} mm {kind}",
                                      (site.ideal_x_mm, site.ideal_y_mm), site=site, depth_mm=d))
            steps.append(Step("solve", "Solve and save"))
        return steps

    def _start(self) -> None:
        c = self.config
        stage, surface = self.do_stage.get(), self.do_surface.get()
        problems = []
        if not stage and not surface:
            problems.append("Choose stage calibration, surface calibration, or both.")
        if stage and not c.stage.calibration_file:
            problems.append("Set the stage calibration file in General settings first.")
        if surface and surface_cal.sets_folder(c.stage) is None:
            problems.append("Set the stage calibration file in General settings first: surface calibrations are "
                            "saved in the same folder (or set a Surface calibration folder).")
        if problems:
            messagebox.showerror("Can't start yet", "\n".join(problems), parent=self)
            return
        self.config = replace(c, stage=replace(c.stage, surface_plates=self.plate_set.get()))
        self.options.pack_forget()
        self.steps = self._plan_steps(stage, surface, self.plate_set.get(), surface and self.do_check.get())
        self._fill_step_list()

        def connect():
            self.stage = self.stage_factory(c.stage)
            if surface:
                self.vna = self.vna_factory(c.vna)
                self.vna.initialize()
            return None
        self._run("Connecting to the stage" + (" and VNA" if surface else "") + "...", connect,
                  lambda _: self._goto(0))

    # ------------------------------------------------------------------ steps ----

    def _fill_step_list(self) -> None:
        self.step_list.delete(0, "end")
        for i, s in enumerate(self.steps):
            mark = "\u2713 " if s.done else ("\u2192 " if i == self.index else "   ")
            self.step_list.insert("end", f"{mark}{i + 1}. {s.title}")
        self._highlight()

    def _highlight(self) -> None:
        self.step_list.selection_clear(0, "end")
        if 0 <= self.index < len(self.steps):
            self.step_list.selection_set(self.index)
            self.step_list.see(self.index)

    @property
    def step(self) -> Step:
        return self.steps[self.index]

    def _goto(self, index: int) -> None:
        self.index = index
        self._fill_step_list()
        self.result.configure(text="", foreground="")
        self.report_frame.pack_forget()
        step = self.step
        self.heading.configure(text=f"Step {index + 1} of {len(self.steps)}: {step.title}")
        self._draw_map()
        if step.kind == "nudge":
            self._enter_nudge(step)
        elif step.kind == "plate":
            self._enter_plate(step)
        else:
            self._enter_solve()

    def _advance(self) -> None:
        self.step.done = True
        if self.index + 1 < len(self.steps):
            self._goto(self.index + 1)
        else:
            self._finish()

    # --- nudge steps (stage calibration) ---

    def _enter_nudge(self, step: Step) -> None:
        self._nudge = np.zeros(2)
        what = {"origin": "the first L element (L row 1, column 1). This is the origin: every position is "
                          "measured from here.",
                "far_y": "the far-Y element: the farthest L element straight down from the origin "
                         f"(about {abs(step.target[1]):.0f} mm away).",
                "far_x": "the far-X element: the last element of the first L row "
                         f"(about {abs(step.target[0]):.0f} mm away)."}[step.role]
        text = [f"Line the probe up exactly over {what}", "",
                "1. Watch the probe and nudge the stage with the buttons (choose the step size first) or by "
                "typing a nudge like 0.2, -0.1.",
                "2. When the probe is centred on the element, click 'Probe is on the element'."]
        if step.role == "origin":
            text += ["", "The stage won't move until you nudge it. If the probe is far from the element, start "
                         "with a big step (5 mm or 1 mm), then switch to small steps (0.1 or 0.05 mm) to finish."]
        self.instructions.configure(text="\n".join(text))
        self.nudge_frame.pack(anchor="nw", fill="x", pady=(4, 0), before=self.result)
        self._update_nudge_label()
        self._set_buttons("Probe is on the element", None)
        if step.role == "origin":
            self._run("Setting the reference...", lambda: self.stage.set_software_zero_here(), lambda _: None)
        else:
            x, y = step.target
            self._run(f"Moving to the {step.title.lower()}...", lambda: self._move_raw(x, y), lambda _: None)

    def _move_raw(self, x: float, y: float) -> None:
        self.stage.goto_xy(x, y)
        self.stage.wait_until_reached(x, y)

    def _nudge_by(self, dx: int, dy: int) -> None:
        try:
            size = float(self.step_var.get())
        except ValueError:
            return
        self._apply_nudge(dx * size, dy * size)

    def _nudge_typed(self) -> None:
        try:
            dx, dy = (float(v) for v in self.typed_var.get().replace(";", ",").split(","))
        except ValueError:
            messagebox.showerror("Nudge", "Type the nudge as x, y in mm, e.g. 0.2, -0.1", parent=self)
            return
        self.typed_var.set("")
        self._apply_nudge(dx, dy)

    def _apply_nudge(self, dx: float, dy: float) -> None:
        if self._busy:
            return
        self._nudge += (dx, dy)
        x, y = np.array(self.step.target) + self._nudge
        self._run("Nudging...", lambda: self._move_raw(x, y), lambda _: self._update_nudge_label())

    def _update_nudge_label(self) -> None:
        self.nudge_label.configure(text=f"Nudged {self._nudge[0]:+.3f}, {self._nudge[1]:+.3f} mm from where the "
                                        "stage expected the element.")

    def _confirm_nudge(self) -> None:
        step = self.step
        if step.role == "origin":
            def zero():
                self.stage.set_software_zero_here()
            self._run("Setting the origin...", zero, lambda _: self._after_nudge())
            return
        pos = self.stage.get_pos()
        step.measured = (pos.x_mm, pos.y_mm)
        if step.role == "far_x":
            self._solve_stage()
        else:
            self._after_nudge()

    def _after_nudge(self) -> None:
        self.nudge_frame.pack_forget()
        self._advance()

    def _solve_stage(self) -> None:
        far_y = next(s for s in self.steps if s.role == "far_y")
        far_x = self.step
        cal = solve_calibration(far_x.target, far_y.target, far_x.measured, far_y.measured)
        cal.save(self.config.stage.calibration_file)
        self.stage.calibration = cal
        d = cal.to_dict()
        self.stage_result = (f"Stage calibration saved to {self.config.stage.calibration_file}: "
                             f"scale x {d['scale_x']:.5f}, scale y {d['scale_y']:.5f}, "
                             f"skew x {d['skew_x']:+.5f}, skew y {d['skew_y']:+.5f}.")
        logging.info(self.stage_result)
        self._after_nudge()
        self.status.configure(text="Stage calibration saved.")

    # --- plate steps (surface calibration) ---

    def _enter_plate(self, step: Step) -> None:
        site = step.site
        if self.writer is None:
            base = surface_cal.sets_folder(self.config.stage)
            base.mkdir(parents=True, exist_ok=True)
            self.writer = surface_cal.SetWriter(surface_cal.new_set_folder(base), self.vna.frequencies_hz,
                                                self.plate_set.get(), self.sites)
        first_at_site = step.depth_mm == site.depths_mm[0]
        number = self.sites.index(site) + 1
        lines = [f"Site {number} of 5: the {site.label}.", ""]
        if first_at_site:
            lines += ["The stage is driving the probe there now. Wait until it stops, then:", ""]
        lines += [f"1. {surface_cal.plate_instruction(step.depth_mm)}",
                  "2. Keep hands clear of the probe and plate.",
                  "3. Click 'Measure'. It takes a few seconds."]
        if step.depth_mm == site.depths_mm[-1] and number < 5:
            lines += ["", "After this measurement, remove the plate before the stage moves on."]
        self.instructions.configure(text="\n".join(lines))
        self._set_buttons("Measure", None)
        if first_at_site:
            x, y = step.target
            def move():
                self.stage.goto_ideal_xy(x, y)
                self.stage.wait_until_reached_ideal(x, y)
            self._run(f"Moving to the {site.label}...", move, lambda _: self.status.configure(text="In position."))

    def _measure(self) -> None:
        step = self.step

        def sweep():
            sweeps = np.array([self.vna.trigger().sdata for _ in range(surface_cal.PLATE_SWEEPS)])
            physical = tuple(self.stage.calibration.transform(*step.target))
            self.writer.save_plate(step.site, step.depth_mm, sweeps, physical)
            return sweeps
        self._run(f"Measuring ({surface_cal.PLATE_SWEEPS} sweeps)...", sweep, self._measured)

    def _measured(self, sweeps: np.ndarray) -> None:
        step = self.step
        self.measured[(step.site.name, step.depth_mm)] = sweeps
        f = self.vna.frequencies_hz
        i = len(f) // 2
        mag = 20 * np.log10(np.abs(np.mean(sweeps[:, i])))
        text = f"Measured. |S11| at {f[i] / 1e9:.2f} GHz: {mag:.1f} dB."
        color = ""
        surface = self.measured.get((step.site.name, 0.0))
        if step.depth_mm > 0 and surface is not None:
            got = surface_cal.measured_phase_step_deg(surface, sweeps)
            want = surface_cal.expected_phase_step_deg(f, step.depth_mm)
            want_wrapped = (want + 180) % 360 - 180
            off = abs((got - want_wrapped + 180) % 360 - 180)
            text += (f"\nPhase change from the surface plate: {got:+.0f} deg (a {step.depth_mm:g} mm spacer should "
                     f"give about {want_wrapped:+.0f} deg).")
            if off > PHASE_CHECK_TOLERANCE_DEG:
                text += ("\nThat's further off than usual. Check the plate is on the right spacers, flat, and "
                         "centred, then click 'Measure again'.")
                color = "#B3261E"
            else:
                text += " Looks right."
        self.result.configure(text=text, foreground=color)
        self._set_buttons("Next", "Measure again")

    # --- solve ---

    def _enter_solve(self) -> None:
        self.instructions.configure(text="All plates are measured. Remove the plate from the array.\n\n"
                                         "Click 'Solve' to compute the surface calibration from the "
                                         "measurements. Nothing is saved until you click 'Save'.")
        self._set_buttons("Solve", None)

    def _solve(self) -> None:
        info = {"stage_calibration_file": str(self.config.stage.calibration_file or ""),
                "stage_calibration": self.stage.calibration.to_dict(),
                "vna": {"resource": self.config.vna.visa_resource}}
        self._run("Solving the surface calibration...", lambda: self.writer.solve(info), self._solved,
                  on_error=self._solve_failed)

    def _solved(self, solution) -> None:
        self.solution = solution
        lines = ["Result:", *[f"  {s}" for s in solution.summary]]
        if solution.warnings:
            lines += ["", "Check before saving:", *[f"  - {w}" for w in solution.warnings]]
        else:
            lines += ["", "No problems found."]
        self.result.configure(text="\n".join(lines), foreground="#B3261E" if solution.warnings else "")
        self.report.delete("1.0", "end")
        self.report.insert("1.0", solution.report)
        self.report_frame.pack(anchor="nw", fill="both", expand=True, pady=(8, 0))
        self._set_buttons("Save" if not solution.warnings else "Save anyway", None)

    def _solve_failed(self, message: str) -> None:
        self.result.configure(text=f"{message}\n\nCancel and run the calibration again; check the plate heights "
                                   "and that the probe didn't move.", foreground="#B3261E")
        self._set_buttons("Solve", None)

    def _finish(self) -> None:
        def wrap_up():
            saved = self.writer.finish(self.solution) if self.writer is not None else None
            self.stage.goto_ideal_xy(0.0, 0.0)          # back to the origin, so a scan can start there
            self.stage.wait_until_reached_ideal(0.0, 0.0)
            return saved
        self._run("Saving and returning to the origin...", wrap_up, self._finished)

    def _finished(self, saved) -> None:
        summary = [s for s in (self.stage_result,
                               f"Surface calibration set saved to {saved}." if saved else "") if s]
        self._close_hardware()
        if self.on_finished:
            self.on_finished("\n\n".join(summary) or "Done.")
        self.destroy()

    # ------------------------------------------------------------------ buttons ----

    def _set_buttons(self, primary: str, secondary: str | None) -> None:
        self.primary.configure(text=primary)
        if secondary:
            self.secondary.configure(text=secondary)
            self.secondary.pack(side="right", padx=(0, 8), after=self.primary)  # primary stays rightmost
        else:
            self.secondary.pack_forget()

    def _primary(self) -> None:
        if self._busy:
            return
        if self.index < 0:
            return self._start()
        step = self.step
        if step.kind == "nudge":
            return self._confirm_nudge()
        if step.kind == "plate":
            if (step.site.name, step.depth_mm) in self.measured and self.primary.cget("text") == "Next":
                return self._advance()
            return self._measure()
        if self.solution is None:
            return self._solve()
        return self._advance()

    def _secondary(self) -> None:
        if not self._busy and self.index >= 0 and self.step.kind == "plate":
            self._measure()

    def _cancel(self) -> None:
        if self._busy:
            return
        if self.index >= 0 and not messagebox.askyesno(
                "Cancel calibration",
                "Stop the calibration? Measurements so far are discarded; earlier surface calibrations are "
                "not affected. (A stage calibration already completed stays saved.)", parent=self):
            return
        if self.writer is not None:
            self.writer.discard()
        self._close_hardware()
        self.destroy()

    def _close_hardware(self) -> None:
        for dev in (self.vna, self.stage):
            try:
                if dev is not None:
                    dev.close()
            except Exception:
                logging.exception("Closing calibration hardware failed")

    # ------------------------------------------------------------------ background work ----

    def _run(self, message: str, work: Callable, done: Callable, on_error: Callable | None = None) -> None:
        self._busy = True
        self.status.configure(text=message)
        self.progress.pack(side="left", padx=8)
        self.progress.start(12)
        for b in (self.primary, self.secondary):
            b.state(["disabled"])

        def worker():
            try:
                self._queue.put(("ok", work(), done, on_error))
            except Exception as e:
                logging.exception("Calibration step failed")
                self._queue.put(("error", f"{type(e).__name__}: {e}", done, on_error))
        threading.Thread(target=worker, daemon=True).start()
        self.after(50, self._poll)

    def _poll(self) -> None:
        try:
            kind, payload, done, on_error = self._queue.get_nowait()
        except queue.Empty:
            self.after(50, self._poll)
            return
        self._busy = False
        self.progress.stop()
        self.progress.pack_forget()
        for b in (self.primary, self.secondary):
            b.state(["!disabled"])
        if kind == "ok":
            self.status.configure(text="")
            done(payload)
        elif on_error is not None:
            self.status.configure(text="")
            on_error(payload)
        else:
            self.status.configure(text="Something went wrong.")
            messagebox.showerror("Calibration", f"{payload}\n\nFix the problem and try again, or cancel.", parent=self)
            if self.stage is None:  # couldn't even connect: back to the start
                self.index = -1
                self.steps = []
                self._fill_step_list()
                self._show_intro()

    # ------------------------------------------------------------------ board map ----

    def _draw_map(self) -> None:
        cv = self.map
        cv.delete("all")
        pts = HexGridPlanner(self.config.geometry).all_points()
        xs = np.array([p.stage_x_mm for p in pts])
        ys = np.array([p.stage_y_mm for p in pts])
        w, h, pad = int(cv["width"]), int(cv["height"]), 30
        sc = min((w - 2 * pad) / np.ptp(xs), (h - 2 * pad) / np.ptp(ys))
        X = lambda x: pad + (x - xs.min()) * sc
        Y = lambda y: h - pad - (y - ys.min()) * sc
        cv.create_rectangle(X(xs.min()), Y(ys.max()), X(xs.max()), Y(ys.min()), outline="#888780", fill="#F1EFE8")
        current = self.step if 0 <= self.index < len(self.steps) else None

        cx, cy = X((xs.min() + xs.max()) / 2), Y((ys.min() + ys.max()) / 2)

        def marker(x, y, label, state, outward):
            """Stage targets label outward (beyond the board edge), sites inward (towards the
            centre), so neighbouring corner markers' labels never collide."""
            color = {"done": "#1D9E75", "now": "#E24B4A", "todo": "#888780"}[state]
            r = 7 if state == "now" else 5
            px, py = X(x), Y(y)
            cv.create_oval(px - r, py - r, px + r, py + r, outline=color, width=2, fill=color if state == "done" else "")
            sx = 1 if px >= cx else -1   # which side of the centre the marker is on
            sy = 1 if py >= cy else -1
            if outward:
                # Beyond the top/bottom edge, running inwards so it stays on the map.
                cv.create_text(px, py + sy * 13, text=label, anchor=("e" if sx > 0 else "w"),
                               fill=color, font=("TkDefaultFont", 8))
                return
            sx, sy = -sx, -sy                            # sites: towards the centre
            if abs(px - cx) < 2 and abs(py - cy) < 2:   # the centre site itself
                sx, sy = 1, -1
            cv.create_text(px + sx * 9, py + sy * 11, text=label, anchor=("w" if sx > 0 else "e"),
                           fill=color, font=("TkDefaultFont", 8))

        t = self.ideal_targets
        nudges = [("origin", (0.0, 0.0), "origin"),
                  ("far_y", (t.y_corner.stage_x_mm, t.y_corner.stage_y_mm), "far Y"),
                  ("far_x", (t.x_corner.stage_x_mm, t.x_corner.stage_y_mm), "far X")]
        roles = {s.role: s for s in self.steps if s.kind == "nudge"}
        for role, (x, y), label in nudges:
            if role in roles or not self.steps:
                s = roles.get(role)
                state = "now" if current is s and s is not None else ("done" if s is not None and s.done else "todo")
                marker(x, y, label, state, outward=True)
        sites = getattr(self, "sites", None) or surface_cal.site_layout(self.config.geometry, "all_full")
        for site in sites:
            site_steps = [s for s in self.steps if s.kind == "plate" and s.site == site]
            if self.steps and not site_steps:
                continue
            if current is not None and current.kind == "plate" and current.site == site:
                state = "now"
            elif site_steps and all(s.done for s in site_steps):
                state = "done"
            else:
                state = "todo"
            marker(site.ideal_x_mm, site.ideal_y_mm, site.name, state, outward=False)
