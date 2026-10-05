"""Convert RunConfig to/from plain JSON-safe dicts, for saving/loading GUI presets."""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

from config import (
    PiControllerConfig, PixelControllerConfig, RunConfig, SaveConfig,
    ScanGeometryConfig, StageConfig, VNAConfig,
)

# Fields that are Path (or Path | None) and need str(...)/Path(...) conversion for JSON.
_PATH_FIELDS = {
    StageConfig: {"calibration_file"},
    PixelControllerConfig: {"pinout_file"},
    SaveConfig: {"output_dir"},
}
# Fields that are tuples in the dataclass; JSON round-trips them as lists.
_TUPLE_FIELDS = {
    PiControllerConfig: {"hb_shape", "lb_shape"},
}

_SUBCONFIGS = {
    "stage": StageConfig,
    "pixels": PixelControllerConfig,
    "pi": PiControllerConfig,
    "vna": VNAConfig,
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
        elif f.name in tuple_fields and value is not None:
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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config_to_dict(config), f, indent=2)


def load_config(path: str | Path) -> RunConfig:
    with open(path, encoding="utf-8") as f:
        return config_from_dict(json.load(f))
