# Test fixtures

Open-Meteo responses use `timeformat=unixtime` (see `fetch/open_meteo.py`), with the
response's `utc_offset_seconds`.

| File | Origin |
| --- | --- |
| `open_meteo_dst_fall_back.json` | **Real** response saved 2026-10-30 for 2026-11-01 to 2026-11-02 (US fall-back day) |
| `open_meteo_sunny.json`, `open_meteo_rainy.json`, `open_meteo_missing_fields.json`, `open_meteo_forecast.json` (snow), `open_meteo_warm_dry.json` (48 warm, dry hours incl. `rain` and `snow_depth`: a good hiking day) | Synthetic: hand-built to hit one scenario each, in the real response shape |
| `nws_alerts_*.json` | Synthetic: hand-built GeoJSON in the real shape |

To capture a real response for a new fixture, run the fetch with `--save-raw` once the CLI exists.

The sunny, rainy, missing-fields and snow fixtures deliberately have no `rain` or
`snow_depth` series, to test that an absent series becomes `None` (and, in scoring, "no values").
