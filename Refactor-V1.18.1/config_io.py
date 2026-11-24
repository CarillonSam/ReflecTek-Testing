"""Convert RunConfig to/from plain JSON-safe dicts, for saving/loading GUI presets."""

from __future__ import annotations

import json
import os
from dataclasses import fields
from pathlib import Path

from config import (
    PiControllerConfig, PixelControllerConfig, RunConfig, SaveConfig,
    ScanGeometryConfig, StageConfig, TransientConfig, VNAConfig,
)

# Fields that are Path (or Path | None) and need str(...)/Path(...) conversion for JSON.
_PATH_FIELDS = {
    StageConfig: {"calibration_file", "surface_cal_dir"},
    PixelControllerConfig: {"pinout_file"},
    SaveConfig: {"output_dir"},
}
# Fields that are tuples in the dataclass; JSON round-trips them as lists.
_TUPLE_FIELDS = {
    PiControllerConfig: {"hb_shape", "lb_shape"},
    TransientConfig: {"voltages_v", "hold_s"},   # hold_s may also be a single number, kept as is
}

_SUBCONFIGS = {
    "stage": StageConfig,
    "pixels": PixelControllerConfig,
    "pi": PiControllerConfig,
    "vna": VNAConfig,
    "transient": TransientConfig,   # presets before V1.18.1 have none: transient defaults apply
    "geometry": ScanGeometryConfig,
    "save": SaveConfig,
}


def _sub_to_dict(obj) -> dict:
    path_fields = _PATH_FIELDS.get(type(obj), set())
    out = {}
    for f in fields(obj):
        v = getattr(obj, f.name)
        if f.name in path_fields and v is not None:
            v = str(v)
        elif isinstance(v, tuple):
            v = list(v)
        out[f.name] = v
    return out


def _sub_from_dict(cls, data: dict):
    path_fields = _PATH_FIELDS.get(cls, set())
    tuple_fields = _TUPLE_FIELDS.get(cls, set())
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        if f.name in path_fields and value is not None:
            value = Path(value)
        elif f.name in tuple_fields and isinstance(value, list):
            value = tuple(value)
        kwargs[f.name] = value
    return cls(**kwargs)


def config_to_dict(config: RunConfig) -> dict:
    out = {name: _sub_to_dict(getattr(config, name)) for name in _SUBCONFIGS}
    for f in fields(RunConfig):
        if f.name not in _SUBCONFIGS:
            v = getattr(config, f.name)
            if f.name == "voltages_v":
                v = list(v)
            elif isinstance(v, Path):
                v = str(v)
            out[f.name] = v
    return out


def config_from_dict(data: dict) -> RunConfig:
    kwargs = {name: _sub_from_dict(cls, data.get(name, {})) for name, cls in _SUBCONFIGS.items()}
    for f in fields(RunConfig):
        if f.name in _SUBCONFIGS or f.name not in data:
            continue  # missing top-level keys fall back to RunConfig's own defaults
        value = data[f.name]
        if f.name == "voltages_v":
            value = tuple(value)
        elif f.name == "pattern_csv" and value is not None:
            value = Path(value)
        kwargs[f.name] = value
    # Presets saved before RunConfig.band existed kept the band only in pi.active_band.
    if "band" not in data and "active_band" in data.get("pi", {}):
        kwargs["band"] = data["pi"]["active_band"]
    return RunConfig(**kwargs)


def save_config(config: RunConfig, path: str | Path) -> None:
    """The whole file is built first and only then written (to a temporary name, renamed over the
    old file), so a save that fails leaves the old file untouched rather than half written."""
    text = json.dumps(config_to_dict(config), indent=2)
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# Before V1.18.1, saving stopped partway through, at the transient settings (this module didn't
# know about them), leaving a file that ends with '"transient": '. Everything before that point
# was written in full: these sections. The rest is lost.
_CUT_OFF_AT = '"transient":'
_KEPT_IN_CUT_OFF = "stage, board controller, Pi, VNA, scan geometry and save"
_LOST_IN_CUT_OFF = ("voltages, scan type, board controller type, settle times, uniform-board and dry-run options, "
                    "voltage pattern file and the transient settings")


def load_config_checked(path: str | Path) -> tuple[RunConfig, str]:
    """(settings, note). The note is "" for a normal file. A file cut off by the pre-V1.18.1 save
    bug is recovered as far as it goes, and the note says what was lost (those settings are at
    their defaults; saving again writes a complete file)."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        return config_from_dict(json.loads(text)), ""
    except json.JSONDecodeError:
        stripped = text.rstrip()
        if not stripped.endswith(_CUT_OFF_AT):
            raise
        head = stripped[: -len(_CUT_OFF_AT)].rstrip().rstrip(",") + "\n}"
        try:
            data = json.loads(head)
        except json.JSONDecodeError:
            raise ValueError("The file is cut off partway through and couldn't be recovered.") from None
    band = data.get("pi", {}).get("active_band")
    note = (f"This file was cut off when it was saved (a bug in versions before 1.18.1). Recovered: the "
            f"{_KEPT_IN_CUT_OFF} settings" + (f", and the band ({band})" if band else "") +
            f". Not in the file, so set to their defaults: {_LOST_IN_CUT_OFF}. Check those, then save "
            "the settings again to write a complete file.")
    return config_from_dict(data), note


def load_config(path: str | Path) -> RunConfig:
    return load_config_checked(path)[0]
