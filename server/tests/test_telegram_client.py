"""M3 step 3 — TelegramClient transport tests. No real network: httpx.MockTransport
only, per repo convention of injected fakes (see client/banter_client/uploader.py).
"""

import httpx
import pytest

from app.telegram.client import TelegramClient, TelegramError

TOKEN = "123456:AAFAKE-TOKEN-SHOULD-NEVER-LEAK-abcdefg"


def _make_client(handler) -> TelegramClient:
    transport = httpx.MockTransport(handler)
    return TelegramClient(TOKEN, transport=transport)


def _ok(result) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


# --- get_updates ---------------------------------------------------------------


async def test_get_updates_offset_omitted_when_none():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return _ok([])

    client = _make_client(handler)
    result = await client.get_updates()
    assert result == []
    assert "offset" not in captured["params"]
    await client.aclose()


async def test_get_updates_offset_included_when_set():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return _ok([{"update_id": 1}])

    client = _make_client(handler)
    result = await client.get_updates(offset=42)
    assert captured["params"]["offset"] == "42"
    assert result == [{"update_id": 1}]
    await client.aclose()


async def test_get_updates_timeout_in_query():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return _ok([])

    client = _make_client(handler)
    await client.get_updates(timeout=25)
    assert captured["params"]["timeout"] == "25"
    await client.aclose()


async def test_get_updates_hits_getupdates_path():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return _ok([])

    client = _make_client(handler)
    await client.get_updates()
    assert captured["path"].endswith("/getUpdates")
    await client.aclose()


# --- send_message ----------------------------------------------------------------


async def test_send_message_method_and_params():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["form"] = dict(httpx.QueryParams(request.content.decode()))
        return _ok({"message_id": 7})

    client = _make_client(handler)
    result = await client.send_message("999", "hello there")
    assert captured["path"].endswith("/sendMessage")
    assert captured["form"]["chat_id"] == "999"
    assert captured["form"]["text"] == "hello there"
    assert result == {"message_id": 7}
    await client.aclose()


# --- send_voice ------------------------------------------------------------------


async def test_send_voice_bytes_multipart_with_caption():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return _ok({"voice": {"file_id": "abc123"}})

    client = _make_client(handler)
    result = await client.send_voice("42", voice=b"OGGDATA", caption="a joke", filename="joke1.ogg")
    assert captured["content_type"].startswith("multipart/form-data")
    assert b'name="voice"' in captured["body"]
    assert b"joke1.ogg" in captured["body"]
    assert b"OGGDATA" in captured["body"]
    assert b'name="caption"' in captured["body"]
    assert b"a joke" in captured["body"]
    assert result["voice"]["file_id"] == "abc123"
    await client.aclose()


async def test_send_voice_bytes_no_caption_omits_field():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return _ok({"voice": {"file_id": "abc123"}})

    client = _make_client(handler)
    await client.send_voice("42", voice=b"OGGDATA")
    assert b'name="caption"' not in captured["body"]
    await client.aclose()


async def test_send_voice_file_id_no_multipart_no_reupload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return _ok({"voice": {"file_id": "reused-id"}})

    client = _make_client(handler)
    result = await client.send_voice("42", voice="existing-file-id-999")
    # Plain form field, not multipart: no file part, no raw bytes anywhere.
    assert not captured["content_type"].startswith("multipart/form-data")
    assert b"voice=existing-file-id-999" in captured["body"]
    assert b'filename="joke.ogg"' not in captured["body"]
    assert result["voice"]["file_id"] == "reused-id"
    await client.aclose()


# --- get_file / download_file -----------------------------------------------------


async def test_get_file_returns_result():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"file_id": "f1", "file_path": "voice/file_0.ogg"})

    client = _make_client(handler)
    result = await client.get_file("f1")
    assert result["file_path"] == "voice/file_0.ogg"
    await client.aclose()


async def test_download_file_uses_file_host_and_path():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, content=b"raw-ogg-bytes")

    client = _make_client(handler)
    data = await client.download_file("voice/file_0.ogg")
    assert data == b"raw-ogg-bytes"
    assert "/file/bot" in captured["url"]
    assert captured["url"].endswith("voice/file_0.ogg")
    await client.aclose()


async def test_download_file_non_2xx_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    client = _make_client(handler)
    with pytest.raises(TelegramError):
        await client.download_file("voice/missing.ogg")
    await client.aclose()


# --- error envelopes ---------------------------------------------------------------


async def test_ok_false_raises_with_code_and_description():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "error_code": 403, "description": "bot was blocked by the user"},
        )

    client = _make_client(handler)
    with pytest.raises(TelegramError) as exc_info:
        await client.send_message("1", "hi")
    message = str(exc_info.value)
    assert "403" in message
    assert "bot was blocked by the user" in message
    await client.aclose()


async def test_http_4xx_with_error_envelope_uses_description():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"ok": False, "error_code": 400, "description": "chat not found"},
        )

    client = _make_client(handler)
    with pytest.raises(TelegramError) as exc_info:
        await client.send_message("bogus", "hi")
    assert "chat not found" in str(exc_info.value)
    await client.aclose()


# --- token redaction ---------------------------------------------------------------


async def test_token_redacted_from_exception_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
        )

    client = _make_client(handler)
    with pytest.raises(TelegramError) as exc_info:
        await client.send_message("1", "hi")
    assert TOKEN not in str(exc_info.value)
    await client.aclose()


async def test_token_redacted_from_logs(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
        )

    client = _make_client(handler)
    with caplog.at_level("WARNING", logger="banter.telegram.client"), pytest.raises(TelegramError):
        await client.send_message("1", "hi")
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
    await client.aclose()


# --- aclose --------------------------------------------------------------------


async def test_aclose_is_idempotent():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok([])

    client = _make_client(handler)
    await client.aclose()
    await client.aclose()  # must not raise
