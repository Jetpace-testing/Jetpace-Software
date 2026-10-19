# The instrument catalogue

What this platform can commission, what it will be able to commission, and
which datasheet each answer comes from.

**THE DATASHEETS ARE NOT IN THIS REPOSITORY.** They were in a `Modbus Mapping/`
folder here and were moved out on 19 October 2026, because they are not this
project's: they are the vendor documentation for the instrument range, and they
live with it. On the build machine today that is
`D:\Jetpace TC Generator\Modbus Mapping`. A transcription records which
document it came from in `DeviceModel.datasheet_ref`, so a commissioned device
can always name its source without the PDF being alongside the code.

This file names each document rather than linking to it, for the same reason.

**A register map lives in the product package that sells the instrument, never
in core, and is pinned to its datasheet by a test in that package** (CLAUDE.md).
So "supported" means exactly one thing here: a `DeviceType` exists, with a test
asserting every address against the PDF named in its row.

This file is the list of what is coming. It is prose on purpose. A declared
instrument with no register map cannot be commissioned — the addresses to read
come from the map — so a half-built `DeviceType` would be a dropdown entry that
throws on submit, which is the thing `DeviceType.__post_init__` refuses to
allow. What the dashboard shows instead is a `Planned` entry: the name, greyed,
saying its map has not been transcribed yet. See `jetpace_core.devices.Planned`.

## How to read the status column

| Status | Means |
|---|---|
| **mapped** | A `DeviceType` exists and its addresses are pinned by a test. Commissionable. |
| **planned** | Declared in the product's `PLANNED` tuple. Visible in the form, greyed, not selectable. |
| *(blank)* | A datasheet exists for it and nothing has been declared yet. |

## Transcribing one

The datasheet gives registers; the product decides what they mean. In order:

0. Find the document. It is not in this repository — see above.
1. `Register` per address — `datasheet_number` exactly as the PDF prints it
   (`wire_address` subtracts 40001; never do that subtraction by hand), the
   data type, and `writable=True` **only** where Phase 2 is meant to write.
   The slave address, baud rate, save and auto-zero registers are never marked
   writable: that is what makes them unreachable from a browser.
2. `Point` per stored parameter — the parameter key, its label, unit, decimals
   and `scale`, and `limited=True` where an operator sets a specification.
3. `DeviceType` — `kind`, `code_prefix`, `scan_class`, `default_port`.
4. A test in the product package asserting each `datasheet_number`, each data
   type, each scale, and that no identity or comms register is writable.

**Scale is the number to get right.** Most of this range reports tenths. Read
as whole units it is out by ten in every reading, trend and report, with
nothing on screen to say so. Confirm it against the instrument on the bench.

## Environmental — `jetpace_environment`

| Instrument | Vendor | Quantities | Datasheet | Status |
|---|---|---|---|---|
| **iCRDN-230-RT** | Jetpace | temperature, humidity | `Cleanroom Modbus Mapping` (2026-10-19) | **mapped** |
| **iCRDN-230-D** | Jetpace | differential pressure | same map | **mapped** |
| **iCRDN-230-DRT** | Jetpace | pressure, temperature, humidity | same map | **mapped** |
| iRHT-320 | Jetpace | temperature, humidity | `iRHT-320 MODBUS MAPPING FOR USER.pdf` | |
| iRHT-320W | Jetpace | temperature, humidity | `iRHT-320W MODBUS MAPPING 250823.pdf` | |
| iRHT-320W (7-terminal) | Jetpace | temperature, humidity | `iRHT-320W (7-terminals) MODBUS MAPPING.pdf` | |
| RHT-320 UTR | Jetpace | temperature, humidity | `RHT-320 UTR Modbus Mapping 260428.pdf` | |
| Cleanroom unit | Jetpace | see the three iCRDN-230 rows above | `clean room manual - Jetpace.pdf` | |
| Cleanroom unit | Nextech | — | `clean room manual - nextech.pdf` | |
| DP transmitter | Jetpace | differential pressure | `Differential Pressure Transmitter Modbus Mapping.pdf`, `DP Manual Jetpace.pdf` | planned |
| RTD probe | Jetpace | temperature | `RTD MODBUS MAPING.pdf` | planned |
| DP gauge | Nextech | differential pressure | `Differential Pressure Gauge Modbus Mapping NEXTECH.pdf`, `DP NEXTECH Manual.pdf` | |
| Filter display unit | Jetpace | differential pressure, filter state | `Filter Display Unit Modbus Mapping.pdf` | |
| Air quality monitor | Jetpace | CO2, TVOC, particulate, temperature, humidity | `Air Quality Monitor Modbus Mapping.pdf` | |
| Air quality monitor WD | Jetpace | as above | `Air Quality Monitor WD Modbus Mapping.pdf` | |
| CO2 duct mount | Jetpace | CO2 | `CO2 Duct Mount Modbus Mapping 250917.pdf` | |
| CO2 RS485 | Jetpace | CO2 | `CO2 RS485 MANUAL.pdf` | |
| CO transmitter | Jetpace | carbon monoxide | `CO Manual.pdf` | |
| LEL detector | Jetpace | combustible gas | `LEL MODBUS Manual.pdf` | |
| Air velocity | Jetpace | velocity | `Air Velocity Manual.pdf` | |
| Flameproof transmitter | Jetpace | — | `Flame Proof Transmitter Manual.pdf`, `FLP Manual Jetpace.pdf` | |

## Energy and process — `jetpace_energy`

| Instrument | Vendor | Quantities | Datasheet | Status |
|---|---|---|---|---|
| BTU meter | Jetpace | flow, energy | `BTU manual USER.pdf` | |
| Flow totalizer | Jetpace | flow, total | `Flow Totalizer Modbus Mapping.pdf` | |
| Batch totalizer | Jetpace | flow, batch total | `Batch Totalizer testing manual.pdf` | |
| Process controller 96×48 | Jetpace | process value, set points | `96x48 Modbus Mapping.pdf` | |
| Process controller 96×96 | Jetpace | process value, set points | `96 X 96 Process Controller Manual Jetpace.pdf` | |
| Loop power unit | Jetpace | — | `Loop Power Manual.pdf` | |
| RIO | Jetpace | remote I/O | `RIO Modbus Mapping.pdf` | |

## Two things to decide as the range lands

**`device_model.kind`.** The vocabulary is `energy, pressure, temperature,
humidity, particle, gas, flow, velocity`, and nothing in the codebase branches
on it — it is stored, selected and written into one CSV column. A transmitter
reporting two quantities has no single kind, and neither `gas` nor `particle`
will fit the air quality monitor, nor `flow` nor `energy` the BTU meter. Pick
the quantity the instrument is bought for and say why in a comment. Changing
the vocabulary is dearer than it looks: the test that pins it parses
`migrations/0001_init.sql` specifically, so a later migration that alters the
constraint leaves that test comparing against the original list.

**Shared parameter keys.** Two instruments measuring temperature should use one
`temp` key, and `devices.check_parameters` makes them agree about its label,
unit, aggregation and decimals or refuses to start. Where two vendors' maps
describe the same quantity, share the point definitions with a function inside
the product package rather than copying them.


## What the first transcription turned up

Worth knowing before the next one, because the same questions will come up:

- **A datasheet can be ambiguous about what an address means.** 40001-40003
  on the Cleanroom Display are temperature/humidity/pressure in one mode and
  three differential pressures in another, decided by `HDIU_EN` at 40065. The
  map reads them as the first and records the mode register so a commissioning
  record can say which the instrument was in.
- **Scale may not be a property of the model at all.** `Decimal_Point_Set` is
  writable per channel, so two identical instruments can report the same
  quantity with different decimal places. It is read at commissioning and
  stored against the device.
- **A written value may not survive a power cycle.** This instrument needs
  1221 committed to `SAVE_EN`, so "write, read back, record" is not enough on
  its own — the read-back would confirm a value that will be forgotten.
- **"SP1" and "SP2" need a human to say which is low.** No datasheet in this
  range appears to.
