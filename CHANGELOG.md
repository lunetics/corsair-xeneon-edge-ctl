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
- Commands `osd open`, `osd key`, `osd where` and `osd close` press the four OSD
  keys over HID (`select`, `right`, `left`, `back`), so the on-screen menu can be
  worked from the computer. The tool tracks the cursor in
  `$XDG_RUNTIME_DIR/xeneon-edge-osd.json`, checks a whole `--repeat` run before
  it sends the first key, and refuses keys when the state is unknown (an open
  menu more than 8 s after the last key, a missing or unreadable state file, a
  failed press, a Reset). `select` on the menu entry Reset, and any `select` in
  Misc after the cursor has touched Reset, needs `--confirm-reset`. The HID
  allowlist admits exactly the four key frames.
- Command `osd interactive` works the on-screen menu with single key presses from
  the terminal: arrow keys, Enter (select), Escape or Backspace (back), `o` (open)
  and `q` (close and leave). Every key goes through the same writer lock, state
  file, planning and Reset rules as `osd key`, with the lock taken per key press.
  A status line shows the tracked position, the DCR assumption and a Reset
  warning; refusals and errors appear there and the mode keeps running. A select
  that needs `--confirm-reset` asks first, and only the exact answer `RESET`
  sends it. It exits with code 2 when stdin or stdout is not a terminal, and with
  `--json`; Ctrl-C leaves without sending a key (exit code 0).
- `set` and `get` for the ten settings that exist only in the on-screen menu
  (`effect`, `saturation`, `low-blue-light`, `aspect`, `dcr`, `rotate`,
  `overdrive`, `volume`, `mute`, `audio-source`). `set` writes them with a
  planned run of OSD keys (`--from` names the value a list starts from), never
  enters the Misc page, and refuses a run in which one lost key would press
  `select` on Reset (exit code 3). The value is written, not verified: `get`
  prints the last value the tool knows from
  `$XDG_STATE_HOME/corsair-xeneon-edge-ctl/osd-values.json`, which is forgotten
  when a hand-made edit or a Reset invalidates it. A `set` run reads the readable
  settings before and after and, on a difference, writes them back and exits
  with code 6. `osd open` and `osd interactive` take the DCR assumption from the
  remembered `dcr` value unless `--dcr` is given.
- Runs on Python 3.11 or newer; CI runs the tests on 3.11 to 3.14.
