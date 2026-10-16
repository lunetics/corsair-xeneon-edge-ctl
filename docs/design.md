# Design

Status: accepted design for version 0.1. Implemented: `status`, `get`, `set`, `profile save`/`apply`/`list`/`show`, `blank`, `unblank`, `osd open`/`key`/`where`/`close`. Not yet implemented: `osd interactive`. Settings that exist only in the on-screen menu follow in a later version.

## Contents

- [Scope](#scope)
- [Channels](#channels)
- [Settings in version 0.1](#settings-in-version-01)
- [Architecture](#architecture)
- [Command-line contract](#command-line-contract)
- [Single writer](#single-writer)
- [Profiles](#profiles)
- [OSD key control](#osd-key-control)
- [Safety](#safety)
- [Testing and acceptance](#testing-and-acceptance)

## Scope

The tool reads and writes the picture settings of the Corsair Xeneon Edge
(USB `1b1c:1d0d`, screen firmware `20250714V1`) and stores them as profiles. It
offers a command-line interface with a stable JSON output, so other programs can
use it as a dependency.

Not in scope: touch input, dashboards or widgets, a graphical interface,
firmware updates.

## Channels

The display has three control paths. None of them reports changes on its own,
so the tool always reads the current value from the device.

| Channel | Access | Used for |
|---|---|---|
| USB HID (`/dev/hidraw*`) | 64-byte reports; command `0x0E` reads the screen state, `0x0F` writes (group `01` OSD keys, `02` sliders 0–100, `03` RGB gains 0–255) | exact RGB gains, OSD brightness, OSD key presses |
| DDC/CI (`ddcutil`) | MCCS VCP codes over the video cable | backlight, contrast, sharpness, colour preset, OSD language, power mode |
| OSD key navigation | HID key presses through the on-screen menu | working the menu by hand ([OSD key control](#osd-key-control)); settings without HID or DDC/CI access (later version) |

Known traps:

- DDC/CI writes the RGB gains (`0x16`, `0x18`, `0x1A`) in percent (0–100) but
  reads them in 0–255, so a DDC/CI write can only restore a gain within ±1.
  Exact gains go through HID `0x0F 03`.
- The OSD entry "Brightness" is HID `brightness`. The MCCS feature "Brightness"
  (VCP `0x10`) is HID `backlight`.
- VCP `0x52` (active control) echoes the previous reply and does not report
  changes.
- With Dynamic Contrast Ratio (DCR) on, the OSD greys out Brightness and
  Contrast. Entering the Picture page skips them; the arrow keys still move over
  them, and Brightness stays editable. DDC/CI and HID writes keep working.

## Settings in version 0.1

| Setting | Write | Read | Range |
|---|---|---|---|
| `backlight` | DDC/CI VCP `0x10` | DDC/CI or HID `0x0E` | 0–100 |
| `contrast` | DDC/CI VCP `0x12` | DDC/CI or HID `0x0E` | 0–100 |
| `brightness` | HID `0x0F 02` | HID `0x0E` | 0–100 |
| `sharpness` | DDC/CI VCP `0x87` | DDC/CI | 0–4 |
| `preset` | DDC/CI VCP `0x14` | DDC/CI | display presets, `user` = `0x0b` |
| `language` | DDC/CI VCP `0xCC` | DDC/CI | `english` = `0x02` |
| `red`, `green`, `blue` | HID `0x0F 03` | HID `0x0E` (effective gains of the active preset) | 0–255 |

The gains belong to the active colour preset: with the 7500K preset active, HID
`0x0E` reported that preset's gains (104/105/107) while DDC/CI VCP `0x16`,
`0x18`, `0x1A` kept reporting the stored user gains. The tool reads gains over
HID and labels DDC/CI gain values as "stored user gains". With the user preset
active, a gain written over HID also changes the stored user gain that DDC/CI
reports, and writing the user preset again left the gains unchanged (one test).

Settings that exist only in the on-screen menu (effect, saturation, low blue
light, aspect, DCR, overdrive, audio, menu time, OSD position) follow in a later
version as "written, not verified" values.

## Architecture

Dependencies point downwards; only `cli` knows about argument parsing and
output formats.

```text
cli ──► apply ──► profile ──► state ──► model ──► transport.hid
   ├──► power ─────────────────┘                └► transport.ddc
   ├──► lock
   └──► osd ──► transport.hid
```

- `transport.hid`: finds the hidraw node by vendor and product ID, sends and
  receives reports, refuses every frame outside an allowlist.
- `transport.ddc`: runs `ddcutil` as a subprocess; finds the bus by the monitor
  identity `CRX:XENEON EDGE` from `ddcutil detect --brief`, never by number.
- `model`: the table above as data — which channel writes and reads each
  setting, its range, and whether a write can be verified.
- `state`: reads settings from the device; never caches.
- `profile`: stores, lists and validates profile files (see [Profiles](#profiles)).
- `apply`: writes a profile to the display in a fixed order and keeps the journal
  of the last apply.
- `power`: `blank` and `unblank` over VCP `0xD6` (see [Safety](#safety)).
- `lock`: the single-writer lock (see [Single writer](#single-writer)).
- `osd`: the menu model, the tracked cursor state and the key planning (see
  [OSD key control](#osd-key-control)).
- `cli`: commands, `--json` output, exit codes.

## Command-line contract

Command name: `xeneon-edge-ctl`.

| Command | Effect |
|---|---|
| `status` | read every readable setting, plus HID node, DDC/CI bus and firmware |
| `get <setting>` | read one setting |
| `set <setting> <value>` | write one setting, read it back, fail on a mismatch |
| `profile save <name>` | store the current readable settings |
| `profile apply <name>` | write a profile in a fixed order and verify it |
| `profile list` / `profile show <name>` | list or print stored profiles |
| `blank` / `unblank` | power mode off/on via VCP `0xD6` (see [Safety](#safety)) |
| `osd open` / `osd key` / `osd where` / `osd close` / `osd interactive` | work the on-screen menu (see [OSD key control](#osd-key-control)) |

`--json` prints exactly one JSON object on stdout:

```json
{"ok": true, "command": "get", "data": {"setting": "contrast", "value": 50}, "error": null}
```

On failure `ok` is `false`, `data` is `null` and `error` holds `code` and
`message`. For the profile commands `command` names the action, for example
`profile save`. The JSON shape of every command is fixed by golden tests.

| Exit code | Meaning |
|---|---|
| 0 | success |
| 1 | unexpected error |
| 2 | usage error |
| 3 | refused by a safety rule |
| 4 | display not found or no access |
| 5 | I/O or protocol error |
| 6 | value read back differs from the value written |
| 7 | another writer holds the lock |

## Single writer

Every command that writes takes an exclusive `flock` on
`$XDG_RUNTIME_DIR/xeneon-edge.lock` and keeps it until the command ends,
including whole OSD key sequences. If the lock is held, the command exits with
code 7 after a short timeout instead of waiting forever.

## Profiles

Profiles are JSON files in `$XDG_CONFIG_HOME/corsair-xeneon-edge-ctl/profiles/`.
Each value carries its class:

- **verified**: written and read back; any difference fails the apply.
- **written**: the device offers no read path (OSD-only settings, later
  version); the value is marked as assumed.

`profile apply` writes the colour preset before the gains, because the gains
belong to the active preset (see [Settings](#settings-in-version-01)). It writes every value, reads every verifiable value
back, and reports each failed value instead of stopping silently. A journal of
the last apply records what was written and read.

Details of the implementation:

- Names are 1 to 64 letters, digits, `-` or `_`; anything else is a usage error
  (exit 2), so a name cannot leave the profiles directory. `profile save` refuses
  an existing profile unless `--force` is given.
- A profile file is validated completely (version, known setting names, whole
  numbers in range, class `verified`) before the first write; a problem is a
  usage error. A profile may hold a subset of the settings.
- `profile apply` reads the device first and skips values that already match. The
  gains are compared after the preset step, because a preset write can change
  them. Exit code 6 when a value differs after the write, 5 on an I/O error (5
  wins when both occur), 7 when the lock is busy.
- The journal is `$XDG_STATE_HOME/corsair-xeneon-edge-ctl/last-apply.json`
  (`~/.local/state` when unset). Its directory is created before the first write.
  `profile list` and `profile show` read files only; `profile save` reads the
  device and takes no lock, because it writes nothing to the display.

## OSD key control

The `osd` commands press the four OSD keys over HID, so a user can work the
on-screen menu from the computer and watch the result on the display. The
display does not report the menu state, so the tool tracks where the cursor is
and refuses key presses whose effect it cannot predict.

### Keys

A key is a `0x0F` write with group `01` and no value byte:
`01 0F 00 00 00 00 01 <selector>`, the rest zero. Measured on the device:

| Key | Selector | Effect |
|---|---|---|
| `select` | `00` | opens the menu; enters a page; starts editing an entry; keeps an edited value |
| `right` | `01` | next category or entry; while editing, one step up |
| `left` | `02` | previous category or entry; while editing, one step down |
| `back` | `03` | discards an edit; leaves a page; closes the menu; harmless when the menu is closed |

The transport allowlist admits exactly these four frames.

### Menu model

| Page | Entries, in order |
|---|---|
| Picture | Brightness, Contrast, Sharpness, Effect |
| Colour | Temperature, R, G, B, Saturation |
| Display | Low Blue Light, Aspect, DCR, Rotate, OD |
| Audio | Volume, Mute, Audio Source |
| Misc | Language, Menu Time, OSD H Position, OSD V Position, Reset |

- The menu has three levels: pages, entries, editing. Opening always starts on
  Picture; entering a page always starts on its first entry.
- Pages and entries are rings: `left` on the first item goes to the last, `right`
  on the last goes to the first.
- While editing, `left` and `right` change the value at once; `select` keeps it,
  `back` restores the previous value. Both return to the entry.
- With DCR on, entering Picture lands on Sharpness instead of Brightness. The
  DCR state cannot be read; the tool assumes off unless `osd open --dcr on` says
  otherwise.
- The menu closes itself after "Menu Time" (10 to 60 s, 20 s by default) without
  a key press. Keys sent after that land somewhere else.

### Commands

| Command | Effect |
|---|---|
| `osd open [--dcr on\|off]` | `back` three times, then `select`: the menu is open on Picture |
| `osd key <key> [--repeat N] [--confirm-reset]` | press `select`, `left`, `right` or `back` once or `N` times (1 to 50) |
| `osd where` | print the tracked state; sends nothing and takes no lock |
| `osd close` | `back` three times: the menu is closed |

- The tracked state lives in `$XDG_RUNTIME_DIR/xeneon-edge-osd.json`: level, page,
  entry, the DCR assumption, whether the cursor has been on Reset, and the time
  of the last key. It is replaced atomically.
- The state is **unknown** when the menu is tracked as open and the last key is
  more than 8 s old (below the shortest Menu Time), when the file is missing or
  unreadable, and after Reset was selected. It is also unknown after a key press
  failed or a key sequence did not finish, because the key may or may not have
  arrived. `osd key` then refuses with exit code 3 and asks for `osd open`.
  `osd open` and `osd close` always work.
- With the menu tracked as closed, `select` opens it on Picture and `back` does
  nothing; `left` and `right` are refused with exit code 3, because their effect
  on a closed menu is not measured.
- `osd key` plans every step of a `--repeat` run and checks it before the first
  key is sent; a refused step sends nothing.
- Keys within one command are 150 ms apart. Every command that sends keys holds
  the writer lock for the whole sequence.
- The JSON `data` of `osd open`, `osd key` and `osd close` names the keys sent and
  the tracked state afterwards; `osd where` adds whether the state is known and
  its age.

### Reset

"Reset" in the Misc page resets the display; what it resets and whether the
display still answers afterwards is not measured. Moving the cursor onto Reset
is allowed and prints a warning. `select` on Reset sends nothing and exits with
code 3 unless `--confirm-reset` is given.

A lost key press (the measured upper bound is about 1 %) can leave the real
cursor on Reset while the tracked cursor moved on. Therefore, once the tracked
cursor has been on Reset, every `select` in the Misc page needs
`--confirm-reset` until the next `osd open` or `osd close`. After a confirmed
Reset the state is unknown.

### Interactive mode

`osd interactive [--dcr on|off]` reads single key presses from the terminal and
turns each into one `osd` action, so the menu can be worked without typing a
command per key. It uses the same state file, planning, lock and Reset rules as
the single commands; the lock is taken for each key press, not for the session.

| Terminal key | Action |
|---|---|
| Left arrow / right arrow | `osd key left` / `osd key right` |
| Enter | `osd key select` |
| Escape, Backspace | `osd key back` |
| `o` | `osd open` (with the `--dcr` value the mode was started with) |
| `q` | `osd close`, then leave the mode |
| Ctrl-C | leave the mode without sending a key |

- It needs a terminal: when stdin or stdout is not a terminal it exits with code
  2. `--json` is a usage error for this command.
- It does not send a key on start. A status line shows the tracked position
  (page, entry, editing), the DCR assumption and, when the cursor is on Reset or
  has been on it, a warning. The line is redrawn after every action.
- A refused action (unknown state, closed-menu arrow, missing confirmation) sends
  nothing and shows the reason in the status line; `o` recovers from an unknown
  state. Other keys are ignored.
- A `select` that needs `--confirm-reset` asks: "Reset the display? Type RESET
  and press Enter to send, anything else cancels." Only the exact text `RESET`
  sends the key with the confirmation. The 8 s rule still applies when the
  answer arrives.
- An error that is not a refusal (I/O, display not found, busy lock) is shown in
  the status line; the mode keeps running. The terminal settings are restored on
  every way out, including errors and Ctrl-C.
- Escape and the arrow keys share their first byte. A lone Escape is recognised
  when no further byte follows within 50 ms.

## Safety

The Xeneon Edge has no buttons. A command that switches it off or resets it can
only be undone through software or by unplugging it. The tool therefore refuses:

| Command | Reason |
|---|---|
| HID `0x0F FF` | the vendor software sends it for its reset function |
| VCP `0x04`, `0x05`, `0x06`, `0x08` | MCCS "restore factory defaults" family |
| VCP `0x60` | input source; no tested way back |
| VCP `0xD6` with any value but `01` and `05` | only these two are measured; the transport checks the value, not only the code |

Each refusal has a test that sends the command through the public interface and
checks that nothing reaches the transport.

The OSD entry "Reset" is not refused outright: `select` on it needs
`--confirm-reset` (see [Reset](#reset)). "Reset" is the neighbour of "Language"
in the Misc page, so `set language` uses DDC/CI only and never the menu.

`blank` writes VCP `0xD6` = `05` under the writer lock. In one test on a DisplayPort connection the
display went dark, kept answering DDC/CI (reading `0xD6` = `04`), and `0xD6` = `01` brought
the picture back. `blank` reads `0xD6` afterwards; if DDC/CI no longer answers,
it sends `01` immediately and fails with exit code 5, with a message that the
display may need a power cycle. A reading that is not `04` or `05` exits with
code 6 without a recovery write. `unblank` writes `01` and exits with code 6
unless it reads back `01`. The way back over a USB-C (DisplayPort Alt Mode)
connection is not tested yet; the tool does not detect the connector.

## Testing and acceptance

- Unit tests replace every transport with a fake; no automated test touches a
  device.
- Golden tests fix the JSON output of every command.
- Device acceptance before a release, each with the display state read before
  and restored after:
  - profile round trip, 20 runs without a difference;
  - every refused command is refused;
  - two writers started together: one succeeds, one exits with code 7;
  - `blank`/`unblank` on each connection type that the documentation claims;
  - `osd`: open, walk every page and back, edit one value and discard it (the
    value read over DDC/CI stays unchanged), the tracked state matches the
    screen, a key after more than 8 s is refused, `select` on Reset without
    `--confirm-reset` sends nothing. `--confirm-reset` is never used in
    acceptance;
  - `osd interactive`: the same walk with the terminal keys, `q` closes the
    menu, the terminal works normally afterwards. The Reset question is
    answered with anything but `RESET`.
