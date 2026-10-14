"""Tracked subprocess execution so shutdown never leaves orphaned children."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class ProcessRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[subprocess.Popen] = set()
        self._closed = False

    def run(self, argv: list[str], timeout: float) -> ProcessResult:
        """Run ``argv``; raises FileNotFoundError / PermissionError like Popen."""
        with self._lock:
            if self._closed:
                return ProcessResult(-1, "", "shutting down")
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )
            self._active.add(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
            return ProcessResult(proc.returncode, out or "", err or "")
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            return ProcessResult(proc.returncode, out or "", err or "", timed_out=True)
        finally:
            with self._lock:
                self._active.discard(proc)

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def terminate_all(self) -> None:
        with self._lock:
            self._closed = True
            procs = list(self._active)
        for proc in procs:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        for proc in procs:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def reopen(self) -> None:
        with self._lock:
            self._closed = False
