"""
Board voltage controller: SSH/file-upload implementation for the Raspberry Pi + SPI DAC
setup. PiController does the SSH/SFTP work; PiBoardController adapts it to BoardController
so run_scan can use it interchangeably with PixelController.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np
import paramiko

from config import PiControllerConfig
from controller import BoardController


class PiController:
    """
    Connects to a raspberry pi via ssh with Paramiko. Copies a local low and high band
    file to the files on the PI. Runs the remote command that starts the python program
    on the PI which updates the DACs via SPI. Creates a stop text file that is watched
    for by that program to stop it so that it can read another set of HB and LB voltages.
    Reopens VNA connection each time, closes resource manager at the very end.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: Optional[str],
        local_file_hb: str,
        local_file_lb: str,
        remote_file_hb: str,
        remote_file_lb: str,
        remote_command: str,
        port: int,
        key_filename: Optional[str],
        stop_file: str,
        stop_settle_s: float = 1.0,
    ):
        self.host = host
        self.username = username
        self.password = password
        self.local_file_hb = local_file_hb
        self.local_file_lb = local_file_lb
        self.remote_file_hb = remote_file_hb
        self.remote_file_lb = remote_file_lb
        self.remote_command = remote_command
        self.port = port
        self.key_filename = key_filename
        self.stop_file = stop_file
        self.stop_settle_s = stop_settle_s
        self.client = None

    def connect(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            key_filename=self.key_filename,
            look_for_keys=True,
        )
        self.client = client
        return self

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            finally:
                self.client = None

    def remove_stop_file(self) -> None:
        sftp = self.client.open_sftp()
        try:
            sftp.remove(self.stop_file)
            logging.info("Stop file removed")
        except FileNotFoundError:
            pass
        sftp.close()

    def stop_program(self) -> None:
        sftp = self.client.open_sftp()
        with sftp.open(self.stop_file, "w"):
            pass
        sftp.close()

    def upload_lb_and_hb_files(self) -> None:
        sftp = self.client.open_sftp()
        logging.info("Uploading %s -> %s", self.local_file_hb, self.remote_file_hb)
        sftp.put(self.local_file_hb, self.remote_file_hb)
        logging.info("Uploading %s -> %s", self.local_file_lb, self.remote_file_lb)
        sftp.put(self.local_file_lb, self.remote_file_lb)
        sftp.close()

    def run_remote_command(self, wait: bool = False, get_pty: bool = True):
        logging.info("Running remote command: %s", self.remote_command)
        stdin, stdout, stderr = self.client.exec_command(self.remote_command, get_pty=get_pty)
        if not wait:
            return None
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return out, err, exit_status

    def update_dacs(self) -> None:
        self.stop_program()
        time.sleep(self.stop_settle_s)
        self.remove_stop_file()
        self.upload_lb_and_hb_files()
        self.run_remote_command()

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc, tb):
        self.close()


class PiBoardController(BoardController):
    """
    Adapts PiController to BoardController. The Pi has its own internal element -> DAC
    channel map, so there's no PC-side mapping at all: set_voltage_grid's array is
    written directly as a row-per-line, comma-separated-columns CSV, where
    csv[row][col] is the voltage for the element at that (row, col) — the Pi's own
    firmware handles the rest. Only the configured active_band ("lb" or "hb") gets the
    real scan-derived grid; the other band's file is still uploaded (in case the remote
    script expects both present) but filled with zeros at its own configured shape.

    ASSUMPTION (confirm against the remote DAC driver script before relying on this):
    each local file is one row per line, comma-separated voltages per column, matching
    a plain `csv.writer` / numpy `savetxt`-style layout. Adjust _write_csv_2d if the
    real file format differs.
    """

    def __init__(self, config: PiControllerConfig) -> None:
        self.config = config
        self.pi = PiController(
            host=config.host,
            username=config.username,
            password=config.password,
            local_file_hb=config.local_file_hb,
            local_file_lb=config.local_file_lb,
            remote_file_hb=config.remote_file_hb,
            remote_file_lb=config.remote_file_lb,
            remote_command=config.remote_command,
            port=config.port,
            key_filename=config.key_filename,
            stop_file=config.stop_file,
            stop_settle_s=config.stop_settle_s,
        ).connect()

    def close(self) -> None:
        self.pi.close()

    def ping(self) -> dict:
        transport = self.pi.client.get_transport() if self.pi.client else None
        alive = bool(transport and transport.is_active())
        return {"ok": alive, "error": None if alive else "SSH connection not active", "status_name": "ALIVE" if alive else "DOWN"}

    @staticmethod
    def _write_csv_2d(path: str, grid_rc: np.ndarray) -> None:
        """grid_rc has shape (rows, cols) — one line per row, comma-separated columns."""
        with open(path, "w", encoding="utf-8") as f:
            for row in grid_rc:
                f.write(",".join(f"{v:.4f}" for v in row) + "\n")

    def set_voltage_grid(self, voltages: np.ndarray) -> None:
        """
        voltages has shape (cols, rows) — this project's indexing convention throughout
        (see HexGridPlanner). Transposed here to (rows, cols) so csv[row][col] lands
        exactly where the Pi's DAC map expects it: no other reordering, no per-element
        lookup, matching what you actually upload determining what gets addressed.
        """
        active_grid = np.clip(voltages, self.config.min_voltage_v, self.config.max_voltage_v).T  # (rows, cols)
        # As written to the file (4 decimals): what the Pi is told for the scanned density.
        self._written_grid = np.round(active_grid, 4)
        inactive_shape = self.config.hb_shape if self.config.active_band == "lb" else self.config.lb_shape
        inactive_grid = np.zeros(inactive_shape)

        if self.config.active_band == "hb":
            self._write_csv_2d(self.config.local_file_hb, active_grid)
            self._write_csv_2d(self.config.local_file_lb, inactive_grid)
        else:
            self._write_csv_2d(self.config.local_file_lb, active_grid)
            self._write_csv_2d(self.config.local_file_hb, inactive_grid)

        self.pi.update_dacs()

    def element_voltage(self, density: str, row: int, col: int) -> float:
        """The value last written to the Pi's grid file for this element (the scanned density's
        grid, 4 decimals); NaN if nothing has been written or it's outside that grid."""
        grid = getattr(self, "_written_grid", None)
        if grid is None or not (0 <= row < grid.shape[0] and 0 <= col < grid.shape[1]):
            return float("nan")
        return float(grid[row, col])
