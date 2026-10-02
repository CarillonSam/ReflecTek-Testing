from pathlib import Path
import pandas as pd

"""
Helper to merge:
1) a 32x32 stage/element mapping template
2) the legacy Pixel_Map_by_ConnRow.csv

This does NOT infer the correct controller_index automatically.
It gives you a workspace to copy/assign controller_index values as you build the real map.
"""

element_csv = Path("mapping_template_32x32.csv")
legacy_csv = Path("Pixel_Map_by_ConnRow.csv")
out_csv = Path("mapping_workspace.csv")

elements = pd.read_csv(element_csv)
legacy = pd.read_csv(legacy_csv)

# Keep the most directly useful legacy columns
legacy_small = legacy[[
    "global_index", "row", "connector", "pin", "dac_sel_int", "dac_ch", "notes"
]].copy()

# Create a blank workspace with repeated legacy columns available for manual assignment/reference
elements["controller_index"] = elements.get("controller_index", "")
elements["legacy_row"] = ""
elements["legacy_connector"] = ""
elements["legacy_pin"] = ""
elements["legacy_dac_sel_int"] = ""
elements["legacy_dac_ch"] = ""
elements["assignment_notes"] = ""

elements.to_csv(out_csv, index=False)
legacy_small.to_csv("legacy_connrow_reference.csv", index=False)

print(f"Wrote {out_csv}")
print("Also wrote legacy_connrow_reference.csv for lookup/reference")
