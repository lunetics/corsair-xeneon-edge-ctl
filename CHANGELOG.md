# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Project skeleton: licence, community files, udev rule for HID access, design
  document.
- Commands `status`, `get` and `set` (`xeneon-edge-ctl`, `python -m
  xeneon_edge_ctl`) for backlight, contrast, brightness, sharpness, colour
  preset, OSD language and the RGB gains. `set` reads the value back and exits
  with code 6 on a mismatch.
- `--json` output with one object per command and exit codes 0-7.
- USB HID transport behind an allowlist and DDC/CI transport that finds the bus
  by the monitor identity; both refuse the factory-reset frame (HID `0x0F FF`)
  and the VCP codes `0x04`, `0x05`, `0x06`, `0x08` and `0x60`.
- Single-writer lock on `$XDG_RUNTIME_DIR/xeneon-edge.lock`; a busy lock exits
  with code 7.
- Commands `profile save`, `profile apply`, `profile list` and `profile show`.
  Profiles are JSON files in `$XDG_CONFIG_HOME/corsair-xeneon-edge-ctl/profiles/`;
  `apply` validates the file first, writes preset, gains, then the rest under the
  writer lock, skips matching values, reads every value back, reports every
  failure and writes a journal to
  `$XDG_STATE_HOME/corsair-xeneon-edge-ctl/last-apply.json`.
- Commands `blank` and `unblank` (VCP `0xD6` = `05` / `01`). `blank` reads the
  power mode back and writes `01` at once if DDC/CI stops answering. The way back
  over USB-C (DisplayPort Alt Mode) is untested and documented as a limitation.
- The DDC/CI transport writes VCP `0xD6` only with the values `01` and `05`.
- Commands `osd open`, `osd where` and `osd close` for the on-screen menu. `osd
  open` and `osd close` press `back` three times (and `select` to open) over HID,
  150 ms apart under the writer lock. The tool tracks the menu in
  `$XDG_RUNTIME_DIR/xeneon-edge-osd.json`, replaced atomically; a press that
  fails leaves the state marked unknown. `osd where` prints the tracked state
  without touching the display. The HID allowlist admits exactly the four OSD key
  frames.
- Runs on Python 3.11 or newer; CI runs the tests on 3.11 to 3.14.
