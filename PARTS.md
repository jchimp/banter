# Banter — Parts List

Device codename: **kidbox** (the Pi). Project: **Banter**.

Prices are ballpark USD. `☐` = buy / check inventory.

## Core

| ☐ | Item | Notes | Link | ~$ |
|---|------|-------|------|----|
| ☐ | Raspberry Pi Zero 2 W **WH** (pre-soldered header) | Get the WH variant — you need the header for the HAT. | https://www.adafruit.com/product/6008 | 18 |
| ☐ | Raspberry Pi Codec Zero HAT | On-board mic + speaker amp + button (GPIO27) + 2 status LEDs (GPIO23/24). Handles record **and** playback. | https://www.raspberrypi.com/products/codec-zero/ | 22 |
| ☐ | Speaker, **8 Ω** 1–3 W | Codec Zero drives 8Ω directly — no amp needed. | https://www.adafruit.com/product/4227 | 3 |
| ☐ | microSD 32 GB A1 | Raspberry Pi OS Lite (64-bit). | any | 8 |
| ☐ | 5V **2.5 A** micro-USB PSU | Must be a good one — LEDs + audio + WiFi spike together. | official RPi PSU | 8 |
| ☐ | Stacking / extra-tall 2×20 header | The HAT covers all 40 pins; this is how buttons + ring reach GPIO. | https://www.adafruit.com/product/2223 | 3 |

## Buttons (2)

| ☐ | Item | Notes | Link | ~$ |
|---|------|-------|------|----|
| ☐ | 2 × Arcade Button with LED, 30mm | **5V** LEDs (not 12V — that's the 60/100mm ones). Gold-contact microswitch, smooth press. Suggest Green = Record, Blue = Play. | Green https://www.adafruit.com/product/3487 · Blue https://www.adafruit.com/product/3490 | 6 ea |
| ☐ | 0.110" quick-connect wire pairs | Arcade spade lugs — makes the whole build solderless. | https://www.adafruit.com/product/1152 | 3 |

> Colors: clear/green/blue LEDs can run dim at 3.3V; **red and yellow are wired in series and need 5V+**. Plan on 5V either way.

## Glow (default: one status ring)

| ☐ | Item | Notes | Link | ~$ |
|---|------|-------|------|----|
| ☐ | NeoPixel Ring — 16 × RGBW **warm white** | Warmer, nicer at a bedside than plain RGB. Plain RGB ring: #1463. | https://www.adafruit.com/product/2854 | 13 |
| ☐ | 1000 µF electrolytic cap (≥6.3V) | Across ring 5V/GND. | any | 1 |
| ☐ | 470 Ω resistor | In series on ring data line. | any | <1 |
| ☐ | 74AHCT125 level shifter *(optional)* | 3.3V → 5V data. Skippable on a short run; add if colors flicker. | https://www.adafruit.com/product/1787 | 2 |

**Why one ring, not two halos:** a 16-ring's inner hole is ~31.7 mm but a 30mm button's bezel is ~34 mm — the button covers the ring. Default = one ring behind a 45 mm diffused window as the status glow.

*Upgrade variant (per-button halos):* swap to 2 × **24mm** Mini LED Arcade Buttons (https://www.adafruit.com/product/3429) + 2 × Ring 16. The 24mm bezel (~28 mm) fits inside the ring's 31.7 mm hole. Chain ring B's DIN off ring A's DOUT — same single data pin, indices 0–15 = record, 16–31 = play.

## Enclosure & small parts

| ☐ | Item | Notes | Link | ~$ |
|---|------|-------|------|----|
| ☐ | ABS project box ~200×120×75 mm | Sold as "waterproof junction box" — cheap, easy to drill. Gasket unused. | https://www.amazon.com/Water-resistant-Electrical-Instrument-Communications-200x120x75mm/dp/B08B1PGN2N | 12 |
| ☐ | Diffuser: 45 mm white acrylic disc or 3D-printed 1.5 mm insert | Over the ring window. Frosted tape works in a pinch. | any | 2 |
| ☐ | Jumper wires F-F / F-M | Ring + button wiring. | any | 3 |
| ☐ | Speaker grille cloth + M2.5 standoffs/screws | Mounting. | any | 4 |

**Panel cutouts:** 2 × 30.0 mm button holes (≥40 mm apart, center to center), 1 × 45 mm ring window, speaker hole pattern, micro-USB power slot on the side.

## Total
~$95–105 new. Less whatever's already in your kit bins (SD, PSU, resistors, jumpers, box).

## Explicitly NOT needed
- No USB mic — the Codec Zero has one on board.
- No amp board — the Codec Zero drives the 8Ω speaker.
- No USB OTG cable — nothing plugs into the USB port.
- No display — dropped in this re-spec.
