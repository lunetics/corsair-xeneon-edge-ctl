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
- [osd: the on-screen menu](#osd-the-on-screen-menu)
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
| `osd open [--dcr on\|off]` | Open the on-screen menu on Picture, see [osd](#osd-the-on-screen-menu). |
| `osd key <key> [--repeat N] [--confirm-reset]` | Press one OSD key once or `N` times. |
| `osd where` | Print the tracked state of the on-screen menu. Reads one file; no device access, no lock. |
| `osd close` | Close the on-screen menu. |

Commands that write (`set`, `profile apply`, `blank`, `unblank`, `osd open`,
`osd key`, `osd close`) take an exclusive lock on `$XDG_RUNTIME_DIR/xeneon-edge.lock`. If another writer holds
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

## osd: the on-screen menu

The `osd` commands work the display's on-screen menu with its four keys over
HID. The display does not report where the menu cursor is, so the tool tracks
it in a state file and refuses keys whose effect it cannot predict. The menu
model and the reasoning are in [design.md](design.md#osd-key-control).

```sh
xeneon-edge-ctl osd open
xeneon-edge-ctl osd key right --repeat 2
xeneon-edge-ctl osd key select
xeneon-edge-ctl osd where
xeneon-edge-ctl osd close
```

### Keys

| Key | Effect |
|---|---|
| `select` | Opens the menu; enters a page; starts editing an entry; keeps an edited value. |
| `right` | Next page or entry; while editing, one step up. |
| `left` | Previous page or entry; while editing, one step down. |
| `back` | Discards an edit; leaves a page; closes the menu. Harmless on a closed menu. |

The menu has the pages Picture, Colour, Display, Audio and Misc. Pages and
entries are rings: `right` on the last one goes to the first. Entering a page
starts on its first entry. With Dynamic Contrast Ratio (DCR) on, entering
Picture starts on Sharpness; the display cannot report DCR, so the tool assumes
it is off unless you say `osd open --dcr on`.

### Commands

- `osd open [--dcr on|off]` presses `back` three times and then `select`, so
  the menu is open on Picture whatever state it was in. It always works, starts
  a new tracked state and clears the Reset mark (see below).
- `osd key <select|left|right|back> [--repeat N] [--confirm-reset]` presses
  the key once, or `N` times (1 to 50; anything else is a usage error, exit
  code 2). The tool checks every step of a `--repeat` run before it sends the
  first key; if a step is refused, nothing is sent. The output names the keys
  sent and the cursor afterwards.
- `osd where` prints the tracked state: the menu level, the page and entry the
  cursor is on, the DCR assumption, whether the cursor has been on Reset, and
  whether the state is still known (if not, why). It sends nothing, takes no
  lock and never changes the state file.
- `osd close` presses `back` three times, so the menu is closed. It always
  works and clears the Reset mark.

Keys within one command are sent 150 ms apart. `osd open`, `osd key` and
`osd close` hold the writer lock from the first key to the last. They use HID
only, never DDC/CI; `--ddc-bus` is accepted and ignored.

### When a key is refused

A refusal exits with code 3 and sends nothing.

- **Unknown state.** `osd key` refuses every key when the menu is tracked as
  open and the last key is more than 8 seconds old (the menu closes itself
  after "Menu Time", 10 seconds at the least, so a later key would land
  somewhere else), when the state file is missing or unreadable, after a
  failed key press, and after a confirmed Reset. Run `osd open`. A closed menu
  stays known however old the state is.
- **Closed menu.** `select` opens the menu on Picture and `back` changes
  nothing. `left` and `right` are refused: their effect on a closed menu is not
  measured.

### Reset

"Reset" is the last entry of the Misc page and resets the display. What it
resets, and whether the display still answers afterwards, is not measured.

- Moving the cursor onto Reset is allowed and prints a warning.
- `select` on Reset is refused unless you pass `--confirm-reset`. Pass it by
  hand only, never from a script, a test against hardware or an automated task.
- A key press can get lost (measured: about 1 % at most), leaving the real
  cursor on Reset while the tracked cursor has moved on. So once the tracked
  cursor has been on Reset, every `select` in the Misc page (the page list on
  Misc included) needs `--confirm-reset` until the next `osd open` or
  `osd close`.
- After a confirmed Reset the state is unknown; `osd open` or `osd close` start
  over.

### Output

With `--json`, `data` of `osd open`, `osd key` and `osd close` holds `keys` (the
keys sent, in order) and `state`; `osd key` adds `warnings`. `state` has
`level` (`closed`, `pages`, `entries` or `editing`), `page`, `entry`, `dcr`,
`reset_touched`, `last_key_time` and `unknown_reason` (`null` unless the
position cannot be trusted). `osd where` has `state`, `known`, `reason` (why the
state is unknown, else `null`) and `age_s` (seconds since the last key, or
`null`). In text mode warnings go to stderr.

```text
$ xeneon-edge-ctl osd where
Menu:          Misc > Language
State:         known, last key 3.5 s ago
DCR assumed:   off
Reset touched: no
```

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
[design.md](design.md#safety). The HID transport sends nothing in group `01`
except the four OSD key frames. `select` on the OSD entry Reset is refused
without `--confirm-reset`, see [Reset](#reset).

## Files

| Path | Content |
|---|---|
| `$XDG_RUNTIME_DIR/xeneon-edge.lock` | Writer lock |
| `$XDG_RUNTIME_DIR/xeneon-edge-osd.json` | Tracked state of the on-screen menu, replaced atomically before the first key and after every key |
| `$XDG_CONFIG_HOME/corsair-xeneon-edge-ctl/profiles/<name>.json` | Profiles (`~/.config` when unset) |
| `$XDG_STATE_HOME/corsair-xeneon-edge-ctl/last-apply.json` | Journal of the last `profile apply` (`~/.local/state` when unset) |
