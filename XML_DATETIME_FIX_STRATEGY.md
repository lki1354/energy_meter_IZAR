# Fix Strategy: XML → datetime translation (M-Bus CP32) and file sequence-number rollover

This document is the detailed fix strategy for two related defects discovered in the
[`meter_data_analyse`](https://github.com/lki1354/meter_data_analyse) pipeline that the
`energy_meter_izar` integration (see `STRATEGY.md`) must get right from day one:

1. **Wrong year in decoded timestamps** — sensor data from January 2026 onwards is still
   stamped **2025**, because the XML → datetime translation hardcodes the year.
2. **File sequence-number rollover** — the running number `XXX` in the XML file name
   `0080A3DB81A5_XXX.xml` is a 3-digit counter that **wraps after 999** and starts again
   at a low number, which breaks any "highest number processed" or filename-sorted logic.

---

## 1. Symptoms observed

- `energy_bill_analysis_Januar_2026.py` / `energy_bill_analysis_Q1_2026.py` read parquet
  files (`output_data_*.parquet`) in which readings taken in 2026 carry **2025** dates.
  Filtering on "Januar 2026" therefore returns wrong/empty periods.
- The gateway's file counter reached `999` and restarted; new files now have *smaller*
  numbers than already-processed ones, so number-based "newest file" logic stalls and
  lexicographic sorting (`sorted(glob('*.xml'))`) misorders the batch across the wrap.

## 2. Root-cause analysis (`meter_data_analyse`)

### 2.1 The year is hardcoded in the production decoder

`playground.py::decode_timestamp` — the function used by `xml_data_pars` to translate every
`<MBTIME>` in the XML into the reading timestamp that ends up in the parquet files — decodes
minute/hour/day/month from the CP32 bit fields but then does:

```python
# Jahr ist in den Beispielen konstant 2025 …
year = 2025
```

Every reading, regardless of when it was taken, is stamped 2025. All downstream notebooks
(`energy_bill_analysis*.py`) consume these parquet files, so the whole 2026 dataset is
mis-dated. Two further latent bugs in the same function:

- `minute = b1` uses the **full first byte**. Bit 7 of that byte is the CP32 **IV (invalid)
  flag** and bit 6 is reserved/DST; an invalid timestamp would decode as minute ≥ 64 and
  crash `datetime()` instead of being detected as invalid.
- No range validation — a corrupt hex string produces a wrong date instead of an error.

### 2.2 The scratch decoders are wrong too

`mbus_date.py` contains two experimental decoders that treat the 4th byte as
*month = lower nibble (BCD), year = upper nibble (BCD)*. That caps the year at 0–9 (or
mis-reads it entirely) and is **not** the EN 13757-3 type F layout — the 7-bit year is
spread **across bytes 3 and 4**. These functions must not be ported.

### 2.3 Sequence number is a 3-digit wrapping counter

Observed file range in the archive was 642–789; the counter has since passed 999 and
restarted. The strategy in `STRATEGY.md` §2.2 originally proposed persisting "the highest
processed sequence number" as the exactly-once high-water mark — that design breaks at the
first wrap (every post-wrap file compares lower and would be skipped forever).

## 3. Correct CP32 (EN 13757-3 type F) decoding

### 3.1 Bit layout

4 bytes, transmitted low byte first (`<MBTIME>00202637</MBTIME>` → B1=`0x00`, B2=`0x20`,
B3=`0x26`, B4=`0x37`). All fields are **binary**, not BCD:

| Byte | Bits 7..5 | Bits 4..0 |
|---|---|---|
| B1 (min)   | bit 7 = **IV** invalid flag, bit 6 = reserved/DST | bits 5..0 = **minute** (0–59) |
| B2 (hour)  | bits 6..5 = **hundred-year** (0–3, optional)      | bits 4..0 = **hour** (0–23) |
| B3 (day)   | bits 7..5 = **year, low 3 bits**                  | bits 4..0 = **day** (1–31) |
| B4 (month) | bits 7..4 = **year, high 4 bits**                 | bits 3..0 = **month** (1–12) |

```python
minute = b1 & 0x3F
hour   = b2 & 0x1F
day    = b3 & 0x1F
month  = b4 & 0x0F
year   = ((b3 & 0xE0) >> 5) | ((b4 & 0xF0) >> 1)   # 7 bits, 0–127
hundred_year = (b2 & 0x60) >> 5                     # 0–3, not always used
invalid      = bool(b1 & 0x80)
```

### 3.2 Century resolution

The 7-bit year alone is ambiguous (0–99 by convention). Verified against the real data:

- The **gateway header** `MBTIME` (`00202637`) sets `hundred_year = 1`
  → `year = 1900 + 100·1 + 25 = 2025`. The gateway uses the EN 13757-3:2018 hundred-year
  extension.
- Timestamps **inside the meter telegrams** (e.g. the `04 6D` date/time record
  `0A 00 26 37`) leave `hundred_year = 0` and rely on the classic heuristic.

Rule to implement (handles both producers):

```python
if hundred_year:
    full_year = 1900 + 100 * hundred_year + year
else:
    full_year = 2000 + year if year <= 80 else 1900 + year   # libmbus convention
```

This decodes the year **from the data**, never from the wall clock. Do *not* infer the year
from the processing date: files are fetched in batches, so December-2025 readings ingested
in January 2026 must still decode as 2025 — only the CP32 bit fields can guarantee that
across the year boundary.

### 3.3 Verified test vectors

| Hex | Decoded | Note |
|---|---|---|
| `00202637` | 2025-07-06 00:00 | gateway MBTIME, hundred-year bits set |
| `2D282537` | 2025-07-05 08:45 | slot MBTIME |
| `1E2F2537` | 2025-07-05 15:30 | slot MBTIME |
| `0A002637` | 2025-07-06 00:10 | `04 6D` record inside telegram, hundred-year = 0 |
| `3B173F3C` | 2025-12-31 23:59 | year-rollover, minute before |
| `00004131` | 2026-01-01 00:00 | year-rollover, minute after — **year advances to 2026** |
| `00204131` | 2026-01-01 00:00 | real gateway MBTIME of file `_844` (hundred-year bits set) |
| `00204736` | 2026-06-07 00:00 | real gateway MBTIME of post-wrap file `_001` |
| `1E084F31` | 2026-01-15 08:30 | representative 2026 value |
| `80xxxxxx` | *invalid* | IV bit set → reject, don't emit a reading |

### 3.4 Validated against real multi-year gateway files

The decoder was run over seven real snapshot files spanning the year boundary and the
counter wrap (4,123 `<MEM>` slots total, **zero decode errors**):

| File seq | Gateway `MBTIME` | Decoded | Confirms |
|---|---|---|---|
| `_837` | `0020393C` | 2025-12-25 | pre-rollover baseline |
| `_843` | `00203F3C` | 2025-12-31 | last 2025 file |
| `_844` | `00204131` | **2026-01-01** | year advances between consecutive files |
| `_856` | `00204D31` | 2026-01-13 | 2026 continues correctly |
| `_999` | `00204536` | 2026-06-05 | last file before counter wrap |
| `_000` | `00204636` | **2026-06-06** | counter restarts at `000`, time keeps increasing |
| `_001` | `00204736` | 2026-06-07 | post-wrap sequence continues |

Every gateway and slot `MBTIME` in these files carries the hundred-year bits
(`hy = 1` → 1900 + 100 + yy); only the `04 6D` records inside the meter telegrams rely on
the ≤ 80 heuristic. Each daily file holds ~589 slots covering a full day of readouts, and
ordering the files by decoded gateway `MBTIME` yields the correct sequence
`… 843 → 844 … 999 → 000 → 001` across both the year boundary and the wrap — confirming
the §5 design.

## 4. Reference implementation for `mbus_parser.py`

```python
def decode_cp32(hex_string: str) -> datetime.datetime | None:
    """Decode an M-Bus CP32 (EN 13757-3 type F) timestamp, e.g. '00202637'.

    Returns None when the sender flagged the timestamp invalid (IV bit).
    Raises ValueError on malformed input or out-of-range fields.
    """
    if len(hex_string) != 8:
        raise ValueError(f"CP32 must be 4 bytes / 8 hex chars, got {hex_string!r}")
    b1, b2, b3, b4 = (int(hex_string[i : i + 2], 16) for i in range(0, 8, 2))

    if b1 & 0x80:            # IV: sender marked the time invalid
        return None

    minute = b1 & 0x3F
    hour   = b2 & 0x1F
    day    = b3 & 0x1F
    month  = b4 & 0x0F
    year   = ((b3 & 0xE0) >> 5) | ((b4 & 0xF0) >> 1)
    hundred_year = (b2 & 0x60) >> 5

    if hundred_year:
        full_year = 1900 + 100 * hundred_year + year
    else:
        full_year = 2000 + year if year <= 80 else 1900 + year

    return datetime.datetime(full_year, month, day, hour, minute)
```

`datetime()` itself rejects impossible dates (month 0, day 32, hour 31, Feb 30, …), so no
extra range checks are needed beyond the masks.

### 4.1 Cross-validation (defense in depth)

Each `<MEM>` slot carries the same instant twice: the slot `<MBTIME>` and the `04 6D`
date/time record inside `<MBTEL>` (decoded independently by `pymeterbus`). The parser
should compare the two and log a warning when they diverge by more than a configurable
tolerance — this would have caught the hardcoded-2025 bug immediately in January 2026.
Measured on the real files from §3.4, meter clocks run up to **1 h 41 min** ahead of the
slot `MBTIME` (poll latency + meter clock drift), so the default tolerance must be ~2 h,
not minutes; the check targets gross errors (wrong day/month/year), which is exactly the
failure mode of the original bug. Additionally,
reject readings whose timestamp is > 24 h in the future relative to the gateway header
`MBTIME` of the same file (clock-corruption guard).

## 5. Sequence-number rollover strategy

**Principle: the 3-digit file counter is an identifier, never a global ordering or
progress key.** Once CP32 decoding is correct, the authoritative order of readings is the
**decoded gateway `MBTIME`** inside each file — a real timestamp that survives any counter
wrap.

Changes to `STRATEGY.md` §2.2 (ingestion / exactly-once):

1. **Ordering** — process fetched files ordered by decoded gateway `MBTIME`, *not* by
   filename. Never sort lexicographically across a batch, and never trust filesystem
   metadata: file mtime is rewritten by every copy/transfer hop and file sizes are near
   identical (verified on real files: seven snapshots spanning Dec 2025 – Jun 2026 all
   arrived with one and the same mtime, `2026-07-08 09:15`, and identical byte size).
   The readout time exists **only inside the file content**.
2. **Progress tracking** — replace the single "highest sequence number" high-water mark
   with, persisted in the HA `Store`:
   - `last_readout_time`: gateway `MBTIME` of the newest ingested file (drives "is this
     file new?"),
   - `recent_files`: a bounded map `{filename: gateway_mbtime}` of the last ~2 counter
     periods (~2000 entries) to make re-listing idempotent even when a wrapped counter
     reuses a name like `0080A3DB81A5_042.xml` for *new* content — same name, different
     decoded gateway `MBTIME` ⇒ re-ingest. (Content identity, not mtime/size, for the
     reason in point 1; deciding requires downloading the file, which is acceptable at
     ~220 KB per candidate and only occurs for names not seen with the same timestamp.)
3. **Wrap detection for gap reporting only** — sequence numbers remain useful to detect
   *missed* files within one counter period. Treat the counter as modulo-1000: a jump from
   `n` to `m` is a wrap when `(m - n) % 1000` is small and `m < n` (e.g. `999 → 000` or
   `997 → 003` with files lost across the wrap). Maintain a monotonic epoch counter and
   compare `(epoch, seq)` pairs; report gaps as repair issues, but never use them to skip
   or order data.
4. **Deduplication at the reading level** — the reading store keys on
   `(device_number, quantity, timestamp)` with upsert semantics, so re-processing a file
   (after restore, wrap confusion, or manual backfill) can never double-count.

## 6. Data repair (`meter_data_analyse`)

The parquet intermediates (`output_data_*.parquet`) contain 2026 readings stamped 2025:

1. Fix `playground.py::decode_timestamp` (done on this branch — see the companion commit in
   `meter_data_analyse`): decode the year from the CP32 bit fields per §3, mask the minute
   byte, and reject IV-flagged values.
2. **Re-parse the XML archive** with the fixed decoder and regenerate all parquet files.
   Re-parsing is preferred over patching the parquet in place, because the wrong rows
   cannot be told apart by value: 2026-01 readings collide with genuine 2025-01 readings
   on identical dates.
3. Re-run the affected notebooks (`energy_bill_analysis_Januar_2026.py`,
   `energy_bill_analysis_Q1_2026.py`) and diff the results against previously exported
   bills; any bill that mixed mis-dated rows must be reissued.
4. Delete/ignore the misleading decoders in `mbus_date.py` (BCD year interpretation) or
   mark them clearly as non-normative scratch code.

## 7. Test plan

Unit tests in `tests/test_mbus_parser.py` (phase 1 of the roadmap):

- All vectors from §3.3, including both century paths (hundred-year set / heuristic).
- **Year rollover**: consecutive readings `2025-12-31 23:59` → `2026-01-01 00:00` decode
  with increasing timestamps (regression test for the observed bug).
- Leap day `2028-02-29` decodes; `2027-02-29` raises.
- IV bit set → `None`; wrong length / non-hex → `ValueError`.
- Cross-check: for the committed fixture `0080A3DB81A5_665.xml`, every slot's decoded
  `<MBTIME>` matches the `04 6D` record decoded by `pymeterbus` within tolerance.
- **Sequence wrap**: ingestion simulation over filenames `…_998, _999, _000, _001` with
  advancing gateway MBTIMEs → all four ingested exactly once, in time order; a re-listed
  `_000` with unchanged mtime/size is skipped; `_000` reappearing later with a new
  mtime/size (next wrap) is ingested again.
- Gap detection across the wrap: `_997 → _003` reports 5 missing files, not 994.

## 8. Rollout

1. `meter_data_analyse`: decoder fix + regenerate parquet + re-validate 2026 notebooks (§6).
2. `energy_meter_IZAR`: implement `decode_cp32` + wrap-aware ingestion exactly as specified
   here in phase 1/2 of the `STRATEGY.md` roadmap, with the §7 tests as the merge gate.
