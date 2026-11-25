# Stage 3: `.goo` sliced-file format (as written by SatelLite 1.0.2.43)

Source: `EleImageApp.dll` (SHA-256 `3e41a7cb…943c9`), analysed with Ghidra 12.2 through REA.
The evidence bundle is `evidence/eleimageapp-goo.json`.
Tags: [OBSERVED] means seen in decompiled code. [INFERRED] means deduced from it. [PUBLIC-SPEC] means the field name comes from ELEGOO's published GOO document, matched by offset.
[UNKNOWN] means not established yet. [SAMPLE] means confirmed against `samples/10mmcube.goo` (SatelLite 1.0.2.43, Saturn 4 Ultra 16K, 200 layers, 738 288 bytes).

## 1. File-level structure
V3.0 layout [SAMPLE]:
```
[ header        0x000000 .. 0x02FB95)   195 477 bytes, all fields big-endian
[ layer × N     starts at LayerDefAddress = 0x2FB95 ]
    64 B  layer definition (§2b)
     2 B  0D 0A
     4 B  u32 BE  image length L
     L B  RLE image (0x55 … checksum, §5)
     2 B  0D 0A
[ 11 B ending   00 00 00 07 00 00 00 44 4C 50 00 ]
```
A V3.0 file has no extra file checksum and no V5 extension area. The V5 extras (§4) appear only when SatelLite exports V5.x.

**How the app writes the file.** The writer builds the header in a zeroed 0x2FBA4-byte struct (`FUN_1800e7760`, called from the driver `FUN_1800e8de0`). It then writes through a temp file + `CreateFileMapping`/`MapViewOfFile`, appends a checksum, and renames the temp file into place. [OBSERVED strings and calls]

**Byte order on disk: big-endian [SAMPLE].** For example, LayerCount reads 200 and LayerDefAddress reads 0x2FB95 only as BE. The native-order struct I saw in `FUN_1800e7760` must be byte-swapped on a later serialisation path that I did not trace. [INFERRED]

**Stale bytes in string slots [SAMPLE].** After the NUL terminator, the fixed-size string fields contain leftover data, for example UTF-16 `t\0e\0l\0L\0…` after "ELEGOO SatelLite". The clean-room writer zero-pads new strings. The reader keeps the raw slots so that a parsed file re-serialises byte-identically.

## 2. Header layout [OBSERVED offsets from `FUN_1800e7760`]
| Offset | Size | Field | Source / value |
|---|---|---|---|
| 0x0000 | 4 | Version | `"V%1"` from the export settings; if that string is not 4 bytes long the writer substitutes `"V5.1"` |
| 0x0004 | 8 | Magic | `07 00 00 00 44 4C 50 00` ("DLP") |
| 0x000C | 32 | SoftwareName | `applicationName()`, truncated to 31 bytes, NUL-terminated |
| 0x002C | 24 | SoftwareVersion | `applicationVersion()` |
| 0x0044 | 24 | FileCreateTime | `yyyy-MM-dd hh:mm:ss`, or a caller-supplied value |
| 0x005C | 32 | MachineName | [PUBLIC-SPEC] |
| 0x007C | 32 | MachineType | platform+400, the model string (e.g. "ELEGOO Jupiter 2") |
| 0x009C | 32 | ResinProfileName | platform+0x538 |
| 0x00BC | 2 | AntiAliasingLevel | from a lookup table {0→0, 1→2, 2→4, 3→8} [OBSERVED table; mapping INFERRED]. Images stay binary; any anti-aliasing happens on the printer's LCD [USER-REPORTED]. Whether the printer reads this field is [UNKNOWN] |
| 0x00BE | 2 | GreyLevel | |
| 0x00C0 | 2 | BlurLevel | |
| 0x00C2 | 116·116·2 | SmallPreview | RGB565 (§3) |
| 0x69E2 | 2 | delimiter | `0D 0A` |
| 0x69E4 | 290·290·2 | BigPreview | RGB565 |
| 0x2FAEC | 2 | delimiter | `0D 0A` |
| 0x2FAEE | 4 | LayerCount | int |
| 0x2FAF2 | 2 | ResolutionX | |
| 0x2FAF4 | 2 | ResolutionY | |
| 0x2FAF6 | 1 | MirrorX | |
| 0x2FAF7 | 1 | MirrorY | |
| 0x2FAF8 | 4×4 | DisplayWidth, DisplayHeight, MachineZ, LayerHeight | float [PUBLIC-SPEC names] |
| 0x2FB08 | 4 | ExposureTime | float |
| 0x2FB0C | 1 | DelayMode | always 1 [OBSERVED] |
| 0x2FB0D | 4 | LightOffDelay | float |
| 0x2FB11 | 6×4 | Bottom/normal WaitAfterCure, WaitAfterLift, WaitBeforeCure | floats; used in the print-time formula `FUN_18000cf70` |
| 0x2FB29 | 4 | BottomExposureTime | float |
| 0x2FB2D | 4 | BottomLayers | int, `min(setting, LayerCount)` [OBSERVED] |
| 0x2FB31 | 16×4 | Lift/retract parameters | two stages × {bottom, normal} × {lift, retract} × {height, speed}, all floats [PUBLIC-SPEC names; order INFERRED] |
| 0x2FB71 | 2 | BottomLightPWM | from a double |
| 0x2FB73 | 2 | LightPWM | |
| 0x2FB75 | 1 | AdvanceMode (per-layer settings) | bool |
| 0x2FB76 | 4 | PrintTime (s) | computed ×0.93 in `FUN_18000cf70` |
| 0x2FB7A | 4 | Volume | float |
| 0x2FB7E | 4 | Weight | float |
| 0x2FB82 | 4 | Price | float |
| 0x2FB86 | 8 | PriceUnit | string, 7 bytes + NUL |
| 0x2FB8E | 4 | LayerDefAddress | **constant 0x2FB95** [OBSERVED] |
| 0x2FB92 | 1 | GrayScaleLevel | 0 in the sample [SAMPLE]; the decompiled V5 path writes 1 |
| 0x2FB93 | 2 | TransitionLayers | |

Wait fields at 0x2FB11 [SAMPLE values 0,0,1.2,0,0,1.2]: bottom {before-lift, after-lift, after-retract}, then normal {before-lift, after-lift, after-retract}. These names come from [PUBLIC-SPEC]; the 1.2 s value matches the layer field "after retract".
Lift/retract block at 0x2FB31: the field names follow [PUBLIC-SPEC] order. In the sample the first 8 values are 0.05 and the last 8 are 0, so the order is not proven.

## 2b. Layer definition (64 bytes, BE) [SAMPLE]
| Off | Type | Field | Sample / rule |
|---|---|---|---|
| 0 | u16 | PauseFlag | 0 |
| 2 | f32 | PauseZ | 200.0 |
| 6 | f32 | Z | `(i+1) × LayerHeight`, computed in decimal (all 200 match) |
| 10 | f32 | Exposure | `i < BottomLayers`: bottom exposure. Then `k = i−BottomLayers+1`; for `k ≤ TransitionLayers` it is `bottom − (bottom−normal)·k/T`; after that, normal (all 200 match) |
| 14 | f32 | LightOffTime | 0 |
| 18,22,26 | f32 | Before-lift, after-lift, after-retract wait | 0, 0, 1.2 |
| 30..58 | 8×f32 | Lift dist/speed, 2nd lift dist/speed, retract dist/speed, 2nd retract dist/speed | all 0.05; names [PUBLIC-SPEC] |
| 62 | u16 | LightPWM | 255 |

The motion values in the layer records don't map one-to-one to the header: header "second lift" is 0 while the layer value is 0.05. So the writer copies them from a template layer (`--template`) and computes only Z and exposure.

## 3. Preview encoding [OBSERVED `FUN_1800e8c50`]
1. Scale the source image to 116×116 or 290×290 with `QImage::scaled(..., aspect, SmoothTransformation)`, then convert to ARGB32.
2. Encode each pixel as `v = ((R & 0xF8) << 8) | ((G & 0xFC) << 3) | (B >> 3)`, which is RGB565.
3. Write `v` **high byte first**. The one exception is MachineType `"ELEGOO Jupiter 2"`, which is written low byte first.

## 4. V5 extensions [OBSERVED, partial]
- The 15 bytes after the header hold:
  - 0x2FB95: the byte `2`
  - three u32 offsets: `A`, `A + 8·N + 1`, `A + 8·N + 1 + 16·N + 1`
  - 0x2FBA3: the byte `3`
  - Here N is the layer count and A is a caller argument. They are presumably per-layer tables with 8-byte and 16-byte rows.
- If the export version is `"5.2"`, 4 more bytes are appended (struct size 0x2FBA8). There is also an optional resin-calibration block: `'S'`, count n, n×u32 exposure, n×u32 values, `0D 0A`.
- Extension-table validity checks mention NFC (table id 0x0F) and calibration tables.
- A 0x8E-byte trailer starts with `0x66`. It holds 127 bytes of resin name, 127 bytes of a second string, an RGB colour, a u32, a u32 0 and `0D 0A`. Its exact position is [UNKNOWN].
- **Clean-room decision:** the writer emits a plain V3.0-style file with no extensions until the sample shows what the printer requires.

## 5. Layer image RLE [OBSERVED, decoder `FUN_1800d10f0`]
Each layer image is an 8-bit grayscale bitmap of ResolutionX × ResolutionY pixels, filled row-major. The encoded form is:
```
0x55 ('U') | chunk* | checksum
```
- **Checksum:** `(~ (sum of every byte between 0x55 and the checksum)) & 0xFF`.
  - Every byte of each chunk counts, including value and length bytes.
  - A mismatch is only logged by the decoder.
- **Chunk header H:** `type = H >> 6`, `nib = H & 0x0F`.

| type | pixel value | length encoding |
|---|---|---|
| 0 | 0x00 | `L = (H>>4)&3`: 0 → `nib`; 1 → `b1<<4 \| nib`; 2 → `(b1<<8\|b2)<<4 \| nib`; 3 → `(b1<<16\|b2<<8\|b3)<<4 \| nib` (max 2^28−1) |
| 3 | 0xFF | same as type 0 |
| 1 | next byte `g` (read **before** the length bytes) | same as type 0 |
| 2 | `prev + d`, where `d = nib` and bit 5 (0x20) set means `−nib`; u8 wrap | bit 4 = 0 → length 1; bit 4 = 1 → length = next byte (1..255) |

- "prev" is the value of the previous run. It starts at 0.
- If the decoder runs out of data before the image is full, it zero-fills the remaining pixels.
- If a run would go past width×height+1, it logs an error and stops.

The sample contains only type 0 and type 3 chunks (anti-aliasing off), always with the minimum number of length bytes and never two adjacent runs of the same value. The clean-room encoder reproduces all 200 layers byte-for-byte [SAMPLE].

## 6. Status
| Item | State |
|---|---|
| Byte order | Big-endian [SAMPLE] |
| Layer record and length location | §2b [SAMPLE] |
| File checksum (V3.0) | None, only the 11-byte ending [SAMPLE] |
| Clean-room round trip | `goo.dumps(goo.read(sample)) == sample` [TEST] |
| Pixels → file | identical layer bytes and definitions [TEST] |
| Grey (anti-aliased) runs | Not needed. ELEGOO printers anti-alias at the LCD, not in the file [USER-REPORTED]. Layers are binary 0x00/0xFF (type 0/3 chunks), as in the sample. The type 1/2 codec stays for format completeness only. |
| V5.x extensions (NFC, calibration, 0x66 trailer) | not written [UNKNOWN] |
| Mapping from header motion fields to layer motion fields | unproven; the writer uses a template [UNKNOWN] |
| Printer accepts a Linux-built file | Accepted, started, paused, resumed and stopped at layer 4 (a 10 mm cube, empty vat, 2026-11-23; 02 §6) [RUNTIME]. A complete print with resin: not run yet [UNKNOWN] |

## 7. Linux implementation
- `elegoo_goo/rle.py`: encoder (regex run scan, about 20 ms per 16K layer) and decoder.
- `elegoo_goo/goo.py`: `Header`, `LayerDef`, `read`, `dumps`, and atomic streaming `write`. It writes a temp file and then calls `os.replace`, replacing the app's `CreateFileMapping`/`MoveFile` flow.
- `python3 -m elegoo_goo info|extract|build`. Pillow is needed only for PNG input and output.
