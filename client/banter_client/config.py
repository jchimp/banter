"""kidbox client configuration. All values come from the env file; no constants inline."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class ClientSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BANTER_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- server ------------------------------------------------------------
    api_url: str = "http://homeserver.local:8080"
    api_key: str = ""
    device_id: str = "kidbox-01"
    # Whole-request budget for an audio download: a 60s clip over weak WiFi needs room.
    http_timeout: float = 30.0
    # ...but /next and /played are ~200 bytes. A dead server that still ACCEPTS the TCP
    # connection (Docker leaves its port proxy bound after the container stops) hangs
    # until the READ timeout, so BTN2 would sit silent for `http_timeout` before
    # falling back to cache. Keep these short so the fallback is quick.
    next_timeout: float = 3.0
    # After a failed fetch, skip the network entirely for this long and go straight to
    # the play cache. Makes every tap after the first one instant during an outage.
    offline_memo_seconds: float = 30.0

    # --- backends (see backends/base.py) ----------------------------------
    # Pi:      alsa / gpio / neopixel
    # Laptop:  sounddevice / keyboard / terminal
    # CI:      synthetic / keyboard / null
    audio_backend: Literal["alsa", "sounddevice", "synthetic"] = "alsa"
    button_backend: Literal["gpio", "keyboard"] = "gpio"
    ring_backend: Literal["neopixel", "terminal", "null"] = "neopixel"

    # --- audio (find devices with `arecord -l` / `aplay -l`) --------------
    alsa_capture: str = "plughw:0,0"
    alsa_playback: str = "plughw:0,0"
    sample_rate: int = 16000

    # --- buttons -----------------------------------------------------------
    pin_record: int = 17
    pin_play: int = 22
    button_mode: Literal["hold", "toggle"] = "hold"
    bounce_seconds: float = 0.05

    # --- recording limits --------------------------------------------------
    max_seconds: int = 60
    min_seconds: float = 0.8

    # --- ring --------------------------------------------------------------
    ring_pixels: int = 16
    led_max_brightness: float = 0.3
    idle_glow: bool = False

    # --- local storage -----------------------------------------------------
    queue_dir: Path = Path.home() / "banter-queue"
    cache_dir: Path = Path.home() / "banter-cache"
    play_cache_size: int = 10

    # --- retry -------------------------------------------------------------
    backoff_start_seconds: float = 2.0
    backoff_max_seconds: float = 60.0

    # --- heartbeat -----------------------------------------------------------
    # Fixed interval, no backoff (see heartbeat.py docstring): the server must
    # notice a dead device promptly, not after a growing backoff delay.
    heartbeat_interval_seconds: float = 60.0

    @property
    def recordings_url(self) -> str:
        return f"{self.api_url.rstrip('/')}/api/recordings"

    @property
    def next_url(self) -> str:
        return f"{self.recordings_url}/next"

    def heartbeat_url(self) -> str:
        return f"{self.api_url.rstrip('/')}/api/devices/{self.device_id}/heartbeat"


@lru_cache
def get_settings() -> ClientSettings:
    return ClientSettings()
