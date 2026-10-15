# corsair-xeneon-edge-ctl

Command-line control for the Corsair Xeneon Edge 14.5" touch display on Linux:
picture settings (backlight, contrast, brightness, sharpness, colour preset,
RGB gains, OSD language) and profiles, over the display's USB HID interface and
DDC/CI.

This project is not affiliated with or endorsed by Corsair. Corsair and Xeneon
are trademarks of their respective owners.

## Status

Pre-release. `status`, `get` and `set` work. Profiles, `blank`/`unblank` and
OSD key navigation are planned; the full design is in
[docs/design.md](docs/design.md).

## Requirements

- Linux with the display connected over DisplayPort or USB-C and its USB cable
- Python 3.12 or newer; no third-party runtime dependencies
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
```

- `status` reads every setting and reports the HID node, the DDC/CI bus and the
  firmware. The RGB gains come from HID (the effective gains); the gains DDC/CI
  reports are listed separately as "stored user gains".
- `get <setting>` reads one setting.
- `set <setting> <value>` writes one setting, reads it back and exits with code
  6 if the value differs. It takes an exclusive lock on
  `$XDG_RUNTIME_DIR/xeneon-edge.lock`; if another writer holds it, the command
  exits with code 7 after about two seconds.

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

The tool refuses to send the factory-reset frame (HID `0x0F FF`) and the VCP
codes `0x04`, `0x05`, `0x06`, `0x08` and `0x60`; see
[docs/design.md](docs/design.md#safety).

## Documentation

[docs/README.md](docs/README.md) lists all documentation.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
and [SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE).
