"""Thin, hand-rolled httpx wrapper over the Telegram Bot API.

Transport only. No allowlist checks, no DB access, no ffmpeg, no knowledge of
recordings — those live in later steps (bot.py / handlers.py). This module's only job
is to talk HTTP to `api.telegram.org` and hand back parsed JSON or raw bytes.

Deliberately not `python-telegram-bot` or `aiogram`: CLAUDE.md wants a small, legible,
self-hosted app, and the repo's testing convention (injected fakes, no real network) is
a much better fit for a client built on `httpx.MockTransport` than a full framework.
"""

import logging
import re

import httpx

log = logging.getLogger("banter.telegram.client")

#: Overridable so tests can point the client at a fake host via a subclass or by
#: passing these through the constructor's transport seam.
API_BASE = "https://api.telegram.org/bot{token}/{method}"
FILE_BASE = "https://api.telegram.org/file/bot{token}/{file_path}"

#: Matches a bot token embedded in a URL or error string (`bot<token>/`), so it can be
#: redacted before the string ever reaches a log line or an exception message.
_TOKEN_IN_URL_RE = re.compile(r"(?<=bot)[^/]+(?=/)")


def _redact(text: str) -> str:
    """Strip any embedded bot token out of a URL-shaped string before it's logged."""
    return _TOKEN_IN_URL_RE.sub("***", text)


class TelegramError(RuntimeError):
    """The Bot API returned `ok=false`, or an HTTP-level failure not retryable here."""


class TelegramClient:
    """One long-lived `httpx.AsyncClient` per instance, closed via `aclose()`."""

    def __init__(
        self,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 40.0,
    ) -> None:
        self._token = token
        # 40s default: comfortably above the 30s default long-poll timeout used by
        # get_updates(), so a normal poll never trips the client's own read timeout.
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout)

    def _method_url(self, method: str) -> str:
        return API_BASE.format(token=self._token, method=method)

    def _file_url(self, file_path: str) -> str:
        return FILE_BASE.format(token=self._token, file_path=file_path)

    async def _request(
        self, method: str, url: str, *, request_timeout: float | None = None, **kwargs
    ) -> dict:
        """POST/GET `url`, parse the Bot API envelope, and raise on any failure.

        Telegram returns its `{ok, error_code, description}` envelope even on a 4xx
        response, so the body is parsed before deciding whether to raise — the
        `description` is a far better error message than a generic "4xx" would be.
        """
        # Only pass `timeout=` through when the caller wants a per-request override:
        # httpx treats an explicit `timeout=None` as "no timeout at all", not "use
        # the client default", so omitting the kwarg is what preserves the default.
        if request_timeout is not None:
            kwargs["timeout"] = request_timeout
        try:
            resp = await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            safe_url = _redact(url)
            log.error("telegram.client | event=transport_error | url=%s", safe_url)
            raise TelegramError(f"transport error calling {safe_url}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            safe_url = _redact(url)
            log.error(
                "telegram.client | event=bad_json | url=%s | status=%d",
                safe_url,
                resp.status_code,
            )
            raise TelegramError(f"non-JSON response ({resp.status_code}) from {safe_url}") from exc

        if not payload.get("ok"):
            error_code = payload.get("error_code")
            description = payload.get("description")
            safe_url = _redact(url)
            log.warning(
                "telegram.client | event=api_error | url=%s | error_code=%s | description=%s",
                safe_url,
                error_code,
                description,
            )
            raise TelegramError(f"telegram api error {error_code}: {description}")

        return payload["result"]

    # -- updates / messages --------------------------------------------------

    async def get_updates(self, offset: int | None = None, *, timeout: int = 30) -> list[dict]:
        """Long-poll for new updates.

        The request-level timeout is `timeout + 10` seconds — strictly greater than
        the long-poll `timeout` sent to Telegram — so the client's own read timeout
        can never fire before Telegram's poll timeout does. Without that margin, a
        caller polling with `timeout=30` against the class default of 40s would be
        racing its own transport, and any jitter could self-deadlock the poll.
        """
        params: dict[str, int] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        result = await self._request(
            "GET",
            self._method_url("getUpdates"),
            params=params,
            request_timeout=timeout + 10,
        )
        return result

    async def send_message(self, chat_id: str, text: str) -> dict:
        """Send a plain text message. Returns the Bot API `Message` result dict."""
        return await self._request(
            "POST",
            self._method_url("sendMessage"),
            data={"chat_id": chat_id, "text": text},
        )

    async def send_voice(
        self,
        chat_id: str,
        *,
        voice: bytes | str,
        caption: str | None = None,
        filename: str = "joke.ogg",
    ) -> dict:
        """Send a voice note, either uploading raw bytes or reusing a `file_id`.

        `voice` as `bytes` triggers a multipart upload (field `voice`, named
        `filename`). `voice` as a `str` is treated as a previously-returned Telegram
        `file_id` and sent as a plain form field — no re-upload, no bytes over the
        wire. Callers cache `file_id`s from prior sends specifically to take this
        path on repeat sends of the same joke.

        Returns the full result dict; callers read `result["voice"]["file_id"]`.
        """
        data = {"chat_id": chat_id}
        if caption is not None:
            data["caption"] = caption

        if isinstance(voice, bytes):
            files = {"voice": (filename, voice, "audio/ogg")}
            return await self._request(
                "POST", self._method_url("sendVoice"), data=data, files=files
            )

        data["voice"] = voice
        return await self._request("POST", self._method_url("sendVoice"), data=data)

    # -- files ----------------------------------------------------------------

    async def get_file(self, file_id: str) -> dict:
        """Resolve a `file_id` to file metadata. Caller reads `result["file_path"]`."""
        return await self._request("GET", self._method_url("getFile"), params={"file_id": file_id})

    async def download_file(self, file_path: str) -> bytes:
        """Fetch raw bytes from the separate `file/bot{token}/` host path.

        This host does not use the `{ok, result}` envelope — a successful response
        body *is* the file, so a non-2xx status is the only failure signal available.
        """
        url = self._file_url(file_path)
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            safe_url = _redact(url)
            log.error(
                "telegram.client | event=download_transport_error | url=%s",
                safe_url,
            )
            raise TelegramError(f"transport error downloading {safe_url}") from exc

        if resp.status_code < 200 or resp.status_code >= 300:
            safe_url = _redact(url)
            log.error(
                "telegram.client | event=download_http_error | url=%s | status=%d",
                safe_url,
                resp.status_code,
            )
            raise TelegramError(f"download failed ({resp.status_code}) for {safe_url}")

        return resp.content

    async def aclose(self) -> None:
        """Close the underlying `httpx.AsyncClient`. Safe to call more than once."""
        if self._client.is_closed:
            return
        await self._client.aclose()
