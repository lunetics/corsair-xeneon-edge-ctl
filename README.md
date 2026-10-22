# corsair-xeneon-edge-ctl

Command-line control for the Corsair Xeneon Edge 14.5" touch display on Linux:
picture settings (backlight, contrast, brightness, sharpness, colour preset,
RGB gains, OSD language) and profiles, over the display's USB HID interface and
DDC/CI.

This project is not affiliated with or endorsed by Corsair. Corsair and Xeneon
are trademarks of their respective owners.

## Status

Version 0.1.0, the first release. `status`, `get`, `set`, the `profile` commands,
`blank`/`unblank` and the `osd` commands for the on-screen menu keys work,
including `osd interactive` for arrow-key control from the terminal. `set` and
`get` also cover the ten settings that exist only in the on-screen menu; their
values are written with OSD keys and not verified. The
full design is in [docs/design.md](docs/design.md).

## Requirements

- Linux with the display connected over DisplayPort or USB-C and its USB cable
- Python 3.11 or newer; no third-party runtime dependencies
- [ddcutil](https://www.ddcutil.com/) for DDC/CI
- The udev rule in [packaging/udev/](packaging/udev/) for HID access as a
  normal user

## Installation

Install the `xeneon-edge-ctl` command with [pipx](https://pipx.pypa.io/):

```sh
git clone https://github.com/lunetics/corsair-xeneon-edge-ctl.git
cd corsair-xeneon-edge-ctl
pipx install .
```

Give the logged-in user access to the display's HID interface:

```sh
sudo install -m 0644 packaging/udev/61-corsair-xeneon-edge-ctl.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=hidraw --action=add
```

`ddcutil detect --brief` must list `CRX:XENEON EDGE` when run as your user; if
it reports missing permissions, see
[ddcutil: I2C permissions](https://www.ddcutil.com/i2c_permissions/).

To run from the checkout without installing, use
`PYTHONPATH=src python3 -m xeneon_edge_ctl` in place of `xeneon-edge-ctl`.

## Quick start

```sh
xeneon-edge-ctl status
xeneon-edge-ctl get contrast
xeneon-edge-ctl set contrast 60
xeneon-edge-ctl profile save work
xeneon-edge-ctl profile apply work
xeneon-edge-ctl blank
xeneon-edge-ctl unblank
xeneon-edge-ctl osd open
xeneon-edge-ctl osd key right --repeat 2
xeneon-edge-ctl osd interactive
```

Every command that writes reads the value back and fails with exit code 6 on a
difference. `--json` prints one JSON object per command for scripts. All
commands, settings, exit codes and files are listed in the
[command-line reference](docs/cli.md).

## Safety

The Xeneon Edge has no buttons, so a command that resets the display or switches
it off can only be undone through software or by unplugging it. The tool refuses
the factory reset, the "restore defaults" commands and input switching (exit
code 3); see [docs/design.md](docs/design.md#safety). The menu entry "Reset"
is not pressed by accident either: `osd key select` on it needs
`--confirm-reset`, which only a person should give; see
[Reset](docs/cli.md#reset).

> [!WARNING]
> The way back after `blank` is tested only over DisplayPort. Over USB-C
> (DisplayPort Alt Mode) it is untested; see
> [blank and unblank](docs/cli.md#blank-and-unblank).

## Documentation

[docs/README.md](docs/README.md) lists all documentation.

## Contributing

See [CONTRIBUTING.md](.github/CONTRIBUTING.md), [CODE_OF_CONDUCT.md](.github/CODE_OF_CONDUCT.md)
and [SECURITY.md](.github/SECURITY.md).

## License

MIT, see [LICENSE](LICENSE).
