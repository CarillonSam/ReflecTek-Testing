from __future__ import annotations

import importlib
import time
from dataclasses import dataclass

import numpy as np


@dataclass
class VNAResult:
    sdata: np.ndarray
    amplitude: np.ndarray
    phase_deg: np.ndarray


class VNAController:
    """
    Thin adapter around the existing VNATest-style module.

    Expected interface:
        module.init(str(start_hz), str(stop_hz), str(points))
        module.trigger() -> array-like complex s-parameter vector

    Optional:
        module.close()
    """

    def __init__(self, module_name: str = "VNATest", dwell_s: float = 0.0) -> None:
        self.module_name = module_name
        self.dwell_s = float(dwell_s)
        self.module = importlib.import_module(module_name)

    def initialize(self, start_hz: float, stop_hz: float, points: int) -> None:
        self.module.init(str(start_hz), str(stop_hz), str(points))

    def trigger(self) -> VNAResult:
        if self.dwell_s > 0:
            time.sleep(self.dwell_s)
        sdata = np.asarray(self.module.trigger())
        return VNAResult(
            sdata=sdata,
            amplitude=np.round(np.abs(sdata), 5),
            phase_deg=np.round(np.degrees(np.angle(sdata)), 3),
        )

    def close(self) -> None:
        close_fn = getattr(self.module, "close", None)
        if callable(close_fn):
            close_fn()
