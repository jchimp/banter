# Banter — ROADMAP

Milestones are ordered so something works end-to-end early, then gets better.
Each has a **Done when** that is objectively checkable. Don't start a milestone
until the prior one's criteria pass.

---

## M0 — Skeleton & harness
Repo scaffolding, no features.

- `uv init` both packages: `server/` and `client/`
- Server: FastAPI app, `/healthz`, settings from env (pydantic-settings)
- SQLite schema + migration runner (plain SQL files, `schema_version` table)
- Dockerfile (uv-based, multi-stage) + `docker-compose.yml` + `.env.example`
- pytest wired up, one trivial passing test
- `ruff` + `ruff format` config

**Done when:** `docker compose up -d` → `curl /healthz` returns `{"ok":true}`;
`uv run pytest` passes; DB file created with all tables.

---

## M0.5 — Hardware seam  ✅ done
Backend protocols + simulators so M1/M2 can be built and tested before parts arrive.

- `backends/base.py` protocols: audio, buttons, ring
- Implementations: `alsa | sounddevice | synthetic`, `gpio | keyboard`,
  `neopixel | terminal | null`
- `factory.py` selects from config; hardware imports are lazy
- `banter-demo` — local record/play loop, no server, no Pi

**Done when:** `uv run banter-demo` with the synthetic backend records a valid WAV and
plays it back with no sound card present; tests cover the full loop. *(verified)*

---

## M1 — Record loop (the spine)
Kid can record; it lands on the server. No playback, no Telegram, no UI.

- `POST /api/recordings` with API-key auth, multipart handling, file layout
  `/data/audio/{source}/{YYYYMM}/{id}.wav`, duration probe
- Client: `gpiozero` BTN1 on GPIO17, hold-to-record via `arecord`, min/max duration
- Client: on-disk queue + background uploader with exponential backoff; re-enqueue
  leftovers on startup
- Client: `banter-client.service` systemd unit + `.env`

**Done when:** hold BTN1, speak, release → row in `recordings` and a playable WAV on
the server. Stop the server, record 3 jokes, restart it → all 3 arrive without
intervention. Reboot the Pi mid-queue → nothing lost.

---

## M2 — Playback loop
Kid can hear jokes. Selection logic lands here.

- `select_next()` as a **pure function** + `GET /api/recordings/next`
- `GET /api/recordings/{id}/audio`, `POST /api/recordings/{id}/played`
- Client: BTN2 on GPIO22 → fetch → `aplay`; tap-while-playing stops
- Client: mutual exclusion between record and play
- Client: local play cache (`PLAY_CACHE_SIZE`) + offline fallback
- **Tests:** `select_next()` against fixtures covering all 4 tiers, the no-repeat rule,
  and the empty-pool case

**Done when:** BTN2 plays a joke within 1.5 s; with parent recordings seeded, tier 1
is preferred until exhausted, then kid recordings appear; unplugging the network
still plays something from cache.

---

## M3 — Telegram bot
Parents join the loop. This is the milestone that makes it a *exchange*.

- Bot as a background task in the server's lifespan, long-polling (no webhook)
- Chat allowlist (mom/dad chat IDs); everything else ignored
- Outbound: notify on new `origin=kidbox` recording, audio + caption; set `notified=1`
- Inbound: voice note → download → ffmpeg transcode OGG/Opus → 16k mono WAV →
  store with `source=mom|dad`, `origin=telegram`; confirm reply
- `/joke [n]` → n random kid recordings as voice messages
- `/stats`
- ffmpeg added to the server image

**Done when:** kid records → both parents get audio in Telegram within ~10 s; a parent
sends a voice note → it's in the DB and BTN2 plays it (tier 1); `/joke` returns kid
recordings; a message from an unknown chat produces no reply and no DB write.

---

## M4 — Web UI
- Jinja2 base + HTMX partials
- Recording list: source badge, timestamp, duration, play count, `<audio>` player
- Source filter via HTMX swap
- Soft-delete + undo
- Device panel (last seen, queue depth) fed by `POST /api/devices/{id}/heartbeat`

**Done when:** the list renders with correct source badges, filtering swaps without a
full reload, soft-delete hides a row and undo restores it, and the deleted file is
still on disk.

---

## M5 — Ring & physical polish
Everything that makes it feel like a real object.

- `adafruit-circuitpython-neopixel-spi` on GPIO10, brightness cap
- State machine → animations per the PRD table
- Prompt sounds: a "go ahead" beep before recording, a confirmation chirp after
- Idle indicator when unplayed parent jokes exist (open question 3)
- Enclosure: cut 2× 30 mm button holes, 45 mm ring window + diffuser, speaker grille
- `core_freq_min=500` + `dtparam=spi=on` documented in setup

**Done when:** each state in the PRD table is visually distinct at a glance from
across a dim room, and audio + LEDs run simultaneously with no stutter or flicker.

---

## M6 — Hardening
- Client heartbeat + server-side stale-device warning
- Log rotation on the Pi; `journalctl` sanity
- Backup script for `/data` (audio + SQLite `.backup`)
- Full-loop smoke test script
- README with flash-to-running setup, and a one-page "how to use it" for the kid

**Done when:** a fresh SD card reaches a working device by following the README alone,
and the backup restores onto a clean container.

---

## Post-v1 backlog
Ratings/favorites · joke-of-the-day scheduled push · transcripts via local Whisper
(ties into your Holler work) · second device for a sibling · LED display · night light
· Raspotify on the same box.

## Sequencing note for the agent
M0.5 means **no milestone is blocked on parts arriving**. Build M1–M4 against the
simulated backends on a dev box; when the Codec Zero and buttons land, flip the three
`BANTER_*_BACKEND` vars and re-verify on hardware.

Only these genuinely need the real box: hold-to-record feel, mic levels and quality,
NeoPixel animations (M5), and speaker volume. Treat the hardware pass as a
verification step at the end of M1/M2, not a prerequisite for starting them.
