"""Backend selection. The only place that decides real vs simulated hardware."""

import logging

from banter_client.backends.audio import AlsaAudio, SounddeviceAudio, SyntheticAudio
from banter_client.backends.base import AudioBackend, ButtonBackend, ButtonCallbacks, RingBackend
from banter_client.backends.io import (
    GpioButtons,
    KeyboardButtons,
    NeoPixelRing,
    NullRing,
    TerminalRing,
)
from banter_client.config import ClientSettings

log = logging.getLogger("banter.backends")

AUDIO_CHOICES = ("alsa", "sounddevice", "synthetic")
BUTTON_CHOICES = ("gpio", "keyboard")
RING_CHOICES = ("neopixel", "terminal", "null")


def make_audio(s: ClientSettings) -> AudioBackend:
    match s.audio_backend:
        case "alsa":
            return AlsaAudio(s.alsa_capture, s.alsa_playback, s.sample_rate, s.max_seconds)
        case "sounddevice":
            return SounddeviceAudio(s.sample_rate, s.max_seconds)
        case "synthetic":
            return SyntheticAudio(s.sample_rate)
        case other:
            raise ValueError(f"unknown audio_backend {other!r}; expected one of {AUDIO_CHOICES}")


def make_buttons(s: ClientSettings, callbacks: ButtonCallbacks) -> ButtonBackend:
    match s.button_backend:
        case "gpio":
            return GpioButtons(callbacks, s.pin_record, s.pin_play, s.bounce_seconds, s.button_mode)
        case "keyboard":
            return KeyboardButtons(callbacks)
        case other:
            raise ValueError(f"unknown button_backend {other!r}; expected one of {BUTTON_CHOICES}")


def make_ring(s: ClientSettings) -> RingBackend:
    match s.ring_backend:
        case "neopixel":
            try:
                return NeoPixelRing(s.ring_pixels, s.led_max_brightness)
            except Exception as exc:
                # NeoPixelRing does `import board; board.SPI()`, which raises when SPI
                # is off, Blinka is missing, or /dev/spidev* isn't there. Unguarded that
                # kills App.__init__ before a single log line, and the unit's
                # Restart=on-failure then burns systemd's start limit in ~15s and gives
                # up for good. A box with no ring is degraded; a box that won't boot and
                # won't say why is a service call.
                log.error(
                    "event=ring_unavailable error=%s hint=needs dtparam=spi=on and "
                    "core_freq_min=500 in /boot/firmware/config.txt, then a reboot; "
                    "running without ring feedback",
                    exc,
                )
                return NullRing()
        case "terminal":
            return TerminalRing()
        case "null":
            return NullRing()
        case other:
            raise ValueError(f"unknown ring_backend {other!r}; expected one of {RING_CHOICES}")


def describe(s: ClientSettings) -> str:
    return f"audio={s.audio_backend} buttons={s.button_backend} ring={s.ring_backend}"
