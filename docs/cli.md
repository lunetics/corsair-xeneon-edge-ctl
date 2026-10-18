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
- [Menu-only settings](#menu-only-settings)
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
| `get <setting>` | Read one setting. For a [menu-only setting](#menu-only-settings) print the remembered value; nothing is read from the display. |
| `set <setting> <value>` | Write one setting under the writer lock, read it back, exit with code 6 if the value differs. |
| `set <menu-only setting> <value> [--from <value>]` | Write a [menu-only setting](#menu-only-settings) with a planned run of OSD keys under the writer lock. The value is not verified; exit code 6 if the key run changed a readable setting. |
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
| `osd interactive [--dcr on\|off]` | Work the on-screen menu with single key presses from the terminal, see [Interactive mode](#interactive-mode). Needs a terminal; no `--json`. |

Commands that write (`set`, `profile apply`, `blank`, `unblank`, `osd open`,
`osd key`, `osd close`) take an exclusive lock on `$XDG_RUNTIME_DIR/xeneon-edge.lock`. If another writer holds
it, the command exits with code 7 after about two seconds. A `set` of a
menu-only setting holds the lock from the first reading to the last. `osd interactive`
takes the lock for each key press, not for the session.

## Settings

| Setting | Values | Written through |
|---|---|---|
| `backlight`, `contrast` | 0-100 | DDC/CI |
| `brightness` | 0-100 (the OSD entry "Brightness") | HID |
| `sharpness` | 0-4 | DDC/CI |
| `preset` | `user` or a raw value such as `0x0b` | DDC/CI |
| `language` | `english` or a raw value such as `0x02` | DDC/CI |
| `red`, `green`, `blue` | 0-255 | HID |

## Menu-only settings

Ten settings exist only in the on-screen menu: no HID or DDC/CI command reads or
writes them. `set` writes them with a planned run of OSD keys, so a value is
**written, not verified**. The reasoning and the key rules are in
[design.md](design.md#menu-only-settings).

| Setting | Menu entry | Kind | Values |
|---|---|---|---|
| `effect` | Picture > Effect | list | `standard`, `game`, `movie`, `photo`, `vivid` |
| `saturation` | Colour > Saturation | slider | 0-100 |
| `low-blue-light` | Display > Low Blue Light | slider | 0-10 |
| `aspect` | Display > Aspect | list | `full`, `16:9`, `4:3` |
| `dcr` | Display > DCR | list | `off`, `on` |
| `rotate` | Display > Rotate | list | `0`, `180` |
| `overdrive` | Display > OD | list | `off`, `on` |
| `volume` | Audio > Volume | slider | 0-100 |
| `mute` | Audio > Mute | list | `off`, `on` |
| `audio-source` | Audio > Audio Source | list | `digital`, `analog` |

List values are matched without regard to case. The entries of the Misc page
(Menu Time, OSD H Position, OSD V Position) cannot be set, because Misc holds
Reset. `status`, `profile save` and `profile apply` cover the readable settings
only; a menu-only setting in a profile file is a usage error (exit code 2).

```sh
xeneon-edge-ctl set saturation 70
xeneon-edge-ctl set effect game --from standard
xeneon-edge-ctl get effect
```

```text
$ xeneon-edge-ctl set saturation 70
saturation = 70 (written with 146 keys, not verified)
$ xeneon-edge-ctl --json set saturation 70
{"ok": true, "command": "set", "data": {"setting": "saturation", "value": 70, "verified": false, "keys": 146}, "error": null}
```

### Setting a menu-only value

`set <setting> <value> [--from <value>]` takes the writer lock for the whole run
and then:

1. reads every readable setting as a snapshot (the display must answer);
2. forgets the remembered value of the setting, so a run that does not finish
   leaves it unknown;
3. sends the keys of `osd open`, walks to the entry, starts editing, changes the
   value, keeps it, and sends the keys of `osd close`;
4. reads the readable settings again and compares them with the snapshot;
5. remembers the new value.

The keys are 150 ms apart and the tracked menu state is saved after every key,
as for the `osd` commands. The whole run is planned and checked before the first
key is sent.

- **Sliders** (`saturation`, `low-blue-light`, `volume`) need no current value:
  the run presses `left` (maximum - minimum + 1) times to reach the minimum and
  `right` up to the value, or clamps at the maximum and counts down when that is
  nearer. A run for `saturation` or `volume` takes up to about 170 keys, about
  25 seconds. `--from` on a slider is a usage error (exit code 2).
- **Lists** are rings, so the run needs the value the list is on now. It comes
  from `--from <value>`, else from the remembered value. With neither, `set`
  exits with code 3 and sends nothing. The run takes the shorter way around the
  ring, unless the key path check below needs the other way. If the list is
  already on the target, `set` sends nothing and exits with code 0 and `keys` 0.
  A `--from` that equals the target is remembered as stated.
- `--from` on a readable setting is a usage error.
- In `--json` the `data` holds `setting`, `value` (a number for a slider, the
  option name for a list), `verified` (always `false`) and `keys`, the number of
  keys sent.

**Key path check (exit code 3).** A key press can get lost (measured: about 1 %
at most). So the run never enters the Misc page, and before any key is sent the
tool simulates it once for every key with that one key lost. A run in which a
lost key would press `select` on Reset, or would reach a key whose effect is not
measured, is not sent; for a list the other way around the ring is tried first.
If no way passes, `set` exits with code 3 and sends nothing.

**Collateral check (exit code 6).** If a readable setting differs after the run,
the keys did not do what was planned (for example `dcr` was stated as `on` while
the display has it off, so `set effect` edits Contrast). The tool then writes
the snapshot back in the order of `profile apply` (preset, gains, the rest),
reads it back, leaves the remembered value of the setting forgotten and exits
with code 6. The message names each changed value, what it was and became, and
whether it is restored. The check sees a miss only when it changes a readable
setting; a miss that changes another menu-only setting goes unseen.

### Remembered values

The display cannot report a menu-only value, so the tool remembers the last one
it knows in `$XDG_STATE_HOME/corsair-xeneon-edge-ctl/osd-values.json`
(`~/.local/state` when unset). Each value records how it became known: `set` (a
completed `set` run wrote it) or `stated` (a person gave it with `--from`, or
with `--dcr` on `osd open` and `osd interactive`). A missing or unreadable file
means every value is unknown.

`get <menu-only setting>` prints the remembered value and reads nothing from the
display. In `--json` the `data` holds `setting`, `value`, `verified` (always
`false`) and `known_by` (`set` or `stated`); an unknown value has `value` and
`known_by` `null`. The exit code is 0 either way. In text mode the bare value
(`unknown` when there is none) goes to stdout and a warning to stderr.

```text
$ xeneon-edge-ctl get effect
game
warning: effect: remembered value (set), not read from the display
```

A value is forgotten when a `set` run of that setting starts, when `osd key` or
`osd interactive` keeps an edit of that entry (`select` while editing), for all
settings after a confirmed Reset, and when the collateral check finds a
difference. A lost key press is not detected: a list value can end one step off
and the remembered value is then wrong without any sign. `--from` corrects it.

`dcr` also decides where entering Picture lands. `set dcr` updates the DCR
assumption of the tracked menu state with the value it wrote. `osd open` and `o`
in `osd interactive` take the remembered `dcr` value when `--dcr` is not given
(off when it is unknown); an explicit `--dcr` wins and is remembered as stated.
A `set` run assumes the same DCR value as `osd open` would.

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
xeneon-edge-ctl osd interactive
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
the remembered `dcr` value (off when it is unknown) unless you say
`osd open --dcr on` or `--dcr off`, see
[Remembered values](#remembered-values).

### Commands

- `osd open [--dcr on|off]` presses `back` three times and then `select`, so
  the menu is open on Picture whatever state it was in. It always works, starts
  a new tracked state and clears the Reset mark (see below). Without `--dcr` it
  assumes the remembered `dcr` value, else off; an explicit `--dcr` wins and is
  remembered as stated.
- `osd key <select|left|right|back> [--repeat N] [--confirm-reset]` presses
  the key once, or `N` times (1 to 50; anything else is a usage error, exit
  code 2). The tool checks every step of a `--repeat` run before it sends the
  first key; if a step is refused, nothing is sent. The output names the keys
  sent and the cursor afterwards. A `select` that keeps an edit of a menu-only
  entry makes the tool forget that setting's remembered value.
- `osd where` prints the tracked state: the menu level, the page and entry the
  cursor is on, the DCR assumption, whether the cursor has been on Reset, and
  whether the state is still known (if not, why). It sends nothing, takes no
  lock and never changes the state file.
- `osd close` presses `back` three times, so the menu is closed. It always
  works and clears the Reset mark.

Keys are sent at least 150 ms apart, also across commands: a command whose
first key would follow the previous key (the time is in the state file) sooner
waits for the rest of the 150 ms. `osd open`, `osd key` and
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
  over. Every remembered menu-only value is forgotten, because the display went
  back to its own defaults.

### Interactive mode

`osd interactive [--dcr on|off]` reads single key presses from the terminal and
turns each into one `osd` action, so you can work the menu without typing a
command per key. It uses the same state file, planning, writer lock and Reset
rules as `osd open`, `osd key` and `osd close`; the lock is taken for each key
press, so another command can run between two presses.

| Terminal key | Action |
|---|---|
| Left arrow, right arrow | `osd key left`, `osd key right` |
| Enter | `osd key select` |
| Escape, Backspace | `osd key back` |
| `o` | `osd open`, with the `--dcr` value the mode was started with (without it, the remembered `dcr` value, else off) |
| `q` | `osd close`, then leave the mode (exit code 0). If the close fails, the error is shown and the mode keeps running. |
| Ctrl-C | Leave the mode without sending a key (exit code 0). |

Every other key is ignored: the up and down arrows, other letters (also `Q` and
`O`), Alt combinations and sequences the tool does not know.

- It needs a terminal. When stdin or stdout is not a terminal, or with `--json`,
  it exits with code 2 and does nothing.
- It sends no key on start (an explicit `--dcr` is remembered at the start, under
  the writer lock, so a busy lock exits with code 7 before the terminal is
  touched). A status line shows the tracked position (page,
  entry, `(editing)`), the DCR assumption and, when the cursor is on Reset or has
  been on it since the menu was opened, a warning. The line is redrawn after
  every action, and by itself when the tracked position stops being trustworthy
  (the last key is more than 8 seconds old); then it says so and `o` starts over.
- A refused action sends nothing and shows the reason in the status line. An
  error that is not a refusal (I/O, display not found, busy lock) is shown the
  same way. The mode keeps running in both cases.
- `Enter` where `osd key select` would need `--confirm-reset` asks: "Reset the
  display? Type RESET and press Enter to send, anything else cancels." Only the
  exact text `RESET` sends the key with the confirmation; anything else, an empty
  line or Ctrl-D cancels and sends nothing. The 8-second rule applies when the
  answer arrives. Typing `RESET` is the same decision as `--confirm-reset`: do it
  by hand only, never from a script or an automated task.
- Escape and the arrow keys start with the same byte, so a lone Escape is
  recognised when no further byte arrives within 50 ms.
- Terminal input that arrives while an action is being carried out is discarded,
  whether the action succeeds, is refused or fails. A held-down arrow key presses
  at most one key per action, and nothing keeps going after you release it.
- The terminal settings are restored when the mode ends, also after an error or
  Ctrl-C. Keys typed ahead before the mode starts, or before and during the Reset
  question, are dropped and never send a key. If the terminal goes away, the mode
  ends without sending a key and exits with code 0.

```text
$ xeneon-edge-ctl osd interactive
Left/Right arrows move, Enter selects, Esc or Backspace goes back, o opens the menu, q closes it and quits, Ctrl-C quits without sending a key.
menu: Misc > Reset | DCR assumed: off | WARNING: the cursor is on Reset; Enter there needs a typed confirmation and resets the display
```

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
| 3 | Refused by a safety rule (also: no key path for a menu-only `set` passes the lost-key check, or a list has no known current value) |
| 4 | Display not found or no access |
| 5 | I/O or protocol error |
| 6 | Value read back differs (also: a menu-only `set` changed a readable setting) |
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
| `$XDG_STATE_HOME/corsair-xeneon-edge-ctl/osd-values.json` | Remembered values of the menu-only settings, replaced atomically, see [Remembered values](#remembered-values) |
