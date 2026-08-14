# Project Progress

## Current Focus
M1 (record loop) is complete and verified on desktop; branch `m1-record-loop` is
unmerged and unpushed. Next up is M2 — playback loop and `select_next()`.

## Open Todos
- [x] M0 — skeleton, config, migrations, healthz, test harness
- [x] M0.5 — hardware seam, backend protocols + simulators
- [x] M1 — `POST /api/recordings` with API-key auth and idempotent upsert
- [x] M1 — client on-disk queue + background uploader with backoff
- [x] M1 — `banter-client.service` systemd unit
- [ ] M1 — hardware pass on the Pi: hold-to-record feel, Codec Zero mic levels
- [ ] Merge `m1-record-loop` into `main`
- [ ] Decide whether the empty-`id` 422 should be normalised to 400
- [ ] M2 — `select_next()` pure function + tests for all 4 tiers, no-repeat, empty pool
- [ ] M2 — `GET /api/recordings/next`, `/{id}/audio`, `POST /{id}/played`
- [ ] M2 — client BTN2 playback, play cache, offline fallback

## Progress Log

### 2026-08-12
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
