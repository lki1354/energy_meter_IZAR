# Energy Meter IZAR (M-Bus FTP)

[![Validate](https://github.com/lki1354/energy_meter_IZAR/actions/workflows/validate.yml/badge.svg)](https://github.com/lki1354/energy_meter_IZAR/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)

A Home Assistant custom integration that turns M-Bus gateway XML snapshots on an
FTP/FTPS/SFTP server into sensor entities, Energy-Dashboard history, and
configurable energy bills (Markdown, CSV, PDF).

It automates what the [`meter_data_analyse`](https://github.com/lki1354/meter_data_analyse)
notebooks did manually:

1. **Fetch** M-Bus gateway XML snapshot files (`0080A3DB81A5_XXX.xml`) from a remote server
2. **Decode** the raw M-Bus telegrams (via [`pyMeterBus`](https://github.com/ganehag/pyMeterBus))
   and expose every meter — electricity, heat, hot/cold water — as HA devices and sensors
3. **Generate energy bills** for arbitrary periods with Swiss Hoch-/Niedertarif pricing,
   PV self-consumption allocation, common-electricity and heat-pump cost splits

## Features

- **Config-flow setup** — host, credentials, protocol (FTP / FTPS / SFTP), remote
  directory, file pattern, poll interval; all options editable later
- **Robust ingestion** — `.rdy`-marker gating, wrap-aware file ordering by decoded
  gateway time (the 3-digit file counter wraps after 999), exactly-once processing
  across restarts, correct CP32 (type F) timestamp decoding across year boundaries
- **Sensor entities** — one HA device per physical meter with energy
  (`total_increasing`, kWh), volume (m³), power (W), temperatures (°C), flow rate
  (m³/h), plus diagnostic entities (alarm status, bus voltage/current, last poll)
- **Energy Dashboard history** — interval data is imported as long-term external
  statistics, so hourly history stays correct even when files arrive hours or days late
- **Own reading archive** — every decoded reading is stored in
  `/config/energy_meter_izar/readings.db` (SQLite), independent of recorder purge
  settings, as the billing source of truth
- **Configurable bills** — tariff schedules, PV allocation, common-electricity split,
  heat-pump reservoir-ratio split, per-profile sections/language/formats; rendered as
  Markdown, CSV, and PDF

## Installation

### HACS (recommended)

1. In HACS, open **⋮ → Custom repositories** and add
   `https://github.com/lki1354/energy_meter_IZAR` with category **Integration**.
2. Search for **Energy Meter IZAR (M-Bus FTP)** in HACS and install it.
3. Restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration** and search for
   **Energy Meter IZAR**.

### Manual

Copy `custom_components/energy_meter_izar/` (or unpack the `energy_meter_izar.zip`
release asset) into `<config>/custom_components/energy_meter_izar/` and restart
Home Assistant.

## Configuration

The config flow asks for:

| Setting | Default | Notes |
|---|---|---|
| Protocol | `ftp` | `ftp`, `ftps`, or `sftp` |
| Host / port | — / 21 (22 for SFTP) | the server the gateway uploads to |
| Username / password | — | |
| Remote directory | `/` | |
| File pattern | `0080A3DB81A5_*.xml` | glob matched against remote file names |
| Poll interval | 15 min | |
| Require `.rdy` marker | on | only ingest files with a matching `.rdy` file |
| Delete after processing | off | leave files on the server by default |

Poll interval, file pattern, `.rdy` gating, and delete-after can be changed
later via the entry's **Configure** dialog; credentials are updated through the
reauthentication flow when the server rejects a login.

## Billing

### Billing configuration (`/config/energy_meter_izar/billing.yaml`)

Bills are driven by a YAML file; without one, a built-in default matching the
original building (3 apartments, PV, heat pump) is used. Full example:

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
    schedule:                              # anything not matched falls to the default tariff
      - { days: [mon, tue, wed, thu, fri], from: "07:00", to: "20:00" }
      - { days: [sat], from: "07:00", to: "13:00" }
  - name: niedertarif
    price_kwh: 0.2028
    default: true
  - name: pv                               # reserved name: prices PV self-consumption
    price_kwh: 0.088

profiles:                                  # named export configurations
  quarterly_full:
    sections: [electricity, heating, hot_water, summary]
    language: de                           # de or en
    formats: [markdown, csv, pdf]
  water_only:
    sections: [water_volume]
    formats: [csv]
```

How costs are computed (ported from the manual notebooks):

- **Consumption** is the diff of each cumulative counter; negative diffs (meter
  reset) are dropped.
- **PV allocation** per timestamp: `pv_factor = min(PV, house) / house`; each
  consumer's interval splits into a grid share (billed at the matching time-of-use
  tariff) and a PV share (billed at the `pv` price).
- **Common electricity** cost is split equally across the units.
- **Heat pump** electricity cost is split between heating and hot water by the
  reservoir energy ratio (fallback share when no reservoir data), then allocated
  to units proportionally to their heat meters (kWh) and hot-water meters (m³).

### Generating a bill

```yaml
service: energy_meter_izar.generate_bill
data:
  start: "2026-01-01"     # inclusive
  end: "2026-04-01"       # exclusive
  profile: quarterly_full # optional, defaults to the first profile
  formats: [markdown, csv, pdf]  # optional, defaults from the profile
```

Files are written to `/config/energy_meter_izar/bills/<start>_<end>_<profile>.<ext>`.
The service fires an `energy_meter_izar_bill_generated` event and creates a
persistent notification with the file paths, so an automation can generate and
e-mail the bill on the first day of each quarter. Called with
`response_variable`, it also returns the file list and total.

## Data source

Designed for a wired M-Bus gateway/concentrator (type `60M`) that polls the
building's meters and uploads XML snapshots:

```xml
<HC2XML>
  <UNIT>…</UNIT>            <!-- gateway metadata -->
  <MEM>
    <T0000>
      <MBTIME>00202637</MBTIME>   <!-- CP32 (type F) timestamp, hex -->
      <ALMSTAT>00</ALMSTAT>
      <MBTEL>68C4C468…16</MBTEL>  <!-- raw M-Bus telegram -->
    </T0000>
    …
  </MEM>
</HC2XML>
```

## Development

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
ruff check .
pytest
```

The parser and the billing engine are pure Python (no HA imports) and fully unit
tested against committed gateway snapshot fixtures. Pull requests into `main` are
gated by HACS validation, hassfest, ruff, and pytest
([`validate.yml`](.github/workflows/validate.yml)); publishing a GitHub release
`vX.Y.Z` builds and attaches the HACS zip
([`release.yml`](.github/workflows/release.yml)).

Design documents: [STRATEGY.md](STRATEGY.md),
[XML_DATETIME_FIX_STRATEGY.md](XML_DATETIME_FIX_STRATEGY.md).

## License

[MIT](LICENSE)
