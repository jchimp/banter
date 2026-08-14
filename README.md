# Banter

A joke exchange between a kid and their parents. The kid records jokes on a physical
box (**kidbox** — Pi Zero 2 W, two arcade buttons, a glowing ring). Parents receive
them on Telegram and send jokes back as voice notes. The box plays them.

Docs: [`PRD.md`](PRD.md) (what) · [`ROADMAP.md`](ROADMAP.md) (order) ·
[`CLAUDE.md`](CLAUDE.md) (how) · [`PARTS.md`](PARTS.md) · [`wiring.svg`](wiring.svg)

**Status: M1 complete** — the record loop works end-to-end: kid holds BTN1 (or the
keyboard sim), the WAV lands in the client's on-disk queue, and the uploader delivers
it to `POST /api/recordings` with retry. No playback, no Telegram, no web UI yet.

```
   ┌──────── kidbox (Pi Zero 2 W) ────────┐        ┌─── home server (Docker) ───┐
   │ banter-client.service (systemd)      │  HTTP  │ banter-server (FastAPI)    │
   │  BTN1 hold → record → POST           │───────▶│  SQLite + audio store      │
   │  BTN2 tap  → GET next → play         │◀───────│  HTMX UI · Telegram bot    │
   └──────────────────────────────────────┘        └───────────┬───────────────┘
                                                        Mom / Dad on Telegram
```

## Layout
```
banter/
├── server/   FastAPI + SQLite + HTMX, runs in Docker on the home server
└── client/   kidbox device agent, runs as a systemd service on the Pi
```

## Developing without the Pi

Nothing here requires hardware to build or test. Three profiles, chosen by three env
vars — the code above the backend seam (`banter_client/backends/base.py`) is identical
in all of them.

| Profile | audio | buttons | ring | Use |
|---|---|---|---|---|
| Pi | `alsa` | `gpio` | `neopixel` | The real box |
| Laptop | `sounddevice` | `keyboard` | `terminal` | Real mic/speakers, keys for buttons |
| CI | `synthetic` | `keyboard` | `null` | No audio device at all |

Run the local record/playback loop on your own machine:

```bash
cd client
cp .env.dev.example .env
uv sync --extra dev-audio        # PortAudio; Linux/macOS/Windows
uv run banter-demo
#   [r] record toggle   [p] play   [q] quit
```

`r` starts recording from your laptop mic, `r` again stops and writes a 16 kHz mono
WAV to the queue dir, `p` plays the newest clip back. The terminal ring prints the
state the NeoPixel would be showing. Hold-to-record is GPIO-only — line-based stdin
can't express a hold, so the sim is always toggle mode.

With `BANTER_AUDIO_BACKEND=synthetic` the loop runs with no sound card whatsoever and
still writes genuine playable WAVs — that's how the loop is tested in CI.

**What still needs hardware:** hold-to-record timing feel, Codec Zero mic quality and
levels, real NeoPixel animations, and speaker volume. Everything else — the queue and
retry logic, the selection algorithm, the Telegram bot, the web UI — is fully
developable now.

## Try it: the M1 record loop
Run the server and a dev-profile client against each other on one machine — no Pi
needed.

```bash
# terminal 1: server
cd server
cp .env.example .env          # set API_KEY, e.g. openssl rand -hex 24
uv sync
DATA_DIR=./data uv run uvicorn app.main:app --reload --port 8080

# terminal 2: client, dev profile
cd client
cp .env.dev.example .env      # set BANTER_API_KEY to match the server's
uv sync --extra dev-audio
uv run banter-client
#   [r]+Enter to start recording, [r]+Enter again to stop and enqueue; ctrl-c to quit
```

Speak a joke, then press `r` again. The client writes a WAV to `BANTER_QUEUE_DIR`
(`./devdata/queue` by default), the uploader thread picks it up immediately, and
within a request or two you should see:
- a new row in the server's `recordings` table, `source=kid origin=kidbox`
- a playable WAV under `server/data/audio/kid/YYYYMM/{id}.wav`
- the sidecar and WAV gone from `BANTER_QUEUE_DIR` (uploader deleted them on the 2xx)

Kill the server mid-recording, record a couple more jokes, then restart the server —
the client keeps retrying with backoff and they all arrive without touching the
client again.

**Queue durability.** A recording lives in `BANTER_QUEUE_DIR` (as `{id}.wav` +
`{id}.json`) from the moment it's captured until the server returns a 2xx; nothing in
the client deletes it before then. The queue is plain files, so it survives a client
restart or a Pi reboot — `recover()` re-enqueues anything left over on startup. An
upload that fails with a permanent error (bad API key, malformed request) is moved
into `BANTER_QUEUE_DIR/rejected/` instead of retried forever, and is never deleted —
only `queue.done()`, called after a 2xx, removes a file.

## Server — local dev
```bash
cd server
cp .env.example .env          # set API_KEY:  openssl rand -hex 24
uv sync
DATA_DIR=./data uv run uvicorn app.main:app --reload --port 8080
curl localhost:8080/healthz   # {"ok":true}
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
```

## Server — Docker
```bash
cd server
cp .env.example .env
docker compose up -d --build
curl localhost:8080/healthz
```
Audio and the SQLite file live in `./data` on the host (mounted at `/data`).

## Client — on the Pi
```bash
sudo apt install -y alsa-utils
curl -LsSf https://astral.sh/uv/install.sh | sh

cd ~/banter/client
cp .env.example .env          # set BANTER_API_URL + BANTER_API_KEY
arecord -l && aplay -l        # fill in BANTER_ALSA_CAPTURE / _PLAYBACK
uv sync --extra hardware      # gpiozero + neopixel; omit --extra off-hardware
uv run banter-client          # hold BTN1 to record; runs until SIGINT/SIGTERM

sudo cp banter-client.service /etc/systemd/system/
sudo systemctl enable --now banter-client
journalctl -u banter-client -f
```

### Pi one-time system config
`/boot/firmware/config.txt` — the NeoPixel ring runs on SPI (not PWM, which fights
the onboard audio):
```
dtparam=spi=on
core_freq_min=500
```
Codec Zero setup follows the official Raspberry Pi HAT instructions.

## API
Implemented so far (full contract in `PRD.md` §5). All `/api/*` require header
`X-API-Key`. JSON errors: `{"detail": "..."}`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/recordings` | multipart: `id`, `audio`, `source`, `device_id`, `recorded_at`, `duration_ms` → `201 {id, status}` created, `200 {id, status}` duplicate |
| GET | `/healthz` | `{"ok": true}` — no auth |

`id` is client-generated; a repeat upload of the same `id` is a no-op 200, not a new
row, which is what makes the client's retry-on-failure safe.

## Migrations
Plain SQL in `server/migrations/NNN_name.sql`, applied on startup and tracked in
`schema_version`. **Write them idempotently** (`IF NOT EXISTS`) — `executescript()`
commits implicitly, so a file can't be wrapped in one transaction and may replay
after a mid-file crash. Details in `app/db.py`.

## Next
M2 — the playback loop (`select_next()`, BTN2, local play cache). See `ROADMAP.md`.
