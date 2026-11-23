# Night Report

A completed local night keogram automatically receives a Night Report: a PNG
combining the keogram and selected sensor histories, plus a CSV of all available
measurements. Open **Night Report** on the night archive card or keogram page.
An administrator can select the plotted measurements and choose **Save and
regenerate**. The selection is remembered per camera for future reports.

The report uses actual capture timestamps. Changing exposure intervals and
missing images do not compress the timeline. Missing or invalid sensor values
break curves; a measured zero remains zero. Air temperature and dew point share
a plot when both are selected. Dew point is calculated from valid temperature
and humidity readings from the same sensor, rather than a potentially stale
derived slot. Lux is illuminance, not calibrated SQM.

Astronomical twilight boundaries (-12 and -18 degrees solar center) and lunar
rise/set are marked when they fall within the recording. Event calculations use
the location recorded with exposures where available. Old nights without a
recorded location explicitly identify the current camera location as a fallback.
Moonrise/set assume an unobstructed standard horizon, not a local tree line.

Failed reports are retried by archive recovery with backoff. They do not make a
completed keogram fail. A compact source cache retains measurements and keogram
strips, allowing a different plot selection after full exposures expire. Report
files and their cache are deleted with their keogram. Existing remote/S3-only
assets are not rendered remotely or exported automatically by this feature.

## CSV interchange format, version 1

UTF-8, comma-separated, a header row, decimal points, no locale-dependent dates.
One row represents an archived Allsky exposure. PNG selection does not discard
other available measurements from CSV. Columns may be added; consumers should
address columns by name and ignore unknown columns.

| Column | Meaning |
| --- | --- |
| `schema_version` | `1` for this contract |
| `camera_uuid`, `camera_id`, `image_id` | Stable station UUID and local database identifiers; UUID plus image ID identifies an observation |
| `night_date` | Observing-night date, including recordings after midnight |
| `timestamp_utc`, `unix_time` | The same observation time as ISO 8601 UTC and epoch seconds |
| `timestamp_local`, `utc_offset_seconds` | Historical local time with explicit offset; repeated DST hours remain distinct |
| `exposure_seconds` | Recorded exposure duration, blank if unknown |
| `time_reference` | `recorded_capture_time`; **not** a promise of shutter-open time |
| `sensor_validity` | `tracked` when validity metadata was recorded, otherwise `legacy_unverified` |
| `gap_before_seconds` | Elapsed time since the preceding observation; blank for the first row. This includes the ordinary capture cadence, not just downtime |
| `valid_fields` | Semicolon-separated IDs of metrics with a usable value in this row |
| `metric_catalog_json` | The first row includes a JSON catalogue of metric IDs, labels, units, kinds and corresponding CSV column names; later rows leave this cell empty |
| Measurement columns | Explicit units in names, e.g. `sky_temperature_c`, `sky_brightness_lux`, `air_temperature_c`, `dew_point_c`, or additional `sensor_user_N_<unit>` columns |

Empty cells mean missing/invalid/unknown, never zero. An omitted measurement
column means no available measurements of that kind in the report. Do not fill
gaps indefinitely. Source measurements are those carried with the exposure;
they are not separate, precisely synchronized sensor acquisition timestamps.
The libcamera and INDI drivers assign the capture timestamp after exposure when
the image is received. Do not treat that time as a precise exposure start or
subtract exposure duration to invent an exact start.

## Preparing for a separate light-frame filtering app

Import the light frame's FITS observation time and exposure duration with their
declared time convention, then compare its interval with nearby Allsky samples
in UTC. Require a configurable maximum sample gap, coverage fraction and
validity policy. Frames without sufficient telemetry should be **unknown**,
not automatically good or bad. Record criteria and evidence before offering
non-destructive copying/tagging of selected lights. This change does not read,
move or delete ASIAIR light frames, and requires no ASIAIR control interface.
