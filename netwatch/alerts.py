"""Sound alerts.

``AlertManager`` is pure policy (driven by state changes and a 1 s ``tick``);
``SoundPlayer`` plays generated WAV files or a user-chosen sound file through
whichever command line player exists (pw-play, paplay, aplay).

The built-in sounds are tuned to 700-1800 Hz, where laptop and monitor
speakers are loudest (the original low "heartbeat" was barely audible on
small speakers).
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
SYNTH_VERSION = 2  # bump whenever synthesize() changes so cached WAVs are regenerated

OUTAGE_SOUNDS = {
    "alarm": "Alarm (three loud beeps)",
    "siren": "Siren (two-tone)",
    "beep": "Single beep",
    "heartbeat": "Heartbeat (quiet)",
    "custom": "Custom sound file…",
}
RECOVERY_SOUNDS = {
    "chime": "Chime (rising)",
    "beep": "Single beep",
    "custom": "Custom sound file…",
}
SOUND_FILE_TYPES = (".wav", ".ogg", ".oga", ".flac", ".mp3")


class Player(Protocol):
    available: bool

    def play(self, sound: str, volume: int) -> None: ...

    def stop(self) -> None: ...

    def is_playing(self) -> bool: ...


def _envelope(i: int, n: int, attack: float = 0.02, release: float = 0.6) -> float:
    a, r = int(n * attack), int(n * release)
    if i < a:
        return i / max(1, a)
    if i > n - r:
        return max(0.0, (n - i) / max(1, r))
    return 1.0


def synthesize(style: str, volume: int) -> bytes:
    """16-bit mono PCM for a built-in sound ``style`` at ``volume`` (0-100), peak-normalised."""
    samples: list[float] = []

    def tone(freqs: list[float], seconds: float, amp: float = 1.0, attack: float = 0.02, release: float = 0.3) -> None:
        n = int(RATE * seconds)
        weights = [1.0] + [0.45] * (len(freqs) - 1)  # fundamental plus quieter harmonics
        norm = sum(weights)
        for i in range(n):
            t = i / RATE
            v = sum(w * math.sin(2 * math.pi * f * t) for f, w in zip(freqs, weights)) / norm
            samples.append(v * amp * _envelope(i, n, attack, release))

    def silence(seconds: float) -> None:
        samples.extend([0.0] * int(RATE * seconds))

    if style == "alarm":
        for _ in range(3):
            tone([880, 1760], 0.16, release=0.15)
            silence(0.07)
    elif style == "siren":
        for _ in range(2):
            tone([740, 1480], 0.22, release=0.1)
            tone([988, 1976], 0.22, release=0.1)
    elif style == "beep":
        tone([1000, 2000], 0.35, release=0.2)
    elif style == "heartbeat":  # lub-dub, with harmonics small speakers can reproduce
        tone([220, 440, 660], 0.12, 0.8, release=0.8)
        silence(0.08)
        tone([196, 392, 588], 0.15, 0.65, release=0.85)
        silence(0.1)
    elif style == "chime":
        tone([660, 1320], 0.14, 0.9)
        silence(0.03)
        tone([880, 1760], 0.28, 0.9, release=0.7)
    else:
        tone([880, 1760], 0.2)
    peak = max((abs(x) for x in samples), default=1.0) or 1.0
    gain = max(0, min(100, volume)) / 100 / peak  # normalise: 100% volume = full scale
    return b"".join(struct.pack("<h", int(max(-1.0, min(1.0, x * gain)) * 32767)) for x in samples)


def check_sound_file(path: str, backend: Optional[str]) -> Optional[str]:
    """Return a problem description, or None if ``path`` can be used as an alert sound."""
    if not path:
        return "Choose a sound file."
    p = Path(path).expanduser()
    if not p.is_file():
        return f"{p} does not exist."
    if p.suffix.lower() not in SOUND_FILE_TYPES:
        return "Use a .wav, .ogg, .oga, .flac or .mp3 file."
    if backend == "aplay" and p.suffix.lower() != ".wav":
        return "Only .wav files can be played with aplay; install pipewire or pulseaudio utilities for other formats."
    return None


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

    def sound_file(self, style: str, volume: int) -> Path:
        volume = int(round(volume / 5) * 5)
        path = self.directory / f"v{SYNTH_VERSION}-{style}-{volume}.wav"
        if not path.exists():
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with wave.open(str(tmp), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(RATE)
                wav.writeframes(synthesize(style, volume))
            tmp.replace(path)
        return path

    def is_playing(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def command(self, sound: str, volume: int) -> Optional[list[str]]:
        """Player command for a built-in style name or a path to a sound file."""
        if "/" in sound or Path(sound).suffix.lower() in SOUND_FILE_TYPES:
            if check_sound_file(sound, self.backend_name):
                return None
            name = self.backend_name
            if name == "pw-play":
                return [*self.backend, f"--volume={volume / 100:.2f}", str(Path(sound).expanduser())]
            if name == "paplay":
                return [*self.backend, f"--volume={int(volume / 100 * 65536)}", str(Path(sound).expanduser())]
            return [*self.backend, str(Path(sound).expanduser())]  # aplay has no volume option
        return [*self.backend, str(self.sound_file(sound, volume))]

    def play(self, sound: str, volume: int) -> None:
        if not self.available or volume <= 0:
            return
        try:
            argv = self.command(sound, volume)
        except OSError as exc:
            log.warning("cannot write sound file: %s", exc)
            return
        if argv is None:
            log.warning("cannot play sound %s", sound)
            return
        self.stop()
        with self._lock:
            try:
                self._proc = subprocess.Popen(
                    argv,
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

    def resolve(self, kind: str) -> str:
        """Configured sound (built-in style name or file path) for ``outage`` / ``recovery``."""
        style = str(self.setting(f"{kind}_sound_style"))
        if style == "custom":
            path = str(self.setting(f"{kind}_sound_file") or "")
            if check_sound_file(path, getattr(self.player, "backend_name", None)) is None:
                return path
            log.warning("custom %s sound unusable (%s); using the built-in sound", kind, path)
            return "alarm" if kind == "outage" else "chime"  # never go silent because a file moved
        return style

    def _play(self, kind: str) -> None:
        self.plays.append(kind)
        self.player.play(self.resolve(kind), int(self.setting("sound_volume")))

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
            self.player.play(self.resolve(kind), int(self.setting("sound_volume")))

    def shutdown(self) -> None:
        self.outage_active = False
        self.next_repeat = None
        self.player.stop()
