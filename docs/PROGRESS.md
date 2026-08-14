# Project Progress

## Current Focus
M1 and M2 are merged to `main`. M3 (the Telegram bot) is in progress on
`m3-telegram-bot`: server-side only, no hardware needed. Still outstanding from M1 is
the hardware pass on the Pi.

## Open Todos
- [x] M0 — skeleton, config, migrations, healthz, test harness
- [x] M0.5 — hardware seam, backend protocols + simulators
- [x] M1 — `POST /api/recordings` with API-key auth and idempotent upsert
- [x] M1 — client on-disk queue + background uploader with backoff
- [x] M1 — `banter-client.service` systemd unit
- [ ] M1 — hardware pass on the Pi: hold-to-record feel, Codec Zero mic levels
- [x] Merge `m1-record-loop` into `main`
- [x] Decide whether the empty-`id` 422 should be normalised to 400 — normalised to 400
- [x] M2 — `select_next()` pure function + tests for all 4 tiers, no-repeat, empty pool
- [x] M2 — `GET /api/recordings/next`, `/{id}/audio`, `POST /{id}/played`
- [x] M2 — client BTN2 playback, play cache, offline fallback
- [x] Merge `m2-playback-loop` into `main`
- [ ] M3 — Telegram bot: outbound notify, inbound voice ingest, `/joke`, `/stats`
- [ ] M3 — end-to-end pass against a real bot token (the part tests can't cover)

## Progress Log

### 2026-08-13 (M3)
- M3 server side built on `m3-telegram-bot`: 18 files, +2746/−13. Suite 155 passed /
  2 skipped (the ffmpeg round trips, which only run where ffmpeg is installed), ruff
  clean. Not yet exercised against a real bot token.
- Decided: hand-rolled `httpx` Bot API client over `python-telegram-bot`. No new
  dependency, and `httpx.MockTransport` fits the repo's injected-fake test style better
  than a framework's own harness.
- Decided: `notified` can't represent "mom got it, dad didn't", so migration `003` splits
  it into `notified_mom` / `notified_dad` and `notified` becomes the derived "all
  configured parents delivered" flag. A blocked parent no longer costs the other one a
  joke, and a retry only re-sends to whoever actually failed.
- Decided: kid WAVs are transcoded to OGG/Opus on send (Telegram only draws the voice
  bubble for Opus), and the `file_id` Telegram returns is cached on the row — so the
  second parent's copy and every later `/joke` resend are zero-upload. No derived `.ogg`
  files accumulate on disk for kid recordings; inbound parent OGGs *are* kept
  (`original_path`) per FR-20.
- Decided: the notify trigger is a DB poller in the bot task, not an event from the
  upload route. It costs ~3s of latency against a 10s budget and is self-healing across
  restarts; an in-process event fired while the bot was down would be lost with no
  catch-up path.
- Decided: no persisted `getUpdates` offset. Replay safety comes from deriving the
  recording id from the voice note's `file_unique_id`, so a redelivered update hits the
  existing idempotent upsert — and `handle_voice` returns before replying, so the parent
  doesn't get a second confirmation either.
- Known rough edge: a permanently blocked parent chat still burns its retry budget on
  every notify pass. Harmless at family scale, worth revisiting if it ever gets noisy.
- Next: the end-to-end pass against a real token — the shutdown check (`docker compose
  down` must not stall) and the ~10s notify budget are the two things tests can't prove.

### 2026-08-12 (M2)
- M2 shipped and committed: 8 commits on `m2-playback-loop`, 38 files, +4659/−119 —
  `select_next()`, the three playback routes, migration `002`, client `player.py` +
  `cache.py`, BTN2 wiring. Suites 74 server / 107 client, ruff clean.
- Decided: two round trips (`/next` then `/{id}/audio`), so a cache hit skips the
  download; `204` means "nothing to play" and does *not* trigger cache fallback (that's
  reserved for unreachable); receipts are best-effort after playback with no retry, the
  server deduping on `(recording_id, device_id, played_at)`.
- Decided: tier 3 is rank-weighted toward least-recently-played, not uniform. FR-13's
  exclusion applies per tier and falls through when it empties one; the single-item
  exception is checked once after tier 4. Upload `id` 422→400 closes the M1 todo.
- Review catches: `store.py` forked its own `Candidate` instead of importing the one
  the pure function is tested against; the play worker could crash and strand the box
  in `PLAYING` with both buttons dead until restart; every `PlayCache` eviction test
  called `_evict()` directly, so `store()`'s call to it was uncovered (mutation-verified).
- BTN2 hung ~30s with the server down — not connection refusal, but Docker keeping its
  port proxy bound so TCP connects and nothing answers. Split the timeouts
  (`BANTER_NEXT_TIMEOUT=3.0` vs `HTTP_TIMEOUT=30.0` for audio) and added a 30s offline
  memo; measured 4.05s then 0.00s. Rejected a `/healthz` pre-check: same hang, plus a
  round trip when healthy.
- Hardening: the cache rejects downloads that don't parse as WAV (captive-portal case),
  sweeps bad entries at startup, and skips them in `oldest()`; the offline fallback now
  advances the LRU so it cycles instead of ping-ponging two clips. Next: M3.

### 2026-08-12 (M1)
- M1 shipped end-to-end: upload endpoint, durable client queue, uploader thread,
  systemd unit, docs. 7 commits on `m1-record-loop`, 21 files, +1949/−118.
- Caught a critical uploader bug in review: `except OSError` preceded
  `except requests.RequestException`, which subclasses `IOError` — every network
  failure was being quarantined instead of retried. Found by the offline drill,
  not by any test.
- Verified the ROADMAP "Done when" as three separate processes (record online →
  server down, record 3 → cold-start recover and drain). Also verified through
  Docker: 201 created, 200 duplicate, one row, `.incoming/` empty.
- Suites: 34 server / 55 client, ruff clean in both.
- Decided: server-side `wave` probe is authoritative over the client's
  `duration_ms`; off-spec WAVs warn and store rather than 400.
- Next: hardware pass on the Pi, then M2.
