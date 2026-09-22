"""kidbox client configuration. All values come from the env file; no constants inline."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
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
    # Optional mixer levels applied once at startup so the box boots the same every
    # time, whatever the last alsamixer session left. Control names are per card
    # (`amixer -c N scontrols`), so they live in .env, never in code. Empty = skip.
    alsa_playback_control: str = ""
    alsa_playback_percent: int = Field(80, ge=0, le=100)
    alsa_capture_control: str = ""
    alsa_capture_percent: int = Field(80, ge=0, le=100)

    # --- buttons -----------------------------------------------------------
    pin_record: int = 17
    pin_play: int = 22
    # BTN3 replay: same pin on both builds (the Codec Zero HAT does not use GPIO5).
    pin_replay: int = 5
    button_mode: Literal["hold", "toggle"] = "hold"
    bounce_seconds: float = 0.05

    # --- recording limits --------------------------------------------------
    max_seconds: int = 60
    min_seconds: float = 0.8
    # BTN1 must stay pressed this long before the beep and capture start; a shorter
    # press (a bump, a curious tap) is ignored outright. 0 disables the guard.
    record_arm_seconds: float = Field(0.25, ge=0.0)
    # Silence / low-content gate (analysis.py). Every clip logs peak_dbfs, floor_dbfs
    # and voiced so these can be tuned from journald rather than by guesswork.
    #   silence_dbfs:       a clip whose loudest sample is under this is `silent`; a
    #                       100 ms window under this is never counted as voiced.
    #   voice_margin_db:    a voiced window must also sit this far above the clip's own
    #                       noise floor — a constant fan hum has no such headroom.
    #   min_voiced_seconds: fewer voiced seconds than this is `low_content`. 0 disables.
    silence_dbfs: float = -45.0
    voice_margin_db: float = Field(6.0, ge=0.0)
    min_voiced_seconds: float = Field(0.5, ge=0.0)
    # After a kept clip, wait this long past the success flash and then play the clip
    # straight back from `replay_path` so the kid hears the joke (FR-27). The pause
    # is there so the flash and the audio don't land on top of each other. 0 disables.
    auto_replay_seconds: float = Field(0.5, ge=0.0)

    # --- tones (tones.py) ----------------------------------------------------
    # The "go ahead" beep right before capture starts and the falling double-blip on a
    # discard. One switch for both; volume is linear amplitude, 0..1.
    tones: bool = True
    tone_volume: float = Field(0.5, ge=0.0, le=1.0)

    # --- playback leveling (leveling.py, FR-28) --------------------------------
    # Clips the box plays are gained once, when written to the play cache or the
    # replay copy, so a whispered joke and a shouted one land at the same level and
    # both sit next to the tones (a 0.5 sine is about -9 dBFS RMS). The upload and the
    # server archive are never touched. max_gain caps the boost so near-silence is not
    # raised into hiss; the clip's own peak is always limited regardless.
    play_leveling: bool = True
    play_target_dbfs: float = -16.0
    play_max_gain_db: float = Field(20.0, ge=0.0)

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

    @property
    def replay_path(self) -> Path:
        """BTN3's copy of the last kept clip. A sibling of the queue's `rejected/`, so
        the queue scan and `play_latest` (both non-recursive) never see it."""
        return self.queue_dir / "replay" / "last.wav"

    def heartbeat_url(self) -> str:
        return f"{self.api_url.rstrip('/')}/api/devices/{self.device_id}/heartbeat"


@lru_cache
def get_settings() -> ClientSettings:
    return ClientSettings()
