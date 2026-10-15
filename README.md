# corsair-xeneon-edge-ctl

Command-line control for the Corsair Xeneon Edge 14.5" touch display on Linux:
picture settings (backlight, contrast, brightness, sharpness, colour preset,
RGB gains, OSD language) and profiles, over the display's USB HID interface and
DDC/CI.

This project is not affiliated with or endorsed by Corsair. Corsair and Xeneon
are trademarks of their respective owners.

## Status

Pre-release. No command is implemented yet. The planned behaviour is described
in [docs/design.md](docs/design.md).

## Requirements

- Linux with the display connected over DisplayPort or USB-C and its USB cable
- Python 3.12 or newer; no third-party runtime dependencies
- [ddcutil](https://www.ddcutil.com/) for DDC/CI
- The udev rule in [packaging/udev/](packaging/udev/) for HID access as a
  normal user

## Documentation

[docs/README.md](docs/README.md) lists all documentation.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
and [SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE).
