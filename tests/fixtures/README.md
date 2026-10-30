# Test fixtures

Open-Meteo responses use `timeformat=unixtime` (see `fetch/open_meteo.py`), with the
response's `utc_offset_seconds`.

| File | Origin |
| --- | --- |
| `open_meteo_dst_fall_back.json` | **Real** response saved 2026-10-30 for 2026-11-01 to 2026-11-02 (US fall-back day) |
| `open_meteo_sunny.json`, `open_meteo_rainy.json`, `open_meteo_missing_fields.json`, `open_meteo_forecast.json` (snow) | Synthetic: hand-built to hit one scenario each, in the real response shape |
| `nws_alerts_*.json` | Synthetic: hand-built GeoJSON in the real shape |

To capture a real response for a new fixture, run the fetch with `--save-raw` once the CLI exists.
