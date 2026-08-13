"""Configuration. Parsed once, imported everywhere. No magic constants in handlers."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- storage -----------------------------------------------------------
    data_dir: Path = Path("/data")

    # --- auth --------------------------------------------------------------
    # Shared secret the kidbox sends as X-API-Key. Empty disables auth (dev only).
    api_key: str = ""

    # --- selection algorithm (PRD 3.3) ------------------------------------
    parent_cooldown_hours: int = 12
    avoid_immediate_repeat: bool = True

    # --- telegram (M3) -----------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_mom: str = ""
    telegram_chat_dad: str = ""

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
