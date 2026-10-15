# Design

Status: draft for version 0.1. Nothing here is implemented yet.

## Contents

- [Scope](#scope)
- [Channels](#channels)
- [Settings in version 0.1](#settings-in-version-01)
- [Architecture](#architecture)
- [Command-line contract](#command-line-contract)
- [Single writer](#single-writer)
- [Profiles](#profiles)
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
| OSD key navigation | HID key presses through the on-screen menu | settings without HID or DDC/CI access (later version) |

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
HID and labels DDC/CI gain values as "stored user gains".

Settings that exist only in the on-screen menu (effect, saturation, low blue
light, aspect, DCR, overdrive, audio, menu time, OSD position) follow in a later
version as "written, not verified" values.

## Architecture

Dependencies point downwards; only `cli` knows about argument parsing and
output formats.

```text
cli ──► profile ──► state ──► model ──► transport.hid
   └──► lock                         └► transport.ddc
osd (later) ──► transport.hid
```

- `transport.hid`: finds the hidraw node by vendor and product ID, sends and
  receives reports, refuses every frame outside an allowlist.
- `transport.ddc`: runs `ddcutil` as a subprocess; finds the bus by the monitor
  identity `CRX:XENEON EDGE` from `ddcutil detect --brief`, never by number.
- `model`: the table above as data — which channel writes and reads each
  setting, its range, and whether a write can be verified.
- `state`: reads settings from the device; never caches.
- `profile`: saves and applies profiles (see [Profiles](#profiles)).
- `lock`: the single-writer lock (see [Single writer](#single-writer)).
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

`--json` prints exactly one JSON object on stdout:

```json
{"ok": true, "command": "get", "data": {"setting": "contrast", "value": 50}, "error": null}
```

On failure `ok` is `false`, `data` is `null` and `error` holds `code` and
`message`. The JSON shape of every command is fixed by golden tests.

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

## Safety

The Xeneon Edge has no buttons. A command that switches it off or resets it can
only be undone through software or by unplugging it. The tool therefore refuses:

| Command | Reason |
|---|---|
| HID `0x0F FF` | the vendor software sends it for its reset function |
| OSD entry "Reset" | factory reset; also the neighbour of "Language" in the menu, so the tool sets the language over DDC/CI only |
| VCP `0x04`, `0x05`, `0x06`, `0x08` | MCCS "restore factory defaults" family |
| VCP `0x60` | input source; no tested way back |

Each refusal has a test that sends the command through the public interface and
checks that nothing reaches the transport.

`blank` writes VCP `0xD6` = `05`. In one test on a DisplayPort connection the
display went dark, kept answering DDC/CI (reading `0xD6` = `04`), and `0xD6` = `01` brought
the picture back. `blank` reads `0xD6` afterwards; if DDC/CI no longer answers,
it sends `01` immediately and fails with exit code 5. The way back over a
USB-C (DisplayPort Alt Mode) connection is not tested yet.

## Testing and acceptance

- Unit tests replace every transport with a fake; no automated test touches a
  device.
- Golden tests fix the JSON output of every command.
- Device acceptance before a release, each with the display state read before
  and restored after:
  - profile round trip, 20 runs without a difference;
  - every refused command is refused;
  - two writers started together: one succeeds, one exits with code 7;
  - `blank`/`unblank` on each connection type that the documentation claims.
