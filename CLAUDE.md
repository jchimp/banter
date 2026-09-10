# CLAUDE.md — Banter

Agent working notes. Read `PRD.md` for *what*, `ROADMAP.md` for *order*, this for
*how*. If any of the three conflict, the PRD wins on behavior, this file wins on style.

## What this is
A joke exchange. A kid records jokes on a physical box (**kidbox**); parents receive
them on Telegram and reply with voice notes; the box plays them back. Server is
FastAPI + SQLite in Docker on a home server; the box is a Pi Zero 2 W running a
systemd service.

## Repo layout
```
banter/
├── server/                  # FastAPI app (Docker)
│   ├── pyproject.toml       # uv-managed
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── .env.example
│   ├── app/
│   │   ├── main.py          # app factory, lifespan (starts bot task)
│   │   ├── config.py        # pydantic-settings
│   │   ├── db.py            # connection helper, migrations
│   │   ├── models.py        # dataclasses / TypedDicts, not an ORM
│   │   ├── selection.py     # select_next() — PURE, heavily tested
│   │   ├── audio.py         # ffmpeg transcode, duration probe
│   │   ├── api/             # routes: recordings.py, devices.py
│   │   ├── ui/              # HTMX routes + Jinja2 templates
│   │   └── telegram/        # bot.py (polling), handlers.py
│   ├── migrations/          # 001_init.sql, 002_*.sql
│   └── tests/
└── client/                  # Pi Zero 2 W (systemd, NOT Docker)
    ├── pyproject.toml
    ├── banter-client.service
    ├── .env.example
    └── banter_client/
        ├── __main__.py      # wiring + main loop
        ├── demo.py          # local record/play loop, no server or Pi needed
        ├── backends/        # HARDWARE SEAM — see base.py
        │   ├── base.py      # AudioBackend / ButtonBackend / RingBackend protocols
        │   ├── audio.py     # alsa | sounddevice | synthetic
        │   ├── io.py        # gpio|keyboard buttons, neopixel|terminal|null ring
        │   └── factory.py   # the only place that picks real vs simulated
        ├── buttons.py       # gpiozero, mode hold|toggle
        ├── recorder.py      # arecord subprocess
        ├── player.py        # aplay subprocess + local cache
        ├── queue.py         # on-disk queue + retry uploader
        ├── ring.py          # NeoPixel state animations
        └── state.py         # IDLE/RECORDING/UPLOADING/PLAYING/ERROR
```

## Stack (non-negotiable)
Python 3.12 · **uv** for all dependency/venv work · FastAPI · Jinja2 + HTMX (no SPA,
no React) · SQLite in WAL mode · Docker Compose (server only) · pytest · ruff.

Use `uv add` / `uv run` / `uv sync`. Never `pip install` into the system, never
hand-edit a lockfile.

## Conventions
- **Plan before code.** For any non-trivial milestone, state the approach and open
  questions first, then implement. Ask rather than guess at ambiguity.
- **Parameterized SQL only.** No f-string interpolation into queries, ever.
- **No ORM.** `sqlite3` with `row_factory = sqlite3.Row`. Keep SQL readable and local
  to a small data-access layer.
- **Non-destructive by default.** Soft-delete only (`deleted=1`). Application code
  never unlinks an audio file. If cleanup is ever needed it's a separate, explicit,
  dry-run-first script.
- **Idempotent uploads.** The client may retry; the server must not create duplicates
  for the same client-side `id`. Client generates the id; server upserts on it.
- **Config via env**, parsed once in `config.py`. No magic constants scattered in
  handlers. `.env.example` stays current — it's the documentation.
- **Terse logging**, structured-ish: `level | component | event | key=value`. The Pi
  logs to journald; don't invent a second log file.
- Type hints on public functions. `ruff` clean before a milestone is called done.

## The backend seam (read before writing client code)
`banter_client/backends/base.py` defines three protocols. **Never call `arecord`,
`aplay`, `gpiozero` or `neopixel` directly from feature code** — go through the
protocol and let `factory.py` choose the implementation from config. This is what lets
the whole loop run on a laptop (`sounddevice`/`keyboard`/`terminal`) or in CI
(`synthetic`/`keyboard`/`null`) before hardware exists. Hardware imports are lazy
inside their classes so the package stays importable off-Pi.

Any new hardware touchpoint gets a protocol + a simulated implementation in the same
commit. `uv run banter-demo` must keep working with `BANTER_AUDIO_BACKEND=synthetic`.

## Hardware facts the code must respect
Two supported builds: **Zero 2 W + Codec Zero** (the default) and **Pi 4 + USB webcam
mic / USB speaker** (`client/.env.pi4.example`). The pin table below holds for both —
they differ only in the two ALSA device strings. The splitter note immediately below
applies to the Codec Zero build only; the Pi 4 has no HAT covering its header.

The Codec Zero's 2×20 socket is **not** pass-through — seated on the Pi it buries every
pin below. BTN1/BTN2/ring therefore reach the header via a GPIO splitter board on a short
ribbon (see `PARTS.md` / `wiring.svg`). Pin numbers below are unchanged by that; code
should not care.

| Pin | Use | Notes |
|---|---|---|
| GPIO17 (pin 11) | BTN1 record | `Button(17)` — internal pull-up, switch to GND |
| GPIO22 (pin 15) | BTN2 play | `Button(22)` — **dead on the current Pi 4**, see below |
| GPIO10 (pin 19) | NeoPixel data | SPI0 MOSI, `neopixel_spi` |
| GPIO2/3 | **RESERVED** I2C (HAT) | do not touch |
| GPIO18/19/20/21 | **RESERVED** I2S audio (HAT) | do not touch |
| GPIO27 | HAT's own button | free on the Pi 4 (no HAT) — **BTN2 play there**, see below |
| GPIO23/24 | HAT status LEDs | usable for debug blinks |

Free spares: GPIO5, 6, 12, 13, 16, 25.

**Pi 4 variant — BTN2 is on GPIO27, not 22.** GPIO22 reads pressed at rest on that
board and produces no edge in either direction; swapping the physical buttons kept the
fault on the pin. Because gpiozero fires on a high->low edge, a line already low at boot
means the button never triggers and logs nothing — `_check_buttons()` in the client's
`__main__.py` exists to say so at startup. The override lives in `.env.pi4.example`
only: `config.py`, `.env.example` and `wiring.svg` all still say 22, which is correct
for the Codec Zero build, where GPIO27 is the HAT's own button and would collide.

- Audio in/out is the **Codec Zero** HAT via ALSA. Record with `arecord -D <dev> -f S16_LE
  -r 16000 -c 1`; play with `aplay`. Device string comes from config, not hardcoded —
  find it with `arecord -l` / `aplay -l`.
- NeoPixel on SPI needs `dtparam=spi=on` and `core_freq_min=500` in
  `/boot/firmware/config.txt`. Using SPI (not PWM) is deliberate: PWM conflicts with
  onboard audio and needs root. Don't "simplify" it back to GPIO18.
- Cap LED brightness at `LED_MAX_BRIGHTNESS` (0.3). 16 RGBW at full white pulls ~1 A
  and browns out the Pi.

## Gotchas that will bite
1. **Telegram voice notes are OGG/Opus**, not WAV. Transcode with ffmpeg to 16 kHz mono
   WAV before kidbox ever sees them. Keep the original file too.
2. **The bot runs as a background task inside the server's lifespan**, long-polling.
   Don't add a webhook — there's no public URL. Make sure the task is cancelled
   cleanly on shutdown or Docker restarts hang.
3. **`arecord` doesn't stop politely.** `terminate()`, then `kill()` after a short
   timeout, then validate the file has a sane size before queueing it.
4. **Debounce the buttons** (`bounce_time=0.05`). Kids press hard and repeatedly.
5. **Record and play must be mutually exclusive.** Guard with a lock in `state.py`;
   a simultaneous `arecord` + `aplay` on this HAT is a bad time.
6. **Selection is a pure function.** `select_next(candidates, device_history, now, cfg)`
   takes data and returns a choice — no DB calls inside it. That's what makes the tier
   rules testable.
7. **The client's queue is the source of truth for unsent audio.** Only delete a local
   file after a 2xx from the server.
8. **SQLite + concurrent writers**: WAL mode, short transactions, and a busy timeout.
   The bot task and HTTP handlers both write.

## Testing expectations
Required unit coverage: `select_next()` (all four tiers, no-repeat rule, empty pool),
queue drain/retry/restart behavior, and Telegram voice ingest (fixture OGG →
transcode → row). Use a tmp DB per test. Mock the Telegram API and GPIO — don't
require hardware for the server suite.

## Definition of done for any milestone
Roadmap criteria met · `uv run pytest` green · `ruff check` clean · `.env.example`
updated if config changed · README setup steps still accurate.

## Things not to do
- Don't add an ORM, a task queue, Redis, or a frontend framework. The whole point is
  a small, legible, self-hosted app.
- Don't hard-delete audio.
- Don't put secrets in the repo or in Docker image layers.
- Don't add features from the out-of-scope list without asking.
