"""Configuration. Parsed once, imported everywhere. No magic constants in handlers."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- storage -----------------------------------------------------------
    data_dir: Path = Path("/data")
    # Reject uploads larger than this. A 60s 16kHz mono 16-bit WAV is ~1.9MB;
    # 10MiB is generous headroom without being unbounded.
    max_upload_bytes: int = 10_485_760

    # --- auth --------------------------------------------------------------
    # Shared secret the kidbox sends as X-API-Key. Empty disables auth (dev only).
    api_key: str = ""

    # --- selection algorithm (PRD 3.3) ------------------------------------
    parent_cooldown_hours: int = 12
    avoid_immediate_repeat: bool = True

    # --- telegram (M3) -----------------------------------------------------
    # Empty token disables the bot entirely (the lifespan skips the task).
    telegram_bot_token: str = ""
    telegram_chat_mom: str = ""
    telegram_chat_dad: str = ""
    # How long getUpdates holds the connection open. Telegram caps this at 50s.
    telegram_poll_timeout_s: int = 30
    # Notify cadence. The roadmap budget is ~10s from recording to parent's phone,
    # so 3s of polling latency leaves room for transcode and upload.
    telegram_notify_interval_s: float = 3.0
    # Per-send attempts before leaving the row for the next notify pass.
    telegram_send_retries: int = 3

    # --- misc --------------------------------------------------------------
    log_level: str = "INFO"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "banter.db"

    def chat_id_for(self, source: str) -> str:
        return {"mom": self.telegram_chat_mom, "dad": self.telegram_chat_dad}.get(source, "")

    def configured_parent_roles(self) -> list[str]:
        """Parent roles that actually have a chat id set.

        A one-parent deployment leaves the other chat id blank; the notifier only
        owes a delivery to the roles listed here, so `notified` can still reach 1.
        """
        return [role for role in ("mom", "dad") if self.chat_id_for(role)]

    def source_for_chat(self, chat_id: str | int) -> str | None:
        """Reverse lookup used by the bot allowlist. Unknown chats -> None (ignored)."""
        cid = str(chat_id)
        if cid and cid == self.telegram_chat_mom:
            return "mom"
        if cid and cid == self.telegram_chat_dad:
            return "dad"
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
