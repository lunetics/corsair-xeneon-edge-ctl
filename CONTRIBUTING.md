# Contributing

## Development setup

- Python 3.12 or newer. The package uses only the standard library.
- The code lives in `src/xeneon_edge_ctl/` (src layout), so tests import the
  package from an explicit path and never pick up a stray copy by accident.

Run the tests from the repository root:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Tests and hardware

Automated tests never talk to a real display. Transports (HID, DDC/CI, OSD key
presses) are replaced by fakes. Device tests are run by hand, with the display
state read before and restored after.

Some commands can reset or switch off the display, and the Xeneon Edge has no
buttons to recover from that. The commands the tool refuses are listed in
[docs/design.md](docs/design.md#safety); every refused command has a test that
proves the refusal.

## Code provenance

Do not copy code from other projects into this repository. Protocol constants
need evidence: a measurement on the device or a cited public source.

## Commits

The subject line states what changed. The body explains why the old behaviour
was a problem, what triggers it, and what the new behaviour is.
