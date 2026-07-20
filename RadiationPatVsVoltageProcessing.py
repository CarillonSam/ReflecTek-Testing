# -*- coding: utf-8 -*-
"""
Created on Thu Jun  4 15:56:56 2026

@author: uconn
"""

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.cm as cm
import os
import glob
import re
 
# ── Configure this ────────────────────────────────────────────────
DATA_DIR = r"C:\Users\uconn\Downloads\RadiationPatVsVoltage\2026-06-05_test2"
# ─────────────────────────────────────────────────────────────────
 
#  Find all matching .npz files
files = glob.glob(os.path.join(DATA_DIR, "MagVsAz_*V.npz"))
 
if not files:
    print("No files found.")
    exit()
 
# Parse voltage from filename
def parse_voltage(filepath):
    match = re.search(r"MagVsAz_([\d.]+)V\.npz", os.path.basename(filepath))
    return float(match.group(1)) if match else None
 
# Sort by voltage
files = sorted(files, key=parse_voltage)
voltages = [parse_voltage(f) for f in files]
 
# Normalise voltages for colormap
v_min, v_max = min(voltages), max(voltages)
v_norm = [(v - v_min) / (v_max - v_min) if v_max != v_min else 0.5 for v in voltages]
 
cmap = cm.viridis
sm = cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(vmin=v_min, vmax=v_max))
sm.set_array([])
 
# Collect phase at -30 degrees for each voltage
phase_at_minus30 = []
 
# ── Magnitude figure ──────────────────────────────────────────────
fig_mag, ax_mag = plt.subplots(figsize=(10, 5))
 
for f, v, vnorm in zip(files, voltages, v_norm):
    data = np.load(f)
    ax_mag.plot(data["position_axis"], data["magnitude"], color=cmap(vnorm), linewidth=1)
 
ax_mag.set_xlabel("Position (°)")
ax_mag.set_ylabel("Magnitude")
ax_mag.set_title("Magnitude vs Position")
ax_mag.grid(True, which="major", linewidth=0.8)
ax_mag.grid(True, which="minor", linewidth=0.3, linestyle=":")
ax_mag.minorticks_on()
ax_mag.set_ylim(-90,-50)
fig_mag.colorbar(sm, ax=ax_mag, label="Voltage (V)")
fig_mag.tight_layout()
 
# ── Phase figure ──────────────────────────────────────────────────
fig_phase, ax_phase = plt.subplots(figsize=(10, 5))
 
for f, v, vnorm in zip(files, voltages, v_norm):
    data = np.load(f)
    position = data["position_axis"]
    phase = data["phase"]
    phase_unwrapped = np.unwrap(phase, period=360)  # unwrap in degrees
 
    ax_phase.plot(position, phase, color=cmap(vnorm), linewidth=1)
 
    # Interpolate phase at -30 degrees position
    idx = np.argmin(np.abs(position - (26)))
    phase_at_minus30.append((v, phase[idx]))
 
ax_phase.set_xlabel("Position (°)")
ax_phase.set_ylabel("Phase (°)")
ax_phase.set_title("wrapped Phase vs Position")
ax_phase.grid(True, which="major", linewidth=0.8)
ax_phase.grid(True, which="minor", linewidth=0.3, linestyle=":")
ax_phase.minorticks_on()
fig_phase.colorbar(sm, ax=ax_phase, label="Voltage (V)")
fig_phase.tight_layout()
 
# ── Phase at -30° vs Voltage figure ──────────────────────────────
phase_at_minus30.sort(key=lambda x: x[0])
vols, phases = zip(*phase_at_minus30)
 
fig_pv, ax_pv = plt.subplots(figsize=(10, 5))
ax_pv.plot(vols, phases, marker="o", color="steelblue", linewidth=1.5)
ax_pv.set_xlabel("Voltage (V)")
ax_pv.set_ylabel("Phase at 26° (°)")
ax_pv.set_title("Phase at Position = 26° vs Voltage")
ax_pv.grid(True, which="major", linewidth=0.8)
ax_pv.grid(True, which="minor", linewidth=0.3, linestyle=":")
ax_pv.minorticks_on()
fig_pv.tight_layout()
 
plt.show()


#-------Export ph 2 voltage

# Export voltage vs phase to CSV
csv_path = os.path.join(DATA_DIR, "voltage_vs_phase_at_26deg.csv")

out = np.column_stack((vols, phases))
np.savetxt(
    csv_path,
    out,
    delimiter=",",
    header="Voltage_V,Phase_deg_at_26deg",
    comments=""
)

print(f"Saved CSV to: {csv_path}")