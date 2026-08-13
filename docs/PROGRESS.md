# Project Progress

## Current Focus
M1 (record loop) and M2 (playback loop) are both complete and verified on desktop.
`m1-record-loop` is unmerged; M2 work is on `m2-playback-loop`, also unmerged. Next up
is M3 — the Telegram bot.

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
- M2 shipped end-to-end: `select_next()` (pure, all 4 tiers + FR-13 no-repeat +
  tier-3 rank weighting), `GET /api/recordings/next`/`/{id}/audio`,
  `POST /{id}/played`, migration `002_plays_device_time_index.sql`, client
  `player.py` + `cache.py`, and `RecordController.play_next()` wired to BTN2.
- Decided: playback is two round trips (`/next` then `/{id}/audio`), not one — keeps
  the selection response small and lets the client skip the download entirely on a
  cache hit.
- Decided: a `204` from `/next` means "nothing to play," full stop. The client does
  *not* fall back to the local cache on a 204 — cache fallback is reserved for
  "the server is unreachable," a materially different condition than "there's
  genuinely nothing selectable."
- Decided: play receipts (`POST /{id}/played`) are best-effort and fire *after*
  playback finishes, with no retry loop. A dropped receipt must never delay or block
  audio; the server dedupes on `(recording_id, device_id, played_at)` so a future
  retry would be safe to add without changing the contract.
- Decided: tier 3 (kid recordings) uses rank-based weighting toward
  least-recently-played rather than a uniform pick, so one clip doesn't dominate
  after a fresh device with no play history starts working through the pool.
- FR-13's no-repeat exclusion applies per tier — a tier emptied *only* by the
  exclusion falls through to the next one rather than returning the repeat. The
  single-item exception (replay the last-played clip when it's the only candidate
  left) is checked once, after tier 4 is exhausted, not per tier.
- Normalised the empty/missing `id` on upload from FastAPI's default 422 to a 400 —
  closes the open todo from M1; the kidbox shouldn't have to distinguish "missing"
  from "malformed."
- Caught in review: `store.py` initially redefined its own `Candidate` dataclass
  instead of importing `app.selection.Candidate`, which would have silently forked
  the type the pure function is tested against from the type the DB layer actually
  builds. Fixed to import.
- Caught in review: the controller's play worker thread could crash inside
  `fetch_next()` (network/serialization edge cases) without the exception handler
  settling the state machine back to `IDLE`, stranding the box in `PLAYING` with
  both buttons dead until restart. Fixed by wrapping the worker body and always
  resetting state on any exception, not just the expected failure paths.
- Added a store-driven eviction test after noticing every `PlayCache` eviction test
  called `_evict()` directly — removing the `_evict` call from `store()` would have
  left the suite green. Verified by mutation: the new test is the only one that fails.
- Suites: 74 server / 92 client, ruff clean in both.
- Post-M2 fix (same day): BTN2 hung ~30s after the server container was stopped.
  Cause was not connection refusal — Docker keeps its port proxy bound after the
  container stops, so TCP connects and nothing answers, stalling until the read
  timeout. Split the timeouts (`BANTER_NEXT_TIMEOUT=3.0` for the small JSON calls,
  `BANTER_HTTP_TIMEOUT=30.0` still for the audio download) and added an offline memo
  (`BANTER_OFFLINE_MEMO_SECONDS=30.0`) so only the first tap of an outage pays a
  timeout. Rejected the obvious `/healthz` pre-check: it would hang exactly as long
  on the same timeout and costs an extra round trip when healthy.
- Measured, not assumed: first offline tap 4.05s (localhost resolves to both ::1 and
  127.0.0.1, ~2s per refusal on Windows — the timeout wasn't even the binding
  constraint locally), subsequent taps 0.00s.
- Found while measuring: the offline fallback claimed to rotate but ping-ponged
  between the two oldest clips, because serving a clip never advanced its LRU
  position. Now touches on serve; verified it cycles all four cached clips.
- Post-M2 hardening: the play cache now validates that a downloaded body parses as a
  WAV before committing it, sweeps unplayable entries at startup, and skips them in
  `oldest()`. Prompted by stub files polluting the dev cache during the timeout work,
  but the real-world case is a captive portal answering 200 with a sign-in page: that
  would have been cached as `{id}.wav` and played back as silence. A rejected download
  also marks the network unreachable, so the junk isn't re-fetched on every press.
- Suites after the fix: 74 server / 107 client.
- Next: M3 — the Telegram bot.

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
