# Project Progress

## Current Focus
M2 (playback loop) is committed on `m2-playback-loop` and verified on desktop against a
real Docker server. Both it and `m1-record-loop` are unmerged and unpushed. Next up is
M3 — the Telegram bot.

## Open Todos
- [x] M0 — skeleton, config, migrations, healthz, test harness
- [x] M0.5 — hardware seam, backend protocols + simulators
- [x] M1 — `POST /api/recordings` with API-key auth and idempotent upsert
- [x] M1 — client on-disk queue + background uploader with backoff
- [x] M1 — `banter-client.service` systemd unit
- [ ] M1 — hardware pass on the Pi: hold-to-record feel, Codec Zero mic levels
- [ ] Merge `m1-record-loop` into `main`
- [x] Decide whether the empty-`id` 422 should be normalised to 400 — normalised to 400
- [x] M2 — `select_next()` pure function + tests for all 4 tiers, no-repeat, empty pool
- [x] M2 — `GET /api/recordings/next`, `/{id}/audio`, `POST /{id}/played`
- [x] M2 — client BTN2 playback, play cache, offline fallback
- [ ] Merge `m2-playback-loop` into `main`
- [ ] M3 — Telegram bot: outbound notify, inbound voice ingest, `/joke`, `/stats`

## Progress Log

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
