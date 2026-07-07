# tvtime-gdpr-to-ryot

Convert a [TV Time](https://www.tvtime.com/) GDPR data export into a JSON file
importable by [Ryot](https://github.com/IgnisDa/ryot), the self-hosted media
tracker.

TV Time shut down in July 2026. A couple of community scripts already exist
for this migration — [SirMartin/TvTimeToRyot](https://github.com/SirMartin/TvTimeToRyot)
and [IAM-marco/TVTime2Ryot](https://github.com/IAM-marco/TVTime2Ryot) — but
neither worked against a current export:

- `TvTimeToRyot` expects `seen_episode.csv`, which isn't in the export anymore.
- `TVTime2Ryot` expects `seen_episode_source.csv`, also missing. Its core
  per-episode history function (`compute_seen_history`) is also defined but
  never actually called in the current source, so even with the right files
  it would only register shows with empty watch history.

This script reads the CSV files a TV Time GDPR export actually contains today,
and writes Ryot's current import schema (which itself has changed since the
JSON examples floating around various GitHub issues — see below).

## What it reads

From the root of your unzipped GDPR export:

| File | Used for |
|---|---|
| `followed_tv_show.csv` | The full list of shows you've followed |
| `watched_on_episode.csv` | A per-episode watch log, timestamped — used when TV Time logged one for a show |
| `tracking-prod-records-v2.csv` | A single "last episode watched" marker per show — fallback for shows with no per-episode log |
| `tracking-prod-records.csv` | Movie watch/rewatch events (`entity_type=movie`) |

Not every show will have full per-episode history — TV Time appears to have
only logged granular per-episode events for some shows/some time ranges. Shows
with only a "last watched" marker get imported as a single "watched through
season X episode Y" entry rather than a full backlog. Shows with no watch
signal at all are still imported (so you keep your list), just with no
progress data.

Movies are matched separately against `/search/movie` on TMDB, using the
release year from the export to disambiguate when it's available — TV Time
uses `0001-01-01` as a sentinel for "unknown release date" on some older
entries, which the script treats as no year rather than a literal filter.
Rewatches produce a second `seen_history` entry rather than being collapsed
into one.

## Setup

```sh
pip install -r requirements.txt
```

Get a free TMDB **read access token** (v4 auth, not the v3 API key) from your
[TMDB account API settings](https://www.themoviedb.org/settings/api), and set:

```sh
export TMDB_READ_ACCESS_TOKEN="your token here"
```

## Usage

1. Request your data from TV Time via GDPR (Settings > Privacy > Download my
   data, or directly at gdpr.tvtime.com) and unzip it somewhere.
2. Run:

   ```sh
   ./tvtime_to_ryot.py /path/to/unzipped-export -o ryot_data.json
   ```

3. In Ryot: **Settings > Import & Export > Import**, choose **Generic Json**
   as the source, and upload the resulting file.

The script prints a TMDB match (or "no match") for every show as it runs, and
a final summary of anything that needs a manual look — usually a show that's
been removed from TMDB or whose name TV Time formats differently.

## Ryot schema gotchas (as of Ryot v10.3.16)

If you're extending this script or debugging a failed import yourself, two
things aren't obvious from older docs/GitHub issue examples:

- The Generic JSON importer expects a `CompleteExport` object —
  `{"metadata": [...]}` — not a bare array of items. A bare array fails
  **silently**: the import report shows `wasSuccess: false` with 0 progress
  and no error detail anywhere (not in the UI, not in container logs, not in
  the `import_report` DB row).
- `lot` and `source` enum values are lowercase (`"show"`, `"tmdb"`), not
  title-cased (`"Show"`, `"Tmdb"`) as shown in some older examples.

## Limitations

- TMDB matching is name-based (TV Time's export doesn't carry TMDB IDs), so a
  handful of shows/movies with ambiguous or reused names may need manual
  correction after import.
- Movie watch dates come from when you marked them watched in TV Time, not
  necessarily when you actually watched them (same caveat applies to show
  progress markers pulled from `tracking-prod-records-v2.csv`).

## License

MIT — see [LICENSE](LICENSE).
