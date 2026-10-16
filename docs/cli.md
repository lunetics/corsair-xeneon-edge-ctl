# Command-line reference

```sh
xeneon-edge-ctl [--json] [--ddc-bus N] <command> [arguments]
```

From a checkout without installing, replace `xeneon-edge-ctl` with
`PYTHONPATH=src python3 -m xeneon_edge_ctl`.

## Contents

- [Global options](#global-options)
- [Commands](#commands)
- [Settings](#settings)
- [Profiles](#profiles)
- [blank and unblank](#blank-and-unblank)
- [JSON output](#json-output)
- [Exit codes](#exit-codes)
- [Refused commands](#refused-commands)
- [Files](#files)

## Global options

The options work before or after the command.

| Option | Effect |
|---|---|
| `--json` | Print exactly one JSON object on stdout, see [JSON output](#json-output). |
| `--ddc-bus N` | Use DDC/CI bus `N`. The tool still checks that the display is on it. Without the option the bus is found by the monitor identity `CRX:XENEON EDGE`. |
| `--version` | Print the version. |
| `-h`, `--help` | Print help for the tool or a command. |

## Commands

| Command | Effect |
|---|---|
| `status` | Read every setting and report the HID node, the DDC/CI bus and the firmware. The RGB gains come from HID (the effective gains); the gains DDC/CI reports are listed separately as "stored user gains". |
| `get <setting>` | Read one setting. |
| `set <setting> <value>` | Write one setting under the writer lock, read it back, exit with code 6 if the value differs. |
| `profile save <name> [--force]` | Store the current settings as a profile, see [Profiles](#profiles). |
| `profile apply <name>` | Write a stored profile to the display and verify it. |
| `profile list` | List the stored profiles. |
| `profile show <name>` | Print a stored profile. |
| `blank` | Switch the picture off, see [blank and unblank](#blank-and-unblank). |
| `unblank` | Switch the picture back on. |

Commands that write (`set`, `profile apply`, `blank`, `unblank`) take an
exclusive lock on `$XDG_RUNTIME_DIR/xeneon-edge.lock`. If another writer holds
it, the command exits with code 7 after about two seconds.

## Settings

| Setting | Values | Written through |
|---|---|---|
| `backlight`, `contrast` | 0-100 | DDC/CI |
| `brightness` | 0-100 (the OSD entry "Brightness") | HID |
| `sharpness` | 0-4 | DDC/CI |
| `preset` | `user` or a raw value such as `0x0b` | DDC/CI |
| `language` | `english` or a raw value such as `0x02` | DDC/CI |
| `red`, `green`, `blue` | 0-255 | HID |

## Profiles

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

## blank and unblank

- `blank` switches the picture off by writing VCP `0xD6` = `05` under the writer
  lock, then reads `0xD6` (the measured answer is `04`: the display is dark but
  DDC/CI keeps answering). If the read fails or times out, the tool writes
  `0xD6` = `01` at once and exits with code 5; the display may then need a power
  cycle.
- `unblank` writes `0xD6` = `01` and exits with code 6 unless it reads `01` back.
- The tool writes `0xD6` only with the values `01` and `05` and refuses all
  others (exit code 3).

> [!WARNING]
> The way back after `blank` was tested only on a DisplayPort connection. Over
> USB-C (DisplayPort Alt Mode) it is untested, and the tool does not detect the
> connector. Try `blank` there only with a plan to unplug the display.

## JSON output

With `--json`, stdout carries exactly one object:

```json
{"ok": true, "command": "get", "data": {"setting": "contrast", "value": 50}, "error": null}
```

On failure `ok` is `false`, `data` is `null` and `error` holds `code` and
`message`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Unexpected error |
| 2 | Usage error |
| 3 | Refused by a safety rule |
| 4 | Display not found or no access |
| 5 | I/O or protocol error |
| 6 | Value read back differs |
| 7 | Another writer holds the lock |

## Refused commands

The tool refuses to send the factory-reset frame (HID `0x0F FF`), the VCP codes
`0x04`, `0x05`, `0x06`, `0x08` and `0x60`, and any value but `01` and `05` for
VCP `0xD6` (exit code 3). The reasons are in
[design.md](design.md#safety).

## Files

| Path | Content |
|---|---|
| `$XDG_RUNTIME_DIR/xeneon-edge.lock` | Writer lock |
| `$XDG_CONFIG_HOME/corsair-xeneon-edge-ctl/profiles/<name>.json` | Profiles (`~/.config` when unset) |
| `$XDG_STATE_HOME/corsair-xeneon-edge-ctl/last-apply.json` | Journal of the last `profile apply` (`~/.local/state` when unset) |
