# corsair-xeneon-edge-ctl

Command-line control for the Corsair Xeneon Edge 14.5" touch display on Linux:
picture settings (backlight, contrast, brightness, sharpness, colour preset,
RGB gains, OSD language) and profiles, over the display's USB HID interface and
DDC/CI.

This project is not affiliated with or endorsed by Corsair. Corsair and Xeneon
are trademarks of their respective owners.

## Status

Pre-release. `status`, `get`, `set`, the `profile` commands and
`blank`/`unblank` work. OSD key navigation is planned; the full design is in
[docs/design.md](docs/design.md).

## Requirements

- Linux with the display connected over DisplayPort or USB-C and its USB cable
- Python 3.11 or newer; no third-party runtime dependencies
- [ddcutil](https://www.ddcutil.com/) for DDC/CI
- The udev rule in [packaging/udev/](packaging/udev/) for HID access as a
  normal user

## Usage

From a checkout (`xeneon-edge-ctl` is the console script that installing the
package provides):

```sh
PYTHONPATH=src python3 -m xeneon_edge_ctl status
PYTHONPATH=src python3 -m xeneon_edge_ctl get contrast
PYTHONPATH=src python3 -m xeneon_edge_ctl set contrast 60
PYTHONPATH=src python3 -m xeneon_edge_ctl set preset user --json
PYTHONPATH=src python3 -m xeneon_edge_ctl profile save work
PYTHONPATH=src python3 -m xeneon_edge_ctl profile apply work
PYTHONPATH=src python3 -m xeneon_edge_ctl blank
PYTHONPATH=src python3 -m xeneon_edge_ctl unblank
```

- `status` reads every setting and reports the HID node, the DDC/CI bus and the
  firmware. The RGB gains come from HID (the effective gains); the gains DDC/CI
  reports are listed separately as "stored user gains".
- `get <setting>` reads one setting.
- `set <setting> <value>` writes one setting, reads it back and exits with code
  6 if the value differs. It takes an exclusive lock on
  `$XDG_RUNTIME_DIR/xeneon-edge.lock`; if another writer holds it, the command
  exits with code 7 after about two seconds.
- `profile ...`, `blank` and `unblank` are described [below](#profiles).

| Setting | Values | Written through |
|---|---|---|
| `backlight`, `contrast` | 0-100 | DDC/CI |
| `brightness` | 0-100 (the OSD entry "Brightness") | HID |
| `sharpness` | 0-4 | DDC/CI |
| `preset` | `user` or a raw value such as `0x0b` | DDC/CI |
| `language` | `english` or a raw value such as `0x02` | DDC/CI |
| `red`, `green`, `blue` | 0-255 | HID |

The DDC/CI bus is found by the monitor identity `CRX:XENEON EDGE`. `--ddc-bus N`
selects a bus explicitly; the tool still checks that the display is on it.

With `--json`, stdout carries exactly one object:

```json
{"ok": true, "command": "get", "data": {"setting": "contrast", "value": 50}, "error": null}
```

On failure `ok` is `false`, `data` is `null` and `error` holds `code` and
`message`. Exit codes: 0 success, 1 unexpected error, 2 usage error, 3 refused
by a safety rule, 4 display not found or no access, 5 I/O or protocol error,
6 value read back differs, 7 another writer holds the lock.

The tool refuses to send the factory-reset frame (HID `0x0F FF`), the VCP
codes `0x04`, `0x05`, `0x06`, `0x08` and `0x60`, and any value but `01` and `05`
for VCP `0xD6`; see [docs/design.md](docs/design.md#safety).

### Profiles

- `profile save <name>` reads every setting from the display and stores it in
  `$XDG_CONFIG_HOME/corsair-xeneon-edge-ctl/profiles/<name>.json` (`~/.config`
  when the variable is unset). Names are 1 to 64 letters, digits, `-` or `_`.
  An existing profile is refused (exit code 2) unless you pass `--force`.
- `profile apply <name>` checks the whole file first (unknown or out-of-range
  values exit with code 2 before anything is written). Then it takes the writer
  lock and writes the colour preset, then the RGB gains, then the other settings.
  Values that already match are skipped, every written value is read back, and a
  failed value does not stop the run: all failures are reported. Exit code 6 if a
  value differs afterwards, 5 on an I/O error, 7 if another writer holds the lock.
  What was read, written and read back is recorded in
  `$XDG_STATE_HOME/corsair-xeneon-edge-ctl/last-apply.json` (`~/.local/state`
  when unset); only the last apply is kept.
- `profile list` and `profile show <name>` read the files only; they take no lock
  and do not touch the display.

### blank and unblank

- `blank` switches the picture off by writing VCP `0xD6` = `05` under the writer
  lock, then reads `0xD6` (the measured answer is `04`: the display is dark but
  DDC/CI keeps answering). If the read fails or times out, the tool writes
  `0xD6` = `01` at once and exits with code 5; the display may then need a power
  cycle.
- `unblank` writes `0xD6` = `01` and exits with code 6 unless it reads `01` back.
- **Limitation:** the way back after `blank` was tested only on a DisplayPort
  connection. Over USB-C (DisplayPort Alt Mode) it is untested; the tool does
  not detect the connector, so try `blank` there only with a plan to unplug the
  display.
- The tool writes `0xD6` only with the values `01` and `05` and refuses all
  others (exit code 3).

## Documentation

[docs/README.md](docs/README.md) lists all documentation.

## Contributing

See [CONTRIBUTING.md](.github/CONTRIBUTING.md), [CODE_OF_CONDUCT.md](.github/CODE_OF_CONDUCT.md)
and [SECURITY.md](.github/SECURITY.md).

## License

MIT, see [LICENSE](LICENSE).
