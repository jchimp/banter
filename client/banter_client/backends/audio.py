"""Audio backends. See backends/base.py for the contract."""

import logging
import math
import struct
import subprocess
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("banter.audio")


def arecord_cmd(device: str, rate: int, max_seconds: int, path: Path) -> list[str]:
    """Pure: build the capture command. Kept separate so it is unit-testable."""
    return [
        "arecord", "-D", device, "-f", "S16_LE", "-r", str(rate),
        "-c", "1", "-d", str(max_seconds), str(path),
    ]  # fmt: skip


def aplay_cmd(device: str, path: Path) -> list[str]:
    """Pure: build the playback command."""
    return ["aplay", "-D", device, str(path)]


class AlsaAudio:
    """Pi / Codec Zero. Shells out to arecord and aplay."""

    def __init__(self, capture: str, playback: str, rate: int, max_seconds: int) -> None:
        self.capture, self.playback = capture, playback
        self.rate, self.max_seconds = rate, max_seconds
        self._rec: subprocess.Popen | None = None
        self._play: subprocess.Popen | None = None
        self._started = 0.0
        self._watch: threading.Thread | None = None

    def start_record(self, path: Path) -> None:
        if self._rec:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._started = time.monotonic()
        self._rec = subprocess.Popen(
            arecord_cmd(self.capture, self.rate, self.max_seconds, path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop_record(self) -> float:
        if not self._rec:
            return 0.0
        # arecord does not stop politely; escalate. (CLAUDE.md gotcha 3)
        self._rec.terminate()
        try:
            self._rec.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._rec.kill()
            self._rec.wait(timeout=2)
        self._rec = None
        return time.monotonic() - self._started

    def play(self, path: Path, on_done: Callable[[], None] | None = None) -> None:
        if self._play:
            return
        self._play = subprocess.Popen(
            aplay_cmd(self.playback, path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        def _wait() -> None:
            proc = self._play
            if proc:
                proc.wait()
            self._play = None
            if on_done:
                on_done()

        self._watch = threading.Thread(target=_wait, daemon=True)
        self._watch.start()

    def stop_play(self) -> None:
        if self._play:
            self._play.terminate()

    @property
    def is_recording(self) -> bool:
        return self._rec is not None

    @property
    def is_playing(self) -> bool:
        return self._play is not None

    def close(self) -> None:
        self.stop_record()
        self.stop_play()


class SounddeviceAudio:
    """Laptop / dev box, any OS. Needs the 'dev-audio' extra (sounddevice)."""

    def __init__(self, rate: int, max_seconds: int) -> None:
        import sounddevice as sd  # imported lazily: not installed on the Pi

        self._sd = sd
        self.rate, self.max_seconds = rate, max_seconds
        self._frames: list[bytes] = []
        self._stream = None
        self._path: Path | None = None
        self._started = 0.0
        self._playing = False

    def start_record(self, path: Path) -> None:
        if self._stream:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path, self._frames, self._started = path, [], time.monotonic()

        def cb(indata, frames, t, status):  # noqa: ANN001, ARG001
            if status:
                log.debug("capture status: %s", status)
            self._frames.append(bytes(indata))

        self._stream = self._sd.RawInputStream(
            samplerate=self.rate, channels=1, dtype="int16", callback=cb
        )
        self._stream.start()

    def stop_record(self) -> float:
        if not self._stream:
            return 0.0
        self._stream.stop()
        self._stream.close()
        self._stream = None
        if self._path:
            with wave.open(str(self._path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.rate)
                wf.writeframes(b"".join(self._frames))
        self._frames = []
        return time.monotonic() - self._started

    def play(self, path: Path, on_done: Callable[[], None] | None = None) -> None:
        if self._playing:
            return
        self._playing = True

        def _run() -> None:
            try:
                with wave.open(str(path), "rb") as wf:
                    rate = wf.getframerate()
                    data = wf.readframes(wf.getnframes())
                self._sd.play(memoryview(data).cast("h"), samplerate=rate)
                self._sd.wait()
            except Exception as exc:  # noqa: BLE001
                log.warning("playback failed: %s", exc)
            finally:
                self._playing = False
                if on_done:
                    on_done()

        threading.Thread(target=_run, daemon=True).start()

    def stop_play(self) -> None:
        self._sd.stop()
        self._playing = False

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    @property
    def is_playing(self) -> bool:
        return self._playing

    def close(self) -> None:
        self.stop_record()
        self.stop_play()


class SyntheticAudio:
    """No audio device at all. Writes a real WAV (a tone) and fakes playback timing.

    This is what lets the whole record -> upload -> select -> play loop run in CI, or
    on a headless box, and still produce genuine playable files.
    """

    def __init__(self, rate: int = 16000, tone_hz: float = 440.0) -> None:
        self.rate, self.tone_hz = rate, tone_hz
        self._path: Path | None = None
        self._started = 0.0
        self._playing = False

    def start_record(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path, self._started = path, time.monotonic()

    def stop_record(self) -> float:
        if not self._path:
            return 0.0
        duration = max(time.monotonic() - self._started, 0.05)
        n = int(self.rate * duration)
        with wave.open(str(self._path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.rate)
            wf.writeframes(
                b"".join(
                    struct.pack(
                        "<h", int(12000 * math.sin(2 * math.pi * self.tone_hz * i / self.rate))
                    )
                    for i in range(n)
                )
            )
        self._path = None
        return duration

    def play(self, path: Path, on_done: Callable[[], None] | None = None) -> None:
        self._playing = True
        try:
            with wave.open(str(path), "rb") as wf:
                seconds = wf.getnframes() / float(wf.getframerate() or self.rate)
        except Exception:  # noqa: BLE001
            seconds = 0.1

        def _run() -> None:
            time.sleep(min(seconds, 5.0))
            self._playing = False
            if on_done:
                on_done()

        threading.Thread(target=_run, daemon=True).start()

    def stop_play(self) -> None:
        self._playing = False

    @property
    def is_recording(self) -> bool:
        return self._path is not None

    @property
    def is_playing(self) -> bool:
        return self._playing

    def close(self) -> None:
        self._playing = False
