"""Sound alerts.

``AlertManager`` is pure policy (driven by state changes and a 1 s ``tick``);
``SoundPlayer`` plays short generated WAV files through whichever command
line player exists (pw-play, paplay, aplay).  The default outage sound is a
soft, low "heartbeat" double-thump; recovery is a gentle rising two-tone.
"""

from __future__ import annotations

import logging
import math
import shutil
import struct
import subprocess
import threading
import wave
from pathlib import Path
from typing import Callable, Optional, Protocol

from netwatch.paths import cache_dir

log = logging.getLogger(__name__)

RATE = 22050


class Player(Protocol):
    available: bool

    def play(self, kind: str, volume: int) -> None: ...

    def stop(self) -> None: ...

    def is_playing(self) -> bool: ...


def _envelope(i: int, n: int, attack: float = 0.02, release: float = 0.6) -> float:
    a, r = int(n * attack), int(n * release)
    if i < a:
        return i / max(1, a)
    if i > n - r:
        return max(0.0, (n - i) / max(1, r))
    return 1.0


def synthesize(kind: str, volume: int) -> bytes:
    """Return 16-bit mono PCM samples for ``kind`` at ``volume`` (0-100)."""
    gain = max(0, min(100, volume)) / 100 * 0.9
    samples: list[float] = []

    def tone(freqs: list[float], seconds: float, amp: float, release: float = 0.6) -> None:
        n = int(RATE * seconds)
        for i in range(n):
            t = i / RATE
            v = sum(math.sin(2 * math.pi * f * t) for f in freqs) / len(freqs)
            samples.append(v * amp * _envelope(i, n, release=release))

    def silence(seconds: float) -> None:
        samples.extend([0.0] * int(RATE * seconds))

    if kind == "outage":  # lub-dub heartbeat
        tone([70, 140], 0.13, 1.0, release=0.8)
        silence(0.09)
        tone([62, 124], 0.16, 0.8, release=0.85)
        silence(0.15)
    elif kind == "recovery":
        tone([660, 1320], 0.12, 0.35)
        silence(0.03)
        tone([880, 1760], 0.22, 0.35, release=0.8)
    else:  # test / generic
        tone([523], 0.2, 0.4)
    return b"".join(struct.pack("<h", int(max(-1.0, min(1.0, s * gain)) * 32767)) for s in samples)


class SoundPlayer:
    BACKENDS = (("pw-play", []), ("paplay", []), ("aplay", ["-q"]))

    def __init__(self, directory: Optional[Path] = None):
        self.directory = directory or (cache_dir() / "sounds")
        self.backend: Optional[list[str]] = None
        for name, args in self.BACKENDS:
            path = shutil.which(name)
            if path:
                self.backend = [path, *args]
                break
        self.available = self.backend is not None
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    @property
    def backend_name(self) -> str:
        return Path(self.backend[0]).name if self.backend else "none"

    def sound_file(self, kind: str, volume: int) -> Path:
        volume = int(round(volume / 5) * 5)
        path = self.directory / f"{kind}-{volume}.wav"
        if not path.exists():
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with wave.open(str(tmp), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(RATE)
                wav.writeframes(synthesize(kind, volume))
            tmp.replace(path)
        return path

    def is_playing(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def play(self, kind: str, volume: int) -> None:
        if not self.available or volume <= 0:
            return
        try:
            path = self.sound_file(kind, volume)
        except OSError as exc:
            log.warning("cannot write sound file: %s", exc)
            return
        self.stop()
        with self._lock:
            try:
                self._proc = subprocess.Popen(
                    [*self.backend, str(path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                log.warning("sound backend failed: %s", exc)
                self.available = False
                self._proc = None

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
        elif proc:
            proc.wait()

    def reap(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is not None:
                self._proc = None


class AlertManager:
    """Decides when to play sounds.  Never affects monitoring or logging."""

    def __init__(self, player: Player, setting: Callable[[str], object], clock: Callable[[], float]):
        self.player = player
        self.setting = setting
        self.clock = clock
        self.muted = bool(setting("start_muted")) or bool(setting("muted"))
        self.outage_active = False
        self.next_repeat: Optional[float] = None
        self.plays: list[str] = []  # history, useful for diagnostics/tests

    def _audible(self) -> bool:
        return bool(self.setting("sound_enabled")) and not self.muted and self.player.available

    def _play(self, kind: str) -> None:
        self.plays.append(kind)
        self.player.play(kind, int(self.setting("sound_volume")))

    def _schedule_repeat(self, now: float) -> None:
        self.next_repeat = now + float(self.setting("sound_repeat_seconds")) if self.setting("sound_repeat") else None

    def outage_started(self) -> None:
        if self.outage_active:
            return  # already alerting for this outage
        self.outage_active = True
        now = self.clock()
        if self._audible():
            self._play("outage")
        self._schedule_repeat(now)

    def outage_ended(self, recovered: bool = True) -> None:
        if not self.outage_active:
            return
        self.outage_active = False
        self.next_repeat = None
        self.player.stop()
        if recovered and self._audible() and self.setting("sound_recovery"):
            self._play("recovery")

    def set_muted(self, muted: bool) -> None:
        if muted == self.muted:
            return
        self.muted = muted
        if muted:
            self.player.stop()
            return
        if self.outage_active and self.setting("resume_alert_on_unmute"):
            if self._audible():
                self._play("outage")
            self._schedule_repeat(self.clock())
        elif self.outage_active:
            self.next_repeat = None  # stay quiet for the rest of this outage

    def tick(self) -> None:
        if hasattr(self.player, "reap"):
            self.player.reap()
        if not self.outage_active or self.next_repeat is None:
            return
        now = self.clock()
        if now < self.next_repeat:
            return
        if self._audible() and not self.player.is_playing():
            self._play("outage")
        self._schedule_repeat(now)

    def test_sound(self, kind: str = "outage") -> None:
        if self.player.available:
            self.player.play(kind, int(self.setting("sound_volume")))

    def shutdown(self) -> None:
        self.outage_active = False
        self.next_repeat = None
        self.player.stop()
