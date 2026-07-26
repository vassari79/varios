# signal_detector — Classroom phone-presence detector

Histórico/estado do projeto (auto-carregado): @../../memories/repo/signal_detector.md

## What it is
Two ESP32-S3 boards passively sniff 2.4 GHz WiFi/BLE (and, optionally, cellular RF
power via an AD8317) and POST what they hear to `collector.py`, running in Termux
on the phone. All logic — baseline, thresholds, zoning, episodes, alerts — lives in
the collector; the boards are deliberately dumb sensors.

## Files
| File | Role |
|---|---|
| `signal_detector.ino` | Board firmware. WiFi promiscuous sniffer + NimBLE scan + optional RF ADC task. Builds one JSON batch every 2 s. |
| `secrets.h` | Per-board config (gitignored). `secrets.example.h` is the committed template — keep both in sync. |
| `collector.py` | Termux-side collector: HTTP ingest, baseline, zoning, episodes, RF bursts, console table, Flask web UI, `termux-notification` alerts. |
| `tests/` | pytest suite. `test_rf.py` (RF logic), `test_ingest.py` (the firmware→collector JSON contract). |
| `instructions.tex` / `.pdf` | Operating manual, written for a non-programmer running an exam. In English. |

## Conventions
- **Firmware stays dumb.** New sensing arms report raw measurements; thresholds and
  state machines go in `collector.py` so they can be retuned without reflashing.
  One deliberate exception: **hunt mode** (`HUNT_ENABLED`) keeps its peak-hold,
  buzzer mapping and re-zero button on the board, because homing in on a signal is
  a hand-eye loop that needs feedback well under a second — a 2 s POST round-trip
  cannot close it. The collector still owns *what* to hunt; the board owns only the
  10 Hz loop. Any future arm wanting board-side logic must clear the same bar.
- Anything optional goes behind a `#define` in `secrets.h` (default off) so the
  sketch compiles both ways — check both before committing:
  `arduino-cli compile --fqbn esp32:esp32:esp32s3 .`
- Flask is only installed on the phone; `collector.py` imports it lazily inside
  functions, so it must remain importable (and testable) without it.
- Tests are part of the project. New collector logic gets a test in `tests/`.
- The manual and the `HELP` string in `collector.py` are user-facing: state the
  limits honestly (airplane mode, randomized MACs, corner-level zoning) — no
  overselling what the hardware can do.
