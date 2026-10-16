# Instructions for coding agents

This repository contains a command-line tool that controls the Corsair Xeneon
Edge display over USB HID and DDC/CI. Read [docs/design.md](docs/design.md)
before changing behaviour.

## Commands

- Tests: `PYTHONPATH=src python3 -m unittest discover -s tests -v`
- The runtime uses only the Python standard library. Do not add runtime
  dependencies.

## Hard rules

- Never run the tool, `ddcutil` or any HID write against a real display from an
  automated task. Tests use fakes for every transport.
- Never send, and never add a code path that sends: HID `0x0F FF` (factory
  reset), the OSD "Reset" entry, DDC/CI VCP `0x04`, `0x05`, `0x06`, `0x08`
  (restore defaults) or VCP `0x60` (input source). The display has no buttons to
  recover from these.
- Every refused command keeps a test that proves the refusal.
- Read the display state from the device; never trust a cached value. The
  display does not report changes.
- Find the DDC/CI bus by the monitor identity `CRX:XENEON EDGE`, never by a bus
  number.

## Conventions

- Code, comments, documentation and commit messages are in English.
- No code copied from other projects; protocol constants need evidence.
- Commit messages: see [CONTRIBUTING.md](.github/CONTRIBUTING.md#commits).
