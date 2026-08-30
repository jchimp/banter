Done, on branch `pi4-usb-audio`. **Short answer to your question: yes, the Pi 4 + USB mic/speaker build is config-only** — two env values. Everything I added is about making that config safe to get wrong.

**Code** (~70 lines + 11 tests):

-   [audio.py](vscode-webview://14cs6u6tqc6l1v1aj62m7udid9evosruj7m3lsrhfagtc86cr00r/client/banter_client/backends/audio.py#L30) — `parse_pcm_names`, `parse_cards`, `alsa_device_ok` (all pure, in the same style as the existing `arecord_cmd`) plus `check_alsa_devices`, the one impure function that shells out to `arecord/aplay -L/-l`.
-   [**main**.py](vscode-webview://14cs6u6tqc6l1v1aj62m7udid9evosruj7m3lsrhfagtc86cr00r/client/banter_client/__main__.py#L96) — runs at startup, only when the backend is `alsa`. Logs and continues rather than exiting; with `Restart=on-failure` in the unit, exiting on a typo'd device name would give you a restart loop instead of a diagnosis.

Verified message on a simulated Pi 4 listing:

```
ERROR | banter.client | event=alsa_device_missing which=capture configured='plughw:9,0' not found; cards=1:Webcam, 2:Speaker (run `arecord -L` for the stable CARD= names)
```

And on a box with no `alsa-utils` at all it reports nothing — no crying wolf.

**Config/docs:** new [client/.env.pi4.example](vscode-webview://14cs6u6tqc6l1v1aj62m7udid9evosruj7m3lsrhfagtc86cr00r/client/.env.pi4.example); README gains a Pi 4 profile row and a "Variant — Pi 4 with a USB mic and USB speaker" section; PARTS.md gains a variant BOM (Pi 4, USB-C 3 A PSU, webcam, powered speaker — no Codec Zero, splitter, ribbon or 8 Ω speaker); CLAUDE.md now says there are two builds and scopes the splitter note to the Zero one. The systemd unit keeps its `pi` defaults with a comment about the Pi 4 image's user.

129 client tests (was 118) and 210 server tests pass, ruff clean.

Two things I'd flag for the actual hardware pass:

-   **Use the `CARD=` device form, not `plughw:1,0`.** Two USB audio gadgets renumber across reboots. That's the whole reason the preflight exists.
-   **Powered speaker, not bus-powered.** A USB speaker drawing from the Pi's rail alongside 16 RGBW pixels is the same brownout problem the LED brightness cap already guards against.

Nothing is committed — the working tree has the 8 modified files plus the new env example.
