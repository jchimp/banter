"""Audio backends. See backends/base.py for the contract."""

import logging
import math
import re
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
    # -q suppresses the "Recording WAVE ..." banner (which goes to stderr), so that
    # anything arecord writes to stderr is an actual error worth logging.
    return [
        "arecord", "-q", "-D", device, "-f", "S16_LE", "-r", str(rate),
        "-c", "1", "-d", str(max_seconds), str(path),
    ]  # fmt: skip


def aplay_cmd(device: str, path: Path) -> list[str]:
    """Pure: build the playback command."""
    return ["aplay", "-D", device, str(path)]


# ----------------------------------------------------------------- preflight
# A wrong ALSA device string fails quietly: arecord exits, the controller probes a
# missing/header-only WAV and discards it (FR-3), and every joke the kid records
# vanishes with nothing but `captured=0.00` to go on. That is easy to hit on the
# Pi 4 + USB profile, where card indices renumber across reboots. These parse the
# ALSA listings so startup can say which device is wrong and what exists instead.


def parse_pcm_names(listing: str) -> list[str]:
    """Pure: PCM names from `arecord -L` / `aplay -L`. Names are the unindented lines."""
    return [line.strip() for line in listing.splitlines() if line and not line[0].isspace()]


def parse_cards(listing: str) -> dict[int, str]:
    """Pure: index -> card name from `arecord -l` / `aplay -l`.

    Lines look like: `card 1: Webcam [USB Webcam], device 0: USB Audio [USB Audio]`.
    """
    cards: dict[int, str] = {}
    for line in listing.splitlines():
        m = re.match(r"card (\d+): (\S+)", line.strip())
        if m:
            cards[int(m.group(1))] = m.group(2)
    return cards


def alsa_device_ok(device: str, pcms: list[str], cards: dict[int, str]) -> bool:
    """Pure: does `device` name something ALSA is currently offering?

    Accepts the three forms people actually write: a full PCM name from `-L`,
    `hw:N,M` / `plughw:N,M` by card index, and `...CARD=name...` by card name.
    """
    if device in pcms:
        return True
    m = re.match(r"(?:plug)?hw:(\d+)", device)
    if m:
        return int(m.group(1)) in cards
    m = re.search(r"CARD=([^,]+)", device)
    if m:
        return m.group(1) in cards.values()
    return False


def _alsa_listing(tool: str, flag: str) -> str:
    """Impure: run `arecord|aplay -L|-l`. Empty string if the tool is missing."""
    try:
        out = subprocess.run([tool, flag], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("event=alsa_listing_failed tool=%s flag=%s err=%s", tool, flag, exc)
        return ""
    return out.stdout


def check_alsa_devices(capture: str, playback: str) -> list[str]:
    """Impure: report configured ALSA devices that don't currently exist.

    Returns one message per problem, each naming the bad value and the alternatives,
    so the caller can log it. An empty list means both devices resolve. If the ALSA
    tools produce nothing (not installed, no sound cards yet) we report nothing
    rather than crying wolf.
    """
    problems: list[str] = []
    for which, device, tool in (("capture", capture, "arecord"), ("playback", playback, "aplay")):
        pcms = parse_pcm_names(_alsa_listing(tool, "-L"))
        cards = parse_cards(_alsa_listing(tool, "-l"))
        if not pcms and not cards:
            continue
        if not alsa_device_ok(device, pcms, cards):
            available = ", ".join(f"{i}:{n}" for i, n in sorted(cards.items())) or "none"
            problems.append(
                f"which={which} configured={device!r} not found; "
                f"cards={available} (run `{tool} -L` for the stable CARD= names)"
            )
    return problems


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
        # stderr is kept: with -q the banner is gone, so anything on stderr is a real
        # ALSA complaint (wrong device, stalled capture) worth having in journald.
        self._rec = subprocess.Popen(
            arecord_cmd(self.capture, self.rate, self.max_seconds, path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def stop_record(self) -> float:
        if not self._rec:
            return 0.0
        proc, self._rec = self._rec, None
        # arecord does not stop politely; escalate. (CLAUDE.md gotcha 3)
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # A capture that has to be SIGKILLed never got a frame from ALSA — the
            # file on disk is header-only even if its header claims otherwise.
            log.warning("event=arecord_killed device=%s", self.capture)
            proc.kill()
            proc.wait(timeout=2)
        stderr = proc.stderr.read() if proc.stderr else b""
        if stderr:
            log.warning(
                "event=arecord_stderr device=%s msg=%s",
                self.capture,
                stderr.decode(errors="replace").strip(),
            )
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
