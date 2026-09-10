# M3 Verify — Telegram bot against a real token

One-shot checklist to exercise `m3-telegram-bot` against the real Telegram API on
Windows 11 + Docker Desktop. Server code and unit tests are done; this proves the
four ROADMAP "Done when" clauses that tests can't (real network, real shutdown timing).
Run from `server/` in PowerShell unless noted.

---

## 1. Setup

```powershell
cd server
cp .env.example .env
```

Edit `.env`:
- [ ] `API_KEY` — `openssl rand -hex 24` (or any long random string)
- [ ] `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather): `/newbot`, follow
      the prompts, copy the token it gives you
- [ ] Leave `TELEGRAM_CHAT_MOM` / `TELEGRAM_CHAT_DAD` blank for now

Get chat ids — have each parent phone send **any message** to the new bot first, then:

```powershell
curl.exe "https://api.telegram.org/bot<TOKEN>/getUpdates"
```

- [ ] Find each sender's `message.chat.id` in the JSON, set `TELEGRAM_CHAT_MOM` /
      `TELEGRAM_CHAT_DAD` in `.env` (leave one blank for a single-parent setup)

Start the stack:

```powershell
docker compose up -d --build
curl.exe http://localhost:8080/healthz
```

- [ ] `healthz` returns `{"ok":true}`

Check the logs:

```powershell
docker compose logs banter-server
```

- [ ] One of:
  - `INFO | banter | schema up to date` (fresh `.env`, migrations already applied to
    an existing `./data`), or
  - `INFO | banter | migrations applied: [...]` (first run against an empty `./data`)
- [ ] `INFO | banter.telegram.bot | bot | event=started | parents=mom,dad` (or
      whichever roles have a chat id configured — order is `mom` then `dad`)

If you see `INFO | banter.telegram.bot | bot | event=disabled | reason=no_token`
instead of `event=started`: `TELEGRAM_BOT_TOKEN` is blank in the running container's
`.env` — the bot task returns immediately and the rest of the server runs normally
(this is by design, not a bug, for a token-less dev deploy). Fix `.env` and
`docker compose up -d --build` again.

---

## 2. ROADMAP "Done when" checks

### 2a. Kid records → both parents get audio within ~10s

Seed a kid recording via the upload API (no hardware/client needed). Make a short
valid WAV first — `probe_wav` requires real WAV framing, so an empty/garbage file
will 400:

```powershell
uv run python -c "import wave; w = wave.open('seed.wav','wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b'\x00\x00' * 16000); w.close()"
```

That's 1s of silence, 16 kHz mono 16-bit (the canonical format `is_canonical` checks
for — a non-canonical rate/channel count still stores, just logs `offspec_wav`).

```powershell
curl.exe -X POST "http://localhost:8080/api/recordings" `
  -H "X-API-Key: <API_KEY from .env>" `
  -F "id=verify-seed-001" `
  -F "audio=@seed.wav;type=audio/wav" `
  -F "source=kid" `
  -F "device_id=verify-box" `
  -F "recorded_at=2026-08-14T12:00:00Z" `
  -F "duration_ms=1000"
```

- [ ] Response is `201 {"id":"verify-seed-001","status":"created"}`
- [ ] Within ~10s, both parent phones receive a voice message with caption
      `Aug 14, 12:00 UTC · 1s`
- [ ] Logs show no `notify | event=send_failed` or `notify | event=send_error` lines
      for `rec_id=verify-seed-001`

(Timing method: see §3.)

### 2b. Parent voice note → DB row + BTN2 plays it (tier 1)

Send a voice note (not a file, not text) from a parent's phone to the bot.

- [ ] Logs show `INFO | banter.telegram.handlers | handle_voice | created | source=mom
      id=tg-... duration_ms=...` (or `source=dad`)
- [ ] The phone gets the reply `Got it! Thanks for the joke.`

Server-side proof it's tier-1-selectable (no button press required — this is what
`select_next` reads):

```powershell
curl.exe "http://localhost:8080/api/recordings/next?device_id=verify-box" `
  -H "X-API-Key: <API_KEY>"
```

- [ ] Response `200` with `"id"` equal to the `tg-...` id from the logs, `"source":
      "mom"` (or `"dad"`) — tier 1 (parent, unplayed) beats the kid seed row from §2a
      as long as no other parent recordings already exist

### 2c. `/joke [n]` returns kid recordings

Send `/joke` (no args) from a parent chat:

- [ ] Bot replies with **1** voice message (default is 1, not 3 — this changed from
      an earlier draft) — a kid-source recording, e.g. `verify-seed-001`

Send `/joke 3`:

- [ ] Bot replies with 3 voice messages (or fewer if fewer than 3 kid recordings exist)

Send `/joke 99`:

- [ ] Bot replies with at most 5 (clamped to `1..5` by `_parse_joke_count`)

Send `/joke abc`:

- [ ] Bot replies with 1 (garbage argument falls back to the default, not an error)

### 2d. Unknown chat → no reply, no DB write

From a **third** Telegram account (not mom's or dad's chat id), send the bot a
voice note and `/stats`.

- [ ] No reply arrives on that account
- [ ] Logs show `handle_update | ignored_unknown_chat | chat_id=<the third account's id>`
      at the default `LOG_LEVEL=INFO`, once per message you sent
- [ ] DB row count unchanged (exact method: §4)

This is a positive assertion on purpose. Until recently the rejection was `log.debug`
and this check read "logs show nothing", which also passes when the bot is dead, the
token is wrong, or `getUpdates` never ran — the three most likely reasons a stranger's
message would produce no reply for the wrong reason.

### 2e. `/stats` sanity check

Send `/stats` from a parent chat:

- [ ] Reply lists one line per source (`kid: N (Mm SSs), last ...`, etc.) plus a
      `total: N (Mm SSs)` line, and the counts match what you seeded in 2a/2b

---

## 3. Measuring the ~10s notify budget

There's no `sent_at` column on `recordings` — the only vantage point is wall-clock
observation around the upload call.

```powershell
$t0 = Get-Date
curl.exe -X POST "http://localhost:8080/api/recordings" `
  -H "X-API-Key: <API_KEY>" `
  -F "id=verify-timing-001" `
  -F "audio=@seed.wav;type=audio/wav" `
  -F "source=kid" `
  -F "device_id=verify-box" `
  -F "recorded_at=2026-08-14T12:05:00Z" `
  -F "duration_ms=1000"
```

- [ ] Note the wall-clock time the voice message actually lands on each phone;
      subtract `$t0`. Budget is `TELEGRAM_NOTIFY_INTERVAL_S` (3s poll cadence) plus
      transcode + upload — comfortably under 10s on a home connection

---

## 4. Unknown-chat DB proof

`source` is stored on the `recordings` row but the Telegram chat id is not, so there's
no query that says "this row came from the unknown chat." The proof is a before/after
row count instead. Don't assume `sqlite3` CLI exists in the image or on the Windows
host — use the venv's `python` that's already on `PATH` inside the container:

```powershell
docker compose exec banter-server python -c "import sqlite3; print(sqlite3.connect('/data/banter.db').execute('SELECT COUNT(*) FROM recordings').fetchone()[0])"
```

- [ ] Run this, note the count
- [ ] Send the unknown-chat voice note / `/stats` from §2d
- [ ] Run the same command again — count is identical

---

## 5. Clean shutdown

```powershell
Measure-Command { docker compose down }
```

- [ ] `TotalSeconds` is low single digits (the bot task cancels immediately —
      `asyncio.CancelledError` propagates through every `except Exception` in
      `bot.py`, never swallowed)
- [ ] If it's ~10s: the cancellation hung and Docker's SIGTERM→SIGKILL grace window
      (10s — the default, since `docker-compose.yml` sets no `stop_grace_period`) is
      what actually killed it — a real regression, not a pass

```powershell
docker compose logs banter-server | Select-String "bot \| event=stopped"
```

- [ ] `INFO | banter.telegram.bot | bot | event=stopped` is present

---

## 6. Log lines to watch

Verbatim format strings from the source (not paraphrased). All are on `logging.info`
unless noted. Logger names shown as they appear after `configure_logging`'s
`"%(levelname)s | %(name)s | %(message)s"` format.

**`app/main.py`** (`logger=banter`)
- `migrations applied: %s`
- `schema up to date`

**`app/telegram/bot.py`** (`logger=banter.telegram.bot`)
- `bot | event=disabled | reason=no_token`
- `bot | event=started | parents=%s`
- `bot | event=stopped`
- `bot | event=poll_failed | attempt=%d` — `log.exception` (ERROR + traceback)
- `bot | event=handler_failed | update_id=%s` — `log.exception` (ERROR)

**`app/telegram/handlers.py`** (`logger=banter.telegram.handlers`)
- `handle_update | ignored_unknown_chat | chat_id=%s` — the unknown-chat line for §2d;
  visible at `LOG_LEVEL=INFO`
- `handle_voice | duplicate_redelivery | source=%s id=%s`
- `handle_voice | ingest_failed | source=%s id=%s` — `log.exception` (ERROR)
- `handle_voice | probe_failed_fallback | source=%s id=%s` — `log.warning`
- `handle_voice | created | source=%s id=%s duration_ms=%d`
- `send_recording | send_failed | id=%s` — `log.exception` (ERROR)
- `_reply | send_failed` — `log.exception` (ERROR)

**`app/telegram/notify.py`** (`logger=banter.telegram.notify`)
- `notify | event=send_error | role=%s | rec_id=%s | attempt=%d | error=%s` —
  `log.warning`
- `notify | event=send_failed | role=%s | rec_id=%s | attempt=%d` — `log.warning`
- `notify | event=pass_failed` — `log.exception` (ERROR)

**`app/telegram/client.py`** (`logger=banter.telegram.client`)
- `telegram.client | event=transport_error | url=%s` — `log.error`
- `telegram.client | event=bad_json | url=%s | status=%d` — `log.error`
- `telegram.client | event=api_error | url=%s | error_code=%s | description=%s` —
  `log.warning`
- `telegram.client | event=download_transport_error | url=%s` — `log.error`
- `telegram.client | event=download_http_error | url=%s | status=%d` — `log.error`

A clean run of §2 produces **zero** `poll_failed`, `handler_failed`, `send_failed`,
`send_error`, or `pass_failed` lines. Any of those appearing during the checklist is
a real failure to chase, not noise.

---

## 7. If a check fails

| Symptom | Open |
|---|---|
| `event=disabled` / `reason=no_token` when a token is set | `.env` not picked up by the running container — confirm `docker compose config` shows the token, then rebuild |
| No voice message ever arrives (§2a) | `app/telegram/notify.py` — `notify_once`; check `configured_parent_roles()` in `app/config.py` isn't empty |
| Voice arrives but caption/timing is wrong | `app/telegram/notify.py` — `caption_for`, `_format_duration` |
| Parent voice note gets no `Got it!` reply | `app/telegram/handlers.py` — `handle_voice`, or `_reply` swallowing a `TelegramError` (check for `_reply | send_failed`) |
| `/next` doesn't return the parent recording (§2b) | `app/selection.py` — `select_next` tier logic, or `app/store.py` — `selectable_candidates` |
| `/joke` sends wrong count | `app/telegram/handlers.py` — `_parse_joke_count` (default/clamp constants at top of file) |
| `/joke` sends nothing | `app/store.py` — `random_kid_recordings`; confirm kid rows actually exist |
| `/stats` numbers look wrong | `app/store.py` — `source_stats`; `app/telegram/handlers.py` — `handle_stats` |
| Unknown chat gets a reply, or a row appears | `app/telegram/handlers.py` — `handle_update`'s allowlist gate (`ctx.settings.source_for_chat`); `app/config.py` — `source_for_chat` |
| `docker compose down` takes ~10s | `app/main.py` — `lifespan`'s shutdown block; `app/telegram/bot.py` — check every `except` clause still says `except Exception` and never bare/`BaseException` |
| Ingest fails / voice note rejected | `app/telegram/handlers.py` — `handle_voice`'s `TelegramError`/`TranscodeError` catch; confirm ffmpeg is on `PATH` in the image (`server/Dockerfile`) |
