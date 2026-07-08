# Strategy: Home Assistant Integration for M-Bus Meter Data (FTP → Sensors → Energy Bills)

This document is the implementation strategy for a Home Assistant (HA) custom integration that
automates what the [`meter_data_analyse`](https://github.com/lki1354/meter_data_analyse) repository
does today with manual marimo notebooks:

1. **Fetch** M-Bus gateway XML snapshot files from an **FTP server**
2. **Decode** the raw M-Bus telegrams and expose every meter as **HA sensor entities**
   (Energy-Dashboard compatible)
3. **Generate configurable energy bill exports** (Markdown, CSV, PDF) for arbitrary billing
   periods — replacing the per-quarter copies of `energy_bill_analysis*.py`

---

## 1. Background: what exists today

### 1.1 Data source

A wired M-Bus gateway/concentrator (type `60M`, unit ID `0080A3DB81A5`, M-Bus device id
`44610100`) polls **19 meters** in a 3-apartment building and writes XML snapshot files:

- File naming: `0080A3DB81A5_XXX.xml` (`XXX` = sequential readout number), with a matching
  `0080A3DB81A5_XXX.rdy` marker file written when the readout is complete.
- XML structure per file:

```xml
<HC2XML>
  <UNIT>            <!-- gateway metadata: TYPE, MBDEVICEID, MBTIME, IMBUS/VMBUS, IPADDR … -->
  <MEM>             <!-- one child per sensor slot -->
    <T0000>
      <MBTIME>00202637</MBTIME>   <!-- 4-byte M-Bus CP32 (type F) timestamp, hex -->
      <ALMSTAT>00</ALMSTAT>       <!-- alarm status -->
      <MBTEL>68C4C468…16</MBTEL>  <!-- full raw M-Bus telegram, hex (0x68 … 0x16) -->
    </T0000>
    <T0001>…</T0001> … <T000A+>
  </MEM>
</HC2XML>
```

Each `<MBTEL>` is decoded with the **`pymeterbus`** library (`meterbus.load(...)`); the meter
serial number comes from the telegram body header (`telegram.body.bodyHeader.id_nr`) and is
matched against the known device inventory.

### 1.2 Device inventory

| Location | Electricity (FIN) | Heat (SON) | Hot water (WZG) | Cold water (WZG) |
|---|---|---|---|---|
| EG (ground floor) | 11601997 | 2800001 | 2800002 | 2800003 |
| 1.OG (1st floor) | 11601989 | 2801004 | 2801005 | 2801006 |
| DG (attic) | 11601959 | 2801007 | 2801008 | 2801009 |
| UG (basement) | — | 2891010 (storage heating), 2891011 (storage hot water) | 2891012 | 2891013 |
| Allgemein (common) | 11601990 | — | — | — |
| Wärmepumpe (heat pump) | 11601992 | — | — | — |
| Photovoltaik | 11601893 | — | — | — |

Measured quantities per telegram: energy (Wh), volume (m³), power (W), voltage/current per
phase, flow/return temperature (°C), flow rate (m³/h), on-time (s), plus historical monthly
reference readings on the water meters.

### 1.3 Billing logic to be ported (from `energy_bill_analysis.py`)

- **Tariff assignment** (Swiss Hoch-/Niedertarif):
  - Hochtarif: Mon–Fri 07:00–20:00, Sat 07:00–13:00
  - Niedertarif: daily 20:00–07:00, and Sat 13:00 → Mon 07:00
- **Consumption** = diff of the cumulative counter (`Energie 1 (Wh)`) per device; negative
  diffs (meter reset) and nulls are dropped.
- **PV allocation** per timestamp: `pv_factor = min(PV, house) / house`; each consumer splits
  into grid share `consumption × (1 − pv_factor)` and PV share `consumption × pv_factor`.
- **Cost** = grid-kWh × tariff price + PV-kWh × PV price
  (reference prices Q3/2025: HT 0.2218, NT 0.2028, PV 0.088 CHF/kWh).
- **Common electricity** (Allgemein) cost is split **1/3** to each apartment.
- **Heat pump split** (per VEWA): heat-pump electricity cost is split heating vs. hot water by
  the reservoir energy ratio of devices 2891010/2891011 (fallback **70/30**); heating cost is
  then allocated proportionally to each apartment's heat meter (Wh), hot-water cost
  proportionally to its hot-water meter (m³).

Everything above is currently **hardcoded** (prices, date windows, device maps) — each quarter
is a copied notebook. The integration makes all of it configuration.

### 1.4 Known bugs to fix during the port

Two confirmed defects — detailed analysis, verified decoding spec, reference implementation,
and test vectors in **[`XML_DATETIME_FIX_STRATEGY.md`](XML_DATETIME_FIX_STRATEGY.md)**:

1. `playground.py::decode_timestamp` hardcodes **year = 2025** when decoding the CP32
   `MBTIME` — all 2026 readings were mis-dated to 2025. The parser must decode the year
   from the CP32 type-F bit fields
   (`year = ((byte3 & 0xE0) >> 5) | ((byte4 & 0xF0) >> 1)`, hundred-year in bits 5–6 of
   the hour byte), mask the minute byte, and honor the IV (invalid) flag.
2. The file counter `XXX` in `0080A3DB81A5_XXX.xml` is a **3-digit counter that wraps
   after 999** and restarts low — sequence numbers must never be used as a global
   ordering or progress key (see §2.2).

---

## 2. Architecture: a HACS-installable custom integration

A single **custom integration** (`custom_components/energy_meter_izar/`) — *not* an add-on:
no extra container, fully configurable from the HA UI, installable/updatable via HACS.

```
FTP server ──(poll)──► FTP Fetcher ──► M-Bus Parser ──► Local reading store (SQLite)
                            │                │                    │
                    DataUpdateCoordinator    ├──► Sensor entities (live values)
                            │                └──► External statistics (backfilled history)
                            │                                     │
                            └────────── Billing engine ◄──────────┘
                                             │
                                  generate_bill service → MD / CSV / PDF
```

### 2.1 Config flow (UI setup)

Connection settings collected in a standard config flow, options editable later:

- Host, port, username, password
- Protocol: **FTP / FTPS** (via `aioftp`) or **SFTP** (via `asyncssh`)
- Remote directory and file pattern (default `0080A3DB81A5_*.xml`)
- Poll interval (default 15 min)
- Require matching `.rdy` marker before ingesting a file (default on)
- After processing: leave files on server (default) or delete them

### 2.2 FTP fetcher + coordinator

- A `DataUpdateCoordinator` drives the poll loop; all network I/O async.
- **Exactly-once ingestion, wrap-aware** (the filename counter wraps after 999, so "highest
  processed sequence number" is *not* a valid high-water mark — see
  [`XML_DATETIME_FIX_STRATEGY.md`](XML_DATETIME_FIX_STRATEGY.md) §5): files are ordered and
  gated by their decoded **gateway `MBTIME`**; a HA `Store` (`.storage/energy_meter_izar`)
  persists the last readout time plus a bounded map of recently processed files
  (`filename → mtime/size`) so restarts never re-process files, wrapped-counter name reuse
  is re-ingested, and gaps are backfilled when older files appear. The reading store
  dedupes on `(device, quantity, timestamp)`, making retries idempotent.
- Retry with exponential backoff on connection errors; the config entry goes to
  `ConfigEntryNotReady`/reauth flows on persistent failures.

### 2.3 M-Bus parser module

A **pure-Python module** (no HA imports → unit-testable standalone), ported from
`meter_data_analyse/playground.py::xml_data_pars`:

- Parse `HC2XML` → iterate `<MEM>` slots → decode `<MBTEL>` with `pymeterbus`
- Decode `<MBTIME>` with a **correct CP32 decoder** (§1.4)
- Map `id_nr` → device number → meter class + location from the configured device map (§3.1)
- Emit typed reading records: `(device_number, medium, timestamp, quantity, value, unit, status)`

### 2.4 Sensor entities

One **HA device** per physical meter, assigned to HA areas (EG, 1.OG, DG, UG, …):

| Entity | device_class | state_class | Unit |
|---|---|---|---|
| Energy total | `energy` | `total_increasing` | kWh |
| Water / heat volume | `water` / `volume` | `total_increasing` | m³ |
| Power | `power` | `measurement` | W |
| Flow / return temperature | `temperature` | `measurement` | °C |
| Flow rate | `volume_flow_rate` | `measurement` | m³/h |

Diagnostic entities: per-meter alarm status (`ALMSTAT`), gateway bus voltage/current, last
processed file number, last successful poll.

`total_increasing` energy/water sensors plug directly into the **HA Energy Dashboard**.

### 2.5 Historical / backfilled data — a key design point

FTP files arrive in batches carrying **past timestamps**; HA sensor states cannot be backdated.
Therefore:

- The **sensor entities** always show the *latest* reading only.
- All interval data is additionally imported as **long-term external statistics** via
  `async_add_external_statistics` (the pattern used by HA's `opower` integration), with
  statistic IDs like `energy_meter_izar:11601997_energy`. The Energy Dashboard then shows
  historically correct hourly data even when files arrive hours or days late.

### 2.6 Raw reading store

The coordinator appends every decoded reading to a **local SQLite database** in the config
directory (`/config/energy_meter_izar/readings.db`). Rationale: bill generation must query
arbitrary past periods, independent of HA recorder purge settings. This replaces the
`output_data_*.parquet` intermediates of the notebook pipeline.

---

## 3. Configurable energy bill exports

### 3.1 Billing configuration

Billing is driven by a YAML config (`/config/energy_meter_izar/billing.yaml`, with UI options
for the common fields). Everything hardcoded in the notebooks becomes configuration. Example
mirroring the current building:

```yaml
currency: CHF
units:                              # billable units (apartments)
  EG:   { electricity: 11601997, heat: 2800001, hot_water: 2800002, cold_water: 2800003 }
  1.OG: { electricity: 11601989, heat: 2801004, hot_water: 2801005, cold_water: 2801006 }
  DG:   { electricity: 11601959, heat: 2801007, hot_water: 2801008, cold_water: 2801009 }

shared:
  common_electricity: { device: 11601990, split: equal }   # Allgemein ÷ N units
  heat_pump:
    device: 11601992
    split_method: reservoir_ratio          # heating vs. hot-water share
    reservoir_heating: 2891010
    reservoir_hot_water: 2891011
    fallback_heating_share: 0.70
  photovoltaic: { device: 11601893, allocation: proportional }  # per-timestamp pv_factor

tariffs:
  - name: hochtarif
    price_kwh: 0.2218
    schedule:                              # anything not matched falls into the next tariff
      - { days: [mon, tue, wed, thu, fri], from: "07:00", to: "20:00" }
      - { days: [sat], from: "07:00", to: "13:00" }
  - name: niedertarif
    price_kwh: 0.2028
    default: true
  - name: pv
    price_kwh: 0.088

profiles:                                  # named export configurations
  quarterly_full:
    sections: [electricity, heating, hot_water, summary]
    language: de
    formats: [markdown, csv, pdf]
  water_only:
    sections: [water_volume]
    formats: [csv]
```

Multiple **profiles** satisfy the requirement of "different energy bill exports from a
configuration": a full per-apartment quarterly bill, a water-volume-only report, etc.

### 3.2 Billing engine

A pure-Python module (`billing/engine.py`) porting `energy_bill_analysis.py`:

- Tariff assignment from the configured schedule rules (replaces `is_niedertarif`)
- Cumulative-counter diff with reset/negative filtering
- Per-timestamp PV factor and grid/PV split
- Common-electricity split, heat-pump reservoir-ratio split, proportional heating/hot-water
  allocation
- Output: a structured `BillResult` (per unit: kWh HT/NT/PV, CHF per line item, totals) that
  renderers consume

One engine + config replaces all per-quarter notebook copies.

### 3.3 Export service

```yaml
service: energy_meter_izar.generate_bill
data:
  start: "2026-01-01"
  end: "2026-04-01"
  profile: quarterly_full
  formats: [markdown, csv, pdf]     # optional, defaults from profile
```

- Renderers: **Markdown** (tables like today's notebook output), **CSV** (one row per unit ×
  line item, spreadsheet-friendly), **PDF** via **`fpdf2`** (pure Python, no system
  dependencies — deliberately *not* WeasyPrint).
- Output to `/config/energy_meter_izar/bills/<period>_<profile>.<ext>`, exposed through the
  HA media source so files are browsable/downloadable from the UI.
- Fires an event / persistent notification with the file paths, so an automation can e.g.
  generate the bill on the 1st day of each quarter and email it.

---

## 4. Repository layout

```
energy_meter_IZAR/
├── custom_components/energy_meter_izar/
│   ├── __init__.py            # setup entry, coordinator wiring
│   ├── manifest.json          # requirements: pymeterbus, aioftp, asyncssh, fpdf2
│   ├── config_flow.py         # connection + options flow
│   ├── coordinator.py         # poll loop, high-water mark, statistics import
│   ├── ftp_client.py          # FTP/FTPS/SFTP abstraction
│   ├── mbus_parser.py         # pure-python HC2XML + telegram + CP32 parsing
│   ├── store.py               # SQLite reading store
│   ├── sensor.py              # entities
│   ├── services.yaml
│   ├── services.py            # generate_bill
│   └── billing/
│       ├── config.py          # billing.yaml schema + validation
│       ├── engine.py          # tariff/PV/split calculations
│       └── render_{markdown,csv,pdf}.py
├── tests/
│   ├── fixtures/0080A3DB81A5_665.xml   # sample from meter_data_analyse
│   ├── test_mbus_parser.py
│   ├── test_billing_engine.py
│   └── test_config_flow.py
├── hacs.json
├── .github/workflows/{validate.yml, release.yml}
├── README.md
└── LICENSE
```

---

## 5. CI/CD & HACS distribution (GitHub Actions)

### 5.1 `validate.yml` — merge gate (push / PR to `main`)

Every PR must pass HACS + hassfest validation and tests before merge:

```yaml
name: Validate
on:
  push: { branches: [main] }
  pull_request: { branches: [main] }
jobs:
  hacs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hacs/action@main
        with: { category: integration }
  hassfest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: home-assistant/actions/hassfest@master
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - run: pip install -r requirements_test.txt
      - run: ruff check .
      - run: pytest
```

Enable branch protection on `main` requiring these three checks.

### 5.2 `release.yml` — release artifact for HACS (on published release / `v*` tag)

```yaml
name: Release
on:
  release: { types: [published] }
permissions: { contents: write }
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Stamp version into manifest
        run: |
          VERSION="${GITHUB_REF_NAME#v}"
          jq --arg v "$VERSION" '.version = $v' \
            custom_components/energy_meter_izar/manifest.json > m.json \
            && mv m.json custom_components/energy_meter_izar/manifest.json
      - name: Build zip artifact
        run: |
          cd custom_components/energy_meter_izar
          zip -r ../../energy_meter_izar.zip .
      - name: Upload release asset
        uses: softprops/action-gh-release@v2
        with: { files: energy_meter_izar.zip }
```

### 5.3 `hacs.json`

```json
{
  "name": "Energy Meter IZAR (M-Bus FTP)",
  "zip_release": true,
  "filename": "energy_meter_izar.zip",
  "homeassistant": "2025.6.0"
}
```

With `zip_release: true`, HACS installs directly from the release asset and offers proper
version updates. Repo prerequisites for adding it as a HACS custom repository: public repo,
description + topics set (`home-assistant`, `hacs`, `integration`), README with install
instructions, and at least one published GitHub release.

Release flow: merge PRs into `main` (gated by `validate.yml`) → create a GitHub release
`vX.Y.Z` → `release.yml` builds and attaches `energy_meter_izar.zip` → HACS users see the
update.

---

## 6. Implementation roadmap

| Phase | Content | Outcome |
|---|---|---|
| 1 | Port parser (`mbus_parser.py`) + CP32 fix + pytest against the sample XML | Verified decoding library |
| 2 | Integration scaffold, config flow, FTP client, coordinator, live sensors | Meters visible in HA |
| 3 | External-statistics backfill + SQLite reading store | Correct Energy Dashboard history |
| 4 | Billing config schema + engine + Markdown/CSV renderers + `generate_bill` service | Automated bills |
| 5 | PDF renderer, HACS packaging, CI workflows (validate + release), README/docs | Installable v1.0.0 via HACS |

Each phase is a PR into `main`, gated by `validate.yml`.

---

## 7. Testing strategy & risks

**Testing**

- `pytest` + `pytest-homeassistant-custom-component` for config flow, coordinator, and entity
  tests (mocked FTP).
- Parser and billing engine are HA-free pure Python → fast unit tests; the committed sample
  file `0080A3DB81A5_665.xml` and golden-output snapshots from the existing notebooks
  (e.g. Q3/2025 results) serve as regression fixtures.
- Integration test against a local FTP server (`pyftpdlib`) serving fixture files.

**Risks / open points**

| Risk | Mitigation |
|---|---|
| CP32 year decoding across year boundaries (2025→2026 bug already observed) | Correct type-F decoder + explicit year-rollover unit tests + cross-check MBTIME against the telegram's own `04 6D` record (`XML_DATETIME_FIX_STRATEGY.md` §4.1) |
| File counter wraps after 999 → high-water mark stalls, filename sort misorders | Order/gate by decoded gateway `MBTIME`; modulo-1000 wrap detection only for gap reporting (`XML_DATETIME_FIX_STRATEGY.md` §5) |
| Meter resets / counter overflows → negative diffs | Filter negatives (as today) + log a repair issue when detected |
| FTP flakiness / partial uploads | `.rdy`-marker gating, retry with backoff, high-water mark makes retries idempotent |
| Gateway deletes/rotates old files | Poll interval well below rotation period; document gateway retention |
| HA recorder purge would lose billing data | Own SQLite store is the billing source of truth |
| Tariff prices change per supplier invoice | Prices live in `billing.yaml`; bills embed the prices used for auditability |
